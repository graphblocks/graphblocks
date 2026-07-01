from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Protocol

from .canonical import canonical_hash
from .evaluation import ResourceSnapshotRef, ReviewDecision, ReviewRecord
from .policy import PrincipalRef


def _parse_review_datetime(value: object, *, owner: str, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{owner} {field_name} must not be empty")
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{owner} {field_name} must be an ISO datetime") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_optional_review_datetime(owner: str, field_name: str, value: object | None) -> None:
    if value is not None:
        _parse_review_datetime(value, owner=owner, field_name=field_name)


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    request_id: str
    subject: ResourceSnapshotRef
    requested_by: PrincipalRef
    required_scopes: tuple[str, ...]
    created_at: str
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _parse_review_datetime(self.created_at, owner="review request", field_name="created_at")
        object.__setattr__(self, "required_scopes", tuple(sorted(set(self.required_scopes))))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def content_digest(self) -> str:
        return canonical_hash(
            {
                "subject": {
                    "resource_id": self.subject.resource_id,
                    "digest": self.subject.digest,
                    "resource_kind": self.subject.resource_kind,
                    "uri": self.subject.uri,
                    "metadata": dict(self.subject.metadata),
                },
                "requested_by": {
                    "principal_id": self.requested_by.principal_id,
                    "tenant_id": self.requested_by.tenant_id,
                    "groups": tuple(sorted(self.requested_by.groups)),
                    "roles": tuple(sorted(self.requested_by.roles)),
                    "attributes": dict(self.requested_by.attributes),
                },
                "required_scopes": self.required_scopes,
                "metadata": self.metadata,
            }
        )


@dataclass(frozen=True, slots=True)
class ReviewerCredential:
    credential_ref: str
    reviewer: PrincipalRef
    scopes: tuple[str, ...]
    issued_at: str
    expires_at: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _parse_review_datetime(self.issued_at, owner="reviewer credential", field_name="issued_at")
        _validate_optional_review_datetime("reviewer credential", "expires_at", self.expires_at)
        if self.expires_at is not None and _parse_review_datetime(
            self.expires_at, owner="reviewer credential", field_name="expires_at"
        ) <= _parse_review_datetime(self.issued_at, owner="reviewer credential", field_name="issued_at"):
            raise ValueError("reviewer credential expires_at must be after issued_at")
        object.__setattr__(self, "scopes", tuple(sorted(set(self.scopes))))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def allows(self, reviewer: PrincipalRef, scope: str) -> bool:
        return self.reviewer.principal_id == reviewer.principal_id and scope in self.scopes

    def is_active_at(self, created_at: str) -> bool:
        if self.expires_at is None:
            return True

        try:
            return _parse_review_datetime(created_at, owner="review", field_name="created_at") < _parse_review_datetime(
                self.expires_at,
                owner="reviewer credential",
                field_name="expires_at",
            )
        except ValueError:
            return False


class ReviewerCredentialProvider(Protocol):
    def credentials_for(self, reviewer: PrincipalRef, scope: str) -> tuple[ReviewerCredential, ...]:
        ...


@dataclass(frozen=True, slots=True)
class InMemoryReviewerCredentialProvider:
    credentials: tuple[ReviewerCredential, ...] = field(default_factory=tuple)

    def __init__(self, credentials: list[ReviewerCredential] | tuple[ReviewerCredential, ...] = ()) -> None:
        object.__setattr__(self, "credentials", tuple(credentials))

    def credentials_for(self, reviewer: PrincipalRef, scope: str) -> tuple[ReviewerCredential, ...]:
        return tuple(credential for credential in self.credentials if credential.allows(reviewer, scope))


class ReviewWorkflowError(ValueError):
    """Base error for review workflow failures."""


class ReviewSubjectChangedError(ReviewWorkflowError):
    def __init__(self, expected_digest: str, actual_digest: str) -> None:
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        super().__init__(f"review subject changed: expected {expected_digest}, got {actual_digest}")


class ReviewCredentialMissingError(ReviewWorkflowError):
    def __init__(self, reviewer: PrincipalRef, scope: str) -> None:
        self.reviewer = reviewer
        self.scope = scope
        super().__init__(f"reviewer {reviewer.principal_id!r} has no credential for scope {scope!r}")


class ReviewScopeNotRequestedError(ReviewWorkflowError):
    def __init__(self, scope: str) -> None:
        self.scope = scope
        super().__init__(f"review scope {scope!r} was not requested")


@dataclass(slots=True)
class ReviewWorkflow:
    request: ReviewRequest
    credential_provider: ReviewerCredentialProvider
    reviews: tuple[ReviewRecord, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.reviews = tuple(self.reviews)

    def with_review(self, review: ReviewRecord) -> ReviewWorkflow:
        reviews = tuple(existing for existing in self.reviews if existing.review_id != review.review_id)
        return replace(self, reviews=(*reviews, review))

    def record_review(
        self,
        *,
        review_id: str,
        reviewer: PrincipalRef,
        scope: str,
        decision: ReviewDecision,
        created_at: str,
        subject: ResourceSnapshotRef | None = None,
        comments: list[str] | None = None,
    ) -> ReviewRecord:
        _parse_review_datetime(created_at, owner="review", field_name="created_at")
        subject = self.request.subject if subject is None else subject
        if subject.resource_id != self.request.subject.resource_id or subject.digest != self.request.subject.digest:
            raise ReviewSubjectChangedError(self.request.subject.digest, subject.digest)
        if scope not in self.request.required_scopes:
            raise ReviewScopeNotRequestedError(scope)
        credentials = tuple(
            credential
            for credential in self.credential_provider.credentials_for(reviewer, scope)
            if credential.is_active_at(created_at)
        )
        if not credentials:
            raise ReviewCredentialMissingError(reviewer, scope)
        review = ReviewRecord(
            review_id=review_id,
            subject=self.request.subject,
            subject_digest=self.request.subject.digest,
            scope=scope,
            reviewer=reviewer,
            decision=decision,
            comments=list(comments or []),
            credential_refs=[credential.credential_ref for credential in credentials],
            created_at=created_at,
        )
        self.reviews = (*self.reviews, review)
        return review

    def completed_scopes(self) -> tuple[str, ...]:
        completed = {
            review.scope
            for review in self.reviews
            if review.decision in {"accept", "accept_with_conditions"} and review.is_valid_for(self.request.subject)
        }
        return tuple(sorted(scope for scope in self.request.required_scopes if scope in completed))

    def is_complete(self) -> bool:
        return self.completed_scopes() == self.request.required_scopes


__all__ = [
    "InMemoryReviewerCredentialProvider",
    "ReviewCredentialMissingError",
    "ReviewRequest",
    "ReviewScopeNotRequestedError",
    "ReviewSubjectChangedError",
    "ReviewWorkflow",
    "ReviewWorkflowError",
    "ReviewerCredential",
    "ReviewerCredentialProvider",
]

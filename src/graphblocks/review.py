from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import wraps
from threading import RLock
from types import MappingProxyType
from typing import ParamSpec, Protocol, TypeVar, cast

from .canonical import MAX_CANONICAL_JSON_DEPTH, canonical_dumps, canonical_hash
from .evaluation import ResourceSnapshotRef, ReviewDecision, ReviewRecord
from .policy import PrincipalRef


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _with_review_workflow_lock(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def locked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        workflow = cast("ReviewWorkflow", args[0])
        with workflow._lock:
            return method(*args, **kwargs)

    return locked


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{owner} {field_name} must not contain surrounding whitespace")
    return value


def _validate_string_tuple(owner: str, field_name: str, values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError(f"{owner} {field_name} must be a collection of strings")
    try:
        normalized = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{owner} {field_name} must be a collection of strings") from error
    for item in normalized:
        if not isinstance(item, str):
            raise ValueError(f"{owner} {field_name} items must be strings")
        if not item.strip():
            raise ValueError(f"{owner} {field_name} item must not be empty")
        if item != item.strip():
            raise ValueError(f"{owner} {field_name} item must not contain surrounding whitespace")
    return tuple(sorted(set(normalized)))


def _freeze_metadata(
    owner: str,
    value: object,
    *,
    active_containers: set[int] | None = None,
    depth: int = 0,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{owner} metadata must be a mapping")
    if depth > MAX_CANONICAL_JSON_DEPTH:
        raise ValueError(
            f"{owner} metadata nesting must not exceed {MAX_CANONICAL_JSON_DEPTH} levels"
        )
    active = set() if active_containers is None else active_containers
    identity = id(value)
    if identity in active:
        raise ValueError(f"{owner} metadata must not contain cyclic values")
    active.add(identity)
    metadata = dict(value)
    try:
        for key in metadata:
            if not isinstance(key, str):
                raise ValueError(f"{owner} metadata keys must be strings")
            if not key.strip():
                raise ValueError(f"{owner} metadata key must not be empty")
            if key != key.strip():
                raise ValueError(f"{owner} metadata key must not contain surrounding whitespace")
        return MappingProxyType(
            {
                key: _freeze_metadata_value(
                    owner,
                    item,
                    active_containers=active,
                    depth=depth + 1,
                )
                for key, item in metadata.items()
            }
        )
    finally:
        active.remove(identity)


def _freeze_metadata_value(
    owner: str,
    value: object,
    *,
    active_containers: set[int],
    depth: int,
) -> object:
    if depth > MAX_CANONICAL_JSON_DEPTH:
        raise ValueError(
            f"{owner} metadata nesting must not exceed {MAX_CANONICAL_JSON_DEPTH} levels"
        )
    if isinstance(value, Mapping):
        return _freeze_metadata(
            owner,
            value,
            active_containers=active_containers,
            depth=depth,
        )
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_containers:
            raise ValueError(f"{owner} metadata must not contain cyclic values")
        active_containers.add(identity)
        try:
            return tuple(
                _freeze_metadata_value(
                    owner,
                    item,
                    active_containers=active_containers,
                    depth=depth + 1,
                )
                for item in value
            )
        finally:
            active_containers.remove(identity)
    try:
        canonical_dumps(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{owner} metadata must contain strict canonical JSON"
        ) from error
    return value


def _thaw_metadata_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_metadata_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_metadata_value(item) for item in value]
    return value


def _parse_review_datetime(value: object, *, owner: str, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    normalized = value
    if normalized != normalized.strip() or len(normalized) <= 19 or normalized[10] != "T":
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    timezone_start = 19
    if normalized[timezone_start] == ".":
        timezone_start += 1
        while timezone_start < len(normalized) and normalized[timezone_start].isdigit():
            timezone_start += 1
        if timezone_start == 20:
            raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    suffix = normalized[timezone_start:]
    if suffix == "Z":
        normalized = f"{normalized[:timezone_start]}+00:00"
    elif (
        len(suffix) == 6
        and suffix[0] in {"+", "-"}
        and suffix[1:3].isdigit()
        and suffix[3] == ":"
        and suffix[4:6].isdigit()
    ):
        offset_hours = int(suffix[1:3])
        offset_minutes = int(suffix[4:6])
        if offset_hours > 23 or offset_minutes > 59:
            raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    else:
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{owner} {field_name} must be an ISO datetime") from error
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
        _validate_non_empty_string("review request", "request_id", self.request_id)
        if not isinstance(self.subject, ResourceSnapshotRef):
            raise ValueError("review request subject must be a ResourceSnapshotRef")
        if not isinstance(self.requested_by, PrincipalRef):
            raise ValueError("review request requested_by must be a PrincipalRef")
        _parse_review_datetime(self.created_at, owner="review request", field_name="created_at")
        object.__setattr__(
            self,
            "required_scopes",
            _validate_string_tuple("review request", "required_scopes", self.required_scopes),
        )
        object.__setattr__(self, "metadata", _freeze_metadata("review request", self.metadata))

    def content_digest(self) -> str:
        return canonical_hash(
            {
                "subject": {
                    "resource_id": self.subject.resource_id,
                    "digest": self.subject.digest,
                    "resource_kind": self.subject.resource_kind,
                    "uri": self.subject.uri,
                    "metadata": _thaw_metadata_value(self.subject.metadata),
                },
                "requested_by": {
                    "principal_id": self.requested_by.principal_id,
                    "tenant_id": self.requested_by.tenant_id,
                    "groups": tuple(sorted(self.requested_by.groups)),
                    "roles": tuple(sorted(self.requested_by.roles)),
                    "attributes": _thaw_metadata_value(self.requested_by.attributes),
                },
                "required_scopes": self.required_scopes,
                "metadata": _thaw_metadata_value(self.metadata),
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
        _validate_non_empty_string("reviewer credential", "credential_ref", self.credential_ref)
        if not isinstance(self.reviewer, PrincipalRef):
            raise ValueError("reviewer credential reviewer must be a PrincipalRef")
        _parse_review_datetime(self.issued_at, owner="reviewer credential", field_name="issued_at")
        _validate_optional_review_datetime("reviewer credential", "expires_at", self.expires_at)
        if self.expires_at is not None and _parse_review_datetime(
            self.expires_at, owner="reviewer credential", field_name="expires_at"
        ) <= _parse_review_datetime(self.issued_at, owner="reviewer credential", field_name="issued_at"):
            raise ValueError("reviewer credential expires_at must be after issued_at")
        object.__setattr__(self, "scopes", _validate_string_tuple("reviewer credential", "scopes", self.scopes))
        object.__setattr__(self, "metadata", _freeze_metadata("reviewer credential", self.metadata))

    def allows(self, reviewer: PrincipalRef, scope: str) -> bool:
        return (
            self.reviewer.principal_id == reviewer.principal_id
            and self.reviewer.tenant_id == reviewer.tenant_id
            and scope in self.scopes
        )

    def is_active_at(self, created_at: str) -> bool:
        try:
            created_at_time = _parse_review_datetime(
                created_at,
                owner="review",
                field_name="created_at",
            )
            issued_at_time = _parse_review_datetime(
                self.issued_at,
                owner="reviewer credential",
                field_name="issued_at",
            )
            if created_at_time < issued_at_time:
                return False
            if self.expires_at is None:
                return True
            return created_at_time < _parse_review_datetime(
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
        try:
            normalized = tuple(credentials)
        except TypeError as error:
            raise ValueError(
                "reviewer credential provider credentials must be ReviewerCredential records"
            ) from error
        if any(not isinstance(credential, ReviewerCredential) for credential in normalized):
            raise ValueError(
                "reviewer credential provider credentials must be ReviewerCredential records"
            )
        object.__setattr__(self, "credentials", normalized)

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
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, ReviewRequest):
            raise ValueError("review workflow request must be a ReviewRequest")
        if not callable(getattr(self.credential_provider, "credentials_for", None)):
            raise ValueError(
                "review workflow credential_provider must provide credentials_for"
            )
        try:
            reviews = tuple(self.reviews)
        except TypeError as error:
            raise ValueError(
                "review workflow reviews must be ReviewRecord records"
            ) from error
        if any(not isinstance(review, ReviewRecord) for review in reviews):
            raise ValueError("review workflow reviews must be ReviewRecord records")
        self.reviews = reviews

    @_with_review_workflow_lock
    def with_review(self, review: ReviewRecord) -> ReviewWorkflow:
        if not isinstance(review, ReviewRecord):
            raise ValueError("review workflow review must be a ReviewRecord")
        reviews = tuple(existing for existing in self.reviews if existing.review_id != review.review_id)
        return replace(self, reviews=(*reviews, review))

    @_with_review_workflow_lock
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
        if not isinstance(reviewer, PrincipalRef):
            raise ValueError("review reviewer must be a PrincipalRef")
        scope = _validate_non_empty_string("review", "scope", scope)
        review_created_at = _parse_review_datetime(created_at, owner="review", field_name="created_at")
        request_created_at = _parse_review_datetime(
            self.request.created_at,
            owner="review request",
            field_name="created_at",
        )
        if review_created_at < request_created_at:
            raise ValueError("review created_at must not precede request created_at")
        subject = self.request.subject if subject is None else subject
        if subject.resource_id != self.request.subject.resource_id or subject.digest != self.request.subject.digest:
            raise ReviewSubjectChangedError(self.request.subject.digest, subject.digest)
        if scope not in self.request.required_scopes:
            raise ReviewScopeNotRequestedError(scope)
        try:
            provided_credentials = tuple(
                self.credential_provider.credentials_for(reviewer, scope)
            )
        except TypeError as error:
            raise ValueError(
                "review credential provider must return ReviewerCredential records"
            ) from error
        if any(
            not isinstance(credential, ReviewerCredential)
            for credential in provided_credentials
        ):
            raise ValueError(
                "review credential provider must return ReviewerCredential records"
            )
        credentials = tuple(
            credential
            for credential in provided_credentials
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
        self.reviews = (
            *(existing for existing in self.reviews if existing.review_id != review.review_id),
            review,
        )
        return review

    @_with_review_workflow_lock
    def completed_scopes(self) -> tuple[str, ...]:
        completed = {
            review.scope
            for review in self.reviews
            if review.decision in {"accept", "accept_with_conditions"} and review.is_valid_for(self.request.subject)
        }
        return tuple(sorted(scope for scope in self.request.required_scopes if scope in completed))

    @_with_review_workflow_lock
    def is_complete(self) -> bool:
        return self.completed_scopes() == self.request.required_scopes


__all__ = [
    "InMemoryReviewerCredentialProvider",
    "PrincipalRef",
    "ReviewCredentialMissingError",
    "ReviewDecision",
    "ReviewRecord",
    "ReviewRequest",
    "ReviewScopeNotRequestedError",
    "ReviewSubjectChangedError",
    "ReviewWorkflow",
    "ReviewWorkflowError",
    "ReviewerCredential",
    "ReviewerCredentialProvider",
    "ResourceSnapshotRef",
]

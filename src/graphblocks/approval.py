from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Literal

from .canonical import canonical_hash
from .evaluation import ResourceSnapshotRef
from .policy import PrincipalRef


ApprovalStatus = Literal["requested", "approved", "denied", "expired", "cancelled", "invalidated"]
VALID_APPROVAL_STATUSES = frozenset(("requested", "approved", "denied", "expired", "cancelled", "invalidated"))


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    return value


def _validate_optional_non_empty_string(owner: str, field_name: str, value: object | None) -> str | None:
    if value is None:
        return None
    return _validate_non_empty_string(owner, field_name, value)


def _freeze_metadata(owner: str, metadata: object) -> MappingProxyType[str, object]:
    if not isinstance(metadata, Mapping):
        raise ValueError(f"{owner} metadata must be a mapping")
    return MappingProxyType(dict(metadata))


def _parse_datetime(value: str) -> datetime:
    normalized = _validate_non_empty_string("approval datetime", "value", value).strip()
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("approval datetime value must be an ISO datetime") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_optional_datetime(owner: str, field_name: str, value: str | None) -> None:
    if value is not None:
        try:
            _parse_datetime(value)
        except ValueError as error:
            raise ValueError(f"{owner} {field_name} must be an ISO datetime") from error


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    approval_id: str
    run_id: str
    subject: ResourceSnapshotRef
    action: str
    arguments_digest: str
    risk: str
    summary: str
    expires_at: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("approval_id", "run_id", "action", "arguments_digest", "risk", "summary"):
            _validate_non_empty_string("approval request", field_name, getattr(self, field_name))
        if not isinstance(self.subject, ResourceSnapshotRef):
            raise ValueError("approval request subject must be a ResourceSnapshotRef")
        _validate_optional_non_empty_string("approval request", "expires_at", self.expires_at)
        _validate_optional_datetime("approval request", "expires_at", self.expires_at)
        object.__setattr__(self, "metadata", _freeze_metadata("approval request", self.metadata))

    @classmethod
    def from_arguments(
        cls,
        approval_id: str,
        *,
        run_id: str,
        subject: ResourceSnapshotRef,
        action: str,
        arguments: Mapping[str, object],
        risk: str,
        summary: str,
        expires_at: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ApprovalRequest:
        if not isinstance(arguments, Mapping):
            raise ValueError("approval request arguments must be a mapping")
        return cls(
            approval_id=approval_id,
            run_id=run_id,
            subject=subject,
            action=action,
            arguments_digest=canonical_hash(dict(arguments)),
            risk=risk,
            summary=summary,
            expires_at=expires_at,
            metadata={} if metadata is None else metadata,
        )


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    request: ApprovalRequest
    status: ApprovalStatus
    approver: PrincipalRef | None = None
    decided_at: str | None = None
    reason: str | None = None
    invalidated_at: str | None = None
    credential_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("approval record", "approval_id", self.approval_id)
        if not isinstance(self.request, ApprovalRequest):
            raise ValueError("approval record request must be an ApprovalRequest")
        if self.approval_id != self.request.approval_id:
            raise ValueError("approval record id must match request approval_id")
        if self.status not in VALID_APPROVAL_STATUSES:
            raise ValueError(f"invalid approval status {self.status}")
        if self.approver is not None and not isinstance(self.approver, PrincipalRef):
            raise ValueError("approval record approver must be a PrincipalRef")
        _validate_optional_non_empty_string("approval record", "decided_at", self.decided_at)
        _validate_optional_non_empty_string("approval record", "reason", self.reason)
        _validate_optional_non_empty_string("approval record", "invalidated_at", self.invalidated_at)
        _validate_optional_datetime("approval record", "decided_at", self.decided_at)
        _validate_optional_datetime("approval record", "invalidated_at", self.invalidated_at)

        if self.status in {"approved", "denied"}:
            if self.approver is None:
                raise ValueError(f"{self.status} approval record requires approver")
            if self.decided_at is None:
                raise ValueError(f"{self.status} approval record requires decided_at")
            if self.request.expires_at is not None and _parse_datetime(self.decided_at) > _parse_datetime(
                self.request.expires_at
            ):
                raise ValueError(f"{self.status} approval record decided_at must not be after expires_at")
        if self.status == "denied" and self.reason is None:
            raise ValueError("denied approval record requires reason")
        if self.status == "invalidated" and self.invalidated_at is None:
            raise ValueError("invalidated approval record requires invalidated_at")

        if isinstance(self.credential_refs, str):
            raise ValueError("approval credential_refs must be a collection of strings")
        try:
            credential_refs = tuple(self.credential_refs)
        except TypeError as error:
            raise ValueError("approval credential_refs must be a collection of strings") from error
        for credential_ref in credential_refs:
            if not isinstance(credential_ref, str):
                raise ValueError("approval credential_refs items must be strings")
            if not credential_ref.strip():
                raise ValueError("approval credential_refs item must not be empty")
        object.__setattr__(self, "credential_refs", credential_refs)
        object.__setattr__(self, "metadata", _freeze_metadata("approval record", self.metadata))

    @classmethod
    def requested(cls, request: ApprovalRequest) -> ApprovalRecord:
        return cls(approval_id=request.approval_id, request=request, status="requested")

    @classmethod
    def approve(
        cls,
        request: ApprovalRequest,
        *,
        approver: PrincipalRef,
        decided_at: str,
        credential_refs: tuple[str, ...] = (),
        metadata: dict[str, object] | None = None,
    ) -> ApprovalRecord:
        return cls(
            approval_id=request.approval_id,
            request=request,
            status="approved",
            approver=approver,
            decided_at=decided_at,
            credential_refs=credential_refs,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def deny(
        cls,
        request: ApprovalRequest,
        *,
        approver: PrincipalRef,
        decided_at: str,
        reason: str,
        credential_refs: tuple[str, ...] = (),
    ) -> ApprovalRecord:
        return cls(
            approval_id=request.approval_id,
            request=request,
            status="denied",
            approver=approver,
            decided_at=decided_at,
            reason=reason,
            credential_refs=credential_refs,
        )

    def is_valid_for(self, subject: ResourceSnapshotRef, arguments_digest: str, *, now: str | None = None) -> bool:
        if self.request.expires_at is not None and now is not None:
            try:
                if _parse_datetime(now) > _parse_datetime(self.request.expires_at):
                    return False
            except ValueError:
                return False
        return (
            self.status == "approved"
            and self.invalidated_at is None
            and self.request.subject.resource_id == subject.resource_id
            and self.request.subject.digest == subject.digest
            and self.request.arguments_digest == arguments_digest
        )

    def invalidate(self, invalidated_at: str) -> ApprovalRecord:
        return replace(self, status="invalidated", invalidated_at=invalidated_at)

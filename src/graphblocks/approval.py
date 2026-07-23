from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Literal

from .canonical import MAX_CANONICAL_JSON_DEPTH, canonical_dumps, canonical_hash
from .documents import FrozenDict
from .evaluation import ResourceSnapshotRef
from .policy import PrincipalRef


ApprovalStatus = Literal["requested", "approved", "denied", "expired", "cancelled", "invalidated"]
VALID_APPROVAL_STATUSES = frozenset(("requested", "approved", "denied", "expired", "cancelled", "invalidated"))


def _contains_forbidden_control(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


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


def _validate_exact_non_empty_string(owner: str, field_name: str, value: object) -> str:
    value = _validate_non_empty_string(owner, field_name, value)
    if value != value.strip():
        raise ValueError(f"{owner} {field_name} must not contain surrounding whitespace")
    if _contains_forbidden_control(value):
        raise ValueError(f"{owner} {field_name} must not contain control characters")
    return value


def _freeze_metadata(
    owner: str,
    metadata: object,
    *,
    active_containers: set[int] | None = None,
    depth: int = 0,
) -> FrozenDict:
    if not isinstance(metadata, Mapping):
        raise ValueError(f"{owner} metadata must be a mapping")
    if depth > MAX_CANONICAL_JSON_DEPTH:
        raise ValueError(
            f"{owner} metadata nesting must not exceed {MAX_CANONICAL_JSON_DEPTH} levels"
        )
    active = set() if active_containers is None else active_containers
    identity = id(metadata)
    if identity in active:
        raise ValueError(f"{owner} metadata must not contain cyclic values")
    active.add(identity)
    metadata_copy = dict(metadata)
    try:
        if any(not isinstance(key, str) or not key.strip() for key in metadata_copy):
            raise ValueError(f"{owner} metadata keys must be non-empty strings")
        if any(key != key.strip() for key in metadata_copy):
            raise ValueError(f"{owner} metadata keys must not contain surrounding whitespace")
        if any(_contains_forbidden_control(key) for key in metadata_copy):
            raise ValueError(f"{owner} metadata keys must not contain control characters")
        return FrozenDict(
            {
                key: _freeze_metadata_value(
                    owner,
                    value,
                    active_containers=active,
                    depth=depth + 1,
                )
                for key, value in metadata_copy.items()
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


def _parse_datetime(value: str) -> datetime:
    normalized = _validate_non_empty_string("approval datetime", "value", value).strip()
    if normalized != value or len(normalized) <= 19 or normalized[10] != "T":
        raise ValueError("approval datetime value must be an ISO datetime")
    suffix_start = 19
    if normalized[suffix_start] == ".":
        suffix_start += 1
        fraction_start = suffix_start
        while suffix_start < len(normalized) and normalized[suffix_start].isdigit():
            suffix_start += 1
        if suffix_start == fraction_start:
            raise ValueError("approval datetime value must be an ISO datetime")
    timezone_suffix = normalized[suffix_start:]
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    elif (
        len(timezone_suffix) != 6
        or timezone_suffix[0] not in {"+", "-"}
        or timezone_suffix[3] != ":"
        or not timezone_suffix[1:3].isdigit()
        or not timezone_suffix[4:6].isdigit()
        or int(timezone_suffix[1:3]) > 23
        or int(timezone_suffix[4:6]) > 59
    ):
        raise ValueError("approval datetime value must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("approval datetime value must be an ISO datetime") from error
    if parsed.tzinfo is None:
        raise ValueError("approval datetime value must be an ISO datetime")
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
        for field_name in ("approval_id", "run_id", "action", "arguments_digest", "risk"):
            _validate_exact_non_empty_string("approval request", field_name, getattr(self, field_name))
        _validate_non_empty_string("approval request", "summary", self.summary)
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
        try:
            arguments_digest = canonical_hash(dict(arguments))
        except (TypeError, ValueError) as error:
            raise ValueError(
                "approval request arguments must contain strict canonical JSON"
            ) from error
        return cls(
            approval_id=approval_id,
            run_id=run_id,
            subject=subject,
            action=action,
            arguments_digest=arguments_digest,
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
        _validate_exact_non_empty_string("approval record", "approval_id", self.approval_id)
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
            if self.request.expires_at is not None and _parse_datetime(self.decided_at) >= _parse_datetime(
                self.request.expires_at
            ):
                raise ValueError(f"{self.status} approval record decided_at must be before expires_at")
        if self.status == "denied" and self.reason is None:
            raise ValueError("denied approval record requires reason")
        if self.status == "invalidated" and self.invalidated_at is None:
            raise ValueError("invalidated approval record requires invalidated_at")
        if self.status != "invalidated" and self.invalidated_at is not None:
            raise ValueError(
                "only invalidated approval records may define invalidated_at"
            )
        if self.status in {"requested", "expired", "cancelled"} and any(
            value is not None
            for value in (self.approver, self.decided_at, self.reason)
        ):
            raise ValueError(
                f"{self.status} approval record must not define decision fields"
            )
        if self.status == "invalidated" and (
            (self.approver is None) != (self.decided_at is None)
        ):
            raise ValueError(
                "invalidated approval record approver and decided_at must be provided together"
            )
        if (
            self.invalidated_at is not None
            and self.decided_at is not None
            and _parse_datetime(self.invalidated_at)
            < _parse_datetime(self.decided_at)
        ):
            raise ValueError(
                "approval record invalidated_at must not precede decided_at"
            )

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
            if credential_ref != credential_ref.strip():
                raise ValueError("approval credential_refs item must not contain surrounding whitespace")
            if _contains_forbidden_control(credential_ref):
                raise ValueError(
                    "approval credential_refs item must not contain control characters"
                )
        if len(set(credential_refs)) != len(credential_refs):
            raise ValueError("approval credential_refs must not contain duplicates")
        if self.status in {"requested", "expired", "cancelled"} and credential_refs:
            raise ValueError(
                f"{self.status} approval record must not define credential_refs"
            )
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
        if not isinstance(subject, ResourceSnapshotRef):
            return False
        try:
            _validate_exact_non_empty_string(
                "approval",
                "arguments_digest",
                arguments_digest,
            )
        except ValueError:
            return False
        if self.request.expires_at is not None:
            if now is None:
                return False
            try:
                if _parse_datetime(now) >= _parse_datetime(self.request.expires_at):
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
        if self.status not in {"requested", "approved"}:
            raise ValueError(
                f"{self.status} approval record cannot be invalidated"
            )
        return replace(self, status="invalidated", invalidated_at=invalidated_at)

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from .canonical import canonical_hash
from .evaluation import ResourceSnapshotRef
from .policy import PrincipalRef


ApprovalStatus = Literal["requested", "approved", "denied", "expired", "cancelled", "invalidated"]


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

    @classmethod
    def from_arguments(
        cls,
        approval_id: str,
        *,
        run_id: str,
        subject: ResourceSnapshotRef,
        action: str,
        arguments: dict[str, object],
        risk: str,
        summary: str,
        expires_at: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ApprovalRequest:
        return cls(
            approval_id=approval_id,
            run_id=run_id,
            subject=subject,
            action=action,
            arguments_digest=canonical_hash(arguments),
            risk=risk,
            summary=summary,
            expires_at=expires_at,
            metadata=dict(metadata or {}),
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

    def is_valid_for(self, subject: ResourceSnapshotRef, arguments_digest: str) -> bool:
        return (
            self.status == "approved"
            and self.invalidated_at is None
            and self.request.subject.resource_id == subject.resource_id
            and self.request.subject.digest == subject.digest
            and self.request.arguments_digest == arguments_digest
        )

    def invalidate(self, invalidated_at: str) -> ApprovalRecord:
        return replace(self, status="invalidated", invalidated_at=invalidated_at)

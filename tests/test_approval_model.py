from __future__ import annotations

from graphblocks.approval import ApprovalRequest, ApprovalRecord
from graphblocks.evaluation import ResourceSnapshotRef
from graphblocks.policy import PrincipalRef


def test_approval_request_hashes_arguments_without_storing_payload() -> None:
    subject = ResourceSnapshotRef("tool-call-1", "sha256:subject")

    request = ApprovalRequest.from_arguments(
        approval_id="approval-1",
        run_id="run-1",
        subject=subject,
        action="process.execute",
        arguments={"cmd": ["echo", "hello"]},
        risk="external_process",
        summary="Run a process",
    )
    same_request = ApprovalRequest.from_arguments(
        approval_id="approval-2",
        run_id="run-1",
        subject=subject,
        action="process.execute",
        arguments={"cmd": ["echo", "hello"]},
        risk="external_process",
        summary="Run a process",
    )

    assert request.arguments_digest.startswith("sha256:")
    assert same_request.arguments_digest == request.arguments_digest
    assert not hasattr(request, "arguments")


def test_approved_record_is_valid_only_for_same_subject_and_arguments() -> None:
    subject = ResourceSnapshotRef("tool-call-1", "sha256:subject")
    request = ApprovalRequest.from_arguments(
        "approval-1",
        run_id="run-1",
        subject=subject,
        action="process.execute",
        arguments={"cmd": ["echo", "hello"]},
        risk="external_process",
        summary="Run a process",
    )

    record = ApprovalRecord.approve(request, approver=PrincipalRef("admin-1"), decided_at="2026-06-22T00:00:00Z")

    assert record.status == "approved"
    assert record.is_valid_for(subject, request.arguments_digest) is True
    assert record.is_valid_for(ResourceSnapshotRef("tool-call-1", "sha256:changed"), request.arguments_digest) is False
    assert record.is_valid_for(subject, "sha256:changed") is False


def test_denied_approval_record_never_authorizes_action() -> None:
    request = ApprovalRequest.from_arguments(
        "approval-1",
        run_id="run-1",
        subject=ResourceSnapshotRef("tool-call-1", "sha256:subject"),
        action="process.execute",
        arguments={"cmd": ["echo", "hello"]},
        risk="external_process",
        summary="Run a process",
    )

    record = ApprovalRecord.deny(
        request,
        approver=PrincipalRef("admin-1"),
        decided_at="2026-06-22T00:00:00Z",
        reason="not needed",
    )

    assert record.status == "denied"
    assert record.is_valid_for(request.subject, request.arguments_digest) is False
    assert record.reason == "not needed"


def test_approval_record_can_be_invalidated_after_subject_change() -> None:
    request = ApprovalRequest.from_arguments(
        "approval-1",
        run_id="run-1",
        subject=ResourceSnapshotRef("tool-call-1", "sha256:subject"),
        action="process.execute",
        arguments={"cmd": ["echo", "hello"]},
        risk="external_process",
        summary="Run a process",
    )
    record = ApprovalRecord.approve(request, approver=PrincipalRef("admin-1"), decided_at="2026-06-22T00:00:00Z")

    invalidated = record.invalidate("2026-06-22T00:05:00Z")

    assert invalidated.status == "invalidated"
    assert invalidated.invalidated_at == "2026-06-22T00:05:00Z"
    assert invalidated.is_valid_for(request.subject, request.arguments_digest) is False

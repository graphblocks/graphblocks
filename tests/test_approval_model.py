from __future__ import annotations

import pytest

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


def test_approval_request_rejects_invalid_identity_fields() -> None:
    subject = ResourceSnapshotRef("tool-call-1", "sha256:subject")
    base_request = {
        "approval_id": "approval-1",
        "run_id": "run-1",
        "subject": subject,
        "action": "process.execute",
        "arguments_digest": "sha256:arguments",
        "risk": "external_process",
        "summary": "Run a process",
    }

    cases = (
        ({"approval_id": " "}, "approval request approval_id must not be empty"),
        ({"run_id": ""}, "approval request run_id must not be empty"),
        ({"action": "\t"}, "approval request action must not be empty"),
        ({"arguments_digest": " "}, "approval request arguments_digest must not be empty"),
        ({"risk": ""}, "approval request risk must not be empty"),
        ({"summary": " "}, "approval request summary must not be empty"),
        ({"expires_at": ""}, "approval request expires_at must not be empty"),
    )
    for overrides, message in cases:
        with pytest.raises(ValueError, match=message):
            ApprovalRequest(**(base_request | overrides))

    with pytest.raises(ValueError, match="approval request subject must be a ResourceSnapshotRef"):
        ApprovalRequest(
            approval_id="approval-1",
            run_id="run-1",
            subject=object(),  # type: ignore[arg-type]
            action="process.execute",
            arguments_digest="sha256:arguments",
            risk="external_process",
            summary="Run a process",
        )


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


def test_approval_record_rejects_invalid_state() -> None:
    request = ApprovalRequest.from_arguments(
        "approval-1",
        run_id="run-1",
        subject=ResourceSnapshotRef("tool-call-1", "sha256:subject"),
        action="process.execute",
        arguments={"cmd": ["echo", "hello"]},
        risk="external_process",
        summary="Run a process",
    )

    with pytest.raises(ValueError, match="approval record id must match request approval_id"):
        ApprovalRecord("approval-2", request, "requested")
    with pytest.raises(ValueError, match="invalid approval status paused"):
        ApprovalRecord("approval-1", request, "paused")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="approved approval record requires approver"):
        ApprovalRecord("approval-1", request, "approved", decided_at="2026-06-22T00:00:00Z")
    with pytest.raises(ValueError, match="approved approval record requires decided_at"):
        ApprovalRecord("approval-1", request, "approved", approver=PrincipalRef("admin-1"))
    with pytest.raises(ValueError, match="denied approval record requires reason"):
        ApprovalRecord(
            "approval-1",
            request,
            "denied",
            approver=PrincipalRef("admin-1"),
            decided_at="2026-06-22T00:00:00Z",
        )
    with pytest.raises(ValueError, match="invalidated approval record requires invalidated_at"):
        ApprovalRecord("approval-1", request, "invalidated")
    with pytest.raises(ValueError, match="approval credential_refs item must not be empty"):
        ApprovalRecord("approval-1", request, "requested", credential_refs=("cred-1", " "))


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

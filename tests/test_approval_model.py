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


def test_approval_request_rejects_non_mapping_arguments_before_digesting() -> None:
    subject = ResourceSnapshotRef("tool-call-1", "sha256:subject")

    for arguments in (object(), ["echo", "hello"], "cmd=echo"):
        with pytest.raises(ValueError, match="approval request arguments must be a mapping"):
            ApprovalRequest.from_arguments(
                approval_id="approval-1",
                run_id="run-1",
                subject=subject,
                action="process.execute",
                arguments=arguments,  # type: ignore[arg-type]
                risk="external_process",
                summary="Run a process",
            )


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

    with pytest.raises(ValueError, match="approval request expires_at must be an ISO datetime"):
        ApprovalRequest(**(base_request | {"expires_at": "later"}))

    with pytest.raises(ValueError, match="approval request expires_at must be an ISO datetime"):
        ApprovalRequest(**(base_request | {"expires_at": "2026-06-22T00:10:00+0000"}))

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


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    (
        (
            {"approval_id": " approval-1"},
            "approval request approval_id must not contain surrounding whitespace",
        ),
        (
            {"run_id": "run-1 "},
            "approval request run_id must not contain surrounding whitespace",
        ),
        (
            {"action": " process.execute"},
            "approval request action must not contain surrounding whitespace",
        ),
        (
            {"arguments_digest": " sha256:arguments"},
            "approval request arguments_digest must not contain surrounding whitespace",
        ),
        (
            {"risk": " external_process"},
            "approval request risk must not contain surrounding whitespace",
        ),
        (
            {"metadata": {" ticket": "T-1"}},
            "approval request metadata keys must not contain surrounding whitespace",
        ),
        (
            {"metadata": {"scope": {" label": "approval"}}},
            "approval request metadata keys must not contain surrounding whitespace",
        ),
    ),
)
def test_approval_request_rejects_whitespace_wrapped_identities(
    overrides: dict[str, object],
    expected_error: str,
) -> None:
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

    with pytest.raises(ValueError, match=expected_error):
        ApprovalRequest(**(base_request | overrides))


def test_approval_request_metadata_is_copied_and_read_only() -> None:
    metadata = {"ticket": "T-1", "scope": {"labels": ["approval"]}}
    request = ApprovalRequest.from_arguments(
        "approval-1",
        run_id="run-1",
        subject=ResourceSnapshotRef("tool-call-1", "sha256:subject"),
        action="process.execute",
        arguments={"cmd": ["echo", "hello"]},
        risk="external_process",
        summary="Run a process",
        metadata=metadata,
    )
    metadata["ticket"] = "mutated"
    metadata["scope"]["labels"].append("mutated")  # type: ignore[index, union-attr]

    assert request.metadata == {"ticket": "T-1", "scope": {"labels": ("approval",)}}
    with pytest.raises(TypeError):
        request.metadata["ticket"] = "direct"
    with pytest.raises(TypeError):
        request.metadata["scope"]["labels"] = ("mutated",)  # type: ignore[index]
    with pytest.raises(AttributeError):
        request.metadata["scope"]["labels"].append("mutated")  # type: ignore[index, union-attr]
    with pytest.raises(ValueError, match="approval request metadata must be a mapping"):
        ApprovalRequest.from_arguments(
            "approval-1",
            run_id="run-1",
            subject=ResourceSnapshotRef("tool-call-1", "sha256:subject"),
            action="process.execute",
            arguments={"cmd": ["echo", "hello"]},
            risk="external_process",
            summary="Run a process",
            metadata=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="approval request metadata keys must be non-empty strings"):
        ApprovalRequest.from_arguments(
            "approval-1",
            run_id="run-1",
            subject=ResourceSnapshotRef("tool-call-1", "sha256:subject"),
            action="process.execute",
            arguments={"cmd": ["echo", "hello"]},
            risk="external_process",
            summary="Run a process",
            metadata={object(): "T-1"},  # type: ignore[dict-item]
        )
    with pytest.raises(ValueError, match="approval request metadata keys must be non-empty strings"):
        ApprovalRequest.from_arguments(
            "approval-1",
            run_id="run-1",
            subject=ResourceSnapshotRef("tool-call-1", "sha256:subject"),
            action="process.execute",
            arguments={"cmd": ["echo", "hello"]},
            risk="external_process",
            summary="Run a process",
            metadata={" ": "T-1"},
        )


@pytest.mark.parametrize("invalid_value", ({"bad"}, object()))
def test_approval_request_rejects_non_json_metadata_values(invalid_value: object) -> None:
    with pytest.raises(
        ValueError,
        match="approval request metadata must contain strict canonical JSON",
    ):
        ApprovalRequest.from_arguments(
            "approval-1",
            run_id="run-1",
            subject=ResourceSnapshotRef("tool-call-1", "sha256:subject"),
            action="process.execute",
            arguments={"cmd": ["echo", "hello"]},
            risk="external_process",
            summary="Run a process",
            metadata={"invalid": invalid_value},
        )


def test_approval_request_rejects_cyclic_metadata_and_invalid_arguments() -> None:
    metadata: dict[str, object] = {}
    metadata["self"] = metadata
    request_args = {
        "approval_id": "approval-1",
        "run_id": "run-1",
        "subject": ResourceSnapshotRef("tool-call-1", "sha256:subject"),
        "action": "process.execute",
        "risk": "external_process",
        "summary": "Run a process",
    }

    with pytest.raises(ValueError, match="metadata must not contain cyclic values"):
        ApprovalRequest.from_arguments(**request_args, arguments={}, metadata=metadata)
    with pytest.raises(ValueError, match="arguments must contain strict canonical JSON"):
        ApprovalRequest.from_arguments(
            **request_args,
            arguments={"invalid": object()},
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


def test_approval_record_validity_honors_request_expiration() -> None:
    subject = ResourceSnapshotRef("tool-call-1", "sha256:subject")
    request = ApprovalRequest.from_arguments(
        "approval-1",
        run_id="run-1",
        subject=subject,
        action="process.execute",
        arguments={"cmd": ["echo", "hello"]},
        risk="external_process",
        summary="Run a process",
        expires_at="2026-06-22T00:10:00Z",
    )
    record = ApprovalRecord.approve(
        request,
        approver=PrincipalRef("admin-1"),
        decided_at="2026-06-22T00:05:00Z",
    )

    assert record.is_valid_for(subject, request.arguments_digest, now="2026-06-21T19:09:59-05:00") is True
    assert record.is_valid_for(subject, request.arguments_digest, now="2026-06-22T00:10:00Z") is False
    assert record.is_valid_for(subject, request.arguments_digest, now="2026-06-22T00:10:01Z") is False
    assert record.is_valid_for(subject, request.arguments_digest) is False
    assert record.is_valid_for(subject, request.arguments_digest, now="not-a-date") is False
    assert record.is_valid_for(subject, request.arguments_digest, now="2026-06-22 00:09:59Z") is False

    with pytest.raises(ValueError, match="approved approval record decided_at must be before expires_at"):
        ApprovalRecord.approve(
            request,
            approver=PrincipalRef("admin-1"),
            decided_at="2026-06-22T00:10:00Z",
        )
    with pytest.raises(ValueError, match="approved approval record decided_at must be before expires_at"):
        ApprovalRecord.approve(
            request,
            approver=PrincipalRef("admin-1"),
            decided_at="2026-06-22T00:10:01Z",
        )
    with pytest.raises(ValueError, match="denied approval record decided_at must be before expires_at"):
        ApprovalRecord.deny(
            request,
            approver=PrincipalRef("admin-1"),
            decided_at="2026-06-22T00:10:01Z",
            reason="too late",
        )


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
    with pytest.raises(ValueError, match="approval record decided_at must be an ISO datetime"):
        ApprovalRecord("approval-1", request, "approved", approver=PrincipalRef("admin-1"), decided_at="later")
    with pytest.raises(ValueError, match="approval record decided_at must be an ISO datetime"):
        ApprovalRecord(
            "approval-1",
            request,
            "approved",
            approver=PrincipalRef("admin-1"),
            decided_at="2026-06-22T00:00:00+0000",
        )
    with pytest.raises(ValueError, match="approval record invalidated_at must be an ISO datetime"):
        ApprovalRecord("approval-1", request, "invalidated", invalidated_at="later")
    with pytest.raises(ValueError, match="approval record invalidated_at must be an ISO datetime"):
        ApprovalRecord("approval-1", request, "invalidated", invalidated_at="2026-06-22 00:00:00Z")
    with pytest.raises(ValueError, match="approval credential_refs item must not be empty"):
        ApprovalRecord("approval-1", request, "requested", credential_refs=("cred-1", " "))
    with pytest.raises(ValueError, match="approval record metadata must be a mapping"):
        ApprovalRecord("approval-1", request, "requested", metadata=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="approval record metadata keys must be non-empty strings"):
        ApprovalRecord("approval-1", request, "requested", metadata={object(): "T-1"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="approval record metadata keys must be non-empty strings"):
        ApprovalRecord("approval-1", request, "requested", metadata={" ": "T-1"})


@pytest.mark.parametrize(
    ("factory", "expected_error"),
    (
        (
            lambda request: ApprovalRecord(" approval-1", request, "requested"),
            "approval record approval_id must not contain surrounding whitespace",
        ),
        (
            lambda request: ApprovalRecord("approval-1", request, "requested", credential_refs=(" cred-1",)),
            "approval credential_refs item must not contain surrounding whitespace",
        ),
        (
            lambda request: ApprovalRecord("approval-1", request, "requested", metadata={" review": "security"}),
            "approval record metadata keys must not contain surrounding whitespace",
        ),
        (
            lambda request: ApprovalRecord(
                "approval-1",
                request,
                "requested",
                metadata={"review": {" label": "security"}},
            ),
            "approval record metadata keys must not contain surrounding whitespace",
        ),
    ),
)
def test_approval_record_rejects_whitespace_wrapped_identities(factory, expected_error: str) -> None:
    request = ApprovalRequest.from_arguments(
        "approval-1",
        run_id="run-1",
        subject=ResourceSnapshotRef("tool-call-1", "sha256:subject"),
        action="process.execute",
        arguments={"cmd": ["echo", "hello"]},
        risk="external_process",
        summary="Run a process",
    )

    with pytest.raises(ValueError, match=expected_error):
        factory(request)


def test_approval_record_metadata_is_copied_and_read_only() -> None:
    request = ApprovalRequest.from_arguments(
        "approval-1",
        run_id="run-1",
        subject=ResourceSnapshotRef("tool-call-1", "sha256:subject"),
        action="process.execute",
        arguments={"cmd": ["echo", "hello"]},
        risk="external_process",
        summary="Run a process",
    )
    metadata = {"review": {"labels": ["security"]}}

    record = ApprovalRecord.approve(
        request,
        approver=PrincipalRef("admin-1"),
        decided_at="2026-06-22T00:00:00Z",
        metadata=metadata,
    )
    metadata["review"]["labels"].append("mutated")  # type: ignore[index, union-attr]

    assert record.metadata == {"review": {"labels": ("security",)}}
    with pytest.raises(TypeError):
        record.metadata["review"]["labels"] = ("mutated",)  # type: ignore[index]
    with pytest.raises(AttributeError):
        record.metadata["review"]["labels"].append("mutated")  # type: ignore[index, union-attr]


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

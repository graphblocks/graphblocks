from __future__ import annotations

import pytest

from graphblocks.worker import (
    WORKER_PROTOCOL_VERSION,
    BlockCapability,
    RunOwnershipLease,
    WorkerAdmissionDecision,
    WorkerAdmissionPolicy,
    WorkerAdvertisement,
    WorkerIncompatiblePackageLockError,
    WorkerIncompatibleVersionError,
    WorkerInvocationContext,
    WorkerInvokeRequest,
    WorkerInvokeResult,
    WorkerMismatchedNodeAttemptError,
    WorkerNoEligibleWorkerError,
    WorkerStaleLeaseEpochError,
    admit_worker,
    admit_worker_with_policy,
    evaluate_worker_admission,
    select_worker_for_block,
    validate_worker_result,
)


def test_worker_advertisement_round_trips_and_admits_current_protocol() -> None:
    advertisement = WorkerAdvertisement.new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability("prompt.render@1"), BlockCapability("model.generate@1")],
    )

    encoded = advertisement.to_wire()
    decoded = WorkerAdvertisement.from_wire(encoded)

    assert encoded["targetId"] == "doc-cpu"
    assert encoded["packageLockHash"] == "sha256:package-lock"
    assert encoded["imageDigest"] == "sha256:image"
    assert encoded["state"] == "ready"
    assert decoded.protocol_version == WORKER_PROTOCOL_VERSION
    assert decoded.worker_id == "worker-local-1"
    assert decoded.supported_blocks == (
        BlockCapability("prompt.render@1"),
        BlockCapability("model.generate@1"),
    )
    assert admit_worker(decoded) is None


def test_worker_admission_rejects_version_and_package_lock_mismatch() -> None:
    advertisement = WorkerAdvertisement.new(
        "worker-local-1",
        "doc-cpu",
        "sha256:actual-package-lock",
        "sha256:image",
        [BlockCapability("prompt.render@1")],
    )

    with pytest.raises(WorkerIncompatibleVersionError) as version_error:
        admit_worker(advertisement.with_protocol_version(WORKER_PROTOCOL_VERSION + 1))

    assert version_error.value.expected == WORKER_PROTOCOL_VERSION
    assert version_error.value.actual == WORKER_PROTOCOL_VERSION + 1

    policy = WorkerAdmissionPolicy.current().require_package_lock_hash("sha256:expected-lock")
    with pytest.raises(WorkerIncompatiblePackageLockError) as package_error:
        admit_worker_with_policy(policy, advertisement)

    assert package_error.value.expected == "sha256:expected-lock"
    assert package_error.value.actual == "sha256:actual-package-lock"


def test_worker_admission_decision_reports_drain_and_missing_capability() -> None:
    advertisement = WorkerAdvertisement.new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability("prompt.render@1")],
    ).with_state("draining")
    policy = (
        WorkerAdmissionPolicy.current()
        .require_package_lock_hash("sha256:package-lock")
        .require_block("model.generate@1")
    )

    decision = evaluate_worker_admission(policy, advertisement)

    assert decision.admitted is False
    assert decision.worker_id == "worker-local-1"
    assert decision.target_id == "doc-cpu"
    assert decision.required_block == "model.generate@1"
    assert decision.reason_codes == ("worker.not_ready", "worker.missing_required_block")
    assert decision.to_wire()["reasonCodes"] == [
        "worker.not_ready",
        "worker.missing_required_block",
    ]
    assert WorkerAdmissionDecision.from_wire(decision.to_wire()) == decision


def test_worker_admission_decision_allows_ready_matching_worker() -> None:
    advertisement = WorkerAdvertisement.new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability("prompt.render@1")],
    )
    policy = WorkerAdmissionPolicy.current().require_block("prompt.render@1")

    decision = evaluate_worker_admission(policy, advertisement)

    assert decision.admitted is True
    assert decision.reason_codes == ()


def test_worker_selection_skips_draining_and_saturated_workers() -> None:
    ready_late = WorkerAdvertisement.new(
        "worker-z",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-z",
        [BlockCapability("model.generate@1")],
    )
    draining = WorkerAdvertisement.new(
        "worker-a",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-a",
        [BlockCapability("model.generate@1")],
    ).with_state("draining")
    saturated = WorkerAdvertisement.new(
        "worker-b",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-b",
        [BlockCapability("model.generate@1")],
    ).with_state("saturated")
    ready_early = WorkerAdvertisement.new(
        "worker-c",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-c",
        [BlockCapability("model.generate@1")],
    )

    selected = select_worker_for_block([ready_late, draining, saturated, ready_early], "model.generate@1")

    assert selected.worker_id == "worker-c"


def test_worker_selection_reports_when_no_ready_worker_supports_block() -> None:
    workers = [
        WorkerAdvertisement.new(
            "worker-a",
            "model-cpu",
            "sha256:package-lock",
            "sha256:image-a",
            [BlockCapability("model.generate@1")],
        ).with_state("draining"),
        WorkerAdvertisement.new(
            "worker-b",
            "model-cpu",
            "sha256:package-lock",
            "sha256:image-b",
            [BlockCapability("prompt.render@1")],
        ),
    ]

    with pytest.raises(WorkerNoEligibleWorkerError) as error:
        select_worker_for_block(workers, "model.generate@1")

    assert error.value.block == "model.generate@1"


def test_worker_invocation_envelopes_preserve_json_payloads_and_context() -> None:
    request = WorkerInvokeRequest(
        invocation_id="invoke-000001",
        run_id="run-000001",
        node_id="render",
        node_attempt_id="render-attempt-1",
        lease_epoch=7,
        block="prompt.render@1",
        context=(
            WorkerInvocationContext("release-1", "rev-1")
            .with_trace("trace-1", "span-parent")
            .with_policy_snapshot("policy-snapshot-1", "sha256:policy")
            .with_budget_permit("permit-1", "sha256:budget-permit")
            .with_attribute("tenant", "acme")
        ),
        inputs={"message": {"text": "Hello"}},
        config={"template": "Echo {message.text}"},
    )
    result = WorkerInvokeResult(
        invocation_id=request.invocation_id,
        node_attempt_id=request.node_attempt_id,
        lease_epoch=request.lease_epoch,
        outputs={"prompt": "Echo Hello"},
    )

    encoded = request.to_wire()

    assert encoded["nodeAttemptId"] == "render-attempt-1"
    assert encoded["leaseEpoch"] == 7
    assert encoded["context"]["deploymentRevisionId"] == "rev-1"
    assert encoded["context"]["policySnapshotDigest"] == "sha256:policy"
    assert encoded["context"]["attributes"]["tenant"] == "acme"
    assert WorkerInvokeRequest.from_wire(encoded) == request
    assert WorkerInvokeResult.from_wire(result.to_wire()) == result
    assert validate_worker_result(request, result) is None


def test_run_ownership_lease_round_trips_with_fencing_epoch() -> None:
    lease = RunOwnershipLease(
        run_id="run-000001",
        owner_instance_id="control-plane-a",
        lease_epoch=42,
        expires_at_unix_ms=1_820_000_000_000,
        last_checkpoint="checkpoint-000004",
    )

    encoded = lease.to_wire()

    assert encoded["leaseEpoch"] == 42
    assert encoded["expiresAtUnixMs"] == 1_820_000_000_000
    assert RunOwnershipLease.from_wire(encoded) == lease


def test_worker_result_validation_rejects_mismatched_attempt_or_lease_epoch() -> None:
    request = WorkerInvokeRequest(
        invocation_id="invoke-000001",
        run_id="run-000001",
        node_id="render",
        node_attempt_id="render-attempt-2",
        lease_epoch=9,
        block="prompt.render@1",
        context=WorkerInvocationContext("release-1", "rev-1"),
        inputs={"message": {"text": "Hello"}},
        config={"template": "Echo {message.text}"},
    )
    mismatched_attempt = WorkerInvokeResult(
        invocation_id=request.invocation_id,
        node_attempt_id="render-attempt-1",
        lease_epoch=request.lease_epoch,
        outputs={},
    )

    with pytest.raises(WorkerMismatchedNodeAttemptError) as attempt_error:
        validate_worker_result(request, mismatched_attempt)

    assert attempt_error.value.expected == "render-attempt-2"
    assert attempt_error.value.actual == "render-attempt-1"

    stale_epoch = WorkerInvokeResult(
        invocation_id=request.invocation_id,
        node_attempt_id=request.node_attempt_id,
        lease_epoch=8,
        outputs={},
    )
    with pytest.raises(WorkerStaleLeaseEpochError) as epoch_error:
        validate_worker_result(request, stale_epoch)

    assert epoch_error.value.expected == 9
    assert epoch_error.value.actual == 8

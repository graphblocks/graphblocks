from __future__ import annotations

import pytest

import graphblocks
from graphblocks.worker import (
    VALID_WORKER_PROTOCOL_MESSAGE_KINDS,
    VALID_WORKER_STATES,
    WORKER_PROTOCOL_VERSION,
    BlockCapability,
    RemoteEdgePayload,
    RemotePayloadInvalidArtifactRefError,
    RemotePayloadInvalidLimitError,
    RemotePayloadInvalidModeError,
    RemotePayloadLimits,
    RemotePayloadOversizedInlineError,
    RunOwnershipLease,
    WorkerAdmissionDecision,
    WorkerAdmissionPolicy,
    WorkerAdvertisement,
    WorkerDrainDecision,
    WorkerDrainPlan,
    WorkerDrainPolicy,
    WorkerDrainTask,
    WorkerIncompatiblePackageLockError,
    WorkerIncompatibleVersionError,
    WorkerInvocationContext,
    WorkerInvokeRequest,
    WorkerInvokeResult,
    WorkerMismatchedNodeAttemptError,
    WorkerNoEligibleWorkerError,
    WorkerProtocolError,
    WorkerProtocolMessage,
    WorkerStaleLeaseEpochError,
    admit_worker,
    admit_worker_with_policy,
    evaluate_worker_admission,
    select_worker_for_block,
    validate_remote_payload,
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


def test_worker_advertisement_rejects_invalid_wire_payloads() -> None:
    with pytest.raises(WorkerProtocolError, match="block capability block must be a string"):
        BlockCapability.from_wire({"block": 7})
    with pytest.raises(WorkerProtocolError, match="block capability block must not be empty"):
        BlockCapability(" ")

    base = WorkerAdvertisement.new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability("prompt.render@1")],
    ).to_wire()
    invalid_advertisements = (
        (
            {**base, "workerId": object()},
            "worker advertisement worker_id must be a string",
        ),
        (
            {**base, "targetId": " "},
            "worker advertisement target_id must not be empty",
        ),
        (
            {**base, "protocolVersion": True},
            "worker advertisement protocol_version must be an integer",
        ),
        (
            {**base, "state": "paused"},
            "worker advertisement state has invalid value",
        ),
        (
            {**base, "supportedBlocks": {}},
            "worker advertisement supportedBlocks must be a list",
        ),
        (
            {**base, "supportedBlocks": [object()]},
            "worker advertisement supportedBlocks entries must be mappings",
        ),
        (
            {**base, "supportedBlocks": [{"block": ""}]},
            "block capability block must not be empty",
        ),
    )

    for payload, message in invalid_advertisements:
        with pytest.raises(WorkerProtocolError, match=message):
            WorkerAdvertisement.from_wire(payload)

    with pytest.raises(
        WorkerProtocolError,
        match="worker advertisement supported_blocks must be BlockCapability",
    ):
        WorkerAdvertisement(
            "worker-local-1",
            "doc-cpu",
            "sha256:package-lock",
            "sha256:image",
            ("prompt.render@1",),
        )


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


def test_worker_admission_policy_and_helpers_validate_inputs() -> None:
    advertisement = WorkerAdvertisement.new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability("prompt.render@1")],
    )

    with pytest.raises(WorkerProtocolError, match="worker admission policy protocol_version must be an integer"):
        WorkerAdmissionPolicy(protocol_version=True)  # type: ignore[arg-type]
    with pytest.raises(WorkerProtocolError, match="worker admission policy protocol_version must not be negative"):
        WorkerAdmissionPolicy(protocol_version=-1)
    with pytest.raises(WorkerProtocolError, match="worker admission policy package_lock_hash must not be empty"):
        WorkerAdmissionPolicy(package_lock_hash=" ")
    with pytest.raises(WorkerProtocolError, match="worker admission policy required_block must not be empty"):
        WorkerAdmissionPolicy.current().require_block(" ")
    with pytest.raises(WorkerProtocolError, match="worker admission policy package_lock_hash must be a string"):
        WorkerAdmissionPolicy.current().require_package_lock_hash(object())  # type: ignore[arg-type]

    with pytest.raises(WorkerProtocolError, match="worker admission advertisement must be WorkerAdvertisement"):
        admit_worker(object())  # type: ignore[arg-type]
    with pytest.raises(WorkerProtocolError, match="worker admission policy must be WorkerAdmissionPolicy"):
        admit_worker_with_policy(object(), advertisement)  # type: ignore[arg-type]
    with pytest.raises(WorkerProtocolError, match="worker admission advertisement must be WorkerAdvertisement"):
        admit_worker_with_policy(WorkerAdmissionPolicy.current(), object())  # type: ignore[arg-type]
    with pytest.raises(WorkerProtocolError, match="worker admission policy must be WorkerAdmissionPolicy"):
        evaluate_worker_admission(object(), advertisement)  # type: ignore[arg-type]
    with pytest.raises(WorkerProtocolError, match="worker admission advertisement must be WorkerAdvertisement"):
        evaluate_worker_admission(WorkerAdmissionPolicy.current(), object())  # type: ignore[arg-type]


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


def test_worker_admission_decision_rejects_invalid_wire_payloads() -> None:
    base = WorkerAdmissionDecision(
        admitted=False,
        worker_id="worker-local-1",
        target_id="doc-cpu",
        protocol_version=WORKER_PROTOCOL_VERSION,
        package_lock_hash="sha256:package-lock",
        state="draining",
        reason_codes=("worker.not_ready",),
        required_block="model.generate@1",
    ).to_wire()
    invalid_decisions = (
        (
            {**base, "admitted": "false"},
            "worker admission decision admitted must be a boolean",
        ),
        (
            {**base, "workerId": object()},
            "worker admission decision worker_id must be a string",
        ),
        (
            {**base, "protocolVersion": False},
            "worker admission decision protocol_version must be an integer",
        ),
        (
            {**base, "state": "paused"},
            "worker admission decision state has invalid value",
        ),
        (
            {**base, "reasonCodes": "worker.not_ready"},
            "worker admission decision reasonCodes must be a list",
        ),
        (
            {**base, "reasonCodes": ["worker.not_ready", " "]},
            "worker admission decision reason_code must not be empty",
        ),
        (
            {**base, "requiredBlock": " "},
            "worker admission decision required_block must not be empty",
        ),
    )

    for payload, message in invalid_decisions:
        with pytest.raises(WorkerProtocolError, match=message):
            WorkerAdmissionDecision.from_wire(payload)


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


def test_top_level_package_exports_worker_admission_decision_api() -> None:
    advertisement = graphblocks.WorkerAdvertisement.new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [graphblocks.BlockCapability("prompt.render@1")],
    )
    policy = graphblocks.WorkerAdmissionPolicy.current().require_block("prompt.render@1")

    decision = graphblocks.evaluate_worker_admission(policy, advertisement)

    assert isinstance(decision, graphblocks.WorkerAdmissionDecision)
    assert decision.admitted is True
    request = graphblocks.WorkerInvokeRequest(
        invocation_id="invoke-1",
        run_id="run-1",
        node_id="render",
        node_attempt_id="render-attempt-1",
        lease_epoch=3,
        block="prompt.render@1",
        context=graphblocks.WorkerInvocationContext("release-1", "rev-old"),
        inputs={},
        config={},
    )
    drain_plan = graphblocks.WorkerDrainPlan.for_worker(
        advertisement,
        graphblocks.WorkerDrainPolicy(),
        (graphblocks.WorkerDrainTask("online_request", request, started_at_unix_ms=0),),
        drain_started_at_unix_ms=1,
        now_unix_ms=1,
    )
    assert isinstance(drain_plan, graphblocks.WorkerDrainPlan)
    assert drain_plan.decisions[0].deployment_revision_id == "rev-old"
    edge_payload = graphblocks.RemoteEdgePayload.inline(
        "graphblocks.ai/Message@1",
        {"text": "hello"},
        graphblocks.RemotePayloadLimits(max_inline_bytes=64),
    )
    assert edge_payload.to_wire()["valueDigest"].startswith("sha256:")
    assert graphblocks.VALID_WORKER_PROTOCOL_MESSAGE_KINDS is VALID_WORKER_PROTOCOL_MESSAGE_KINDS
    assert graphblocks.VALID_WORKER_STATES is VALID_WORKER_STATES
    assert "VALID_WORKER_PROTOCOL_MESSAGE_KINDS" in graphblocks.__all__
    assert "VALID_WORKER_STATES" in graphblocks.__all__


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


def test_worker_selection_validates_inputs() -> None:
    advertisement = WorkerAdvertisement.new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability("prompt.render@1")],
    )

    with pytest.raises(WorkerProtocolError, match="worker selection workers must be iterable"):
        select_worker_for_block("worker-local-1", "prompt.render@1")  # type: ignore[arg-type]
    with pytest.raises(WorkerProtocolError, match="worker selection workers must be WorkerAdvertisement"):
        select_worker_for_block([object()], "prompt.render@1")  # type: ignore[list-item]
    with pytest.raises(WorkerProtocolError, match="worker selection block must not be empty"):
        select_worker_for_block([advertisement], " ")
    with pytest.raises(WorkerProtocolError, match="worker selection block must be a string"):
        select_worker_for_block([advertisement], object())  # type: ignore[arg-type]


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
    assert encoded["context"]["budgetPermitId"] == "permit-1"
    assert encoded["context"]["budgetPermitDigest"] == "sha256:budget-permit"
    assert encoded["context"]["attributes"]["tenant"] == "acme"
    assert WorkerInvokeRequest.from_wire(encoded) == request
    assert WorkerInvokeResult.from_wire(result.to_wire()) == result
    assert validate_worker_result(request, result) is None


def test_worker_protocol_message_envelopes_route_typed_payloads() -> None:
    advertisement = WorkerAdvertisement.new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability("prompt.render@1")],
    )
    request = WorkerInvokeRequest(
        invocation_id="invoke-000001",
        run_id="run-000001",
        node_id="render",
        node_attempt_id="render-attempt-1",
        lease_epoch=7,
        block="prompt.render@1",
        context=WorkerInvocationContext("release-1", "rev-1"),
        inputs={"message": {"text": "Hello"}},
        config={"template": "Echo {message.text}"},
    )
    result = WorkerInvokeResult(
        invocation_id=request.invocation_id,
        node_attempt_id=request.node_attempt_id,
        lease_epoch=request.lease_epoch,
        outputs={"prompt": "Echo Hello"},
    )
    decision = evaluate_worker_admission(
        WorkerAdmissionPolicy.current().require_block("prompt.render@1"),
        advertisement,
    )
    drain_plan = WorkerDrainPlan.for_worker(
        advertisement,
        WorkerDrainPolicy(),
        (WorkerDrainTask("online_request", request, started_at_unix_ms=0),),
        drain_started_at_unix_ms=1,
        now_unix_ms=1,
    )
    messages = (
        WorkerProtocolMessage.advertisement("msg-1", 1, advertisement, correlation_id="worker-local-1"),
        WorkerProtocolMessage.admission_decision(
            "msg-2",
            2,
            decision,
            correlation_id="worker-local-1",
            causation_id="msg-1",
        ),
        WorkerProtocolMessage.invoke_request("msg-3", 3, request),
        WorkerProtocolMessage.invoke_result("msg-4", 4, result, causation_id="msg-3"),
        WorkerProtocolMessage.drain_plan("msg-5", 5, drain_plan, causation_id="msg-1"),
        WorkerProtocolMessage.error(
            "msg-6",
            6,
            code="worker.timeout",
            message="worker timed out",
            retryable=True,
            correlation_id=request.invocation_id,
            causation_id="msg-3",
        ),
    )

    encoded = [message.to_wire() for message in messages]

    assert [item["kind"] for item in encoded] == [
        "advertisement",
        "admission_decision",
        "invoke_request",
        "invoke_result",
        "drain_plan",
        "error",
    ]
    assert encoded[2]["correlationId"] == "invoke-000001"
    assert encoded[3]["correlationId"] == "invoke-000001"
    assert encoded[5]["payload"] == {
        "code": "worker.timeout",
        "message": "worker timed out",
        "retryable": True,
    }
    assert [WorkerProtocolMessage.from_wire(item) for item in encoded] == list(messages)
    assert messages[0].content_digest().startswith("sha256:")
    assert messages[0].content_digest() == WorkerProtocolMessage.from_wire(encoded[0]).content_digest()
    canonical_message = WorkerProtocolMessage.invoke_request("message-000001", 42, request)
    assert (
        canonical_message.content_digest()
        == "sha256:7f9eb71b38fd97576ffe9c6d07a6f93a5decd8b76a2ebbe800221ce07099e7e0"
    )
    assert canonical_message.content_digest() == WorkerProtocolMessage.from_wire(
        canonical_message.to_wire()
    ).content_digest()
    assert isinstance(graphblocks.WorkerProtocolMessage.from_wire(encoded[2]), graphblocks.WorkerProtocolMessage)


def test_worker_protocol_message_rejects_invalid_wire_shapes() -> None:
    advertisement = WorkerAdvertisement.new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability("prompt.render@1")],
    )
    base = WorkerProtocolMessage.advertisement("msg-1", 1, advertisement).to_wire()
    invalid_messages = (
        (
            {**base, "messageId": " "},
            "worker protocol message message_id must not be empty",
        ),
        (
            {**base, "protocolVersion": True},
            "worker protocol message protocol_version must be an integer",
        ),
        (
            {**base, "sequence": -1},
            "worker protocol message sequence must not be negative",
        ),
        (
            {**base, "kind": "heartbeat"},
            "worker protocol message kind has invalid value",
        ),
        (
            {**base, "payload": []},
            "worker protocol message advertisement payload must be a mapping",
        ),
        (
            {**base, "correlationId": ""},
            "worker protocol message correlation_id must not be empty",
        ),
        (
            WorkerProtocolMessage.error("msg-2", 2, code="worker.failed", message="failed").to_wire()
            | {"payload": {"code": "worker.failed", "message": "failed", "retryable": "yes"}},
            "worker protocol message error retryable must be a boolean",
        ),
        (
            WorkerProtocolMessage.error("msg-3", 3, code="worker.failed", message="failed").to_wire()
            | {"payload": {"code": " ", "message": "failed"}},
            "worker protocol message error code must not be empty",
        ),
    )

    for payload, message in invalid_messages:
        with pytest.raises(WorkerProtocolError, match=message):
            WorkerProtocolMessage.from_wire(payload)

    with pytest.raises(
        WorkerProtocolError,
        match="worker protocol message invoke_request payload must be WorkerInvokeRequest",
    ):
        WorkerProtocolMessage("msg-4", "invoke_request", 4, advertisement)


def test_worker_invocation_context_rejects_invalid_propagation_fields() -> None:
    invalid_contexts = (
        (
            lambda: WorkerInvocationContext(7, "rev-1"),  # type: ignore[arg-type]
            "worker invocation context release_id must be a string",
        ),
        (
            lambda: WorkerInvocationContext(" ", "rev-1"),
            "worker invocation context release_id must not be empty",
        ),
        (
            lambda: WorkerInvocationContext("release-1", ""),
            "worker invocation context deployment_revision_id must not be empty",
        ),
        (
            lambda: WorkerInvocationContext("release-1", "rev-1", trace_id=" "),
            "worker invocation context trace_id must not be empty",
        ),
        (
            lambda: WorkerInvocationContext(
                "release-1",
                "rev-1",
                policy_snapshot_id="policy-snapshot-1",
            ),
            "worker invocation context policy snapshot id and digest must be provided together",
        ),
        (
            lambda: WorkerInvocationContext(
                "release-1",
                "rev-1",
                budget_permit_digest="sha256:budget-permit",
            ),
            "worker invocation context budget permit id and digest must be provided together",
        ),
        (
            lambda: WorkerInvocationContext(  # type: ignore[arg-type]
                "release-1",
                "rev-1",
                attributes="tenant",
            ),
            "worker invocation context attributes must be a mapping",
        ),
        (
            lambda: WorkerInvocationContext(  # type: ignore[arg-type]
                "release-1",
                "rev-1",
                attributes={object(): "acme"},
            ),
            "worker invocation context attribute keys must be strings",
        ),
        (
            lambda: WorkerInvocationContext("release-1", "rev-1", attributes={" ": "acme"}),
            "worker invocation context attribute keys must not be empty",
        ),
        (
            lambda: WorkerInvocationContext(  # type: ignore[arg-type]
                "release-1",
                "rev-1",
                attributes={"tenant": object()},
            ),
            "worker invocation context attribute values must be strings",
        ),
        (
            lambda: WorkerInvocationContext("release-1", "rev-1").with_attribute(" ", "acme"),
            "worker invocation context attribute keys must not be empty",
        ),
        (
            lambda: WorkerInvocationContext.from_wire(
                {
                    "releaseId": "release-1",
                    "deploymentRevisionId": "rev-1",
                    "attributes": [("tenant", "acme")],
                }
            ),
            "worker invocation context attributes must be a mapping",
        ),
    )

    for build_context, message in invalid_contexts:
        with pytest.raises(WorkerProtocolError, match=message):
            build_context()


def test_worker_invoke_request_rejects_invalid_envelope_fields() -> None:
    base_request = {
        "invocation_id": "invoke-000001",
        "run_id": "run-000001",
        "node_id": "render",
        "node_attempt_id": "render-attempt-1",
        "lease_epoch": 7,
        "block": "prompt.render@1",
        "context": WorkerInvocationContext("release-1", "rev-1"),
        "inputs": {},
        "config": {},
    }
    invalid_requests = (
        (
            {**base_request, "invocation_id": " "},
            "worker invoke request invocation_id must not be empty",
        ),
        (
            {**base_request, "run_id": object()},
            "worker invoke request run_id must be a string",
        ),
        (
            {**base_request, "node_id": ""},
            "worker invoke request node_id must not be empty",
        ),
        (
            {**base_request, "node_attempt_id": " "},
            "worker invoke request node_attempt_id must not be empty",
        ),
        (
            {**base_request, "block": ""},
            "worker invoke request block must not be empty",
        ),
        (
            {**base_request, "lease_epoch": True},
            "worker invoke request lease_epoch must be an integer",
        ),
        (
            {**base_request, "lease_epoch": -1},
            "worker invoke request lease_epoch must not be negative",
        ),
        (
            {**base_request, "context": object()},
            "worker invoke request context must be a WorkerInvocationContext",
        ),
    )

    for kwargs, message in invalid_requests:
        with pytest.raises(WorkerProtocolError, match=message):
            WorkerInvokeRequest(**kwargs)

    encoded = WorkerInvokeRequest(**base_request).to_wire()
    encoded["invocationId"] = 7
    with pytest.raises(
        WorkerProtocolError,
        match="worker invoke request invocation_id must be a string",
    ):
        WorkerInvokeRequest.from_wire(encoded)

    encoded = WorkerInvokeRequest(**base_request).to_wire()
    encoded["context"] = []
    with pytest.raises(
        WorkerProtocolError,
        match="worker invoke request context must be a mapping",
    ):
        WorkerInvokeRequest.from_wire(encoded)


def test_worker_invoke_result_rejects_invalid_envelope_fields() -> None:
    base_result = {
        "invocation_id": "invoke-000001",
        "node_attempt_id": "render-attempt-1",
        "lease_epoch": 7,
        "outputs": {"prompt": "Echo Hello"},
    }
    invalid_results = (
        (
            {**base_result, "invocation_id": " "},
            "worker invoke result invocation_id must not be empty",
        ),
        (
            {**base_result, "node_attempt_id": object()},
            "worker invoke result node_attempt_id must be a string",
        ),
        (
            {**base_result, "lease_epoch": False},
            "worker invoke result lease_epoch must be an integer",
        ),
        (
            {**base_result, "lease_epoch": -1},
            "worker invoke result lease_epoch must not be negative",
        ),
        (
            {**base_result, "outputs": []},
            "worker invoke result outputs must be a mapping",
        ),
        (
            {**base_result, "outputs": {object(): "Echo Hello"}},
            "worker invoke result output keys must be strings",
        ),
        (
            {**base_result, "outputs": {" ": "Echo Hello"}},
            "worker invoke result output keys must not be empty",
        ),
    )

    for kwargs, message in invalid_results:
        with pytest.raises(WorkerProtocolError, match=message):
            WorkerInvokeResult(**kwargs)

    encoded = WorkerInvokeResult(**base_result).to_wire()
    encoded["invocationId"] = 7
    with pytest.raises(
        WorkerProtocolError,
        match="worker invoke result invocation_id must be a string",
    ):
        WorkerInvokeResult.from_wire(encoded)

    encoded = WorkerInvokeResult(**base_result).to_wire()
    encoded["outputs"] = []
    with pytest.raises(
        WorkerProtocolError,
        match="worker invoke result outputs must be a mapping",
    ):
        WorkerInvokeResult.from_wire(encoded)


def test_worker_drain_plan_closes_admission_and_preserves_inflight_affinity() -> None:
    worker = WorkerAdvertisement.new(
        "worker-a",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-a",
        [BlockCapability("model.generate@1")],
    )
    online_request = WorkerInvokeRequest(
        invocation_id="invoke-online",
        run_id="run-online",
        node_id="model",
        node_attempt_id="model-attempt-1",
        lease_epoch=7,
        block="model.generate@1",
        context=WorkerInvocationContext("release-1", "rev-old"),
        inputs={"prompt": "hello"},
        config={},
    )
    durable_request = WorkerInvokeRequest(
        invocation_id="invoke-durable",
        run_id="run-durable",
        node_id="embed",
        node_attempt_id="embed-attempt-2",
        lease_epoch=13,
        block="embedding.generate@1",
        context=WorkerInvocationContext("release-1", "rev-old"),
        inputs={"text": "hello"},
        config={},
    )

    plan = WorkerDrainPlan.for_worker(
        worker,
        WorkerDrainPolicy(),
        (
            WorkerDrainTask("online_request", online_request, started_at_unix_ms=1_000),
            WorkerDrainTask("durable_task", durable_request, started_at_unix_ms=1_000, checkpointable=True),
        ),
        drain_started_at_unix_ms=2_000,
        now_unix_ms=302_000,
    )

    assert plan.worker_id == "worker-a"
    assert plan.worker_state == "draining"
    assert plan.admission_closed is True
    assert plan.decisions[0].run_id == "run-online"
    assert plan.decisions[0].lease_epoch == 7
    assert plan.decisions[0].deployment_revision_id == "rev-old"
    assert plan.decisions[0].disposition == "cancel"
    assert plan.decisions[1].run_id == "run-durable"
    assert plan.decisions[1].lease_epoch == 13
    assert plan.decisions[1].disposition == "checkpoint"
    assert WorkerDrainPlan.from_wire(plan.to_wire()) == plan


def test_worker_drain_payloads_reject_invalid_wire_shapes() -> None:
    request = WorkerInvokeRequest(
        invocation_id="invoke-000001",
        run_id="run-000001",
        node_id="render",
        node_attempt_id="render-attempt-1",
        lease_epoch=7,
        block="prompt.render@1",
        context=WorkerInvocationContext("release-1", "rev-old"),
        inputs={"message": {"text": "Hello"}},
        config={},
    )
    decision = WorkerDrainDecision(
        workload="online_request",
        run_id=request.run_id,
        invocation_id=request.invocation_id,
        node_attempt_id=request.node_attempt_id,
        lease_epoch=request.lease_epoch,
        release_id=request.context.release_id,
        deployment_revision_id=request.context.deployment_revision_id,
        disposition="cancel",
        deadline_unix_ms=30_000,
        reason="deadline_reached",
    )

    invalid_policy_checks = (
        (
            lambda: WorkerDrainPolicy(online_request_timeout_ms=True),
            "online_request_timeout_ms must be an integer",
        ),
        (
            lambda: WorkerDrainPolicy.from_wire({"onDeadline": []}),
            "worker drain policy onDeadline must be a mapping",
        ),
        (
            lambda: WorkerDrainPolicy.from_wire({"onlineRequestTimeoutMs": "30"}),
            "online_request_timeout_ms must be an integer",
        ),
    )
    for build, message in invalid_policy_checks:
        with pytest.raises(WorkerProtocolError, match=message):
            build()

    invalid_task_checks = (
        (
            lambda: WorkerDrainTask("online_request", object(), started_at_unix_ms=0),
            "worker drain task request must be a WorkerInvokeRequest",
        ),
        (
            lambda: WorkerDrainTask("online_request", request, started_at_unix_ms=True),
            "worker drain task started_at_unix_ms must be an integer",
        ),
        (
            lambda: WorkerDrainTask(
                "online_request",
                request,
                started_at_unix_ms=0,
                checkpointable="false",
            ),
            "worker drain task checkpointable must be a boolean",
        ),
    )
    for build, message in invalid_task_checks:
        with pytest.raises(WorkerProtocolError, match=message):
            build()

    task_wire = WorkerDrainTask("online_request", request, started_at_unix_ms=0).to_wire()
    task_wire["checkpointable"] = "false"
    with pytest.raises(
        WorkerProtocolError,
        match="worker drain task checkpointable must be a boolean",
    ):
        WorkerDrainTask.from_wire(task_wire)

    invalid_decision_checks = (
        (
            lambda: WorkerDrainDecision(
                workload="online_request",
                run_id=object(),
                invocation_id=request.invocation_id,
                node_attempt_id=request.node_attempt_id,
                lease_epoch=request.lease_epoch,
                release_id=request.context.release_id,
                deployment_revision_id=request.context.deployment_revision_id,
                disposition="cancel",
                deadline_unix_ms=30_000,
                reason="deadline_reached",
            ),
            "worker drain decision run_id must be a string",
        ),
        (
            lambda: WorkerDrainDecision(
                workload="online_request",
                run_id=request.run_id,
                invocation_id=request.invocation_id,
                node_attempt_id=request.node_attempt_id,
                lease_epoch=False,
                release_id=request.context.release_id,
                deployment_revision_id=request.context.deployment_revision_id,
                disposition="cancel",
                deadline_unix_ms=30_000,
                reason="deadline_reached",
            ),
            "worker drain decision lease_epoch must be an integer",
        ),
        (
            lambda: WorkerDrainDecision(
                workload="online_request",
                run_id=request.run_id,
                invocation_id=request.invocation_id,
                node_attempt_id=request.node_attempt_id,
                lease_epoch=request.lease_epoch,
                release_id=request.context.release_id,
                deployment_revision_id=request.context.deployment_revision_id,
                disposition="delay",
                deadline_unix_ms=30_000,
                reason="deadline_reached",
            ),
            "worker drain decision disposition has invalid disposition",
        ),
    )
    for build, message in invalid_decision_checks:
        with pytest.raises(WorkerProtocolError, match=message):
            build()

    decision_wire = decision.to_wire()
    decision_wire["runId"] = 7
    with pytest.raises(
        WorkerProtocolError,
        match="worker drain decision run_id must be a string",
    ):
        WorkerDrainDecision.from_wire(decision_wire)

    invalid_plan_checks = (
        (
            lambda: WorkerDrainPlan(" ", "target-1", 0, (decision,)),
            "worker drain plan worker_id must not be empty",
        ),
        (
            lambda: WorkerDrainPlan("worker-1", "target-1", True, (decision,)),
            "worker drain plan drain_started_at_unix_ms must be an integer",
        ),
        (
            lambda: WorkerDrainPlan("worker-1", "target-1", 0, (object(),)),
            "worker drain plan decisions must be WorkerDrainDecision",
        ),
        (
            lambda: WorkerDrainPlan(
                "worker-1",
                "target-1",
                0,
                (decision,),
                admission_closed="true",
            ),
            "worker drain plan admission_closed must be a boolean",
        ),
    )
    for build, message in invalid_plan_checks:
        with pytest.raises(WorkerProtocolError, match=message):
            build()

    plan_wire = WorkerDrainPlan("worker-1", "target-1", 0, (decision,)).to_wire()
    plan_wire["decisions"] = {}
    with pytest.raises(
        WorkerProtocolError,
        match="worker drain plan decisions must be a list",
    ):
        WorkerDrainPlan.from_wire(plan_wire)

    plan_wire = WorkerDrainPlan("worker-1", "target-1", 0, (decision,)).to_wire()
    plan_wire["decisions"] = [object()]
    with pytest.raises(
        WorkerProtocolError,
        match="worker drain plan decisions must be mappings",
    ):
        WorkerDrainPlan.from_wire(plan_wire)


def test_remote_payload_validator_rejects_oversized_inline_payload() -> None:
    payload = {
        "mode": "inline",
        "schema": "graphblocks.ai/Message@1",
        "value": {"body": "this inline payload is too large"},
    }

    with pytest.raises(RemotePayloadOversizedInlineError) as error:
        validate_remote_payload(payload, RemotePayloadLimits(max_inline_bytes=8))

    assert error.value.max_inline_bytes == 8
    assert error.value.actual_inline_bytes > 8


def test_remote_payload_validator_allows_artifact_reference_payload() -> None:
    payload = {
        "mode": "artifact_ref",
        "schema": "graphblocks.ai/ArtifactRef@1",
        "artifact": {
            "artifact_id": "artifact-000001",
            "uri": "s3://graphblocks/documents/source.pdf",
            "size_bytes": 10_000_000,
        },
    }

    assert validate_remote_payload(payload, RemotePayloadLimits(max_inline_bytes=8)) is None


def test_remote_edge_payload_envelope_records_inline_digest_and_artifact_refs() -> None:
    limits = RemotePayloadLimits(max_inline_bytes=128)
    value = {"message": {"text": "hello"}}

    inline_payload = RemoteEdgePayload.inline("graphblocks.ai/Message@1", value, limits)
    value["message"]["text"] = "mutated"
    inline_wire = inline_payload.to_wire()

    assert inline_wire["mode"] == "inline"
    assert inline_wire["schema"] == "graphblocks.ai/Message@1"
    assert inline_wire["value"] == {"message": {"text": "hello"}}
    assert inline_wire["valueDigest"].startswith("sha256:")
    assert validate_remote_payload(inline_wire, limits) is None
    assert RemoteEdgePayload.from_wire(inline_wire) == inline_payload
    with pytest.raises(RemotePayloadInvalidModeError):
        RemoteEdgePayload.from_wire(inline_wire | {"valueDigest": "sha256:mismatch"})
    with pytest.raises(RemotePayloadInvalidModeError):
        RemoteEdgePayload.from_wire(inline_wire | {"schema": 7})
    with pytest.raises(RemotePayloadInvalidModeError):
        RemoteEdgePayload.from_wire(inline_wire | {"valueDigest": object()})

    artifact_payload = RemoteEdgePayload.artifact_ref(
        "graphblocks.ai/PdfDocument@1",
        artifact_id="artifact-1",
        uri="s3://graphblocks/documents/source.pdf",
        size_bytes=10_000_000,
        digest="sha256:artifact",
    )
    artifact_wire = artifact_payload.to_wire()

    assert artifact_wire == {
        "mode": "artifact_ref",
        "schema": "graphblocks.ai/PdfDocument@1",
        "artifact": {
            "artifact_id": "artifact-1",
            "uri": "s3://graphblocks/documents/source.pdf",
            "size_bytes": 10_000_000,
            "digest": "sha256:artifact",
        },
    }
    assert validate_remote_payload(artifact_wire, RemotePayloadLimits(max_inline_bytes=8)) is None
    assert RemoteEdgePayload.from_wire(artifact_wire) == artifact_payload

    with pytest.raises(RemotePayloadInvalidArtifactRefError) as artifact_key_error:
        RemoteEdgePayload(
            mode="artifact_ref",
            schema="graphblocks.ai/PdfDocument@1",
            artifact={object(): "artifact-1", "uri": "s3://graphblocks/documents/source.pdf"},
        )
    assert artifact_key_error.value.field == "artifact"

    with pytest.raises(RemotePayloadInvalidArtifactRefError) as artifact_id_error:
        RemoteEdgePayload(
            mode="artifact_ref",
            schema="graphblocks.ai/PdfDocument@1",
            artifact={"artifact_id": "", "uri": "s3://graphblocks/documents/source.pdf"},
        )
    assert artifact_id_error.value.field == "artifact_id"


def test_remote_payload_validator_rejects_invalid_artifact_reference() -> None:
    with pytest.raises(RemotePayloadInvalidArtifactRefError) as error:
        validate_remote_payload(
            {
                "mode": "artifact_ref",
                "schema": "graphblocks.ai/ArtifactRef@1",
                "artifact": {"artifact_id": "", "uri": "s3://graphblocks/documents/source.pdf"},
            },
            RemotePayloadLimits(max_inline_bytes=8),
        )

    assert error.value.field == "artifact_id"


def test_remote_payload_limits_validate_non_negative_integer_bound() -> None:
    assert RemotePayloadLimits(max_inline_bytes=0).max_inline_bytes == 0

    with pytest.raises(RemotePayloadInvalidLimitError, match="max_inline_bytes must be an integer"):
        RemotePayloadLimits(max_inline_bytes=True)  # type: ignore[arg-type]
    with pytest.raises(RemotePayloadInvalidLimitError, match="max_inline_bytes must be an integer"):
        RemotePayloadLimits(max_inline_bytes="8")  # type: ignore[arg-type]
    with pytest.raises(RemotePayloadInvalidLimitError, match="max_inline_bytes must be non-negative"):
        RemotePayloadLimits(max_inline_bytes=-1)


def test_remote_payload_validator_rejects_non_mapping_payload_or_limits() -> None:
    with pytest.raises(RemotePayloadInvalidModeError) as payload_error:
        validate_remote_payload(["inline"], RemotePayloadLimits(max_inline_bytes=8))  # type: ignore[arg-type]
    assert payload_error.value.mode == "payload"

    with pytest.raises(RemotePayloadInvalidLimitError, match="limits must be RemotePayloadLimits"):
        validate_remote_payload({"mode": "inline", "value": "ok"}, object())  # type: ignore[arg-type]


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


def test_run_ownership_lease_rejects_invalid_wire_payloads() -> None:
    base = RunOwnershipLease(
        run_id="run-000001",
        owner_instance_id="control-plane-a",
        lease_epoch=42,
        expires_at_unix_ms=1_820_000_000_000,
        last_checkpoint="checkpoint-000004",
    ).to_wire()
    invalid_leases = (
        (
            {**base, "runId": object()},
            "run ownership lease run_id must be a string",
        ),
        (
            {**base, "ownerInstanceId": " "},
            "run ownership lease owner_instance_id must not be empty",
        ),
        (
            {**base, "leaseEpoch": False},
            "run ownership lease lease_epoch must be an integer",
        ),
        (
            {**base, "expiresAtUnixMs": -1},
            "run ownership lease expires_at_unix_ms must not be negative",
        ),
        (
            {**base, "lastCheckpoint": ""},
            "run ownership lease last_checkpoint must not be empty",
        ),
    )

    for payload, message in invalid_leases:
        with pytest.raises(WorkerProtocolError, match=message):
            RunOwnershipLease.from_wire(payload)


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

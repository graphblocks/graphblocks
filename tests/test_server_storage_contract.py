from __future__ import annotations

from dataclasses import replace

import pytest

from graphblocks.canonical import canonical_dumps, canonical_hash, canonical_loads
from graphblocks.runtime import RuntimeCheckpoint
from graphblocks.server_storage import (
    CHECKPOINT_FORMAT_VERSION,
    AcceptedRunClaim,
    AcceptedRunEffectDeliveryAck,
    AcceptedRunEffectDeliveryClaim,
    AcceptedRunEffectDeliveryClaimRequest,
    AcceptedRunEffectDeliveryRecord,
    AcceptedRunEffectDeliveryRetry,
    AcceptedRunEffectDeliveryState,
    AcceptedRunEffectIntent,
    AcceptedRunEffectKind,
    AcceptedRunEventIntent,
    AcceptedRunExecutionEnvelope,
    AcceptedRunPhase,
    AcceptedRunWaitingCommit,
    AcceptedRunCallbackInput,
    AcceptedRunWorkItem,
    AdmissionIdempotencyConflictError,
    AdmissionIdentity,
    AdmissionResult,
    CallbackAcceptance,
    CallbackIssuanceConflictError,
    CallbackIssuanceIdentity,
    CallbackPayloadConflictError,
    CallbackSubmissionIdentity,
    CheckpointIntegrityError,
    InvalidAcceptedRunTransitionError,
    StaleAcceptedRunEffectDeliveryClaimError,
    StaleAcceptedRunClaimError,
    assert_accepted_run_transition,
    assert_current_claim,
    assert_current_effect_delivery_claim,
    decode_runtime_checkpoint,
    encode_runtime_checkpoint,
    resolve_admission_replay,
    resolve_callback_replay,
)


_DIGEST_A = "sha256:" + ("a" * 64)
_DIGEST_B = "sha256:" + ("b" * 64)
_DIGEST_C = "sha256:" + ("c" * 64)


def _admission_identity(*, request_digest: str = _DIGEST_A) -> AdmissionIdentity:
    return AdmissionIdentity(
        tenant_id="tenant-1",
        owner_principal_id="principal-1",
        admission_scope="POST:/runs",
        idempotency_key="admission-1",
        request_digest=request_digest,
    )


def _claim(
    *,
    lease_generation: int = 1,
    fencing_token: int = 1,
) -> AcceptedRunClaim:
    return AcceptedRunClaim(
        tenant_id="tenant-1",
        run_id="run-1",
        lease_owner_id="worker-1",
        lease_generation=lease_generation,
        fencing_token=fencing_token,
        lease_expires_at_unix_ms=2_000,
    )


def _callback_issuance(
    *,
    lease_generation: int = 1,
    fencing_token: int = 1,
) -> CallbackIssuanceIdentity:
    return CallbackIssuanceIdentity(
        run_id="run-1",
        checkpoint_digest=_DIGEST_B,
        operation_id="operation-1",
        operation_attempt_id="attempt-1",
        callback_idempotency_key="callback-1",
        lease_generation=lease_generation,
        fencing_token=fencing_token,
    )


def _runtime_checkpoint() -> RuntimeCheckpoint:
    values: dict[str, object] = {
        "checkpoint_id": "checkpoint-1",
        "run_id": "run-1",
        "graph_hash": _DIGEST_A,
        "wait_node": "wait",
        "remaining_nodes": ("wait",),
        "inputs": {"request": {"value": "hello"}},
        "node_outputs": {},
        "output_values": {},
        "operation": {
            "operation_id": "operation-1",
            "run_id": "run-1",
            "node_id": "wait",
            "attempt_id": "attempt-1",
            "kind": "ci_job",
            "resume_token_hash": _DIGEST_C,
            "idempotency_key": "operation-idempotency-1",
            "expected_schema": "schemas/CICallback@1",
            "state": "waiting_callback",
            "created_at_unix_ms": 1_000,
            "submitted_at_unix_ms": 1_050,
            "expires_at_unix_ms": 61_050,
        },
    }
    state_digest = canonical_hash(
        {
            key: value
            for key, value in values.items()
            if key != "state_digest"
        }
    )
    return RuntimeCheckpoint(**values, state_digest=state_digest)  # type: ignore[arg-type]


def _event_intent(kind: str) -> AcceptedRunEventIntent:
    payload_json = canonical_dumps({"kind": kind, "runId": "run-1"})
    return AcceptedRunEventIntent(
        kind=kind,
        payload_json=payload_json,
        payload_digest=canonical_hash(canonical_loads(payload_json)),
        created_at_unix_ms=1_100,
    )


def _effect_intent(
    kind: AcceptedRunEffectKind,
) -> AcceptedRunEffectIntent:
    payload_json = canonical_dumps({"kind": kind.value, "runId": "run-1"})
    return AcceptedRunEffectIntent(
        effect_id=f"effect-{kind.value}-1",
        kind=kind,
        idempotency_key=f"effect-idempotency-{kind.value}-1",
        payload_json=payload_json,
        payload_digest=canonical_hash(canonical_loads(payload_json)),
    )


def _effect_delivery_claim(
    *,
    claim_generation: int = 1,
    fencing_token: int = 1,
    delivery_owner_id: str = "dispatcher-1",
) -> AcceptedRunEffectDeliveryClaim:
    return AcceptedRunEffectDeliveryClaim(
        effect_id="effect-operation_dispatch-1",
        delivery_owner_id=delivery_owner_id,
        claim_generation=claim_generation,
        fencing_token=fencing_token,
        lease_expires_at_unix_ms=2_000,
    )


def _execution_envelope() -> AcceptedRunExecutionEnvelope:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "restartable"},
        "spec": {"edges": [], "nodes": {}},
    }
    return AcceptedRunExecutionEnvelope(
        run_id="run-1",
        identity=_admission_identity(),
        graph_json=canonical_dumps(graph),
        graph_hash=_DIGEST_A,
        inputs_json=canonical_dumps(
            {"request": {"value": "hello"}}
        ),
        invocation_json=canonical_dumps(
            {
                "policySnapshotId": "policy-1",
                "releaseId": "release-1",
                "responseId": "response-1",
                "turnId": None,
            }
        ),
        ticket_json=canonical_dumps(
            {"runId": "run-1", "state": "accepted"}
        ),
        graph_format_version="graphblocks.ai/Graph@v1",
        runtime_format_version="graphblocks.runtime@v1",
        checkpoint_format_version=CHECKPOINT_FORMAT_VERSION,
        created_at_unix_ms=1_000,
    )


def test_accepted_run_phase_transition_matrix_is_closed() -> None:
    allowed = {
        (AcceptedRunPhase.READY_INITIAL, AcceptedRunPhase.RUNNING),
        (AcceptedRunPhase.RUNNING, AcceptedRunPhase.WAITING_CALLBACK),
        (AcceptedRunPhase.RUNNING, AcceptedRunPhase.TERMINAL),
        (AcceptedRunPhase.WAITING_CALLBACK, AcceptedRunPhase.READY_RESUME),
        (AcceptedRunPhase.READY_RESUME, AcceptedRunPhase.RUNNING),
    }

    for current in AcceptedRunPhase:
        for target in AcceptedRunPhase:
            if (current, target) in allowed:
                assert_accepted_run_transition(current, target)
            else:
                with pytest.raises(InvalidAcceptedRunTransitionError):
                    assert_accepted_run_transition(current, target)


def test_terminal_accepted_run_is_immutable() -> None:
    for target in AcceptedRunPhase:
        with pytest.raises(
            InvalidAcceptedRunTransitionError,
            match="terminal accepted run cannot transition",
        ):
            assert_accepted_run_transition(AcceptedRunPhase.TERMINAL, target)


def test_same_admission_identity_and_digest_replays_stored_result() -> None:
    existing = AdmissionResult(
        run_id="run-1",
        ticket_json=canonical_dumps({"runId": "run-1", "state": "accepted"}),
        replayed=False,
    )

    replay = resolve_admission_replay(
        existing_identity=_admission_identity(),
        existing_result=existing,
        requested_identity=_admission_identity(),
    )

    assert replay == replace(existing, replayed=True)


def test_same_admission_key_with_different_digest_conflicts() -> None:
    existing = AdmissionResult(
        run_id="run-1",
        ticket_json=canonical_dumps({"runId": "run-1", "state": "accepted"}),
    )

    with pytest.raises(
        AdmissionIdempotencyConflictError,
        match="admission idempotency key conflicts with a different request digest",
    ):
        resolve_admission_replay(
            existing_identity=_admission_identity(),
            existing_result=existing,
            requested_identity=_admission_identity(request_digest=_DIGEST_B),
        )


def test_different_admission_namespace_is_not_a_replay() -> None:
    existing = AdmissionResult(
        run_id="run-1",
        ticket_json=canonical_dumps({"runId": "run-1"}),
    )
    other_tenant = replace(_admission_identity(), tenant_id="tenant-2")

    assert (
        resolve_admission_replay(
            existing_identity=_admission_identity(),
            existing_result=existing,
            requested_identity=other_tenant,
        )
        is None
    )


def test_exact_callback_replays_stored_acceptance() -> None:
    issuance = _callback_issuance()
    submission = CallbackSubmissionIdentity(
        issuance=issuance,
        payload_digest=_DIGEST_C,
    )
    existing = CallbackAcceptance(
        submission=submission,
        receipt_json=canonical_dumps({"callbackId": "callback-1", "accepted": True}),
        accepted_event_sequence=3,
        state_version=4,
    )

    assert (
        resolve_callback_replay(
            expected_issuance=issuance,
            existing_acceptance=existing,
            requested_submission=submission,
        )
        is existing
    )


def test_callback_payload_conflict_is_not_acknowledged_as_replay() -> None:
    issuance = _callback_issuance()
    existing = CallbackAcceptance(
        submission=CallbackSubmissionIdentity(
            issuance=issuance,
            payload_digest=_DIGEST_B,
        ),
        receipt_json=canonical_dumps({"accepted": True}),
        accepted_event_sequence=3,
        state_version=4,
    )

    with pytest.raises(
        CallbackPayloadConflictError,
        match="callback idempotency key conflicts with a different payload digest",
    ):
        resolve_callback_replay(
            expected_issuance=issuance,
            existing_acceptance=existing,
            requested_submission=CallbackSubmissionIdentity(
                issuance=issuance,
                payload_digest=_DIGEST_C,
            ),
        )


@pytest.mark.parametrize(
    "requested",
    [
        _callback_issuance(lease_generation=2),
        _callback_issuance(fencing_token=2),
        replace(_callback_issuance(), checkpoint_digest=_DIGEST_C),
        replace(_callback_issuance(), operation_attempt_id="attempt-2"),
    ],
)
def test_callback_must_match_complete_immutable_issuance(
    requested: CallbackIssuanceIdentity,
) -> None:
    with pytest.raises(
        CallbackIssuanceConflictError,
        match="callback does not match the current checkpoint issuance",
    ):
        resolve_callback_replay(
            expected_issuance=_callback_issuance(),
            existing_acceptance=None,
            requested_submission=CallbackSubmissionIdentity(
                issuance=requested,
                payload_digest=_DIGEST_C,
            ),
        )


@pytest.mark.parametrize(
    "provided",
    [
        _claim(lease_generation=2),
        _claim(fencing_token=2),
        replace(_claim(), lease_owner_id="worker-2"),
    ],
)
def test_stale_or_foreign_claim_cannot_commit(
    provided: AcceptedRunClaim,
) -> None:
    with pytest.raises(
        StaleAcceptedRunClaimError,
        match="accepted run claim is stale or no longer authoritative",
    ):
        assert_current_claim(current=_claim(), provided=provided)


@pytest.mark.parametrize(
    "provided",
    [
        _effect_delivery_claim(claim_generation=2),
        _effect_delivery_claim(fencing_token=2),
        _effect_delivery_claim(delivery_owner_id="dispatcher-2"),
        replace(
            _effect_delivery_claim(),
            lease_expires_at_unix_ms=2_001,
        ),
    ],
)
def test_stale_or_foreign_effect_delivery_claim_cannot_ack(
    provided: AcceptedRunEffectDeliveryClaim,
) -> None:
    with pytest.raises(
        StaleAcceptedRunEffectDeliveryClaimError,
        match="effect delivery claim is stale or no longer authoritative",
    ):
        assert_current_effect_delivery_claim(
            current=_effect_delivery_claim(),
            provided=provided,
        )


def test_claimed_effect_delivery_record_binds_authority_and_payload() -> None:
    payload_json = canonical_dumps(
        {"operationId": "operation-1", "runId": "run-1"}
    )
    claim = _effect_delivery_claim()

    record = AcceptedRunEffectDeliveryRecord(
        effect_id=claim.effect_id,
        tenant_id="tenant-1",
        run_id="run-1",
        owner_principal_id="principal-1",
        checkpoint_digest=_DIGEST_B,
        kind=AcceptedRunEffectKind.OPERATION_DISPATCH,
        idempotency_key="dispatch-run-1-operation-1-attempt-1",
        payload_json=payload_json,
        payload_digest=canonical_hash(canonical_loads(payload_json)),
        delivery_state=AcceptedRunEffectDeliveryState.CLAIMED,
        attempt_count=1,
        available_at_unix_ms=1_100,
        claim=claim,
        created_at_unix_ms=1_100,
        delivered_at_unix_ms=None,
    )

    assert record.claim == claim
    assert record.delivery_state is AcceptedRunEffectDeliveryState.CLAIMED
    assert record.kind is AcceptedRunEffectKind.OPERATION_DISPATCH


def test_effect_delivery_record_rejects_claim_outside_claimed_state() -> None:
    payload_json = canonical_dumps({"runId": "run-1"})

    with pytest.raises(
        ValueError,
        match="claim must be present only while delivery_state is claimed",
    ):
        AcceptedRunEffectDeliveryRecord(
            effect_id="effect-completion-1",
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-1",
            checkpoint_digest=None,
            kind=AcceptedRunEffectKind.COMPLETION,
            idempotency_key="completion-run-1",
            payload_json=payload_json,
            payload_digest=canonical_hash(canonical_loads(payload_json)),
            delivery_state=AcceptedRunEffectDeliveryState.PENDING,
            attempt_count=0,
            available_at_unix_ms=1_100,
            claim=_effect_delivery_claim(),
            created_at_unix_ms=1_100,
            delivered_at_unix_ms=None,
        )


def test_effect_delivery_claim_request_requires_positive_bounded_lease() -> None:
    with pytest.raises(
        ValueError,
        match="lease_duration_ms must be a positive unsigned 64-bit integer",
    ):
        AcceptedRunEffectDeliveryClaimRequest(
            delivery_owner_id="dispatcher-1",
            now_unix_ms=1_000,
            lease_duration_ms=0,
        )

    with pytest.raises(
        ValueError,
        match="lease expiry exceeds unsigned 64-bit range",
    ):
        AcceptedRunEffectDeliveryClaimRequest(
            delivery_owner_id="dispatcher-1",
            now_unix_ms=(1 << 64) - 1,
            lease_duration_ms=1,
        )


def test_effect_delivery_commands_bind_ack_and_retry_to_claim() -> None:
    claim = _effect_delivery_claim()

    ack = AcceptedRunEffectDeliveryAck(
        claim=claim,
        delivered_at_unix_ms=1_500,
    )
    retry = AcceptedRunEffectDeliveryRetry(
        claim=claim,
        released_at_unix_ms=1_500,
        available_at_unix_ms=1_750,
    )

    assert ack.claim == claim
    assert retry.claim == claim
    assert retry.available_at_unix_ms == 1_750


def test_effect_delivery_retry_cannot_be_available_before_release() -> None:
    with pytest.raises(
        ValueError,
        match="available_at_unix_ms must not precede released_at_unix_ms",
    ):
        AcceptedRunEffectDeliveryRetry(
            claim=_effect_delivery_claim(),
            released_at_unix_ms=1_500,
            available_at_unix_ms=1_499,
        )


def test_initial_claimed_work_contains_complete_execution_envelope() -> None:
    work = AcceptedRunWorkItem(
        claim=_claim(),
        envelope=_execution_envelope(),
        state_version=2,
        event_high_watermark=2,
        checkpoint=None,
        callback=None,
    )

    assert not work.is_resume
    assert work.envelope.identity.tenant_id == work.claim.tenant_id
    assert work.envelope.run_id == work.claim.run_id
    assert canonical_loads(work.envelope.invocation_json)["releaseId"] == (
        "release-1"
    )


def test_resume_claimed_work_binds_checkpoint_and_callback_payload() -> None:
    checkpoint = encode_runtime_checkpoint(_runtime_checkpoint())
    issuance = replace(
        _callback_issuance(),
        checkpoint_digest=checkpoint.checkpoint_digest,
    )
    callback_payload = {"conclusion": "success", "status": "completed"}
    callback = AcceptedRunCallbackInput(
        acceptance=CallbackAcceptance(
            submission=CallbackSubmissionIdentity(
                issuance=issuance,
                payload_digest=canonical_hash(callback_payload),
            ),
            receipt_json=canonical_dumps(
                {"accepted": True, "callbackId": "callback-1"}
            ),
            accepted_event_sequence=4,
            state_version=4,
        ),
        payload_json=canonical_dumps(callback_payload),
        received_at_unix_ms=1_500,
    )

    work = AcceptedRunWorkItem(
        claim=_claim(lease_generation=2, fencing_token=2),
        envelope=_execution_envelope(),
        state_version=5,
        event_high_watermark=5,
        checkpoint=checkpoint,
        callback=callback,
    )

    assert work.is_resume
    assert work.checkpoint == checkpoint
    assert work.callback == callback


def test_claimed_work_rejects_partial_resume_base() -> None:
    with pytest.raises(
        ValueError,
        match="checkpoint and callback must either both be present or both be absent",
    ):
        AcceptedRunWorkItem(
            claim=_claim(),
            envelope=_execution_envelope(),
            state_version=2,
            event_high_watermark=2,
            checkpoint=encode_runtime_checkpoint(_runtime_checkpoint()),
            callback=None,
        )


def test_claimed_work_rejects_cross_tenant_envelope() -> None:
    with pytest.raises(
        ValueError,
        match="claim identity must match its execution envelope",
    ):
        AcceptedRunWorkItem(
            claim=_claim(),
            envelope=replace(
                _execution_envelope(),
                identity=replace(
                    _admission_identity(),
                    tenant_id="tenant-2",
                ),
            ),
            state_version=2,
            event_high_watermark=2,
            checkpoint=None,
            callback=None,
        )


def test_waiting_commit_binds_checkpoint_and_dispatch_outbox_to_claim() -> None:
    checkpoint = _runtime_checkpoint()
    stored = encode_runtime_checkpoint(checkpoint)
    claim = _claim()
    issuance = replace(
        _callback_issuance(),
        checkpoint_digest=checkpoint.state_digest,
    )

    command = AcceptedRunWaitingCommit(
        claim=claim,
        expected_state_version=2,
        checkpoint=stored,
        callback_issuance=issuance,
        waiting_event=_event_intent("run_waiting_callback"),
        dispatch_effect=_effect_intent(
            AcceptedRunEffectKind.OPERATION_DISPATCH
        ),
    )

    assert command.checkpoint.checkpoint_digest == issuance.checkpoint_digest
    assert (
        command.dispatch_effect.kind
        is AcceptedRunEffectKind.OPERATION_DISPATCH
    )


def test_waiting_commit_rejects_missing_operation_dispatch_semantics() -> None:
    checkpoint = _runtime_checkpoint()

    with pytest.raises(
        ValueError,
        match="waiting commit requires an operation dispatch effect",
    ):
        AcceptedRunWaitingCommit(
            claim=_claim(),
            expected_state_version=2,
            checkpoint=encode_runtime_checkpoint(checkpoint),
            callback_issuance=replace(
                _callback_issuance(),
                checkpoint_digest=checkpoint.state_digest,
            ),
            waiting_event=_event_intent("run_waiting_callback"),
            dispatch_effect=_effect_intent(
                AcceptedRunEffectKind.COMPLETION
            ),
        )


def test_waiting_commit_rejects_stale_issuance_fence() -> None:
    checkpoint = _runtime_checkpoint()

    with pytest.raises(
        ValueError,
        match="waiting commit identities must match its claim and checkpoint",
    ):
        AcceptedRunWaitingCommit(
            claim=_claim(),
            expected_state_version=2,
            checkpoint=encode_runtime_checkpoint(checkpoint),
            callback_issuance=replace(
                _callback_issuance(fencing_token=2),
                checkpoint_digest=checkpoint.state_digest,
            ),
            waiting_event=_event_intent("run_waiting_callback"),
            dispatch_effect=_effect_intent(
                AcceptedRunEffectKind.OPERATION_DISPATCH
            ),
        )


def test_runtime_checkpoint_codec_round_trips_canonical_json() -> None:
    checkpoint = _runtime_checkpoint()

    encoded = encode_runtime_checkpoint(checkpoint)
    decoded = decode_runtime_checkpoint(encoded)

    assert encoded.format_version == CHECKPOINT_FORMAT_VERSION
    assert encoded.checkpoint_digest == checkpoint.state_digest
    assert encoded.checkpoint_json == canonical_dumps(checkpoint.to_json())
    assert decoded == checkpoint


def test_runtime_checkpoint_codec_rejects_payload_tampering() -> None:
    checkpoint = _runtime_checkpoint()
    encoded = encode_runtime_checkpoint(checkpoint)
    tampered_payload = canonical_loads(encoded.checkpoint_json)
    tampered_payload["inputs"]["request"]["value"] = "tampered"

    with pytest.raises(
        CheckpointIntegrityError,
        match="stored runtime checkpoint failed validating reconstruction",
    ):
        decode_runtime_checkpoint(
            replace(
                encoded,
                checkpoint_json=canonical_dumps(tampered_payload),
            )
        )


def test_runtime_checkpoint_codec_rejects_record_digest_tampering() -> None:
    encoded = encode_runtime_checkpoint(_runtime_checkpoint())

    with pytest.raises(
        CheckpointIntegrityError,
        match="stored checkpoint digest does not match reconstructed checkpoint",
    ):
        decode_runtime_checkpoint(
            replace(encoded, checkpoint_digest=_DIGEST_C)
        )

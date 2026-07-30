from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from graphblocks.canonical import canonical_dumps, canonical_hash
from graphblocks.runtime import RuntimeCheckpoint
from graphblocks.server_storage import (
    AcceptedRunAdmission,
    AcceptedRunCancelCommand,
    AcceptedRunClaim,
    AcceptedRunClaimRequest,
    AcceptedRunControlAction,
    AcceptedRunControlConflictError,
    AcceptedRunEffectDeliveryAck,
    AcceptedRunEffectDeliveryClaimRequest,
    AcceptedRunEffectDeliveryState,
    AcceptedRunEffectDeliveryStateConflictError,
    AcceptedRunEffectIntent,
    AcceptedRunEffectKind,
    AcceptedRunEventIntent,
    AcceptedRunNotFoundError,
    AcceptedRunPhase,
    AcceptedRunStateConflictError,
    AcceptedRunTerminalCommit,
    AcceptedRunWaitingCommit,
    AdmissionIdentity,
    CallbackIssuanceIdentity,
    StaleAcceptedRunClaimError,
    encode_runtime_checkpoint,
)
from graphblocks.sqlite_outbox import SQLiteOutboxDispatcherRepository
from graphblocks.sqlite_server_storage import SQLiteAcceptedRunRepository


def _admission() -> AcceptedRunAdmission:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "durable-cancel"},
        "spec": {"nodes": {}, "edges": []},
    }
    inputs = {"request": {"value": "hello"}}
    invocation = {
        "policySnapshotId": "policy-1",
        "releaseId": "release-1",
        "responseId": "response-1",
        "turnId": None,
    }
    event_payload = {
        "runId": "run-1",
        "tenantId": "tenant-1",
        "state": "ready_initial",
    }
    return AcceptedRunAdmission(
        run_id="run-1",
        identity=AdmissionIdentity(
            tenant_id="tenant-1",
            owner_principal_id="principal-1",
            admission_scope="POST:/runs",
            idempotency_key="admission-1",
            request_digest=canonical_hash(
                {
                    "graph": graph,
                    "inputs": inputs,
                    "invocation": invocation,
                    "runId": "run-1",
                }
            ),
        ),
        graph_json=canonical_dumps(graph),
        graph_hash=canonical_hash(graph),
        inputs_json=canonical_dumps(inputs),
        invocation_json=canonical_dumps(invocation),
        ticket_json=canonical_dumps(
            {"runId": "run-1", "state": "accepted"}
        ),
        graph_format_version="graphblocks.ai/Graph@v1",
        runtime_format_version="graphblocks.runtime@v1",
        checkpoint_format_version="graphblocks.runtime-checkpoint.v1",
        created_at_unix_ms=1_000,
        accepted_event=AcceptedRunEventIntent(
            kind="run_accepted",
            payload_json=canonical_dumps(event_payload),
            payload_digest=canonical_hash(event_payload),
            created_at_unix_ms=1_000,
        ),
    )


def _cancel_command(
    *,
    expected_state_version: int,
    requested_at_unix_ms: int = 2_000,
    reason: str = "user_requested",
    idempotency_key: str = "cancel-1",
    owner_principal_id: str = "principal-1",
) -> AcceptedRunCancelCommand:
    request_value = {
        "action": "cancel",
        "expectedStateVersion": expected_state_version,
        "ownerPrincipalId": owner_principal_id,
        "reason": reason,
        "runId": "run-1",
        "tenantId": "tenant-1",
    }
    result = {
        "reason": reason,
        "requestId": idempotency_key,
        "status": "cancelled",
    }
    result_digest = canonical_hash(result)
    event_payload = {
        "reason": reason,
        "requestId": idempotency_key,
        "runId": "run-1",
        "state": "cancelled",
    }
    completion_payload = {
        "result": result,
        "resultDigest": result_digest,
        "runId": "run-1",
        "tenantId": "tenant-1",
    }
    completion_digest = canonical_hash(completion_payload)
    return AcceptedRunCancelCommand(
        tenant_id="tenant-1",
        owner_principal_id=owner_principal_id,
        run_id="run-1",
        expected_state_version=expected_state_version,
        idempotency_key=idempotency_key,
        request_digest=canonical_hash(request_value),
        requested_at_unix_ms=requested_at_unix_ms,
        result_json=canonical_dumps(result),
        result_digest=result_digest,
        cancelled_event=AcceptedRunEventIntent(
            kind="run_cancelled",
            payload_json=canonical_dumps(event_payload),
            payload_digest=canonical_hash(event_payload),
            created_at_unix_ms=requested_at_unix_ms,
        ),
        completion_effect=AcceptedRunEffectIntent(
            effect_id=(
                "effect-completion:"
                f"{completion_digest.removeprefix('sha256:')}"
            ),
            kind=AcceptedRunEffectKind.COMPLETION,
            idempotency_key="completion-run-1",
            payload_json=canonical_dumps(completion_payload),
            payload_digest=completion_digest,
        ),
    )


def _terminal_command(claim: AcceptedRunClaim) -> AcceptedRunTerminalCommit:
    result = {"outputs": {}, "status": "succeeded"}
    result_digest = canonical_hash(result)
    event_payload = {
        "resultDigest": result_digest,
        "runId": claim.run_id,
        "state": "succeeded",
    }
    completion_payload = {
        "result": result,
        "resultDigest": result_digest,
        "runId": claim.run_id,
        "tenantId": claim.tenant_id,
    }
    return AcceptedRunTerminalCommit(
        claim=claim,
        expected_state_version=2,
        terminal_status="succeeded",
        result_json=canonical_dumps(result),
        result_digest=result_digest,
        terminal_event=AcceptedRunEventIntent(
            kind="run_succeeded",
            payload_json=canonical_dumps(event_payload),
            payload_digest=canonical_hash(event_payload),
            created_at_unix_ms=2_600,
        ),
        completion_effect=AcceptedRunEffectIntent(
            effect_id="effect-worker-completion",
            kind=AcceptedRunEffectKind.COMPLETION,
            idempotency_key="completion-run-1",
            payload_json=canonical_dumps(completion_payload),
            payload_digest=canonical_hash(completion_payload),
        ),
    )


def _waiting_command(claim: AcceptedRunClaim) -> AcceptedRunWaitingCommit:
    values: dict[str, object] = {
        "checkpoint_id": "checkpoint-1",
        "run_id": claim.run_id,
        "graph_hash": _admission().graph_hash,
        "wait_node": "wait",
        "remaining_nodes": ("wait",),
        "inputs": {"request": {"value": "hello"}},
        "node_outputs": {},
        "output_values": {},
        "operation": {
            "operation_id": "operation-1",
            "run_id": claim.run_id,
            "node_id": "wait",
            "attempt_id": "attempt-1",
            "kind": "ci_job",
            "resume_token_hash": "sha256:" + ("c" * 64),
            "idempotency_key": "operation-idempotency-1",
            "expected_schema": "schemas/CICallback@1",
            "state": "waiting_callback",
            "created_at_unix_ms": 2_050,
            "submitted_at_unix_ms": 2_100,
            "expires_at_unix_ms": 60_000,
        },
    }
    operation = values["operation"]
    assert isinstance(operation, dict)
    checkpoint = RuntimeCheckpoint(
        checkpoint_id="checkpoint-1",
        run_id=claim.run_id,
        graph_hash=_admission().graph_hash,
        wait_node="wait",
        remaining_nodes=("wait",),
        inputs={"request": {"value": "hello"}},
        node_outputs={},
        output_values={},
        operation=operation,
        state_digest=canonical_hash(values),
    )
    stored = encode_runtime_checkpoint(checkpoint)
    waiting_payload = {
        "checkpointDigest": stored.checkpoint_digest,
        "runId": claim.run_id,
        "state": "waiting_callback",
    }
    dispatch_payload = {
        "operationId": "operation-1",
        "runId": claim.run_id,
    }
    return AcceptedRunWaitingCommit(
        claim=claim,
        expected_state_version=2,
        checkpoint=stored,
        callback_issuance=CallbackIssuanceIdentity(
            run_id=claim.run_id,
            checkpoint_digest=stored.checkpoint_digest,
            operation_id="operation-1",
            operation_attempt_id="attempt-1",
            callback_idempotency_key="callback-1",
            lease_generation=claim.lease_generation,
            fencing_token=claim.fencing_token,
        ),
        waiting_event=AcceptedRunEventIntent(
            kind="run_waiting_callback",
            payload_json=canonical_dumps(waiting_payload),
            payload_digest=canonical_hash(waiting_payload),
            created_at_unix_ms=2_200,
        ),
        dispatch_effect=AcceptedRunEffectIntent(
            effect_id="effect-operation-dispatch-1",
            kind=AcceptedRunEffectKind.OPERATION_DISPATCH,
            idempotency_key="dispatch-operation-1",
            payload_json=canonical_dumps(dispatch_payload),
            payload_digest=canonical_hash(dispatch_payload),
        ),
    )


def test_sqlite_repository_cancels_and_replays_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())
    command = _cancel_command(expected_state_version=1)

    accepted = repository.cancel_run(command)
    replayed = SQLiteAcceptedRunRepository(path).cancel_run(
        _cancel_command(
            expected_state_version=1,
            requested_at_unix_ms=3_000,
        )
    )

    assert accepted.action is AcceptedRunControlAction.CANCEL
    assert not accepted.replayed
    assert accepted.state_version == 2
    assert accepted.accepted_event_sequence == 2
    assert replayed == replace(accepted, replayed=True)
    snapshot = repository.get_run(
        tenant_id="tenant-1",
        run_id="run-1",
    )
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.TERMINAL
    assert snapshot.terminal_status == "cancelled"
    assert snapshot.terminal_result_json == command.result_json
    assert [
        event.kind
        for event in repository.read_events(
            tenant_id="tenant-1",
            run_id="run-1",
            after_sequence=0,
            limit=10,
        ).events
    ] == ["run_accepted", "run_cancelled"]
    connection = sqlite3.connect(path)
    control = connection.execute(
        """
        SELECT action, idempotency_key, accepted_state_version,
               accepted_event_sequence, resulting_phase
        FROM run_controls
        """
    ).fetchone()
    completion_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM effect_outbox
            WHERE effect_kind = 'completion'
            """
        ).fetchone()[0]
    )
    run_fence = connection.execute(
        "SELECT lease_generation, fencing_token FROM accepted_runs"
    ).fetchone()
    connection.close()
    assert control == ("cancel", "cancel-1", 2, 2, "terminal")
    assert completion_count == 1
    assert run_fence == (1, 1)


def test_sqlite_repository_cancel_is_owner_scoped_and_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())

    with pytest.raises(AcceptedRunNotFoundError):
        repository.cancel_run(
            _cancel_command(
                expected_state_version=1,
                owner_principal_id="principal-2",
            )
        )
    with pytest.raises(AcceptedRunStateConflictError):
        repository.cancel_run(
            _cancel_command(expected_state_version=2)
        )

    repository.cancel_run(_cancel_command(expected_state_version=1))

    with pytest.raises(AcceptedRunControlConflictError):
        repository.cancel_run(
            _cancel_command(
                expected_state_version=1,
                reason="different",
            )
        )


@pytest.mark.parametrize(
    "failpoint",
    [
        "cancel_run.after_outbox_insert",
        "cancel_run.after_event_insert",
        "cancel_run.after_control_insert",
        "cancel_run.after_state_update",
    ],
)
def test_sqlite_repository_rolls_back_precommit_cancellation_failure(
    tmp_path: Path,
    failpoint: str,
) -> None:
    path = tmp_path / f"{failpoint}.sqlite3"
    SQLiteAcceptedRunRepository(path).accept_run(_admission())

    def inject(point: str) -> None:
        if point == failpoint:
            raise RuntimeError(f"injected {point}")

    with pytest.raises(RuntimeError, match=f"injected {failpoint}"):
        SQLiteAcceptedRunRepository(
            path,
            failpoint=inject,
        ).cancel_run(_cancel_command(expected_state_version=1))

    repository = SQLiteAcceptedRunRepository(path)
    snapshot = repository.get_run(
        tenant_id="tenant-1",
        run_id="run-1",
    )
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.READY_INITIAL
    assert snapshot.state_version == 1
    assert snapshot.event_high_watermark == 1
    connection = sqlite3.connect(path)
    counts = (
        int(connection.execute("SELECT COUNT(*) FROM run_controls").fetchone()[0]),
        int(connection.execute("SELECT COUNT(*) FROM effect_outbox").fetchone()[0]),
        int(connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]),
    )
    connection.close()
    assert counts == (0, 0, 1)


def test_sqlite_repository_replays_cancel_after_response_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    SQLiteAcceptedRunRepository(path).accept_run(_admission())

    def inject(point: str) -> None:
        if point == "cancel_run.after_commit":
            raise RuntimeError("injected cancellation response loss")

    with pytest.raises(
        RuntimeError,
        match="injected cancellation response loss",
    ):
        SQLiteAcceptedRunRepository(
            path,
            failpoint=inject,
        ).cancel_run(_cancel_command(expected_state_version=1))

    replayed = SQLiteAcceptedRunRepository(path).cancel_run(
        _cancel_command(
            expected_state_version=1,
            requested_at_unix_ms=3_000,
        )
    )
    assert replayed.replayed
    assert replayed.state_version == 2


def test_sqlite_repository_cancel_fences_active_worker_commit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())
    claim = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-1",
            now_unix_ms=2_000,
            lease_duration_ms=2_000,
        )
    )
    assert claim is not None

    repository.cancel_run(
        _cancel_command(
            expected_state_version=2,
            requested_at_unix_ms=2_500,
        )
    )

    with pytest.raises(StaleAcceptedRunClaimError):
        repository.commit_terminal(_terminal_command(claim))
    connection = sqlite3.connect(path)
    run_fence = connection.execute(
        "SELECT lease_generation, fencing_token FROM accepted_runs"
    ).fetchone()
    completion_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM effect_outbox
            WHERE effect_kind = 'completion'
            """
        ).fetchone()[0]
    )
    connection.close()
    assert run_fence == (2, 2)
    assert completion_count == 1


def test_sqlite_repository_cancel_suppresses_claimed_callback_dispatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())
    claim = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-1",
            now_unix_ms=2_000,
            lease_duration_ms=1_000,
        )
    )
    assert claim is not None
    waiting = repository.commit_waiting(_waiting_command(claim))
    dispatcher = SQLiteOutboxDispatcherRepository(path)
    dispatch = dispatcher.claim_next_effect(
        AcceptedRunEffectDeliveryClaimRequest(
            delivery_owner_id="dispatcher-1",
            now_unix_ms=2_300,
            lease_duration_ms=1_000,
        )
    )
    assert dispatch is not None
    assert dispatch.claim is not None
    dispatch_claim = dispatch.claim

    repository.cancel_run(
        _cancel_command(
            expected_state_version=waiting.state_version,
            requested_at_unix_ms=2_500,
        )
    )

    cancelled_dispatch = dispatcher.get_effect(
        effect_id=dispatch.effect_id,
    )
    assert cancelled_dispatch is not None
    assert (
        cancelled_dispatch.delivery_state
        is AcceptedRunEffectDeliveryState.CANCELLED
    )
    assert cancelled_dispatch.claim is None
    assert cancelled_dispatch.cancelled_at_unix_ms == 2_500
    with pytest.raises(AcceptedRunEffectDeliveryStateConflictError):
        dispatcher.mark_effect_delivered(
            AcceptedRunEffectDeliveryAck(
                claim=dispatch_claim,
                delivered_at_unix_ms=2_600,
            )
        )
    next_effect = dispatcher.claim_next_effect(
        AcceptedRunEffectDeliveryClaimRequest(
            delivery_owner_id="dispatcher-2",
            now_unix_ms=4_000,
            lease_duration_ms=1_000,
        )
    )
    assert next_effect is not None
    assert next_effect.kind is AcceptedRunEffectKind.COMPLETION
    assert next_effect.effect_id != dispatch.effect_id


def test_sqlite_repository_rolls_back_dispatch_cancellation_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())
    claim = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-1",
            now_unix_ms=2_000,
            lease_duration_ms=1_000,
        )
    )
    assert claim is not None
    waiting = repository.commit_waiting(_waiting_command(claim))
    dispatcher = SQLiteOutboxDispatcherRepository(path)
    dispatch = dispatcher.claim_next_effect(
        AcceptedRunEffectDeliveryClaimRequest(
            delivery_owner_id="dispatcher-1",
            now_unix_ms=2_300,
            lease_duration_ms=1_000,
        )
    )
    assert dispatch is not None

    def inject(point: str) -> None:
        if point == "cancel_run.after_dispatch_cancellation":
            raise RuntimeError("injected dispatch cancellation failure")

    with pytest.raises(
        RuntimeError,
        match="injected dispatch cancellation failure",
    ):
        SQLiteAcceptedRunRepository(
            path,
            failpoint=inject,
        ).cancel_run(
            _cancel_command(
                expected_state_version=waiting.state_version,
                requested_at_unix_ms=2_500,
            )
        )

    reopened = SQLiteAcceptedRunRepository(path)
    snapshot = reopened.get_run(
        tenant_id="tenant-1",
        run_id="run-1",
    )
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.WAITING_CALLBACK
    visible_dispatch = dispatcher.get_effect(effect_id=dispatch.effect_id)
    assert visible_dispatch == dispatch
    connection = sqlite3.connect(path)
    assert int(
        connection.execute("SELECT COUNT(*) FROM run_controls").fetchone()[0]
    ) == 0
    connection.close()

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from graphblocks.canonical import (
    canonical_dumps,
    canonical_hash,
    canonical_loads,
)
from graphblocks.compiler import compile_graph_reference
from graphblocks.durable_server import DurableAcceptedRunService
from graphblocks.runtime import RuntimeCheckpoint
from graphblocks.server_storage import (
    AcceptedRunCallbackCommit,
    AcceptedRunClaimRequest,
    AcceptedRunControlAction,
    AcceptedRunControlConflictError,
    AcceptedRunEffectIntent,
    AcceptedRunEffectKind,
    AcceptedRunEventIntent,
    AcceptedRunNotFoundError,
    AcceptedRunPhase,
    AcceptedRunStateConflictError,
    AcceptedRunTerminalCommit,
    AcceptedRunWaitingCommit,
    CallbackIssuanceIdentity,
    CallbackSubmissionIdentity,
    InvalidAcceptedRunTransitionError,
    StaleAcceptedRunClaimError,
    encode_runtime_checkpoint,
)
from graphblocks.sqlite_server_storage import SQLiteAcceptedRunRepository


_TENANT_ID = "tenant-1"
_OWNER_ID = "principal-1"
_RUN_ID = "run-pause-1"


def _service(
    path: Path,
    *,
    clock_value: int,
    failpoint: Callable[[str], None] | None = None,
) -> DurableAcceptedRunService:
    def clock() -> int:
        return clock_value

    return DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(
            path,
            failpoint=failpoint,
            clock=clock,
        ),
        lease_owner_id=f"worker-{clock_value}",
        lease_duration_ms=10_000,
        compiler=compile_graph_reference,
        clock=clock,
    )


def _admit(service: DurableAcceptedRunService) -> None:
    admission = service.admit_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        idempotency_key="admission-pause-1",
        graph={
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "durable-pause"},
            "spec": {"nodes": {}},
        },
        inputs={"request": "hello"},
        invocation={
            "policySnapshotId": "policy-1",
            "releaseId": "release-1",
            "responseId": "response-1",
            "turnId": None,
        },
    )
    assert not admission.replayed


def test_sqlite_repository_pause_and_resume_survive_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    _admit(_service(path, clock_value=1_000))

    paused = _service(path, clock_value=2_000).pause_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=1,
        idempotency_key="pause-1",
        reason="operator_requested",
    )
    replayed = _service(path, clock_value=2_500).pause_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=1,
        idempotency_key="pause-1",
        reason="operator_requested",
    )

    assert paused.action is AcceptedRunControlAction.PAUSE
    assert paused.resulting_phase is AcceptedRunPhase.PAUSED
    assert paused.state_version == 2
    assert replayed == replace(paused, replayed=True)
    repository = SQLiteAcceptedRunRepository(path)
    snapshot = repository.get_run(
        tenant_id=_TENANT_ID,
        run_id=_RUN_ID,
    )
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.PAUSED
    assert snapshot.paused_from_phase is AcceptedRunPhase.READY_INITIAL
    assert (
        repository.claim_run(
            AcceptedRunClaimRequest(
                tenant_id=_TENANT_ID,
                run_id=_RUN_ID,
                lease_owner_id="paused-worker",
                now_unix_ms=2_600,
                lease_duration_ms=1_000,
            )
        )
        is None
    )

    resumed = _service(path, clock_value=3_000).resume_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=paused.state_version,
        idempotency_key="resume-1",
        reason="operator_released",
    )

    assert resumed.action is AcceptedRunControlAction.RESUME
    assert resumed.resulting_phase is AcceptedRunPhase.READY_INITIAL
    assert resumed.state_version == 3
    restarted = SQLiteAcceptedRunRepository(path)
    resumed_snapshot = restarted.get_run(
        tenant_id=_TENANT_ID,
        run_id=_RUN_ID,
    )
    assert resumed_snapshot is not None
    assert resumed_snapshot.phase is AcceptedRunPhase.READY_INITIAL
    assert resumed_snapshot.paused_from_phase is None
    assert [
        event.kind
        for event in restarted.read_events(
            tenant_id=_TENANT_ID,
            run_id=_RUN_ID,
            after_sequence=0,
            limit=10,
        ).events
    ] == ["run_accepted", "run_paused", "run_resumed"]
    connection = sqlite3.connect(path)
    controls = connection.execute(
        """
        SELECT action, resulting_phase
        FROM run_controls
        ORDER BY accepted_event_sequence
        """
    ).fetchall()
    connection.close()
    assert controls == [
        ("pause", "paused"),
        ("resume", "ready_initial"),
    ]


def test_sqlite_repository_state_controls_are_owner_scoped_and_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    service = _service(path, clock_value=1_000)
    _admit(service)

    with pytest.raises(AcceptedRunNotFoundError):
        service.pause_run(
            tenant_id=_TENANT_ID,
            owner_principal_id="principal-2",
            run_id=_RUN_ID,
            expected_state_version=1,
            idempotency_key="pause-owner",
            reason="not_owner",
        )
    with pytest.raises(InvalidAcceptedRunTransitionError):
        service.resume_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=1,
            idempotency_key="resume-too-early",
            reason="not_paused",
        )
    with pytest.raises(AcceptedRunStateConflictError):
        service.pause_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=2,
            idempotency_key="pause-stale",
            reason="stale",
        )

    service.pause_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=1,
        idempotency_key="pause-conflict",
        reason="first",
    )
    with pytest.raises(AcceptedRunControlConflictError):
        service.pause_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=1,
            idempotency_key="pause-conflict",
            reason="different",
        )
    with pytest.raises(InvalidAcceptedRunTransitionError):
        service.pause_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=2,
            idempotency_key="pause-again",
            reason="already_paused",
        )


def test_sqlite_repository_pause_fences_active_worker_and_requeues_on_resume(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    _admit(_service(path, clock_value=1_000))
    repository = SQLiteAcceptedRunRepository(path)
    work = repository.claim_work(
        AcceptedRunClaimRequest(
            tenant_id=_TENANT_ID,
            run_id=_RUN_ID,
            lease_owner_id="worker-running",
            now_unix_ms=2_000,
            lease_duration_ms=5_000,
        )
    )
    assert work is not None

    paused = _service(path, clock_value=2_500).pause_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=work.state_version,
        idempotency_key="pause-running",
        reason="maintenance",
    )

    snapshot = repository.get_run(
        tenant_id=_TENANT_ID,
        run_id=_RUN_ID,
    )
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.PAUSED
    assert snapshot.paused_from_phase is AcceptedRunPhase.READY_INITIAL
    result = {"outputs": {}, "status": "succeeded"}
    result_digest = canonical_hash(result)
    event_payload = {
        "resultDigest": result_digest,
        "runId": work.claim.run_id,
        "state": "succeeded",
    }
    completion_payload = {
        "result": result,
        "resultDigest": result_digest,
        "runId": work.claim.run_id,
        "tenantId": work.claim.tenant_id,
    }
    with pytest.raises(StaleAcceptedRunClaimError):
        repository.commit_terminal(
            AcceptedRunTerminalCommit(
                claim=work.claim,
                expected_state_version=work.state_version,
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
                    idempotency_key="completion-run-pause-1",
                    payload_json=canonical_dumps(completion_payload),
                    payload_digest=canonical_hash(completion_payload),
                ),
            )
        )

    _service(path, clock_value=3_000).resume_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=paused.state_version,
        idempotency_key="resume-running",
        reason="maintenance_complete",
    )
    replacement = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id=_TENANT_ID,
            run_id=_RUN_ID,
            lease_owner_id="worker-replacement",
            now_unix_ms=3_100,
            lease_duration_ms=1_000,
        )
    )
    assert replacement is not None
    assert replacement.lease_generation > work.claim.lease_generation
    assert replacement.fencing_token > work.claim.fencing_token


def test_sqlite_repository_accepts_callback_while_paused_without_claiming_resume(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    _admit(_service(path, clock_value=1_000))
    repository = SQLiteAcceptedRunRepository(path, clock=lambda: 2_200)
    work = repository.claim_work(
        AcceptedRunClaimRequest(
            tenant_id=_TENANT_ID,
            run_id=_RUN_ID,
            lease_owner_id="worker-waiting",
            now_unix_ms=2_000,
            lease_duration_ms=1_000,
        )
    )
    assert work is not None
    decoded_inputs = canonical_loads(work.envelope.inputs_json)
    assert isinstance(decoded_inputs, dict)
    checkpoint_values: dict[str, object] = {
        "checkpoint_id": "checkpoint-pause-1",
        "run_id": work.claim.run_id,
        "graph_hash": work.envelope.graph_hash,
        "wait_node": "wait",
        "remaining_nodes": ("wait",),
        "inputs": decoded_inputs,
        "node_outputs": {},
        "output_values": {},
        "operation": {
            "operation_id": "operation-pause-1",
            "run_id": work.claim.run_id,
            "node_id": "wait",
            "attempt_id": "attempt-1",
            "kind": "ci_job",
            "resume_token_hash": "sha256:" + ("c" * 64),
            "idempotency_key": "operation-idempotency-pause-1",
            "expected_schema": "schemas/CICallback@1",
            "state": "waiting_callback",
            "created_at_unix_ms": 2_050,
            "submitted_at_unix_ms": 2_100,
            "expires_at_unix_ms": 60_000,
        },
    }
    operation = checkpoint_values["operation"]
    assert isinstance(operation, dict)
    checkpoint = RuntimeCheckpoint(
        checkpoint_id="checkpoint-pause-1",
        run_id=work.claim.run_id,
        graph_hash=work.envelope.graph_hash,
        wait_node="wait",
        remaining_nodes=("wait",),
        inputs=decoded_inputs,
        node_outputs={},
        output_values={},
        operation=operation,
        state_digest=canonical_hash(checkpoint_values),
    )
    stored = encode_runtime_checkpoint(checkpoint)
    waiting_payload = {
        "checkpointDigest": stored.checkpoint_digest,
        "runId": work.claim.run_id,
        "state": "waiting_callback",
    }
    dispatch_payload = {
        "operationId": "operation-pause-1",
        "runId": work.claim.run_id,
    }
    waiting_command = AcceptedRunWaitingCommit(
        claim=work.claim,
        expected_state_version=2,
        checkpoint=stored,
        callback_issuance=CallbackIssuanceIdentity(
            run_id=work.claim.run_id,
            checkpoint_digest=stored.checkpoint_digest,
            operation_id="operation-pause-1",
            operation_attempt_id="attempt-1",
            callback_idempotency_key="callback-pause-1",
            lease_generation=work.claim.lease_generation,
            fencing_token=work.claim.fencing_token,
        ),
        waiting_event=AcceptedRunEventIntent(
            kind="run_waiting_callback",
            payload_json=canonical_dumps(waiting_payload),
            payload_digest=canonical_hash(waiting_payload),
            created_at_unix_ms=2_200,
        ),
        dispatch_effect=AcceptedRunEffectIntent(
            effect_id="effect-operation-dispatch-pause-1",
            kind=AcceptedRunEffectKind.OPERATION_DISPATCH,
            idempotency_key="dispatch-operation-pause-1",
            payload_json=canonical_dumps(dispatch_payload),
            payload_digest=canonical_hash(dispatch_payload),
        ),
    )
    waiting = repository.commit_waiting(waiting_command)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        UPDATE run_checkpoints
        SET callback_expected_state_version = NULL
        """
    )
    connection.commit()
    connection.close()
    paused = _service(path, clock_value=2_500).pause_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=waiting.state_version,
        idempotency_key="pause-waiting",
        reason="hold_resume",
    )
    assert paused.resulting_phase is AcceptedRunPhase.PAUSED
    connection = sqlite3.connect(path)
    callback_expected_state_version = int(
        connection.execute(
            """
            SELECT callback_expected_state_version
            FROM run_checkpoints
            """
        ).fetchone()[0]
    )
    connection.close()
    assert callback_expected_state_version == waiting.state_version

    callback_payload = {"status": "completed"}
    callback_event_payload = {
        "checkpointDigest": waiting_command.checkpoint.checkpoint_digest,
        "resumeState": "ready_resume",
        "runId": waiting_command.claim.run_id,
    }
    callback = repository.accept_callback_and_queue_resume(
        AcceptedRunCallbackCommit(
            tenant_id=waiting_command.claim.tenant_id,
            owner_principal_id=_OWNER_ID,
            expected_state_version=waiting.state_version,
            submission=CallbackSubmissionIdentity(
                issuance=waiting_command.callback_issuance,
                payload_digest=canonical_hash(callback_payload),
            ),
            payload_json=canonical_dumps(callback_payload),
            receipt_json=canonical_dumps({"received": True}),
            received_at_unix_ms=3_000,
            accepted_event=AcceptedRunEventIntent(
                kind="external_callback_received",
                payload_json=canonical_dumps(callback_event_payload),
                payload_digest=canonical_hash(callback_event_payload),
                created_at_unix_ms=3_000,
            ),
        )
    )

    callback_snapshot = repository.get_run(
        tenant_id=_TENANT_ID,
        run_id=_RUN_ID,
    )
    assert callback_snapshot is not None
    assert callback_snapshot.phase is AcceptedRunPhase.PAUSED
    assert callback_snapshot.paused_from_phase is AcceptedRunPhase.READY_RESUME
    assert callback_snapshot.state_version == callback.state_version
    assert (
        repository.claim_run(
            AcceptedRunClaimRequest(
                tenant_id=_TENANT_ID,
                run_id=_RUN_ID,
                lease_owner_id="worker-before-resume",
                now_unix_ms=3_100,
                lease_duration_ms=1_000,
            )
        )
        is None
    )

    resumed = _service(path, clock_value=3_500).resume_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=callback.state_version,
        idempotency_key="resume-callback",
        reason="release_callback",
    )
    assert resumed.resulting_phase is AcceptedRunPhase.READY_RESUME
    resumed_work = repository.claim_work(
        AcceptedRunClaimRequest(
            tenant_id=_TENANT_ID,
            run_id=_RUN_ID,
            lease_owner_id="worker-after-resume",
            now_unix_ms=3_600,
            lease_duration_ms=1_000,
        )
    )
    assert resumed_work is not None
    assert resumed_work.is_resume
    assert resumed_work.callback is not None


@pytest.mark.parametrize(
    "failpoint",
    [
        "pause_run.after_event_insert",
        "pause_run.after_control_insert",
        "pause_run.after_state_update",
    ],
)
def test_sqlite_repository_rolls_back_pause_failure(
    tmp_path: Path,
    failpoint: str,
) -> None:
    path = tmp_path / f"{failpoint}.sqlite3"
    _admit(_service(path, clock_value=1_000))

    def inject(point: str) -> None:
        if point == failpoint:
            raise RuntimeError(f"injected {point}")

    with pytest.raises(RuntimeError, match=f"injected {failpoint}"):
        _service(
            path,
            clock_value=2_000,
            failpoint=inject,
        ).pause_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=1,
            idempotency_key="pause-rollback",
            reason="rollback",
        )

    repository = SQLiteAcceptedRunRepository(path)
    snapshot = repository.get_run(
        tenant_id=_TENANT_ID,
        run_id=_RUN_ID,
    )
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.READY_INITIAL
    assert snapshot.state_version == 1
    assert snapshot.event_high_watermark == 1
    connection = sqlite3.connect(path)
    assert (
        int(connection.execute("SELECT COUNT(*) FROM run_controls").fetchone()[0]) == 0
    )
    connection.close()


@pytest.mark.parametrize(
    "failpoint",
    [
        "resume_run.after_event_insert",
        "resume_run.after_control_insert",
        "resume_run.after_state_update",
    ],
)
def test_sqlite_repository_rolls_back_resume_failure(
    tmp_path: Path,
    failpoint: str,
) -> None:
    path = tmp_path / f"{failpoint}.sqlite3"
    _admit(_service(path, clock_value=1_000))
    paused = _service(path, clock_value=2_000).pause_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=1,
        idempotency_key="pause-before-resume-rollback",
        reason="prepare_rollback",
    )

    def inject(point: str) -> None:
        if point == failpoint:
            raise RuntimeError(f"injected {point}")

    with pytest.raises(RuntimeError, match=f"injected {failpoint}"):
        _service(
            path,
            clock_value=3_000,
            failpoint=inject,
        ).resume_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=paused.state_version,
            idempotency_key="resume-rollback",
            reason="rollback",
        )

    repository = SQLiteAcceptedRunRepository(path)
    snapshot = repository.get_run(
        tenant_id=_TENANT_ID,
        run_id=_RUN_ID,
    )
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.PAUSED
    assert snapshot.paused_from_phase is AcceptedRunPhase.READY_INITIAL
    assert snapshot.state_version == paused.state_version
    assert snapshot.event_high_watermark == paused.accepted_event_sequence
    connection = sqlite3.connect(path)
    assert (
        int(connection.execute("SELECT COUNT(*) FROM run_controls").fetchone()[0]) == 1
    )
    connection.close()


def test_sqlite_repository_replays_pause_and_resume_after_response_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    _admit(_service(path, clock_value=1_000))

    def lose_pause(point: str) -> None:
        if point == "pause_run.after_commit":
            raise RuntimeError("lost pause response")

    with pytest.raises(RuntimeError, match="lost pause response"):
        _service(
            path,
            clock_value=2_000,
            failpoint=lose_pause,
        ).pause_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=1,
            idempotency_key="pause-loss",
            reason="response_loss",
        )
    paused = _service(path, clock_value=2_500).pause_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=1,
        idempotency_key="pause-loss",
        reason="response_loss",
    )
    assert paused.replayed

    def lose_resume(point: str) -> None:
        if point == "resume_run.after_commit":
            raise RuntimeError("lost resume response")

    with pytest.raises(RuntimeError, match="lost resume response"):
        _service(
            path,
            clock_value=3_000,
            failpoint=lose_resume,
        ).resume_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=paused.state_version,
            idempotency_key="resume-loss",
            reason="response_loss",
        )
    resumed = _service(path, clock_value=3_500).resume_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=paused.state_version,
        idempotency_key="resume-loss",
        reason="response_loss",
    )
    assert resumed.replayed
    assert resumed.resulting_phase is AcceptedRunPhase.READY_INITIAL


def test_sqlite_repository_can_cancel_paused_run_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    _admit(_service(path, clock_value=1_000))
    paused = _service(path, clock_value=2_000).pause_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=1,
        idempotency_key="pause-before-cancel",
        reason="inspect",
    )

    cancelled = _service(path, clock_value=2_500).cancel_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=paused.state_version,
        idempotency_key="cancel-paused",
        reason="stop",
    )

    assert cancelled.resulting_phase is AcceptedRunPhase.TERMINAL
    snapshot = SQLiteAcceptedRunRepository(path).get_run(
        tenant_id=_TENANT_ID,
        run_id=_RUN_ID,
    )
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.TERMINAL
    assert snapshot.paused_from_phase is None
    assert snapshot.terminal_status == "cancelled"

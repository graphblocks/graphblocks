from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3

import pytest

from graphblocks.canonical import canonical_dumps, canonical_hash
from graphblocks.runtime import RuntimeCheckpoint
from graphblocks.server_storage import (
    AcceptedRunAdmission,
    AcceptedRunCallbackCommit,
    AcceptedRunCallbackExpiredError,
    AcceptedRunClaim,
    AcceptedRunClaimRequest,
    AcceptedRunEffectDeliveryAck,
    AcceptedRunEffectDeliveryClaimRequest,
    AcceptedRunEffectDeliveryState,
    AcceptedRunEffectIntent,
    AcceptedRunEffectKind,
    AcceptedRunEventIntent,
    AcceptedRunNotFoundError,
    AcceptedRunPhase,
    AcceptedRunWaitingCommit,
    AdmissionIdentity,
    CallbackIssuanceConflictError,
    CallbackIssuanceIdentity,
    CallbackPayloadConflictError,
    CallbackSubmissionIdentity,
    encode_runtime_checkpoint,
)
from graphblocks.sqlite_outbox import SQLiteOutboxDispatcherRepository
from graphblocks.sqlite_server_storage import SQLiteAcceptedRunRepository


def _admission() -> AcceptedRunAdmission:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "durable-callback"},
        "spec": {"nodes": {}, "edges": []},
    }
    inputs = {"request": {"value": "hello"}}
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
                {"graph": graph, "inputs": inputs, "runId": "run-1"}
            ),
        ),
        graph_json=canonical_dumps(graph),
        graph_hash=canonical_hash(graph),
        inputs_json=canonical_dumps(inputs),
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


def _checkpoint(claim: AcceptedRunClaim) -> RuntimeCheckpoint:
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
    return RuntimeCheckpoint(
        **values,
        state_digest=canonical_hash(values),
    )  # type: ignore[arg-type]


def _waiting_command(claim: AcceptedRunClaim) -> AcceptedRunWaitingCommit:
    checkpoint = _checkpoint(claim)
    waiting_payload = {
        "checkpointDigest": checkpoint.state_digest,
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
        checkpoint=encode_runtime_checkpoint(checkpoint),
        callback_issuance=CallbackIssuanceIdentity(
            run_id=claim.run_id,
            checkpoint_digest=checkpoint.state_digest,
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


def _waiting_run(path):
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
    waiting = _waiting_command(claim)
    repository.commit_waiting(waiting)
    return repository, waiting


def _callback_command(
    waiting: AcceptedRunWaitingCommit,
    *,
    payload: dict[str, object] | None = None,
    received_at_unix_ms: int = 3_000,
) -> AcceptedRunCallbackCommit:
    actual_payload = payload or {
        "conclusion": "success",
        "status": "completed",
    }
    receipt = {
        "accepted": True,
        "callbackId": "callback-1",
        "runId": waiting.claim.run_id,
    }
    event_payload = {
        "callbackIdempotencyKey": "callback-1",
        "runId": waiting.claim.run_id,
        "state": "ready_resume",
    }
    return AcceptedRunCallbackCommit(
        tenant_id=waiting.claim.tenant_id,
        owner_principal_id="principal-1",
        expected_state_version=3,
        submission=CallbackSubmissionIdentity(
            issuance=waiting.callback_issuance,
            payload_digest=canonical_hash(actual_payload),
        ),
        payload_json=canonical_dumps(actual_payload),
        receipt_json=canonical_dumps(receipt),
        received_at_unix_ms=received_at_unix_ms,
        accepted_event=AcceptedRunEventIntent(
            kind="external_callback_received",
            payload_json=canonical_dumps(event_payload),
            payload_digest=canonical_hash(event_payload),
            created_at_unix_ms=received_at_unix_ms,
        ),
    )


def test_sqlite_repository_accepts_callback_and_queues_resume_atomically(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository, waiting = _waiting_run(path)
    command = _callback_command(waiting)

    acceptance = repository.accept_callback_and_queue_resume(command)

    assert acceptance.submission == command.submission
    assert acceptance.receipt_json == command.receipt_json
    assert acceptance.accepted_event_sequence == 4
    assert acceptance.state_version == 4
    snapshot = repository.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.READY_RESUME
    assert snapshot.state_version == 4
    assert snapshot.event_high_watermark == 4
    events = repository.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        after_sequence=3,
        limit=10,
    )
    assert [(event.sequence, event.kind) for event in events.events] == [
        (4, "external_callback_received")
    ]
    connection = sqlite3.connect(path)
    inbox_count = int(
        connection.execute("SELECT COUNT(*) FROM callback_inbox").fetchone()[0]
    )
    delivery_state = str(
        connection.execute(
            "SELECT delivery_state FROM effect_outbox"
        ).fetchone()[0]
    )
    connection.close()
    assert inbox_count == 1
    assert delivery_state == "satisfied_by_callback"


def test_sqlite_repository_replays_exact_callback_after_restart(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository, waiting = _waiting_run(path)
    command = _callback_command(waiting)
    first = repository.accept_callback_and_queue_resume(command)
    retry = replace(
        command,
        receipt_json=canonical_dumps(
            {"accepted": True, "candidate": "must-not-replace-stored"}
        ),
        received_at_unix_ms=3_100,
        accepted_event=replace(
            command.accepted_event,
            created_at_unix_ms=3_100,
        ),
    )

    replay = SQLiteAcceptedRunRepository(
        path
    ).accept_callback_and_queue_resume(retry)

    assert replay == first
    assert replay.receipt_json == command.receipt_json
    snapshot = repository.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.state_version == 4
    assert snapshot.event_high_watermark == 4


def test_sqlite_repository_rejects_callback_payload_conflict(tmp_path) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository, waiting = _waiting_run(path)
    repository.accept_callback_and_queue_resume(_callback_command(waiting))

    with pytest.raises(CallbackPayloadConflictError):
        repository.accept_callback_and_queue_resume(
            _callback_command(
                waiting,
                payload={"status": "completed", "conclusion": "failure"},
            )
        )


@pytest.mark.parametrize(
    "issuance",
    [
        lambda value: replace(value, checkpoint_digest="sha256:" + ("d" * 64)),
        lambda value: replace(value, operation_id="operation-2"),
        lambda value: replace(value, operation_attempt_id="attempt-2"),
        lambda value: replace(value, callback_idempotency_key="callback-2"),
        lambda value: replace(value, lease_generation=value.lease_generation + 1),
        lambda value: replace(value, fencing_token=value.fencing_token + 1),
    ],
)
def test_sqlite_repository_rejects_callback_with_wrong_issuance(
    tmp_path,
    issuance,
) -> None:
    repository, waiting = _waiting_run(tmp_path / "accepted-runs.sqlite3")
    command = _callback_command(waiting)

    with pytest.raises(CallbackIssuanceConflictError):
        repository.accept_callback_and_queue_resume(
            replace(
                command,
                submission=replace(
                    command.submission,
                    issuance=issuance(command.submission.issuance),
                ),
            )
        )


def test_sqlite_repository_serializes_concurrent_identical_callbacks(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    _, waiting = _waiting_run(path)
    command = _callback_command(waiting)

    with ThreadPoolExecutor(max_workers=2) as executor:
        acceptances = tuple(
            executor.map(
                lambda _: SQLiteAcceptedRunRepository(
                    path
                ).accept_callback_and_queue_resume(command),
                range(2),
            )
        )

    assert acceptances == (acceptances[0], acceptances[0])
    connection = sqlite3.connect(path)
    inbox_count = int(
        connection.execute("SELECT COUNT(*) FROM callback_inbox").fetchone()[0]
    )
    callback_event_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM run_events
            WHERE kind = 'external_callback_received'
            """
        ).fetchone()[0]
    )
    connection.close()
    assert inbox_count == 1
    assert callback_event_count == 1


def test_sqlite_repository_serializes_concurrent_conflicting_callbacks(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    _, waiting = _waiting_run(path)
    commands = (
        _callback_command(
            waiting,
            payload={"status": "completed", "conclusion": "success"},
        ),
        _callback_command(
            waiting,
            payload={"status": "completed", "conclusion": "failure"},
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(
            executor.submit(
                SQLiteAcceptedRunRepository(
                    path
                ).accept_callback_and_queue_resume,
                command,
            )
            for command in commands
        )
        outcomes: list[str] = []
        for future in futures:
            try:
                future.result()
            except CallbackPayloadConflictError:
                outcomes.append("conflict")
            else:
                outcomes.append("accepted")

    assert sorted(outcomes) == ["accepted", "conflict"]
    connection = sqlite3.connect(path)
    inbox_count = int(
        connection.execute("SELECT COUNT(*) FROM callback_inbox").fetchone()[0]
    )
    callback_event_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM run_events
            WHERE kind = 'external_callback_received'
            """
        ).fetchone()[0]
    )
    connection.close()
    assert inbox_count == 1
    assert callback_event_count == 1


def test_sqlite_repository_callback_satisfies_claimed_dispatch(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository, waiting = _waiting_run(path)
    dispatcher = SQLiteOutboxDispatcherRepository(path)
    claimed = dispatcher.claim_next_effect(
        AcceptedRunEffectDeliveryClaimRequest(
            delivery_owner_id="dispatcher-1",
            now_unix_ms=2_300,
            lease_duration_ms=1_700,
        )
    )
    assert claimed is not None
    assert claimed.claim is not None

    repository.accept_callback_and_queue_resume(_callback_command(waiting))

    satisfied = dispatcher.mark_effect_delivered(
        AcceptedRunEffectDeliveryAck(
            claim=claimed.claim,
            delivered_at_unix_ms=3_100,
        )
    )
    assert (
        satisfied.delivery_state
        is AcceptedRunEffectDeliveryState.SATISFIED_BY_CALLBACK
    )
    assert satisfied.claim is None
    assert satisfied.delivered_at_unix_ms == 3_000


@pytest.mark.parametrize(
    "failpoint",
    [
        "accept_callback.after_event_insert",
        "accept_callback.after_inbox_insert",
        "accept_callback.after_dispatch_satisfied",
        "accept_callback.after_state_update",
    ],
)
def test_sqlite_repository_rolls_back_precommit_callback_failure(
    tmp_path,
    failpoint: str,
) -> None:
    path = tmp_path / f"{failpoint}.sqlite3"
    _, waiting = _waiting_run(path)
    command = _callback_command(waiting)

    def inject(point: str) -> None:
        if point == failpoint:
            raise RuntimeError(f"injected {point}")

    with pytest.raises(RuntimeError, match=f"injected {failpoint}"):
        SQLiteAcceptedRunRepository(
            path,
            failpoint=inject,
        ).accept_callback_and_queue_resume(command)

    reopened = SQLiteAcceptedRunRepository(path)
    snapshot = reopened.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.WAITING_CALLBACK
    assert snapshot.state_version == 3
    assert snapshot.event_high_watermark == 3
    connection = sqlite3.connect(path)
    inbox_count = int(
        connection.execute("SELECT COUNT(*) FROM callback_inbox").fetchone()[0]
    )
    delivery_state = str(
        connection.execute(
            "SELECT delivery_state FROM effect_outbox"
        ).fetchone()[0]
    )
    connection.close()
    assert inbox_count == 0
    assert delivery_state == "pending"


def test_sqlite_repository_recovers_callback_after_response_loss(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    _, waiting = _waiting_run(path)
    command = _callback_command(waiting)

    def inject(point: str) -> None:
        if point == "accept_callback.after_commit":
            raise RuntimeError("injected callback response loss")

    with pytest.raises(RuntimeError, match="injected callback response loss"):
        SQLiteAcceptedRunRepository(
            path,
            failpoint=inject,
        ).accept_callback_and_queue_resume(command)

    replay = SQLiteAcceptedRunRepository(
        path
    ).accept_callback_and_queue_resume(command)
    assert replay.receipt_json == command.receipt_json
    assert replay.state_version == 4


def test_sqlite_repository_resumes_callback_after_process_restart(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository, waiting = _waiting_run(path)
    repository.accept_callback_and_queue_resume(_callback_command(waiting))

    restarted = SQLiteAcceptedRunRepository(path)
    claim = restarted.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-2",
            now_unix_ms=3_100,
            lease_duration_ms=500,
        )
    )

    assert claim is not None
    assert claim.lease_generation == 2
    assert claim.fencing_token == 2
    snapshot = restarted.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.RUNNING
    assert snapshot.state_version == 5
    assert snapshot.event_high_watermark == 5


def test_sqlite_repository_rejects_expired_callback(tmp_path) -> None:
    repository, waiting = _waiting_run(
        tmp_path / "accepted-runs.sqlite3"
    )

    with pytest.raises(
        AcceptedRunCallbackExpiredError,
        match="callback arrived after operation expiration",
    ):
        repository.accept_callback_and_queue_resume(
            _callback_command(
                waiting,
                received_at_unix_ms=60_000,
            )
        )


@pytest.mark.parametrize(
    ("tenant_id", "owner_principal_id"),
    [
        ("tenant-2", "principal-1"),
        ("tenant-1", "principal-2"),
    ],
)
def test_sqlite_repository_hides_callback_target_from_wrong_owner(
    tmp_path,
    tenant_id: str,
    owner_principal_id: str,
) -> None:
    repository, waiting = _waiting_run(
        tmp_path / "accepted-runs.sqlite3"
    )
    command = replace(
        _callback_command(waiting),
        tenant_id=tenant_id,
        owner_principal_id=owner_principal_id,
    )

    with pytest.raises(AcceptedRunNotFoundError):
        repository.accept_callback_and_queue_resume(command)

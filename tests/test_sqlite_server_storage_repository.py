from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from threading import Event
import uuid

import pytest

from graphblocks.canonical import canonical_dumps, canonical_hash
from graphblocks.runtime import RuntimeCheckpoint
from graphblocks.server_storage import (
    AcceptedRunAdmission,
    AcceptedRunClaim,
    AcceptedRunClaimRequest,
    AcceptedRunEffectIntent,
    AcceptedRunEffectKind,
    AcceptedRunEventIntent,
    AcceptedRunIdConflictError,
    AcceptedRunLeaseExpiredError,
    AcceptedRunNotFoundError,
    AcceptedRunPhase,
    AcceptedRunQueueClaimRequest,
    AcceptedRunStateConflictError,
    AcceptedRunWaitingCommit,
    AdmissionIdempotencyConflictError,
    AdmissionIdentity,
    CallbackIssuanceIdentity,
    CheckpointIntegrityError,
    StaleAcceptedRunClaimError,
    assert_current_claim,
    decode_runtime_checkpoint,
    encode_runtime_checkpoint,
)
from graphblocks.sqlite_server_storage import (
    MAX_ACCEPTED_RUN_EVENT_PAGE_SIZE,
    SQLiteAcceptedRunCorruptionError,
    SQLiteAcceptedRunRepository,
)


_DIGEST_B = "sha256:" + ("b" * 64)


def _admission(
    *,
    tenant_id: str = "tenant-1",
    owner_principal_id: str = "principal-1",
    run_id: str = "run-1",
    idempotency_key: str = "admission-1",
    ticket_state: str = "accepted",
) -> AcceptedRunAdmission:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "durable-admission"},
        "spec": {"nodes": {}, "edges": []},
    }
    inputs = {"request": {"value": "hello"}}
    invocation = {
        "policySnapshotId": "policy-1",
        "releaseId": "release-1",
        "responseId": "response-1",
        "turnId": None,
    }
    request_digest = canonical_hash(
        {
            "tenantId": tenant_id,
            "ownerPrincipalId": owner_principal_id,
            "runId": run_id,
            "graph": graph,
            "inputs": inputs,
            "invocation": invocation,
        }
    )
    event_payload = {
        "runId": run_id,
        "tenantId": tenant_id,
        "state": "ready_initial",
    }
    event_json = canonical_dumps(event_payload)
    return AcceptedRunAdmission(
        run_id=run_id,
        identity=AdmissionIdentity(
            tenant_id=tenant_id,
            owner_principal_id=owner_principal_id,
            admission_scope="POST:/runs",
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        ),
        graph_json=canonical_dumps(graph),
        graph_hash=canonical_hash(graph),
        inputs_json=canonical_dumps(inputs),
        invocation_json=canonical_dumps(invocation),
        ticket_json=canonical_dumps(
            {"runId": run_id, "state": ticket_state}
        ),
        graph_format_version="graphblocks.ai/Graph@v1",
        runtime_format_version="graphblocks.runtime@v1",
        checkpoint_format_version="graphblocks.runtime-checkpoint.v1",
        created_at_unix_ms=1_000,
        accepted_event=AcceptedRunEventIntent(
            kind="run_accepted",
            payload_json=event_json,
            payload_digest=canonical_hash(event_payload),
            created_at_unix_ms=1_000,
        ),
    )


def _runtime_checkpoint(
    claim: AcceptedRunClaim,
    *,
    graph_hash: str | None = None,
) -> RuntimeCheckpoint:
    values: dict[str, object] = {
        "checkpoint_id": "checkpoint-1",
        "run_id": claim.run_id,
        "graph_hash": graph_hash or _admission().graph_hash,
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
    state_digest = canonical_hash(values)
    return RuntimeCheckpoint(**values, state_digest=state_digest)  # type: ignore[arg-type]


def _waiting_commit(
    claim: AcceptedRunClaim,
    *,
    event_time: int = 2_200,
    graph_hash: str | None = None,
) -> AcceptedRunWaitingCommit:
    checkpoint = _runtime_checkpoint(claim, graph_hash=graph_hash)
    waiting_payload = {
        "checkpointDigest": checkpoint.state_digest,
        "runId": claim.run_id,
        "state": "waiting_callback",
    }
    waiting_json = canonical_dumps(waiting_payload)
    dispatch_payload = {
        "operationId": "operation-1",
        "runId": claim.run_id,
    }
    dispatch_json = canonical_dumps(dispatch_payload)
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
            payload_json=waiting_json,
            payload_digest=canonical_hash(waiting_payload),
            created_at_unix_ms=event_time,
        ),
        dispatch_effect=AcceptedRunEffectIntent(
            effect_id="effect-operation-dispatch-1",
            kind=AcceptedRunEffectKind.OPERATION_DISPATCH,
            idempotency_key="dispatch-operation-1",
            payload_json=dispatch_json,
            payload_digest=canonical_hash(dispatch_payload),
        ),
    )


def _claim_ready_run(
    repository: SQLiteAcceptedRunRepository,
    *,
    lease_owner_id: str = "worker-1",
    now_unix_ms: int = 2_000,
    lease_duration_ms: int = 500,
) -> AcceptedRunClaim:
    claim = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id=lease_owner_id,
            now_unix_ms=now_unix_ms,
            lease_duration_ms=lease_duration_ms,
        )
    )
    assert claim is not None
    return claim


def test_sqlite_repository_accepts_run_and_initial_event_atomically(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    admission = _admission()

    result = repository.accept_run(admission)
    snapshot = repository.get_run(tenant_id="tenant-1", run_id="run-1")
    events = repository.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        after_sequence=0,
        limit=10,
    )

    assert result.run_id == "run-1"
    assert not result.replayed
    assert result.ticket_json == admission.ticket_json
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.READY_INITIAL
    assert snapshot.state_version == 1
    assert snapshot.event_low_watermark == 1
    assert snapshot.event_high_watermark == 1
    assert events.low_watermark == 1
    assert events.high_watermark == 1
    assert events.next_after_sequence is None
    assert [(event.sequence, event.kind) for event in events.events] == [
        (1, "run_accepted")
    ]
    assert events.events[0].payload_json == admission.accepted_event.payload_json
    assert (
        events.events[0].payload_digest
        == admission.accepted_event.payload_digest
    )

    connection = sqlite3.connect(path)
    stored = connection.execute(
        "SELECT internal_id, invocation_json FROM accepted_runs"
    ).fetchone()
    assert stored is not None
    internal_id = str(stored[0])
    invocation_json = str(stored[1])
    connection.close()
    assert uuid.UUID(internal_id).version == 7
    assert invocation_json == admission.invocation_json


def test_sqlite_repository_replays_same_admission_ticket_after_restart(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    admission = _admission()
    first = SQLiteAcceptedRunRepository(path).accept_run(admission)
    retry = replace(
        admission,
        ticket_json=canonical_dumps(
            {"runId": "run-1", "state": "candidate-retry-ticket"}
        ),
    )

    replay = SQLiteAcceptedRunRepository(path).accept_run(retry)

    assert not first.replayed
    assert replay.replayed
    assert replay.ticket_json == first.ticket_json
    assert (
        SQLiteAcceptedRunRepository(path)
        .read_events(
            tenant_id="tenant-1",
            run_id="run-1",
            after_sequence=0,
            limit=10,
        )
        .high_watermark
        == 1
    )


def test_sqlite_repository_rejects_same_admission_key_with_new_digest(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    admission = _admission()
    repository.accept_run(admission)

    with pytest.raises(AdmissionIdempotencyConflictError):
        repository.accept_run(
            replace(
                admission,
                identity=replace(
                    admission.identity,
                    request_digest=_DIGEST_B,
                ),
            )
        )


def test_sqlite_repository_rejects_same_key_digest_with_new_run_id(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    admission = _admission()
    repository.accept_run(admission)

    with pytest.raises(
        AcceptedRunIdConflictError,
        match="admission replay run_id does not match stored run_id",
    ):
        repository.accept_run(replace(admission, run_id="run-2"))


def test_sqlite_repository_serializes_concurrent_same_key_admission(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    admission = _admission()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(lambda _: repository.accept_run(admission), range(2))
        )

    assert sorted(result.replayed for result in results) == [False, True]
    events = repository.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        after_sequence=0,
        limit=10,
    )
    assert len(events.events) == 1


def test_sqlite_repository_scopes_external_run_ids_by_tenant(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )

    repository.accept_run(_admission(tenant_id="tenant-1"))
    repository.accept_run(
        _admission(
            tenant_id="tenant-2",
            owner_principal_id="principal-2",
        )
    )

    tenant_one = repository.get_run(
        tenant_id="tenant-1",
        run_id="run-1",
    )
    tenant_two = repository.get_run(
        tenant_id="tenant-2",
        run_id="run-1",
    )
    assert tenant_one is not None
    assert tenant_one.owner_principal_id == "principal-1"
    assert tenant_two is not None
    assert tenant_two.owner_principal_id == "principal-2"


def test_sqlite_repository_rejects_duplicate_run_id_within_tenant(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())

    with pytest.raises(
        AcceptedRunIdConflictError,
        match="accepted run_id already exists in tenant",
    ):
        repository.accept_run(
            _admission(
                owner_principal_id="principal-2",
                idempotency_key="admission-2",
            )
        )


@pytest.mark.parametrize(
    "failpoint",
    [
        "accept_run.after_run_insert",
        "accept_run.after_event_insert",
    ],
)
def test_sqlite_repository_rolls_back_precommit_admission_failure(
    tmp_path,
    failpoint: str,
) -> None:
    path = tmp_path / f"{failpoint}.sqlite3"

    def inject(point: str) -> None:
        if point == failpoint:
            raise RuntimeError(f"injected {point}")

    repository = SQLiteAcceptedRunRepository(path, failpoint=inject)
    with pytest.raises(RuntimeError, match=f"injected {failpoint}"):
        repository.accept_run(_admission())

    reopened = SQLiteAcceptedRunRepository(path)
    assert reopened.get_run(tenant_id="tenant-1", run_id="run-1") is None
    with pytest.raises(AcceptedRunNotFoundError):
        reopened.read_events(
            tenant_id="tenant-1",
            run_id="run-1",
            after_sequence=0,
            limit=10,
        )


def test_sqlite_repository_recovers_committed_admission_after_response_loss(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"

    def inject(point: str) -> None:
        if point == "accept_run.after_commit":
            raise RuntimeError("injected response loss")

    with pytest.raises(RuntimeError, match="injected response loss"):
        SQLiteAcceptedRunRepository(path, failpoint=inject).accept_run(
            _admission()
        )

    replay = SQLiteAcceptedRunRepository(path).accept_run(_admission())
    assert replay.replayed
    assert replay.ticket_json == _admission().ticket_json


def test_sqlite_repository_hides_cross_tenant_run_existence(tmp_path) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())

    assert (
        repository.get_run(tenant_id="tenant-2", run_id="run-1") is None
    )
    with pytest.raises(AcceptedRunNotFoundError):
        repository.read_events(
            tenant_id="tenant-2",
            run_id="run-1",
            after_sequence=0,
            limit=10,
        )


def test_sqlite_repository_claims_ready_run_with_fenced_authority(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())

    claim = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-1",
            now_unix_ms=2_000,
            lease_duration_ms=500,
        )
    )

    assert claim is not None
    assert claim.tenant_id == "tenant-1"
    assert claim.lease_generation == 1
    assert claim.fencing_token == 1
    assert claim.lease_expires_at_unix_ms == 2_500
    snapshot = repository.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.RUNNING
    assert snapshot.claim == claim
    assert snapshot.state_version == 2
    assert snapshot.event_high_watermark == 2
    events = repository.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        after_sequence=1,
        limit=10,
    )
    assert [(event.sequence, event.kind) for event in events.events] == [
        (2, "run_claimed")
    ]


def test_sqlite_repository_claim_work_returns_complete_initial_envelope(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    admission = _admission()
    repository.accept_run(admission)

    work = repository.claim_work(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-1",
            now_unix_ms=2_000,
            lease_duration_ms=500,
        )
    )

    assert work is not None
    assert not work.is_resume
    assert work.claim.lease_generation == 1
    assert work.state_version == 2
    assert work.event_high_watermark == 2
    assert work.envelope.run_id == admission.run_id
    assert work.envelope.identity == admission.identity
    assert work.envelope.graph_json == admission.graph_json
    assert work.envelope.graph_hash == admission.graph_hash
    assert work.envelope.inputs_json == admission.inputs_json
    assert work.envelope.invocation_json == admission.invocation_json
    assert work.envelope.ticket_json == admission.ticket_json
    assert work.envelope.created_at_unix_ms == admission.created_at_unix_ms
    assert work.checkpoint is None
    assert work.callback is None


def test_sqlite_repository_claim_work_fails_closed_on_tampered_envelope(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())
    connection = sqlite3.connect(path)
    connection.execute(
        """
        UPDATE accepted_runs
        SET invocation_json = '{ "responseId":"r-1","releaseId":"release-1" }'
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(
        SQLiteAcceptedRunCorruptionError,
        match="claimed work is invalid",
    ):
        repository.claim_work(
            AcceptedRunClaimRequest(
                tenant_id="tenant-1",
                run_id="run-1",
                lease_owner_id="worker-1",
                now_unix_ms=2_000,
                lease_duration_ms=500,
            )
        )

    snapshot = repository.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.READY_INITIAL
    assert snapshot.state_version == 1
    assert snapshot.event_high_watermark == 1


def test_sqlite_repository_discovers_oldest_claimable_work_after_restart(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    newer = _admission(
        run_id="run-newer",
        idempotency_key="admission-newer",
    )
    older = _admission(
        run_id="run-older",
        idempotency_key="admission-older",
    )
    older = replace(
        older,
        created_at_unix_ms=900,
        accepted_event=replace(
            older.accepted_event,
            created_at_unix_ms=900,
        ),
    )
    repository.accept_run(newer)
    repository.accept_run(older)

    restarted = SQLiteAcceptedRunRepository(path)
    first = restarted.claim_next_work(
        AcceptedRunQueueClaimRequest(
            lease_owner_id="worker-after-restart",
            now_unix_ms=2_000,
            lease_duration_ms=500,
        )
    )
    second = restarted.claim_next_work(
        AcceptedRunQueueClaimRequest(
            lease_owner_id="worker-after-restart",
            now_unix_ms=2_000,
            lease_duration_ms=500,
        )
    )

    assert first is not None
    assert first.claim.run_id == "run-older"
    assert first.envelope.run_id == "run-older"
    assert second is not None
    assert second.claim.run_id == "run-newer"
    assert second.envelope.run_id == "run-newer"


def test_sqlite_repository_claim_next_work_honors_tenant_scope(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission(run_id="tenant-1-run"))
    repository.accept_run(
        _admission(
            tenant_id="tenant-2",
            owner_principal_id="principal-2",
            run_id="tenant-2-run",
            idempotency_key="tenant-2-admission",
        )
    )

    tenant_work = repository.claim_next_work(
        AcceptedRunQueueClaimRequest(
            tenant_id="tenant-2",
            lease_owner_id="tenant-2-worker",
            now_unix_ms=2_000,
            lease_duration_ms=500,
        )
    )
    remaining_work = repository.claim_next_work(
        AcceptedRunQueueClaimRequest(
            lease_owner_id="global-worker",
            now_unix_ms=2_000,
            lease_duration_ms=500,
        )
    )

    assert tenant_work is not None
    assert tenant_work.claim.tenant_id == "tenant-2"
    assert tenant_work.claim.run_id == "tenant-2-run"
    assert remaining_work is not None
    assert remaining_work.claim.tenant_id == "tenant-1"
    assert remaining_work.claim.run_id == "tenant-1-run"


def test_sqlite_repository_claim_next_work_is_atomic_across_workers(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())
    request = AcceptedRunQueueClaimRequest(
        lease_owner_id="placeholder",
        now_unix_ms=2_000,
        lease_duration_ms=500,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        work_items = tuple(
            executor.map(
                lambda worker: SQLiteAcceptedRunRepository(
                    path
                ).claim_next_work(
                    replace(request, lease_owner_id=worker)
                ),
                ("worker-1", "worker-2"),
            )
        )

    granted = tuple(work for work in work_items if work is not None)
    assert len(granted) == 1
    assert granted[0].claim.run_id == "run-1"
    assert granted[0].claim.lease_generation == 1
    assert granted[0].claim.fencing_token == 1


def test_sqlite_repository_claim_next_work_reclaims_only_expired_lease(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())
    first = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-1",
            now_unix_ms=2_000,
            lease_duration_ms=500,
        )
    )
    assert first is not None

    unavailable = repository.claim_next_work(
        AcceptedRunQueueClaimRequest(
            lease_owner_id="worker-2",
            now_unix_ms=2_499,
            lease_duration_ms=500,
        )
    )
    reclaimed = SQLiteAcceptedRunRepository(path).claim_next_work(
        AcceptedRunQueueClaimRequest(
            lease_owner_id="worker-2",
            now_unix_ms=2_500,
            lease_duration_ms=750,
        )
    )

    assert unavailable is None
    assert reclaimed is not None
    assert reclaimed.claim.run_id == "run-1"
    assert reclaimed.claim.lease_generation == 2
    assert reclaimed.claim.fencing_token == 2
    assert reclaimed.claim.lease_expires_at_unix_ms == 3_250


def test_sqlite_repository_pages_committed_events_across_claim(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())
    repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-1",
            now_unix_ms=2_000,
            lease_duration_ms=500,
        )
    )

    first = repository.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        after_sequence=0,
        limit=1,
    )
    second = repository.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        after_sequence=first.next_after_sequence,
        limit=1,
    )

    assert [(event.sequence, event.kind) for event in first.events] == [
        (1, "run_accepted")
    ]
    assert first.next_after_sequence == 1
    assert [(event.sequence, event.kind) for event in second.events] == [
        (2, "run_claimed")
    ]
    assert second.next_after_sequence is None


def test_sqlite_repository_allows_only_one_concurrent_claim(tmp_path) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())
    request = AcceptedRunClaimRequest(
        tenant_id="tenant-1",
        run_id="run-1",
        lease_owner_id="worker-1",
        now_unix_ms=2_000,
        lease_duration_ms=500,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(
            executor.map(
                lambda worker: SQLiteAcceptedRunRepository(path).claim_run(
                    replace(request, lease_owner_id=worker)
                ),
                ("worker-1", "worker-2"),
            )
        )

    granted = tuple(claim for claim in claims if claim is not None)
    assert len(granted) == 1
    assert granted[0].lease_generation == 1
    assert granted[0].fencing_token == 1
    assert (
        repository.get_run(tenant_id="tenant-1", run_id="run-1").claim
        == granted[0]
    )


def test_sqlite_repository_does_not_reclaim_unexpired_lease(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())
    first = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-1",
            now_unix_ms=2_000,
            lease_duration_ms=500,
        )
    )

    second = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-2",
            now_unix_ms=2_499,
            lease_duration_ms=500,
        )
    )

    assert first is not None
    assert second is None
    snapshot = repository.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.claim == first
    assert snapshot.state_version == 2
    assert snapshot.event_high_watermark == 2


def test_sqlite_repository_reclaims_expired_lease_with_new_fence(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())
    first = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-1",
            now_unix_ms=2_000,
            lease_duration_ms=500,
        )
    )
    assert first is not None

    reclaimed = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-2",
            now_unix_ms=2_500,
            lease_duration_ms=750,
        )
    )

    assert reclaimed is not None
    assert reclaimed.lease_owner_id == "worker-2"
    assert reclaimed.lease_generation == 2
    assert reclaimed.fencing_token == 2
    assert reclaimed.lease_expires_at_unix_ms == 3_250
    with pytest.raises(StaleAcceptedRunClaimError):
        assert_current_claim(current=reclaimed, provided=first)
    snapshot = repository.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.claim == reclaimed
    assert snapshot.state_version == 3
    assert snapshot.event_high_watermark == 3
    events = repository.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        after_sequence=2,
        limit=10,
    )
    assert [(event.sequence, event.kind) for event in events.events] == [
        (3, "run_reclaimed")
    ]


def test_sqlite_repository_claim_is_tenant_scoped(tmp_path) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())

    with pytest.raises(AcceptedRunNotFoundError):
        repository.claim_run(
            AcceptedRunClaimRequest(
                tenant_id="tenant-2",
                run_id="run-1",
                lease_owner_id="worker-2",
                now_unix_ms=2_000,
                lease_duration_ms=500,
            )
        )


@pytest.mark.parametrize(
    "failpoint",
    [
        "claim_run.after_state_update",
        "claim_run.after_event_insert",
    ],
)
def test_sqlite_repository_rolls_back_precommit_claim_failure(
    tmp_path,
    failpoint: str,
) -> None:
    path = tmp_path / f"{failpoint}.sqlite3"
    SQLiteAcceptedRunRepository(path).accept_run(_admission())

    def inject(point: str) -> None:
        if point == failpoint:
            raise RuntimeError(f"injected {point}")

    repository = SQLiteAcceptedRunRepository(path, failpoint=inject)
    request = AcceptedRunClaimRequest(
        tenant_id="tenant-1",
        run_id="run-1",
        lease_owner_id="worker-1",
        now_unix_ms=2_000,
        lease_duration_ms=500,
    )
    with pytest.raises(RuntimeError, match=f"injected {failpoint}"):
        repository.claim_run(request)

    reopened = SQLiteAcceptedRunRepository(path)
    snapshot = reopened.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.READY_INITIAL
    assert snapshot.state_version == 1
    assert snapshot.event_high_watermark == 1
    claim = reopened.claim_run(request)
    assert claim is not None
    assert claim.lease_generation == 1
    assert claim.fencing_token == 1


def test_sqlite_repository_recovers_claim_after_response_loss(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    SQLiteAcceptedRunRepository(path).accept_run(_admission())

    def inject(point: str) -> None:
        if point == "claim_run.after_commit":
            raise RuntimeError("injected claim response loss")

    request = AcceptedRunClaimRequest(
        tenant_id="tenant-1",
        run_id="run-1",
        lease_owner_id="worker-1",
        now_unix_ms=2_000,
        lease_duration_ms=500,
    )
    with pytest.raises(RuntimeError, match="injected claim response loss"):
        SQLiteAcceptedRunRepository(path, failpoint=inject).claim_run(request)

    reopened = SQLiteAcceptedRunRepository(path)
    snapshot = reopened.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.RUNNING
    assert snapshot.claim is not None
    assert snapshot.claim.lease_generation == 1
    assert reopened.claim_run(replace(request, lease_owner_id="worker-2")) is None
    reclaimed = reopened.claim_run(
        replace(
            request,
            lease_owner_id="worker-2",
            now_unix_ms=2_500,
        )
    )
    assert reclaimed is not None
    assert reclaimed.lease_generation == 2
    assert reclaimed.fencing_token == 2


def test_sqlite_repository_rejects_claim_time_outside_sqlite_range(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())

    with pytest.raises(ValueError, match="lease expiration exceeds SQLite"):
        repository.claim_run(
            AcceptedRunClaimRequest(
                tenant_id="tenant-1",
                run_id="run-1",
                lease_owner_id="worker-1",
                now_unix_ms=(1 << 63) - 1,
                lease_duration_ms=1,
            )
        )


def test_sqlite_repository_commits_waiting_checkpoint_and_dispatch_atomically(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())
    claim = _claim_ready_run(repository)
    command = _waiting_commit(claim)

    snapshot = repository.commit_waiting(command)

    assert snapshot.phase is AcceptedRunPhase.WAITING_CALLBACK
    assert snapshot.claim is None
    assert snapshot.state_version == 3
    assert snapshot.event_high_watermark == 3
    assert snapshot.checkpoint_digest == command.checkpoint.checkpoint_digest
    events = repository.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        after_sequence=2,
        limit=10,
    )
    assert [(event.sequence, event.kind) for event in events.events] == [
        (3, "run_waiting_callback")
    ]

    reopened = SQLiteAcceptedRunRepository(path)
    stored = reopened.get_checkpoint(
        tenant_id="tenant-1",
        run_id="run-1",
        checkpoint_digest=command.checkpoint.checkpoint_digest,
    )
    assert stored == command.checkpoint
    assert decode_runtime_checkpoint(stored) == decode_runtime_checkpoint(
        command.checkpoint
    )
    connection = sqlite3.connect(path)
    checkpoint_count = int(
        connection.execute("SELECT COUNT(*) FROM run_checkpoints").fetchone()[0]
    )
    effect = connection.execute(
        """
        SELECT effect_id, effect_kind, delivery_state, attempt_count
        FROM effect_outbox
        """
    ).fetchone()
    connection.close()
    assert checkpoint_count == 1
    assert effect == (
        "effect-operation-dispatch-1",
        "operation_dispatch",
        "pending",
        0,
    )


def test_sqlite_repository_does_not_expose_uncommitted_dispatch_effect(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())
    command = _waiting_commit(_claim_ready_run(repository))
    outbox_inserted = Event()
    allow_commit = Event()

    def pause(point: str) -> None:
        if point == "commit_waiting.after_outbox_insert":
            outbox_inserted.set()
            assert allow_commit.wait(timeout=5)

    paused = SQLiteAcceptedRunRepository(path, failpoint=pause)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(paused.commit_waiting, command)
        assert outbox_inserted.wait(timeout=5)
        connection = sqlite3.connect(path)
        visible_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM effect_outbox"
            ).fetchone()[0]
        )
        connection.close()
        assert visible_count == 0
        allow_commit.set()
        snapshot = future.result(timeout=5)

    assert snapshot.phase is AcceptedRunPhase.WAITING_CALLBACK
    connection = sqlite3.connect(path)
    committed_count = int(
        connection.execute("SELECT COUNT(*) FROM effect_outbox").fetchone()[0]
    )
    connection.close()
    assert committed_count == 1


@pytest.mark.parametrize(
    "failpoint",
    [
        "commit_waiting.after_checkpoint_insert",
        "commit_waiting.after_outbox_insert",
        "commit_waiting.after_event_insert",
        "commit_waiting.after_state_update",
    ],
)
def test_sqlite_repository_rolls_back_precommit_waiting_failure(
    tmp_path,
    failpoint: str,
) -> None:
    path = tmp_path / f"{failpoint}.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())
    claim = _claim_ready_run(repository)
    command = _waiting_commit(claim)

    def inject(point: str) -> None:
        if point == failpoint:
            raise RuntimeError(f"injected {point}")

    with pytest.raises(RuntimeError, match=f"injected {failpoint}"):
        SQLiteAcceptedRunRepository(
            path,
            failpoint=inject,
        ).commit_waiting(command)

    reopened = SQLiteAcceptedRunRepository(path)
    snapshot = reopened.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.RUNNING
    assert snapshot.claim == claim
    assert snapshot.state_version == 2
    assert snapshot.event_high_watermark == 2
    connection = sqlite3.connect(path)
    counts = (
        int(
            connection.execute(
                "SELECT COUNT(*) FROM run_checkpoints"
            ).fetchone()[0]
        ),
        int(
            connection.execute(
                "SELECT COUNT(*) FROM effect_outbox"
            ).fetchone()[0]
        ),
    )
    connection.close()
    assert counts == (0, 0)


def test_sqlite_repository_replays_identical_waiting_commit_after_response_loss(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())
    command = _waiting_commit(_claim_ready_run(repository))

    def inject(point: str) -> None:
        if point == "commit_waiting.after_commit":
            raise RuntimeError("injected waiting response loss")

    with pytest.raises(RuntimeError, match="injected waiting response loss"):
        SQLiteAcceptedRunRepository(
            path,
            failpoint=inject,
        ).commit_waiting(command)

    reopened = SQLiteAcceptedRunRepository(path)
    replay = reopened.commit_waiting(command)
    assert replay.phase is AcceptedRunPhase.WAITING_CALLBACK
    assert replay.state_version == 3
    assert replay.event_high_watermark == 3
    connection = sqlite3.connect(path)
    counts = (
        int(
            connection.execute(
                "SELECT COUNT(*) FROM run_checkpoints"
            ).fetchone()[0]
        ),
        int(
            connection.execute(
                "SELECT COUNT(*) FROM effect_outbox"
            ).fetchone()[0]
        ),
    )
    connection.close()
    assert counts == (1, 1)


def test_sqlite_repository_rejects_conflicting_waiting_commit_retry(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())
    command = _waiting_commit(_claim_ready_run(repository))
    repository.commit_waiting(command)
    conflicting_payload = {
        "operationId": "operation-1",
        "runId": "run-1",
        "variant": "conflicting",
    }

    with pytest.raises(
        CheckpointIntegrityError,
        match="stored waiting transition conflicts with retry",
    ):
        repository.commit_waiting(
            replace(
                command,
                dispatch_effect=replace(
                    command.dispatch_effect,
                    payload_json=canonical_dumps(conflicting_payload),
                    payload_digest=canonical_hash(conflicting_payload),
                ),
            )
        )

    snapshot = repository.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.state_version == 3
    assert snapshot.event_high_watermark == 3


def test_sqlite_repository_rejects_waiting_commit_from_stale_claim(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository = SQLiteAcceptedRunRepository(path)
    repository.accept_run(_admission())
    stale = _claim_ready_run(repository)
    current = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-2",
            now_unix_ms=2_500,
            lease_duration_ms=500,
        )
    )
    assert current is not None

    with pytest.raises(StaleAcceptedRunClaimError):
        repository.commit_waiting(
            _waiting_commit(stale, event_time=2_400)
        )

    snapshot = repository.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.claim == current
    assert snapshot.state_version == 3
    assert snapshot.event_high_watermark == 3
    connection = sqlite3.connect(path)
    counts = (
        int(
            connection.execute(
                "SELECT COUNT(*) FROM run_checkpoints"
            ).fetchone()[0]
        ),
        int(
            connection.execute(
                "SELECT COUNT(*) FROM effect_outbox"
            ).fetchone()[0]
        ),
    )
    connection.close()
    assert counts == (0, 0)


def test_sqlite_repository_rejects_waiting_commit_at_lease_expiry(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())
    claim = _claim_ready_run(repository)

    with pytest.raises(
        AcceptedRunLeaseExpiredError,
        match="accepted run claim expired before waiting commit",
    ):
        repository.commit_waiting(
            _waiting_commit(
                claim,
                event_time=claim.lease_expires_at_unix_ms,
            )
        )

    snapshot = repository.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.RUNNING
    assert snapshot.claim == claim


def test_sqlite_repository_rejects_waiting_commit_state_version_conflict(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())
    claim = _claim_ready_run(repository)

    with pytest.raises(
        AcceptedRunStateConflictError,
        match="state version conflict",
    ):
        repository.commit_waiting(
            replace(
                _waiting_commit(claim),
                expected_state_version=1,
            )
        )

    snapshot = repository.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.RUNNING
    assert snapshot.claim == claim


def test_sqlite_repository_rejects_checkpoint_for_different_graph(
    tmp_path,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())
    claim = _claim_ready_run(repository)

    with pytest.raises(
        CheckpointIntegrityError,
        match="checkpoint graph hash does not match accepted run",
    ):
        repository.commit_waiting(
            _waiting_commit(
                claim,
                graph_hash="sha256:" + ("d" * 64),
            )
        )


def test_sqlite_repository_hides_cross_tenant_checkpoint(tmp_path) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())
    command = _waiting_commit(_claim_ready_run(repository))
    repository.commit_waiting(command)

    assert (
        repository.get_checkpoint(
            tenant_id="tenant-2",
            run_id="run-1",
            checkpoint_digest=command.checkpoint.checkpoint_digest,
        )
        is None
    )


@pytest.mark.parametrize(
    ("after_sequence", "limit", "message"),
    [
        (-1, 10, "after_sequence"),
        (0, 0, "limit"),
        (0, MAX_ACCEPTED_RUN_EVENT_PAGE_SIZE + 1, "limit"),
    ],
)
def test_sqlite_repository_bounds_event_page_requests(
    tmp_path,
    after_sequence: int,
    limit: int,
    message: str,
) -> None:
    repository = SQLiteAcceptedRunRepository(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.accept_run(_admission())

    with pytest.raises(ValueError, match=message):
        repository.read_events(
            tenant_id="tenant-1",
            run_id="run-1",
            after_sequence=after_sequence,
            limit=limit,
        )

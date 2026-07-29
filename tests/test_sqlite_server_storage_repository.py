from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
import uuid

import pytest

from graphblocks.canonical import canonical_dumps, canonical_hash
from graphblocks.server_storage import (
    AcceptedRunAdmission,
    AcceptedRunClaimRequest,
    AcceptedRunEventIntent,
    AcceptedRunIdConflictError,
    AcceptedRunNotFoundError,
    AcceptedRunPhase,
    AdmissionIdempotencyConflictError,
    AdmissionIdentity,
    StaleAcceptedRunClaimError,
    assert_current_claim,
)
from graphblocks.sqlite_server_storage import (
    MAX_ACCEPTED_RUN_EVENT_PAGE_SIZE,
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
    request_digest = canonical_hash(
        {
            "tenantId": tenant_id,
            "ownerPrincipalId": owner_principal_id,
            "runId": run_id,
            "graph": graph,
            "inputs": inputs,
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
    internal_id = str(
        connection.execute(
            "SELECT internal_id FROM accepted_runs"
        ).fetchone()[0]
    )
    connection.close()
    assert uuid.UUID(internal_id).version == 7


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

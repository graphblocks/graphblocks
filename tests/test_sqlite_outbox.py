from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import replace
from multiprocessing import get_context
import sqlite3
from threading import Barrier, Event

import pytest

from graphblocks.canonical import canonical_dumps, canonical_hash
from graphblocks.server_storage import (
    AcceptedRunAdmission,
    AcceptedRunClaimRequest,
    AcceptedRunEffectDeliveryAck,
    AcceptedRunEffectDeliveryClaimRequest,
    AcceptedRunEffectDeliveryLeaseExpiredError,
    AcceptedRunEffectDeliveryRetry,
    AcceptedRunEffectDeliveryState,
    AcceptedRunEffectDeliveryStateConflictError,
    AcceptedRunEffectIntent,
    AcceptedRunEffectKind,
    AcceptedRunEventIntent,
    AcceptedRunTerminalCommit,
    AdmissionIdentity,
    StaleAcceptedRunEffectDeliveryClaimError,
)
from graphblocks.sqlite_outbox import SQLiteOutboxDispatcherRepository
from graphblocks.sqlite_server_storage import (
    SQLiteAcceptedRunCorruptionError,
    SQLiteAcceptedRunRepository,
)


class _MutableClock:
    def __init__(self, now_unix_ms: int = 3_000) -> None:
        self.now_unix_ms = now_unix_ms

    def __call__(self) -> int:
        return self.now_unix_ms


_REPLAY_CLAIM_MUTATIONS = (
    {"delivery_owner_id": "dispatcher-forged"},
    {"claim_generation": 2},
    {"fencing_token": 2},
    {"claim_started_at_unix_ms": 2_999},
    {"lease_expires_at_unix_ms": 4_001},
)


def _outbox_repository(
    path,
    *,
    clock: Callable[[], int] | None = None,
    failpoint: Callable[[str], None] | None = None,
    max_lease_duration_ms: int = 30_000,
) -> SQLiteOutboxDispatcherRepository:
    if clock is None:
        clock = _MutableClock()
    return SQLiteOutboxDispatcherRepository(
        path,
        clock=clock,
        failpoint=failpoint,
        max_lease_duration_ms=max_lease_duration_ms,
    )


def _admission() -> AcceptedRunAdmission:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "durable-outbox"},
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
        ticket_json=canonical_dumps({"runId": "run-1", "state": "accepted"}),
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


def _seed_completion_effect(path) -> str:
    repository = SQLiteAcceptedRunRepository(path, clock=lambda: 2_500)
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
    result = {"answer": "done"}
    event_payload = {"runId": "run-1", "state": "succeeded"}
    completion_payload = {
        "resultDigest": canonical_hash(result),
        "runId": "run-1",
        "status": "succeeded",
    }
    repository.commit_terminal(
        AcceptedRunTerminalCommit(
            claim=claim,
            expected_state_version=2,
            terminal_status="succeeded",
            result_json=canonical_dumps(result),
            result_digest=canonical_hash(result),
            terminal_event=AcceptedRunEventIntent(
                kind="run_succeeded",
                payload_json=canonical_dumps(event_payload),
                payload_digest=canonical_hash(event_payload),
                created_at_unix_ms=2_500,
            ),
            completion_effect=AcceptedRunEffectIntent(
                effect_id="effect-completion-1",
                kind=AcceptedRunEffectKind.COMPLETION,
                idempotency_key="completion-run-1",
                payload_json=canonical_dumps(completion_payload),
                payload_digest=canonical_hash(completion_payload),
            ),
        )
    )
    return "effect-completion-1"


@pytest.fixture
def outbox_path(tmp_path):
    path = tmp_path / "accepted-runs.sqlite3"
    _seed_completion_effect(path)
    return path


def _claim_request(
    *,
    owner: str = "dispatcher-1",
    now: int = 2_600,
    lease_duration: int = 1_000,
) -> AcceptedRunEffectDeliveryClaimRequest:
    return AcceptedRunEffectDeliveryClaimRequest(
        delivery_owner_id=owner,
        now_unix_ms=now,
        lease_duration_ms=lease_duration,
    )


def _claim_effect(
    repository: SQLiteOutboxDispatcherRepository,
    **request_overrides,
):
    claimed = repository.claim_next_effect(_claim_request(**request_overrides))
    assert claimed is not None
    assert claimed.claim is not None
    return claimed


def _claim_effect_in_process(
    arguments: tuple[str, str],
) -> tuple[str, int, int] | None:
    path, owner = arguments
    claimed = _outbox_repository(path).claim_next_effect(_claim_request(owner=owner))
    if claimed is None:
        return None
    assert claimed.claim is not None
    return (
        claimed.effect_id,
        claimed.claim.claim_generation,
        claimed.claim.fencing_token,
    )


def _stored_replay_identity(path, effect_id: str) -> tuple[object, object]:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            """
            SELECT last_delivery_command_json,
                   last_delivery_command_digest
            FROM effect_outbox
            WHERE effect_id = ?
            """,
            (effect_id,),
        ).fetchone()
        assert row is not None
        return (
            row[0],
            row[1],
        )
    finally:
        connection.close()


def test_sqlite_outbox_claim_returns_bound_effect_envelope(outbox_path) -> None:
    repository = _outbox_repository(outbox_path)

    claimed = _claim_effect(repository)

    assert claimed.effect_id == "effect-completion-1"
    assert claimed.tenant_id == "tenant-1"
    assert claimed.run_id == "run-1"
    assert claimed.owner_principal_id == "principal-1"
    assert claimed.kind is AcceptedRunEffectKind.COMPLETION
    assert claimed.delivery_state is AcceptedRunEffectDeliveryState.CLAIMED
    assert claimed.attempt_count == 1
    assert claimed.available_at_unix_ms == 2_500
    assert claimed.claim.delivery_owner_id == "dispatcher-1"
    assert claimed.claim.claim_generation == 1
    assert claimed.claim.fencing_token == 1
    assert claimed.claim.claim_started_at_unix_ms == 3_000
    assert claimed.claim.lease_expires_at_unix_ms == 4_000


@pytest.mark.parametrize("max_lease_duration_ms", [True, 0, -1, 1 << 63])
def test_sqlite_outbox_rejects_invalid_lease_policy(
    tmp_path,
    max_lease_duration_ms,
) -> None:
    with pytest.raises(
        ValueError,
        match="max lease duration must be a positive SQLite integer",
    ):
        SQLiteOutboxDispatcherRepository(
            tmp_path / "accepted-runs.sqlite3",
            max_lease_duration_ms=max_lease_duration_ms,
        )


def test_sqlite_outbox_rejects_non_callable_clock(tmp_path) -> None:
    with pytest.raises(ValueError, match="clock must be callable"):
        SQLiteOutboxDispatcherRepository(
            tmp_path / "accepted-runs.sqlite3",
            clock=3_000,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("clock_value", [True, "3000", -1, 1 << 63])
def test_sqlite_outbox_clock_fails_closed(
    outbox_path,
    clock_value,
) -> None:
    repository = SQLiteOutboxDispatcherRepository(
        outbox_path,
        clock=lambda: clock_value,  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(
        ValueError,
        match="clock must return a non-negative SQLite integer",
    ):
        repository.claim_next_effect(_claim_request())

    stored = repository.get_effect(effect_id="effect-completion-1")
    assert stored is not None
    assert stored.delivery_state is AcceptedRunEffectDeliveryState.PENDING


def test_sqlite_outbox_clock_exception_fails_closed(outbox_path) -> None:
    def broken_clock() -> int:
        raise RuntimeError("clock unavailable")

    repository = _outbox_repository(outbox_path, clock=broken_clock)

    with pytest.raises(RuntimeError, match="clock unavailable"):
        repository.claim_next_effect(_claim_request())

    stored = repository.get_effect(effect_id="effect-completion-1")
    assert stored is not None
    assert stored.delivery_state is AcceptedRunEffectDeliveryState.PENDING


def test_sqlite_outbox_claim_uses_bounded_authoritative_time(
    outbox_path,
) -> None:
    repository = _outbox_repository(
        outbox_path,
        max_lease_duration_ms=1_000,
    )

    with pytest.raises(
        ValueError,
        match="claim request timestamp must not be later than the repository clock",
    ):
        repository.claim_next_effect(_claim_request(now=3_001))
    with pytest.raises(
        ValueError,
        match="lease duration exceeds the repository maximum",
    ):
        repository.claim_next_effect(_claim_request(lease_duration=1_001))

    claimed = _claim_effect(repository, lease_duration=1_000)
    assert claimed.claim is not None
    assert claimed.claim.lease_expires_at_unix_ms == 4_000


def test_sqlite_outbox_rolls_back_claim_that_expires_before_commit(
    outbox_path,
) -> None:
    clock = _MutableClock()

    def expire_claim(point: str) -> None:
        if point == "claim_next_effect.after_state_update":
            clock.now_unix_ms = 4_000

    repository = _outbox_repository(
        outbox_path,
        clock=clock,
        failpoint=expire_claim,
    )

    with pytest.raises(
        AcceptedRunEffectDeliveryLeaseExpiredError,
        match="effect delivery claim expired before claim acquisition",
    ):
        repository.claim_next_effect(_claim_request())

    stored = repository.get_effect(effect_id="effect-completion-1")
    assert stored is not None
    assert stored.delivery_state is AcceptedRunEffectDeliveryState.PENDING


def test_sqlite_outbox_rolls_back_when_clock_moves_backwards(
    outbox_path,
) -> None:
    clock_values = iter((3_000, 2_999))
    repository = _outbox_repository(
        outbox_path,
        clock=lambda: next(clock_values),
    )

    with pytest.raises(
        ValueError,
        match="clock moved backwards during claim acquisition",
    ):
        repository.claim_next_effect(_claim_request())

    stored = repository.get_effect(effect_id="effect-completion-1")
    assert stored is not None
    assert stored.delivery_state is AcceptedRunEffectDeliveryState.PENDING


def test_two_sqlite_outbox_dispatchers_cannot_claim_same_effect(
    outbox_path,
) -> None:
    repositories = (
        _outbox_repository(outbox_path),
        _outbox_repository(outbox_path),
    )
    starting = Barrier(2)

    def claim(index: int):
        starting.wait()
        return repositories[index].claim_next_effect(
            _claim_request(owner=f"dispatcher-{index + 1}")
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(executor.map(claim, range(2)))

    assert sum(item is not None for item in claims) == 1
    winner = next(item for item in claims if item is not None)
    assert winner.attempt_count == 1
    assert winner.claim is not None
    assert winner.claim.claim_generation == 1
    assert winner.claim.fencing_token == 1


def test_two_processes_cannot_claim_same_sqlite_outbox_effect(
    outbox_path,
) -> None:
    arguments = (
        (str(outbox_path), "dispatcher-1"),
        (str(outbox_path), "dispatcher-2"),
    )

    with ProcessPoolExecutor(
        max_workers=2,
        mp_context=get_context("spawn"),
    ) as executor:
        claims = tuple(executor.map(_claim_effect_in_process, arguments))

    assert sum(item is not None for item in claims) == 1
    assert next(item for item in claims if item is not None) == (
        "effect-completion-1",
        1,
        1,
    )


def test_sqlite_outbox_does_not_claim_effect_before_availability(
    outbox_path,
) -> None:
    clock = _MutableClock(2_499)
    repository = _outbox_repository(outbox_path, clock=clock)

    assert repository.claim_next_effect(_claim_request(now=2_499)) is None
    clock.now_unix_ms = 2_500
    assert _claim_effect(repository, now=2_500).attempt_count == 1


def test_sqlite_outbox_claim_is_not_visible_before_commit(outbox_path) -> None:
    state_updated = Event()
    allow_commit = Event()

    def pause(point: str) -> None:
        if point == "claim_next_effect.after_state_update":
            state_updated.set()
            assert allow_commit.wait(timeout=5)

    claiming = _outbox_repository(
        outbox_path,
        failpoint=pause,
    )
    observing = _outbox_repository(outbox_path)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            claiming.claim_next_effect,
            _claim_request(),
        )
        assert state_updated.wait(timeout=5)
        visible = observing.get_effect(effect_id="effect-completion-1")
        assert visible is not None
        assert visible.delivery_state is AcceptedRunEffectDeliveryState.PENDING
        assert visible.claim is None
        allow_commit.set()
        claimed = future.result(timeout=5)

    assert claimed is not None
    assert claimed.delivery_state is AcceptedRunEffectDeliveryState.CLAIMED


def test_sqlite_outbox_takeover_requires_expired_lease(outbox_path) -> None:
    clock = _MutableClock()
    first = _outbox_repository(outbox_path, clock=clock)
    current = _claim_effect(first)
    assert current.claim is not None
    second = _outbox_repository(outbox_path, clock=clock)

    clock.now_unix_ms = current.claim.lease_expires_at_unix_ms - 1
    assert (
        second.claim_next_effect(
            _claim_request(
                owner="dispatcher-2",
                now=current.claim.lease_expires_at_unix_ms - 1,
            )
        )
        is None
    )
    with pytest.raises(
        ValueError,
        match="claim request timestamp must not be later than the repository clock",
    ):
        second.claim_next_effect(
            _claim_request(
                owner="dispatcher-2",
                now=current.claim.lease_expires_at_unix_ms,
            )
        )
    clock.now_unix_ms = current.claim.lease_expires_at_unix_ms
    takeover = _claim_effect(
        second,
        owner="dispatcher-2",
        now=current.claim.lease_expires_at_unix_ms,
        lease_duration=500,
    )

    assert takeover.attempt_count == 2
    assert takeover.claim is not None
    assert takeover.claim.delivery_owner_id == "dispatcher-2"
    assert takeover.claim.claim_generation == 2
    assert takeover.claim.fencing_token == 2
    assert takeover.claim.lease_expires_at_unix_ms == 4_500


def test_sqlite_outbox_rejects_stale_ack_after_takeover(outbox_path) -> None:
    clock = _MutableClock()
    repository = _outbox_repository(outbox_path, clock=clock)
    stale = _claim_effect(repository)
    assert stale.claim is not None
    clock.now_unix_ms = stale.claim.lease_expires_at_unix_ms
    current = _claim_effect(
        repository,
        owner="dispatcher-2",
        now=stale.claim.lease_expires_at_unix_ms,
        lease_duration=500,
    )
    assert current.claim is not None

    with pytest.raises(StaleAcceptedRunEffectDeliveryClaimError):
        repository.mark_effect_delivered(
            AcceptedRunEffectDeliveryAck(
                claim=stale.claim,
                delivered_at_unix_ms=3_000,
            )
        )

    stored = repository.get_effect(effect_id=stale.effect_id)
    assert stored is not None
    assert stored.claim == current.claim
    assert stored.delivery_state is AcceptedRunEffectDeliveryState.CLAIMED


def test_sqlite_outbox_rejects_stale_retry_after_takeover(outbox_path) -> None:
    clock = _MutableClock()
    repository = _outbox_repository(outbox_path, clock=clock)
    stale = _claim_effect(repository)
    assert stale.claim is not None
    clock.now_unix_ms = stale.claim.lease_expires_at_unix_ms
    current = _claim_effect(
        repository,
        owner="dispatcher-2",
        now=stale.claim.lease_expires_at_unix_ms,
        lease_duration=500,
    )
    assert current.claim is not None

    with pytest.raises(StaleAcceptedRunEffectDeliveryClaimError):
        repository.release_effect_for_retry(
            AcceptedRunEffectDeliveryRetry(
                claim=stale.claim,
                released_at_unix_ms=3_000,
                available_at_unix_ms=4_500,
            )
        )

    stored = repository.get_effect(effect_id=stale.effect_id)
    assert stored is not None
    assert stored.claim == current.claim
    assert stored.delivery_state is AcceptedRunEffectDeliveryState.CLAIMED


def test_sqlite_outbox_rejects_ack_at_lease_expiry(outbox_path) -> None:
    clock = _MutableClock()
    repository = _outbox_repository(outbox_path, clock=clock)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    clock.now_unix_ms = claimed.claim.lease_expires_at_unix_ms

    with pytest.raises(
        AcceptedRunEffectDeliveryLeaseExpiredError,
        match="effect delivery claim expired before delivery acknowledgement",
    ):
        repository.mark_effect_delivered(
            AcceptedRunEffectDeliveryAck(
                claim=claimed.claim,
                delivered_at_unix_ms=3_000,
            )
        )


def test_sqlite_outbox_rejects_ack_timestamp_before_claim_start(
    outbox_path,
) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None

    with pytest.raises(
        ValueError,
        match="delivery timestamp must not precede claim start",
    ):
        repository.mark_effect_delivered(
            AcceptedRunEffectDeliveryAck(
                claim=claimed.claim,
                delivered_at_unix_ms=(
                    claimed.claim.claim_started_at_unix_ms - 1
                ),
            )
        )

    stored = repository.get_effect(effect_id=claimed.effect_id)
    assert stored is not None
    assert stored.claim == claimed.claim
    assert _stored_replay_identity(outbox_path, claimed.effect_id) == (None, None)


def test_sqlite_outbox_rejects_future_delivery_timestamp(outbox_path) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None

    with pytest.raises(
        ValueError,
        match="delivery timestamp must not be later than the repository clock",
    ):
        repository.mark_effect_delivered(
            AcceptedRunEffectDeliveryAck(
                claim=claimed.claim,
                delivered_at_unix_ms=3_001,
            )
        )

    stored = repository.get_effect(effect_id=claimed.effect_id)
    assert stored is not None
    assert stored.delivery_state is AcceptedRunEffectDeliveryState.CLAIMED


def test_sqlite_outbox_rolls_back_ack_that_expires_before_commit(
    outbox_path,
) -> None:
    clock = _MutableClock()
    repository = _outbox_repository(outbox_path, clock=clock)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None

    def expire_claim(point: str) -> None:
        if point == "mark_effect_delivered.after_state_update":
            clock.now_unix_ms = claimed.claim.lease_expires_at_unix_ms

    with pytest.raises(
        AcceptedRunEffectDeliveryLeaseExpiredError,
        match="effect delivery claim expired before delivery acknowledgement",
    ):
        _outbox_repository(
            outbox_path,
            clock=clock,
            failpoint=expire_claim,
        ).mark_effect_delivered(
            AcceptedRunEffectDeliveryAck(
                claim=claimed.claim,
                delivered_at_unix_ms=3_000,
            )
        )

    stored = repository.get_effect(effect_id=claimed.effect_id)
    assert stored is not None
    assert stored.delivery_state is AcceptedRunEffectDeliveryState.CLAIMED
    assert stored.claim == claimed.claim


def test_sqlite_outbox_ack_is_fenced_and_idempotent(outbox_path) -> None:
    clock = _MutableClock()
    repository = _outbox_repository(outbox_path, clock=clock)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    command = AcceptedRunEffectDeliveryAck(
        claim=claimed.claim,
        delivered_at_unix_ms=3_000,
    )

    delivered = repository.mark_effect_delivered(command)
    replay = repository.mark_effect_delivered(command)

    assert delivered == replay
    assert delivered.delivery_state is AcceptedRunEffectDeliveryState.DELIVERED
    assert delivered.claim is None
    assert delivered.delivered_at_unix_ms == 3_000
    clock.now_unix_ms = 4_000
    assert (
        repository.claim_next_effect(_claim_request(owner="dispatcher-2", now=4_000))
        is None
    )


@pytest.mark.parametrize("claim_changes", _REPLAY_CLAIM_MUTATIONS)
def test_sqlite_outbox_delivered_replay_requires_exact_claim_identity(
    outbox_path,
    claim_changes,
) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    command = AcceptedRunEffectDeliveryAck(
        claim=claimed.claim,
        delivered_at_unix_ms=3_000,
    )
    repository.mark_effect_delivered(command)

    with pytest.raises(StaleAcceptedRunEffectDeliveryClaimError):
        repository.mark_effect_delivered(
            replace(command, claim=replace(claimed.claim, **claim_changes))
        )


def test_sqlite_outbox_delivered_replay_requires_exact_ack_command(
    outbox_path,
) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    command = AcceptedRunEffectDeliveryAck(
        claim=claimed.claim,
        delivered_at_unix_ms=3_000,
    )
    repository.mark_effect_delivered(command)

    with pytest.raises(AcceptedRunEffectDeliveryStateConflictError):
        repository.mark_effect_delivered(
            replace(command, delivered_at_unix_ms=3_001)
        )


def test_sqlite_outbox_rejects_retry_as_delivered_ack_replay(
    outbox_path,
) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    repository.mark_effect_delivered(
        AcceptedRunEffectDeliveryAck(
            claim=claimed.claim,
            delivered_at_unix_ms=3_000,
        )
    )

    with pytest.raises(AcceptedRunEffectDeliveryStateConflictError):
        repository.release_effect_for_retry(
            AcceptedRunEffectDeliveryRetry(
                claim=claimed.claim,
                released_at_unix_ms=3_000,
                available_at_unix_ms=4_000,
            )
        )


def test_sqlite_outbox_ack_replays_after_response_loss(outbox_path) -> None:
    clock = _MutableClock()
    repository = _outbox_repository(outbox_path, clock=clock)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    command = AcceptedRunEffectDeliveryAck(
        claim=claimed.claim,
        delivered_at_unix_ms=3_000,
    )

    def inject(point: str) -> None:
        if point == "mark_effect_delivered.after_commit":
            raise RuntimeError("injected acknowledgement response loss")

    with pytest.raises(
        RuntimeError,
        match="injected acknowledgement response loss",
    ):
        _outbox_repository(
            outbox_path,
            clock=clock,
            failpoint=inject,
        ).mark_effect_delivered(command)

    clock.now_unix_ms = claimed.claim.lease_expires_at_unix_ms
    replay = _outbox_repository(outbox_path, clock=clock).mark_effect_delivered(
        command
    )
    assert replay.delivery_state is AcceptedRunEffectDeliveryState.DELIVERED
    assert replay.delivered_at_unix_ms == 3_000


def test_sqlite_outbox_rolls_back_failed_ack_transaction(outbox_path) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None

    def inject(point: str) -> None:
        if point == "mark_effect_delivered.after_state_update":
            raise RuntimeError("injected acknowledgement failure")

    with pytest.raises(RuntimeError, match="injected acknowledgement failure"):
        _outbox_repository(
            outbox_path,
            failpoint=inject,
        ).mark_effect_delivered(
            AcceptedRunEffectDeliveryAck(
                claim=claimed.claim,
                delivered_at_unix_ms=3_000,
            )
        )

    stored = repository.get_effect(effect_id=claimed.effect_id)
    assert stored is not None
    assert stored.delivery_state is AcceptedRunEffectDeliveryState.CLAIMED
    assert stored.claim == claimed.claim
    assert _stored_replay_identity(outbox_path, claimed.effect_id) == (None, None)


def test_sqlite_outbox_retry_waits_until_scheduled_time(outbox_path) -> None:
    clock = _MutableClock()
    repository = _outbox_repository(outbox_path, clock=clock)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None

    first_retry = AcceptedRunEffectDeliveryRetry(
        claim=claimed.claim,
        released_at_unix_ms=3_000,
        available_at_unix_ms=4_000,
    )
    pending = repository.release_effect_for_retry(first_retry)

    assert pending.delivery_state is AcceptedRunEffectDeliveryState.PENDING
    assert pending.claim is None
    assert pending.attempt_count == 1
    assert pending.available_at_unix_ms == 4_000
    clock.now_unix_ms = 3_999
    assert (
        repository.claim_next_effect(_claim_request(owner="dispatcher-2", now=3_999))
        is None
    )
    clock.now_unix_ms = 4_000
    retried = _claim_effect(
        repository,
        owner="dispatcher-2",
        now=4_000,
        lease_duration=500,
    )
    assert retried.attempt_count == 2
    assert retried.claim is not None
    assert retried.claim.claim_generation == 2
    assert retried.claim.fencing_token == 2
    assert retried.idempotency_key == claimed.idempotency_key
    assert retried.payload_json == claimed.payload_json
    assert retried.payload_digest == claimed.payload_digest
    assert _stored_replay_identity(outbox_path, claimed.effect_id) == (None, None)
    with pytest.raises(StaleAcceptedRunEffectDeliveryClaimError):
        repository.release_effect_for_retry(first_retry)

    second_retry = AcceptedRunEffectDeliveryRetry(
        claim=retried.claim,
        released_at_unix_ms=4_000,
        available_at_unix_ms=4_500,
    )
    second_pending = repository.release_effect_for_retry(second_retry)
    assert repository.release_effect_for_retry(second_retry) == second_pending
    with pytest.raises(StaleAcceptedRunEffectDeliveryClaimError):
        repository.release_effect_for_retry(first_retry)


def test_sqlite_outbox_rejects_retry_timestamp_before_claim_start(
    outbox_path,
) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None

    with pytest.raises(
        ValueError,
        match="retry timestamp must not precede claim start",
    ):
        repository.release_effect_for_retry(
            AcceptedRunEffectDeliveryRetry(
                claim=claimed.claim,
                released_at_unix_ms=(
                    claimed.claim.claim_started_at_unix_ms - 1
                ),
                available_at_unix_ms=4_000,
            )
        )

    stored = repository.get_effect(effect_id=claimed.effect_id)
    assert stored is not None
    assert stored.claim == claimed.claim
    assert _stored_replay_identity(outbox_path, claimed.effect_id) == (None, None)


@pytest.mark.parametrize("claim_changes", _REPLAY_CLAIM_MUTATIONS)
def test_sqlite_outbox_pending_retry_replay_requires_exact_claim_identity(
    outbox_path,
    claim_changes,
) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    command = AcceptedRunEffectDeliveryRetry(
        claim=claimed.claim,
        released_at_unix_ms=3_000,
        available_at_unix_ms=4_000,
    )
    repository.release_effect_for_retry(command)

    with pytest.raises(StaleAcceptedRunEffectDeliveryClaimError):
        repository.release_effect_for_retry(
            replace(command, claim=replace(claimed.claim, **claim_changes))
        )


def test_sqlite_outbox_rejects_expired_backdated_retry(outbox_path) -> None:
    clock = _MutableClock()
    repository = _outbox_repository(outbox_path, clock=clock)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    clock.now_unix_ms = claimed.claim.lease_expires_at_unix_ms

    with pytest.raises(
        AcceptedRunEffectDeliveryLeaseExpiredError,
        match="effect delivery claim expired before retry release",
    ):
        repository.release_effect_for_retry(
            AcceptedRunEffectDeliveryRetry(
                claim=claimed.claim,
                released_at_unix_ms=3_000,
                available_at_unix_ms=4_500,
            )
        )


def test_sqlite_outbox_rejects_future_or_immediately_available_retry(
    outbox_path,
) -> None:
    clock = _MutableClock()
    repository = _outbox_repository(outbox_path, clock=clock)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None

    with pytest.raises(
        ValueError,
        match="retry timestamp must not be later than the repository clock",
    ):
        repository.release_effect_for_retry(
            AcceptedRunEffectDeliveryRetry(
                claim=claimed.claim,
                released_at_unix_ms=3_001,
                available_at_unix_ms=4_000,
            )
        )
    clock.now_unix_ms = 3_100
    with pytest.raises(
        ValueError,
        match="retry availability must not precede the repository clock",
    ):
        repository.release_effect_for_retry(
            AcceptedRunEffectDeliveryRetry(
                claim=claimed.claim,
                released_at_unix_ms=3_050,
                available_at_unix_ms=3_099,
            )
        )


def test_sqlite_outbox_rolls_back_retry_that_expires_before_commit(
    outbox_path,
) -> None:
    clock = _MutableClock()
    repository = _outbox_repository(outbox_path, clock=clock)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None

    def expire_claim(point: str) -> None:
        if point == "release_effect_for_retry.after_state_update":
            clock.now_unix_ms = claimed.claim.lease_expires_at_unix_ms

    with pytest.raises(
        AcceptedRunEffectDeliveryLeaseExpiredError,
        match="effect delivery claim expired before retry release",
    ):
        _outbox_repository(
            outbox_path,
            clock=clock,
            failpoint=expire_claim,
        ).release_effect_for_retry(
            AcceptedRunEffectDeliveryRetry(
                claim=claimed.claim,
                released_at_unix_ms=3_000,
                available_at_unix_ms=4_500,
            )
        )

    stored = repository.get_effect(effect_id=claimed.effect_id)
    assert stored is not None
    assert stored.delivery_state is AcceptedRunEffectDeliveryState.CLAIMED
    assert stored.claim == claimed.claim
    assert _stored_replay_identity(outbox_path, claimed.effect_id) == (None, None)


def test_sqlite_outbox_retry_replays_after_response_loss(outbox_path) -> None:
    clock = _MutableClock()
    repository = _outbox_repository(outbox_path, clock=clock)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    command = AcceptedRunEffectDeliveryRetry(
        claim=claimed.claim,
        released_at_unix_ms=3_000,
        available_at_unix_ms=4_000,
    )

    def inject(point: str) -> None:
        if point == "release_effect_for_retry.after_commit":
            raise RuntimeError("injected retry response loss")

    with pytest.raises(RuntimeError, match="injected retry response loss"):
        _outbox_repository(
            outbox_path,
            clock=clock,
            failpoint=inject,
        ).release_effect_for_retry(command)

    clock.now_unix_ms = claimed.claim.lease_expires_at_unix_ms
    replay = _outbox_repository(outbox_path, clock=clock).release_effect_for_retry(
        command
    )
    assert replay.delivery_state is AcceptedRunEffectDeliveryState.PENDING
    assert replay.available_at_unix_ms == 4_000
    assert replay.attempt_count == 1


def test_sqlite_outbox_rolls_back_failed_retry_transaction(outbox_path) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None

    def inject(point: str) -> None:
        if point == "release_effect_for_retry.after_state_update":
            raise RuntimeError("injected retry failure")

    with pytest.raises(RuntimeError, match="injected retry failure"):
        _outbox_repository(
            outbox_path,
            failpoint=inject,
        ).release_effect_for_retry(
            AcceptedRunEffectDeliveryRetry(
                claim=claimed.claim,
                released_at_unix_ms=3_000,
                available_at_unix_ms=4_000,
            )
        )

    stored = repository.get_effect(effect_id=claimed.effect_id)
    assert stored is not None
    assert stored.delivery_state is AcceptedRunEffectDeliveryState.CLAIMED
    assert stored.claim == claimed.claim
    assert _stored_replay_identity(outbox_path, claimed.effect_id) == (None, None)


@pytest.mark.parametrize(
    "command_changes",
    (
        {"released_at_unix_ms": 3_001},
        {"available_at_unix_ms": 4_500},
    ),
)
def test_sqlite_outbox_rejects_conflicting_retry_replay(
    outbox_path,
    command_changes,
) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    command = AcceptedRunEffectDeliveryRetry(
        claim=claimed.claim,
        released_at_unix_ms=3_000,
        available_at_unix_ms=4_000,
    )
    repository.release_effect_for_retry(command)

    with pytest.raises(
        AcceptedRunEffectDeliveryStateConflictError,
        match="cannot transition from delivery state 'pending'",
    ):
        repository.release_effect_for_retry(replace(command, **command_changes))


def test_sqlite_outbox_rejects_partial_replay_identity_as_corruption(
    outbox_path,
) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    repository.mark_effect_delivered(
        AcceptedRunEffectDeliveryAck(
            claim=claimed.claim,
            delivered_at_unix_ms=3_000,
        )
    )
    connection = sqlite3.connect(outbox_path)
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            """
            UPDATE effect_outbox
            SET last_delivery_command_digest = NULL
            WHERE effect_id = ?
            """,
            (claimed.effect_id,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SQLiteAcceptedRunCorruptionError,
        match="replay command identity is partial",
    ):
        repository.get_effect(effect_id=claimed.effect_id)


def test_sqlite_outbox_rejects_open_replay_envelope_as_corruption(
    outbox_path,
) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    command = AcceptedRunEffectDeliveryAck(
        claim=claimed.claim,
        delivered_at_unix_ms=3_000,
    )
    repository.mark_effect_delivered(command)
    payload = {
        "formatVersion": "graphblocks.effect-delivery-command.v1",
        "kind": "ack",
        "claim": {
            "effectId": claimed.claim.effect_id,
            "deliveryOwnerId": claimed.claim.delivery_owner_id,
            "claimGeneration": claimed.claim.claim_generation,
            "fencingToken": claimed.claim.fencing_token,
            "claimStartedAtUnixMs": claimed.claim.claim_started_at_unix_ms,
            "leaseExpiresAtUnixMs": claimed.claim.lease_expires_at_unix_ms,
        },
        "deliveredAtUnixMs": 3_000,
        "unexpected": True,
    }
    connection = sqlite3.connect(outbox_path)
    try:
        connection.execute(
            """
            UPDATE effect_outbox
            SET last_delivery_command_json = ?,
                last_delivery_command_digest = ?
            WHERE effect_id = ?
            """,
            (
                canonical_dumps(payload),
                canonical_hash(payload),
                claimed.effect_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SQLiteAcceptedRunCorruptionError,
        match="replay command identity is invalid",
    ):
        repository.get_effect(effect_id=claimed.effect_id)


def test_sqlite_outbox_rejects_replay_digest_mismatch_before_reclaim(
    outbox_path,
) -> None:
    clock = _MutableClock()
    repository = _outbox_repository(outbox_path, clock=clock)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    repository.release_effect_for_retry(
        AcceptedRunEffectDeliveryRetry(
            claim=claimed.claim,
            released_at_unix_ms=3_000,
            available_at_unix_ms=4_000,
        )
    )
    stored_identity = _stored_replay_identity(outbox_path, claimed.effect_id)
    connection = sqlite3.connect(outbox_path)
    try:
        connection.execute(
            """
            UPDATE effect_outbox
            SET last_delivery_command_digest = ?
            WHERE effect_id = ?
            """,
            (canonical_hash({"mismatch": True}), claimed.effect_id),
        )
        connection.commit()
    finally:
        connection.close()
    clock.now_unix_ms = 4_000

    with pytest.raises(
        SQLiteAcceptedRunCorruptionError,
        match="replay command identity is invalid",
    ):
        repository.claim_next_effect(
            _claim_request(owner="dispatcher-2", now=4_000)
        )

    corrupted_identity = _stored_replay_identity(outbox_path, claimed.effect_id)
    assert corrupted_identity[0] == stored_identity[0]
    assert corrupted_identity[1] != stored_identity[1]


@pytest.mark.parametrize(
    "column",
    (
        "claim_generation",
        "claim_fencing_token",
        "delivered_at_unix_ms",
    ),
)
def test_sqlite_outbox_rejects_replay_projection_mismatch(
    outbox_path,
    column,
) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    repository.mark_effect_delivered(
        AcceptedRunEffectDeliveryAck(
            claim=claimed.claim,
            delivered_at_unix_ms=3_000,
        )
    )
    connection = sqlite3.connect(outbox_path)
    try:
        connection.execute(
            f"""
            UPDATE effect_outbox
            SET {column} = {column} + 1
            WHERE effect_id = ?
            """,
            (claimed.effect_id,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SQLiteAcceptedRunCorruptionError,
        match="replay command identity is invalid",
    ):
        repository.get_effect(effect_id=claimed.effect_id)


def test_sqlite_outbox_rejects_ack_claim_before_stored_availability(
    outbox_path,
) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    repository.mark_effect_delivered(
        AcceptedRunEffectDeliveryAck(
            claim=claimed.claim,
            delivered_at_unix_ms=3_000,
        )
    )
    connection = sqlite3.connect(outbox_path)
    try:
        connection.execute(
            """
            UPDATE effect_outbox
            SET available_at_unix_ms = ?
            WHERE effect_id = ?
            """,
            (
                claimed.claim.claim_started_at_unix_ms + 1,
                claimed.effect_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SQLiteAcceptedRunCorruptionError,
        match="replay command identity is invalid",
    ):
        repository.get_effect(effect_id=claimed.effect_id)


def test_sqlite_outbox_rejects_replay_observation_outside_claim(
    outbox_path,
) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    command = AcceptedRunEffectDeliveryAck(
        claim=claimed.claim,
        delivered_at_unix_ms=3_000,
    )
    repository.mark_effect_delivered(command)
    payload = {
        "formatVersion": "graphblocks.effect-delivery-command.v1",
        "kind": "ack",
        "claim": {
            "effectId": claimed.claim.effect_id,
            "deliveryOwnerId": claimed.claim.delivery_owner_id,
            "claimGeneration": claimed.claim.claim_generation,
            "fencingToken": claimed.claim.fencing_token,
            "claimStartedAtUnixMs": claimed.claim.claim_started_at_unix_ms,
            "leaseExpiresAtUnixMs": claimed.claim.lease_expires_at_unix_ms,
        },
        "deliveredAtUnixMs": claimed.claim.claim_started_at_unix_ms - 1,
    }
    connection = sqlite3.connect(outbox_path)
    try:
        connection.execute(
            """
            UPDATE effect_outbox
            SET delivered_at_unix_ms = ?,
                last_delivery_command_json = ?,
                last_delivery_command_digest = ?
            WHERE effect_id = ?
            """,
            (
                payload["deliveredAtUnixMs"],
                canonical_dumps(payload),
                canonical_hash(payload),
                claimed.effect_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SQLiteAcceptedRunCorruptionError,
        match="replay command identity is invalid",
    ):
        repository.get_effect(effect_id=claimed.effect_id)


def test_sqlite_outbox_rejects_invalid_claim_interval_as_corruption(
    outbox_path,
) -> None:
    repository = _outbox_repository(outbox_path)
    claimed = _claim_effect(repository)
    assert claimed.claim is not None
    connection = sqlite3.connect(outbox_path)
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            """
            UPDATE effect_outbox
            SET claim_started_at_unix_ms = claim_expires_at_unix_ms
            WHERE effect_id = ?
            """,
            (claimed.effect_id,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SQLiteAcceptedRunCorruptionError,
        match="outbox effect is invalid",
    ):
        repository.get_effect(effect_id=claimed.effect_id)

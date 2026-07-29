from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from threading import Event

import pytest

from graphblocks.canonical import canonical_dumps, canonical_hash
from graphblocks.server_storage import (
    AcceptedRunAdmission,
    AcceptedRunClaim,
    AcceptedRunClaimRequest,
    AcceptedRunEffectIntent,
    AcceptedRunEffectKind,
    AcceptedRunEventIntent,
    AcceptedRunLeaseExpiredError,
    AcceptedRunPhase,
    AcceptedRunTerminalCommit,
    AcceptedRunTerminalConflictError,
    AdmissionIdentity,
    StaleAcceptedRunClaimError,
)
from graphblocks.sqlite_server_storage import SQLiteAcceptedRunRepository


def _admission() -> AcceptedRunAdmission:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "durable-terminal"},
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


def _running_run(path) -> tuple[SQLiteAcceptedRunRepository, AcceptedRunClaim]:
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
    return repository, claim


def _terminal_command(
    claim: AcceptedRunClaim,
    *,
    result: dict[str, object] | None = None,
    event_time: int = 2_500,
) -> AcceptedRunTerminalCommit:
    actual_result = result or {"answer": "done"}
    event_payload = {
        "runId": claim.run_id,
        "state": "succeeded",
    }
    completion_payload = {
        "resultDigest": canonical_hash(actual_result),
        "runId": claim.run_id,
        "status": "succeeded",
    }
    return AcceptedRunTerminalCommit(
        claim=claim,
        expected_state_version=2,
        terminal_status="succeeded",
        result_json=canonical_dumps(actual_result),
        result_digest=canonical_hash(actual_result),
        terminal_event=AcceptedRunEventIntent(
            kind="run_succeeded",
            payload_json=canonical_dumps(event_payload),
            payload_digest=canonical_hash(event_payload),
            created_at_unix_ms=event_time,
        ),
        completion_effect=AcceptedRunEffectIntent(
            effect_id="effect-completion-1",
            kind=AcceptedRunEffectKind.COMPLETION,
            idempotency_key="completion-run-1",
            payload_json=canonical_dumps(completion_payload),
            payload_digest=canonical_hash(completion_payload),
        ),
    )


def test_sqlite_repository_commits_terminal_result_and_outbox_atomically(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository, claim = _running_run(path)
    command = _terminal_command(claim)

    snapshot = repository.commit_terminal(command)

    assert snapshot.phase is AcceptedRunPhase.TERMINAL
    assert snapshot.claim is None
    assert snapshot.state_version == 3
    assert snapshot.event_high_watermark == 3
    assert snapshot.terminal_status == "succeeded"
    assert snapshot.terminal_result_json == command.result_json
    events = repository.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        after_sequence=2,
        limit=10,
    )
    assert [(event.sequence, event.kind) for event in events.events] == [
        (3, "run_succeeded")
    ]
    connection = sqlite3.connect(path)
    outbox = connection.execute(
        """
        SELECT effect_id, checkpoint_digest, effect_kind, delivery_state,
               attempt_count
        FROM effect_outbox
        """
    ).fetchone()
    result_digest = str(
        connection.execute(
            "SELECT terminal_result_digest FROM accepted_runs"
        ).fetchone()[0]
    )
    connection.close()
    assert outbox == (
        "effect-completion-1",
        None,
        "completion",
        "pending",
        0,
    )
    assert result_digest == command.result_digest


def test_sqlite_repository_does_not_expose_uncommitted_completion(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository, claim = _running_run(path)
    command = _terminal_command(claim)
    outbox_inserted = Event()
    allow_commit = Event()

    def pause(point: str) -> None:
        if point == "commit_terminal.after_outbox_insert":
            outbox_inserted.set()
            assert allow_commit.wait(timeout=5)

    paused = SQLiteAcceptedRunRepository(path, failpoint=pause)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(paused.commit_terminal, command)
        assert outbox_inserted.wait(timeout=5)
        connection = sqlite3.connect(path)
        visible_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM effect_outbox
                WHERE effect_kind = 'completion'
                """
            ).fetchone()[0]
        )
        connection.close()
        assert visible_count == 0
        allow_commit.set()
        snapshot = future.result(timeout=5)

    assert snapshot.phase is AcceptedRunPhase.TERMINAL


@pytest.mark.parametrize(
    "failpoint",
    [
        "commit_terminal.after_outbox_insert",
        "commit_terminal.after_event_insert",
        "commit_terminal.after_state_update",
    ],
)
def test_sqlite_repository_rolls_back_precommit_terminal_failure(
    tmp_path,
    failpoint: str,
) -> None:
    path = tmp_path / f"{failpoint}.sqlite3"
    repository, claim = _running_run(path)
    command = _terminal_command(claim)

    def inject(point: str) -> None:
        if point == failpoint:
            raise RuntimeError(f"injected {point}")

    with pytest.raises(RuntimeError, match=f"injected {failpoint}"):
        SQLiteAcceptedRunRepository(
            path,
            failpoint=inject,
        ).commit_terminal(command)

    snapshot = repository.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.RUNNING
    assert snapshot.claim == claim
    assert snapshot.state_version == 2
    assert snapshot.event_high_watermark == 2
    connection = sqlite3.connect(path)
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
    assert completion_count == 0


def test_sqlite_repository_replays_terminal_commit_after_response_loss(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    _, claim = _running_run(path)
    command = _terminal_command(claim)

    def inject(point: str) -> None:
        if point == "commit_terminal.after_commit":
            raise RuntimeError("injected terminal response loss")

    with pytest.raises(RuntimeError, match="injected terminal response loss"):
        SQLiteAcceptedRunRepository(
            path,
            failpoint=inject,
        ).commit_terminal(command)

    repository = SQLiteAcceptedRunRepository(path)
    replay = repository.commit_terminal(command)
    assert replay.phase is AcceptedRunPhase.TERMINAL
    assert replay.state_version == 3
    assert replay.event_high_watermark == 3
    connection = sqlite3.connect(path)
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
    assert completion_count == 1


def test_sqlite_repository_rejects_conflicting_terminal_retry(
    tmp_path,
) -> None:
    repository, claim = _running_run(
        tmp_path / "accepted-runs.sqlite3"
    )
    command = _terminal_command(claim)
    repository.commit_terminal(command)

    with pytest.raises(
        AcceptedRunTerminalConflictError,
        match="terminal commit conflicts with stored result",
    ):
        repository.commit_terminal(
            _terminal_command(
                claim,
                result={"answer": "conflicting"},
            )
        )


def test_sqlite_repository_rejects_terminal_commit_from_stale_claim(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    repository, stale = _running_run(path)
    current = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-2",
            now_unix_ms=3_000,
            lease_duration_ms=500,
        )
    )
    assert current is not None

    with pytest.raises(StaleAcceptedRunClaimError):
        repository.commit_terminal(
            _terminal_command(stale, event_time=2_900)
        )

    snapshot = repository.get_run(tenant_id="tenant-1", run_id="run-1")
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.RUNNING
    assert snapshot.claim == current
    connection = sqlite3.connect(path)
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
    assert completion_count == 0


def test_sqlite_repository_rejects_terminal_commit_at_lease_expiry(
    tmp_path,
) -> None:
    repository, claim = _running_run(
        tmp_path / "accepted-runs.sqlite3"
    )

    with pytest.raises(
        AcceptedRunLeaseExpiredError,
        match="accepted run claim expired before terminal commit",
    ):
        repository.commit_terminal(
            _terminal_command(
                claim,
                event_time=claim.lease_expires_at_unix_ms,
            )
        )


def test_sqlite_repository_terminal_run_cannot_be_claimed_again(
    tmp_path,
) -> None:
    repository, claim = _running_run(
        tmp_path / "accepted-runs.sqlite3"
    )
    repository.commit_terminal(_terminal_command(claim))

    next_claim = repository.claim_run(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            lease_owner_id="worker-2",
            now_unix_ms=3_100,
            lease_duration_ms=500,
        )
    )

    assert next_claim is None


def test_sqlite_repository_rejects_terminal_event_status_mismatch(
    tmp_path,
) -> None:
    repository, claim = _running_run(
        tmp_path / "accepted-runs.sqlite3"
    )
    command = _terminal_command(claim)

    with pytest.raises(
        ValueError,
        match="terminal event kind must match terminal_status",
    ):
        repository.commit_terminal(
            replace(
                command,
                terminal_event=replace(
                    command.terminal_event,
                    kind="run_failed",
                ),
            )
        )

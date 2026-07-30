from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from graphblocks.canonical import canonical_dumps, canonical_hash
from graphblocks.compiler import compile_graph_reference
from graphblocks.durable_server import DurableAcceptedRunService
from graphblocks.server_storage import (
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
    InvalidAcceptedRunTransitionError,
    StaleAcceptedRunClaimError,
)
from graphblocks.sqlite_server_storage import SQLiteAcceptedRunRepository


_TENANT_ID = "tenant-expire"
_OWNER_ID = "principal-expire"
_RUN_ID = "run-expire-1"


def _service(
    path: Path,
    *,
    clock_value: int,
    failpoint: Callable[[str], None] | None = None,
) -> DurableAcceptedRunService:
    return DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(
            path,
            failpoint=failpoint,
        ),
        lease_owner_id=f"worker-{clock_value}",
        lease_duration_ms=10_000,
        compiler=compile_graph_reference,
        clock=lambda: clock_value,
    )


def _admit(service: DurableAcceptedRunService) -> None:
    admission = service.admit_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        idempotency_key="admission-expire-1",
        graph={
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "durable-expire"},
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


def test_sqlite_repository_expiration_survives_restart_and_replays(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    _admit(_service(path, clock_value=1_000))

    expired = _service(path, clock_value=2_000).expire_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=1,
        idempotency_key="expire-1",
        reason="deadline_elapsed",
    )
    replayed = _service(path, clock_value=2_500).expire_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=1,
        idempotency_key="expire-1",
        reason="deadline_elapsed",
    )

    assert expired.action is AcceptedRunControlAction.EXPIRE
    assert expired.resulting_phase is AcceptedRunPhase.TERMINAL
    assert expired.state_version == 2
    assert replayed == replace(expired, replayed=True)
    repository = SQLiteAcceptedRunRepository(path)
    snapshot = repository.get_run(
        tenant_id=_TENANT_ID,
        run_id=_RUN_ID,
    )
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.TERMINAL
    assert snapshot.terminal_status == "expired"
    assert snapshot.terminal_result_json == canonical_dumps(
        {
            "reason": "deadline_elapsed",
            "requestId": "expire-1",
            "status": "expired",
        }
    )
    assert [
        event.kind
        for event in repository.read_events(
            tenant_id=_TENANT_ID,
            run_id=_RUN_ID,
            after_sequence=0,
            limit=10,
        ).events
    ] == ["run_accepted", "run_expired"]
    connection = sqlite3.connect(path)
    control = connection.execute(
        """
        SELECT action, resulting_phase
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
    connection.close()
    assert control == ("expire", "terminal")
    assert completion_count == 1


def test_sqlite_repository_expiration_is_owner_scoped_and_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    service = _service(path, clock_value=1_000)
    _admit(service)

    with pytest.raises(AcceptedRunNotFoundError):
        service.expire_run(
            tenant_id=_TENANT_ID,
            owner_principal_id="principal-other",
            run_id=_RUN_ID,
            expected_state_version=1,
            idempotency_key="expire-owner",
            reason="not_owner",
        )
    with pytest.raises(AcceptedRunStateConflictError):
        service.expire_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=2,
            idempotency_key="expire-stale",
            reason="stale",
        )

    service.expire_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=1,
        idempotency_key="expire-conflict",
        reason="first",
    )
    with pytest.raises(AcceptedRunControlConflictError):
        service.expire_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=1,
            idempotency_key="expire-conflict",
            reason="different",
        )
    with pytest.raises(AcceptedRunControlConflictError):
        service.cancel_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=1,
            idempotency_key="expire-conflict",
            reason="different_action",
        )
    with pytest.raises(InvalidAcceptedRunTransitionError):
        service.expire_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=2,
            idempotency_key="expire-again",
            reason="already_terminal",
        )


def test_sqlite_repository_expiration_fences_active_worker(
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

    _service(path, clock_value=2_500).expire_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=work.state_version,
        idempotency_key="expire-running",
        reason="execution_deadline",
    )

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
                    effect_id="effect-late-worker-completion",
                    kind=AcceptedRunEffectKind.COMPLETION,
                    idempotency_key="completion-late-worker",
                    payload_json=canonical_dumps(completion_payload),
                    payload_digest=canonical_hash(completion_payload),
                ),
            )
        )


def test_sqlite_repository_can_expire_paused_run_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    _admit(_service(path, clock_value=1_000))
    paused = _service(path, clock_value=2_000).pause_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=1,
        idempotency_key="pause-before-expire",
        reason="inspect",
    )

    expired = _service(path, clock_value=2_500).expire_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=paused.state_version,
        idempotency_key="expire-paused",
        reason="paused_deadline",
    )

    assert expired.resulting_phase is AcceptedRunPhase.TERMINAL
    snapshot = SQLiteAcceptedRunRepository(path).get_run(
        tenant_id=_TENANT_ID,
        run_id=_RUN_ID,
    )
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.TERMINAL
    assert snapshot.paused_from_phase is None
    assert snapshot.terminal_status == "expired"


@pytest.mark.parametrize(
    "failpoint",
    [
        "expire_run.after_outbox_insert",
        "expire_run.after_event_insert",
        "expire_run.after_control_insert",
        "expire_run.after_state_update",
    ],
)
def test_sqlite_repository_rolls_back_expiration_failure(
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
        ).expire_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=1,
            idempotency_key="expire-rollback",
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
    control_count = int(
        connection.execute("SELECT COUNT(*) FROM run_controls").fetchone()[0]
    )
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
    assert control_count == 0
    assert completion_count == 0


def test_sqlite_repository_replays_expiration_after_response_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    _admit(_service(path, clock_value=1_000))

    def lose_response(point: str) -> None:
        if point == "expire_run.after_commit":
            raise RuntimeError("lost expiration response")

    with pytest.raises(RuntimeError, match="lost expiration response"):
        _service(
            path,
            clock_value=2_000,
            failpoint=lose_response,
        ).expire_run(
            tenant_id=_TENANT_ID,
            owner_principal_id=_OWNER_ID,
            run_id=_RUN_ID,
            expected_state_version=1,
            idempotency_key="expire-loss",
            reason="response_loss",
        )

    replayed = _service(path, clock_value=2_500).expire_run(
        tenant_id=_TENANT_ID,
        owner_principal_id=_OWNER_ID,
        run_id=_RUN_ID,
        expected_state_version=1,
        idempotency_key="expire-loss",
        reason="response_loss",
    )
    assert replayed.replayed
    assert replayed.action is AcceptedRunControlAction.EXPIRE

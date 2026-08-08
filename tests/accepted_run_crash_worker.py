from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3

from graphblocks.canonical import canonical_dumps, canonical_hash
from graphblocks.runtime import RuntimeCheckpoint
from graphblocks.server_storage import (
    AcceptedRunAdmission,
    AcceptedRunClaim,
    AcceptedRunClaimRequest,
    AcceptedRunEffectIntent,
    AcceptedRunEffectKind,
    AcceptedRunEventIntent,
    AcceptedRunPhase,
    AcceptedRunTerminalCommit,
    AcceptedRunWaitingCommit,
    AdmissionIdentity,
    CallbackIssuanceIdentity,
    StaleAcceptedRunClaimError,
    assert_current_claim,
    encode_runtime_checkpoint,
)
from graphblocks.sqlite_server_storage import SQLiteAcceptedRunRepository


_TENANT_ID = "tenant-crash"
_RUN_ID = "run-crash"
_CRASHED_WORKER_ID = "worker-crashed"


def _admission() -> AcceptedRunAdmission:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "accepted-run-crash-recovery"},
        "spec": {"edges": [], "nodes": {}},
    }
    inputs = {"request": {"value": "hello"}}
    invocation = {
        "policySnapshotId": "policy-crash",
        "releaseId": "release-crash",
        "responseId": "response-crash",
        "turnId": None,
    }
    event_payload = {
        "runId": _RUN_ID,
        "state": "ready_initial",
        "tenantId": _TENANT_ID,
    }
    return AcceptedRunAdmission(
        run_id=_RUN_ID,
        identity=AdmissionIdentity(
            tenant_id=_TENANT_ID,
            owner_principal_id="principal-crash",
            admission_scope="POST:/runs",
            idempotency_key="admission-crash",
            request_digest=canonical_hash(
                {
                    "graph": graph,
                    "inputs": inputs,
                    "invocation": invocation,
                    "runId": _RUN_ID,
                }
            ),
        ),
        graph_json=canonical_dumps(graph),
        graph_hash=canonical_hash(graph),
        inputs_json=canonical_dumps(inputs),
        invocation_json=canonical_dumps(invocation),
        ticket_json=canonical_dumps({"runId": _RUN_ID, "state": "accepted"}),
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


def _claim_request(
    lease_owner_id: str = _CRASHED_WORKER_ID,
    *,
    now_unix_ms: int = 2_000,
    lease_duration_ms: int = 1_000,
) -> AcceptedRunClaimRequest:
    return AcceptedRunClaimRequest(
        tenant_id=_TENANT_ID,
        run_id=_RUN_ID,
        lease_owner_id=lease_owner_id,
        now_unix_ms=now_unix_ms,
        lease_duration_ms=lease_duration_ms,
    )


def _known_claim(
    lease_owner_id: str = _CRASHED_WORKER_ID,
) -> AcceptedRunClaim:
    return AcceptedRunClaim(
        tenant_id=_TENANT_ID,
        run_id=_RUN_ID,
        lease_owner_id=lease_owner_id,
        lease_generation=1,
        fencing_token=1,
        lease_expires_at_unix_ms=3_000,
    )


def _runtime_checkpoint(claim: AcceptedRunClaim) -> RuntimeCheckpoint:
    values: dict[str, object] = {
        "checkpoint_id": "checkpoint-crash",
        "run_id": _RUN_ID,
        "graph_hash": _admission().graph_hash,
        "wait_node": "wait",
        "remaining_nodes": ("wait",),
        "inputs": {"request": {"value": "hello"}},
        "node_outputs": {},
        "output_values": {},
        "operation": {
            "operation_id": "operation-crash",
            "run_id": _RUN_ID,
            "node_id": "wait",
            "attempt_id": "attempt-crash",
            "kind": "ci_job",
            "resume_token_hash": "sha256:" + ("c" * 64),
            "idempotency_key": "operation-idempotency-crash",
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


def _waiting_commit(claim: AcceptedRunClaim) -> AcceptedRunWaitingCommit:
    checkpoint = _runtime_checkpoint(claim)
    waiting_payload = {
        "checkpointDigest": checkpoint.state_digest,
        "runId": _RUN_ID,
        "state": "waiting_callback",
    }
    dispatch_payload = {
        "operationId": "operation-crash",
        "runId": _RUN_ID,
    }
    return AcceptedRunWaitingCommit(
        claim=claim,
        expected_state_version=2,
        checkpoint=encode_runtime_checkpoint(checkpoint),
        callback_issuance=CallbackIssuanceIdentity(
            run_id=_RUN_ID,
            checkpoint_digest=checkpoint.state_digest,
            operation_id="operation-crash",
            operation_attempt_id="attempt-crash",
            callback_idempotency_key="callback-crash",
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
            effect_id="effect-dispatch-crash",
            kind=AcceptedRunEffectKind.OPERATION_DISPATCH,
            idempotency_key="dispatch-operation-crash",
            payload_json=canonical_dumps(dispatch_payload),
            payload_digest=canonical_hash(dispatch_payload),
        ),
    )


def _terminal_commit(claim: AcceptedRunClaim) -> AcceptedRunTerminalCommit:
    result = {"answer": "done"}
    event_payload = {"runId": _RUN_ID, "state": "succeeded"}
    effect_payload = {
        "resultDigest": canonical_hash(result),
        "runId": _RUN_ID,
        "status": "succeeded",
    }
    return AcceptedRunTerminalCommit(
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
            effect_id="effect-completion-crash",
            kind=AcceptedRunEffectKind.COMPLETION,
            idempotency_key="completion-run-crash",
            payload_json=canonical_dumps(effect_payload),
            payload_digest=canonical_hash(effect_payload),
        ),
    )


def _repository(
    path: Path,
    *,
    now_unix_ms: int = 2_200,
    failpoint=None,
) -> SQLiteAcceptedRunRepository:
    return SQLiteAcceptedRunRepository(
        path,
        clock=lambda: now_unix_ms,
        failpoint=failpoint,
    )


def _prepare(path: Path, operation: str) -> None:
    if operation == "accept":
        return
    repository = _repository(path)
    repository.accept_run(_admission())
    if operation in {"waiting", "terminal"}:
        claim = repository.claim_run(_claim_request())
        if claim != _known_claim():
            raise AssertionError("prepared claim identity is not deterministic")


def _crash(path: Path, operation: str, target: str) -> None:
    _prepare(path, operation)

    def kill_process(point: str) -> None:
        if point == target:
            os.kill(os.getpid(), signal.SIGKILL)

    repository = _repository(
        path,
        now_unix_ms=2_500 if operation == "terminal" else 2_200,
        failpoint=kill_process,
    )
    if operation == "accept":
        repository.accept_run(_admission())
    elif operation == "claim":
        repository.claim_run(_claim_request())
    elif operation == "waiting":
        repository.commit_waiting(_waiting_commit(_known_claim()))
    elif operation == "terminal":
        repository.commit_terminal(_terminal_commit(_known_claim()))
    else:
        raise ValueError(f"unknown crash operation {operation!r}")
    raise AssertionError(f"crash failpoint {target!r} was not reached")


def _database_counts(path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(path)
    try:
        checkpoints = int(
            connection.execute("SELECT COUNT(*) FROM run_checkpoints").fetchone()[0]
        )
        outbox = int(
            connection.execute("SELECT COUNT(*) FROM effect_outbox").fetchone()[0]
        )
        return checkpoints, outbox
    finally:
        connection.close()


def _verify_case(path: Path, operation: str, outcome: str) -> None:
    repository = _repository(
        path,
        now_unix_ms=2_500 if operation == "terminal" else 2_200,
    )
    snapshot = repository.get_run(tenant_id=_TENANT_ID, run_id=_RUN_ID)
    committed = outcome == "commit"

    if operation == "accept":
        if not committed:
            if snapshot is not None:
                raise AssertionError("uncommitted admission survived SIGKILL")
            result = repository.accept_run(_admission())
            if result.replayed:
                raise AssertionError("rolled-back admission replayed")
        else:
            if snapshot is None or snapshot.phase is not AcceptedRunPhase.READY_INITIAL:
                raise AssertionError("committed admission was lost")
            if not repository.accept_run(_admission()).replayed:
                raise AssertionError("committed admission did not replay")
        return

    if snapshot is None:
        raise AssertionError("prepared accepted run was lost")
    if operation == "claim":
        if not committed:
            if snapshot.phase is not AcceptedRunPhase.READY_INITIAL:
                raise AssertionError("uncommitted lease transition survived")
            recovered = repository.claim_run(_claim_request("worker-recovery"))
            if recovered is None or (
                recovered.lease_generation,
                recovered.fencing_token,
            ) != (1, 1):
                raise AssertionError("rolled-back lease consumed a fence")
        else:
            if snapshot.phase is not AcceptedRunPhase.RUNNING:
                raise AssertionError("committed lease transition was lost")
            if snapshot.claim != _known_claim():
                raise AssertionError("committed claim identity changed")
            reclaimer = _repository(path, now_unix_ms=3_000)
            recovered = reclaimer.claim_run(
                _claim_request(
                    "worker-recovery",
                    now_unix_ms=3_000,
                )
            )
            if recovered is None or (
                recovered.lease_generation,
                recovered.fencing_token,
            ) != (2, 2):
                raise AssertionError("expired lease did not advance its fence")
            try:
                assert_current_claim(
                    current=recovered,
                    provided=_known_claim(),
                )
            except StaleAcceptedRunClaimError:
                pass
            else:
                raise AssertionError("stale crashed-worker claim remained valid")
        return

    expected_phase = (
        AcceptedRunPhase.WAITING_CALLBACK
        if operation == "waiting"
        else AcceptedRunPhase.TERMINAL
    )
    command = (
        _waiting_commit(_known_claim())
        if operation == "waiting"
        else _terminal_commit(_known_claim())
    )
    if not committed:
        if snapshot.phase is not AcceptedRunPhase.RUNNING:
            raise AssertionError("uncommitted durable transition survived")
        if snapshot.claim != _known_claim():
            raise AssertionError("pre-crash claim was not restored")
        if _database_counts(path) != (0, 0):
            raise AssertionError("uncommitted checkpoint or outbox row survived")
    recovered_snapshot = (
        repository.commit_waiting(command)
        if operation == "waiting"
        else repository.commit_terminal(command)
    )
    if recovered_snapshot.phase is not expected_phase:
        raise AssertionError("durable transition did not recover")
    expected_counts = (1, 1) if operation == "waiting" else (0, 1)
    if _database_counts(path) != expected_counts:
        raise AssertionError("recovery duplicated or lost durable rows")


def _run_crash_plan(path: Path) -> None:
    raw_plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_plan, list):
        raise ValueError("crash recovery plan must be an array")
    for raw_case in raw_plan:
        if not isinstance(raw_case, dict):
            raise ValueError("crash recovery plan entries must be objects")
        database = Path(str(raw_case["path"]))
        operation = str(raw_case["operation"])
        failpoint = str(raw_case["failpoint"])
        child_pid = os.fork()
        if child_pid == 0:
            try:
                _crash(database, operation, failpoint)
            except BaseException:
                os._exit(120)
            os._exit(121)
        waited_pid, wait_status = os.waitpid(child_pid, 0)
        if (
            waited_pid != child_pid
            or not os.WIFSIGNALED(wait_status)
            or os.WTERMSIG(wait_status) != signal.SIGKILL
        ):
            raise AssertionError(
                f"{operation}:{failpoint} did not terminate with SIGKILL"
            )
        _verify_case(
            database,
            operation,
            str(raw_case["outcome"]),
        )
    print(
        json.dumps(
            {
                "killedCases": len(raw_plan),
                "verifiedCases": len(raw_plan),
            },
            sort_keys=True,
        )
    )


def _claim_once(
    path: Path,
    lease_owner_id: str,
    now_unix_ms: int,
    lease_duration_ms: int,
) -> None:
    repository = _repository(path, now_unix_ms=now_unix_ms)
    claim = repository.claim_run(
        _claim_request(
            lease_owner_id,
            now_unix_ms=now_unix_ms,
            lease_duration_ms=lease_duration_ms,
        )
    )
    print(
        json.dumps(
            {
                "claimed": claim is not None,
                "fencingToken": (None if claim is None else claim.fencing_token),
                "leaseGeneration": (None if claim is None else claim.lease_generation),
                "leaseOwnerId": lease_owner_id,
            },
            sort_keys=True,
        )
    )


def _assert_stale(path: Path, lease_owner_id: str) -> None:
    snapshot = _repository(path, now_unix_ms=3_100).get_run(
        tenant_id=_TENANT_ID,
        run_id=_RUN_ID,
    )
    if snapshot is None or snapshot.claim is None:
        raise AssertionError("current recovery claim is missing")
    try:
        assert_current_claim(
            current=snapshot.claim,
            provided=_known_claim(lease_owner_id),
        )
    except StaleAcceptedRunClaimError:
        print(json.dumps({"staleRejected": True}, sort_keys=True))
        return
    raise AssertionError("stale claim was not fenced")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("path", type=Path)
    prepare_parser.add_argument("operation")

    crash_parser = subparsers.add_parser("crash")
    crash_parser.add_argument("path", type=Path)
    crash_parser.add_argument("operation")
    crash_parser.add_argument("failpoint")

    crash_plan_parser = subparsers.add_parser("run-crash-plan")
    crash_plan_parser.add_argument("path", type=Path)

    claim_parser = subparsers.add_parser("claim-once")
    claim_parser.add_argument("path", type=Path)
    claim_parser.add_argument("lease_owner_id")
    claim_parser.add_argument("now_unix_ms", type=int)
    claim_parser.add_argument("lease_duration_ms", type=int)

    stale_parser = subparsers.add_parser("assert-stale")
    stale_parser.add_argument("path", type=Path)
    stale_parser.add_argument("lease_owner_id")

    args = parser.parse_args()
    if args.command == "prepare":
        _prepare(args.path, args.operation)
    elif args.command == "crash":
        _crash(args.path, args.operation, args.failpoint)
    elif args.command == "run-crash-plan":
        _run_crash_plan(args.path)
    elif args.command == "claim-once":
        _claim_once(
            args.path,
            args.lease_owner_id,
            args.now_unix_ms,
            args.lease_duration_ms,
        )
    elif args.command == "assert-stale":
        _assert_stale(args.path, args.lease_owner_id)
    else:
        raise AssertionError("unreachable command")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

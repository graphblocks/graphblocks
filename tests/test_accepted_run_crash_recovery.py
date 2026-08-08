from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
WORKER = ROOT / "tests" / "accepted_run_crash_worker.py"
_CRASH_CASES = (
    ("accept", "accept_run.after_run_insert", "rollback"),
    ("accept", "accept_run.after_event_insert", "rollback"),
    ("accept", "accept_run.after_commit", "commit"),
    ("claim", "claim_run.after_state_update", "rollback"),
    ("claim", "claim_run.after_event_insert", "rollback"),
    ("claim", "claim_run.after_commit", "commit"),
    ("waiting", "commit_waiting.after_checkpoint_insert", "rollback"),
    ("waiting", "commit_waiting.after_outbox_insert", "rollback"),
    ("waiting", "commit_waiting.after_event_insert", "rollback"),
    ("waiting", "commit_waiting.after_state_update", "rollback"),
    ("waiting", "commit_waiting.after_commit", "commit"),
    ("terminal", "commit_terminal.after_outbox_insert", "rollback"),
    ("terminal", "commit_terminal.after_event_insert", "rollback"),
    ("terminal", "commit_terminal.after_state_update", "rollback"),
    ("terminal", "commit_terminal.after_commit", "commit"),
)


pytestmark = pytest.mark.skipif(
    os.name != "posix" or not hasattr(signal, "SIGKILL"),
    reason="deterministic SIGKILL recovery evidence requires POSIX",
)


def _worker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    current_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(ROOT)
        if not current_pythonpath
        else os.pathsep.join((str(ROOT), current_pythonpath))
    )
    environment["PATH"] = os.pathsep.join(
        (str(Path(sys.executable).parent), environment.get("PATH", ""))
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _worker_command(*arguments: object) -> list[str]:
    return [sys.executable, str(WORKER), *(str(value) for value in arguments)]


def _run_worker(
    *arguments: object,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _worker_command(*arguments),
        cwd=ROOT,
        env=_worker_environment(),
        check=check,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_sqlite_accepted_run_survives_sigkill_at_atomic_boundaries(
    tmp_path: Path,
) -> None:
    recovery_plan: list[dict[str, str]] = []
    for operation, failpoint, outcome in _CRASH_CASES:
        database = tmp_path / (f"{operation}-{failpoint.replace('.', '-')}.sqlite3")
        recovery_plan.append(
            {
                "failpoint": failpoint,
                "operation": operation,
                "outcome": outcome,
                "path": str(database),
            }
        )

    plan_path = tmp_path / "recovery-plan.json"
    plan_path.write_text(
        json.dumps(recovery_plan, sort_keys=True),
        encoding="utf-8",
    )
    recovered = _run_worker("run-crash-plan", plan_path)

    assert json.loads(recovered.stdout) == {
        "killedCases": len(_CRASH_CASES),
        "verifiedCases": len(_CRASH_CASES),
    }
    assert recovered.stderr == ""


def test_sqlite_accepted_run_claim_is_fenced_across_competing_processes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "competing-processes.sqlite3"
    _run_worker("prepare", database, "claim")
    processes = [
        subprocess.Popen(
            _worker_command(
                "claim-once",
                database,
                worker_id,
                2_000,
                1_000,
            ),
            cwd=ROOT,
            env=_worker_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for worker_id in ("worker-a", "worker-b")
    ]
    results: list[dict[str, object]] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))

    winners = [result for result in results if result["claimed"] is True]
    assert len(winners) == 1
    assert (winners[0]["leaseGeneration"], winners[0]["fencingToken"]) == (
        1,
        1,
    )

    reclaimed = _run_worker(
        "claim-once",
        database,
        "worker-recovery",
        3_000,
        1_000,
    )
    reclaimed_payload = json.loads(reclaimed.stdout)
    assert reclaimed_payload == {
        "claimed": True,
        "fencingToken": 2,
        "leaseGeneration": 2,
        "leaseOwnerId": "worker-recovery",
    }
    stale = _run_worker(
        "assert-stale",
        database,
        winners[0]["leaseOwnerId"],
    )
    assert json.loads(stale.stdout) == {"staleRejected": True}

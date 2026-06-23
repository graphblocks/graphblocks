from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal


RunStatus = Literal["created", "running", "succeeded", "failed", "cancelled"]
MutableRunStatus = Literal["running", "succeeded", "failed", "cancelled"]
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


class StateConflictError(RuntimeError):
    def __init__(self, run_id: str, expected_revision: int, current_revision: int) -> None:
        super().__init__(
            f"state revision conflict for {run_id}: expected {expected_revision}, current {current_revision}"
        )
        self.run_id = run_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision


class RunTerminalStateError(RuntimeError):
    def __init__(self, run_id: str, status: str) -> None:
        super().__init__(f"run {run_id} is terminal with status {status}")
        self.run_id = run_id
        self.status = status


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    graph_hash: str
    inputs: dict[str, Any]
    status: RunStatus = "created"
    state: dict[str, Any] = field(default_factory=dict)
    state_revision: int = 0


@dataclass(slots=True)
class InMemoryRunStore:
    runs: dict[str, RunRecord] = field(default_factory=dict)
    next_id: int = 1

    def create_run(self, graph_hash: str, inputs: dict[str, Any]) -> RunRecord:
        run_id = f"run-{self.next_id:06d}"
        self.next_id += 1
        record = RunRecord(run_id=run_id, graph_hash=graph_hash, inputs=deepcopy(inputs))
        self.runs[run_id] = record
        return deepcopy(record)

    def get_run(self, run_id: str) -> RunRecord:
        return deepcopy(self.runs[run_id])

    def patch_state(self, run_id: str, patch: dict[str, Any], expected_revision: int) -> RunRecord:
        current = self.runs[run_id]
        if current.status in TERMINAL_RUN_STATUSES:
            raise RunTerminalStateError(run_id, current.status)
        if current.state_revision != expected_revision:
            raise StateConflictError(run_id, expected_revision, current.state_revision)

        next_state = deepcopy(current.state)
        stack: list[tuple[dict[str, Any], dict[str, Any]]] = [(next_state, patch)]
        while stack:
            target, source = stack.pop()
            for key, value in source.items():
                if value is None:
                    target.pop(key, None)
                elif isinstance(value, dict) and isinstance(target.get(key), dict):
                    stack.append((target[key], value))
                else:
                    target[key] = deepcopy(value)

        updated = RunRecord(
            run_id=current.run_id,
            graph_hash=current.graph_hash,
            inputs=deepcopy(current.inputs),
            status=current.status,
            state=next_state,
            state_revision=current.state_revision + 1,
        )
        self.runs[run_id] = updated
        return deepcopy(updated)

    def set_status(self, run_id: str, status: MutableRunStatus) -> RunRecord:
        current = self.runs[run_id]
        if current.status in TERMINAL_RUN_STATUSES:
            raise RunTerminalStateError(run_id, current.status)
        updated = RunRecord(
            run_id=current.run_id,
            graph_hash=current.graph_hash,
            inputs=deepcopy(current.inputs),
            status=status,
            state=deepcopy(current.state),
            state_revision=current.state_revision,
        )
        self.runs[run_id] = updated
        return deepcopy(updated)


@dataclass(slots=True)
class SQLiteRunStore:
    path: Path | str
    connection: sqlite3.Connection = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
              run_id TEXT PRIMARY KEY,
              sequence INTEGER NOT NULL UNIQUE,
              graph_hash TEXT NOT NULL,
              inputs_json TEXT NOT NULL,
              status TEXT NOT NULL,
              state_json TEXT NOT NULL,
              state_revision INTEGER NOT NULL
            )
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def create_run(self, graph_hash: str, inputs: dict[str, Any]) -> RunRecord:
        row = self.connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM runs").fetchone()
        sequence = int(row[0])
        run_id = f"run-{sequence:06d}"
        record = RunRecord(run_id=run_id, graph_hash=graph_hash, inputs=deepcopy(inputs))
        self.connection.execute(
            """
            INSERT INTO runs (run_id, sequence, graph_hash, inputs_json, status, state_json, state_revision)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.run_id,
                sequence,
                record.graph_hash,
                json.dumps(record.inputs, sort_keys=True, separators=(",", ":")),
                record.status,
                json.dumps(record.state, sort_keys=True, separators=(",", ":")),
                record.state_revision,
            ),
        )
        self.connection.commit()
        return deepcopy(record)

    def get_run(self, run_id: str) -> RunRecord:
        row = self.connection.execute(
            """
            SELECT run_id, graph_hash, inputs_json, status, state_json, state_revision
            FROM runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunRecord(
            run_id=str(row["run_id"]),
            graph_hash=str(row["graph_hash"]),
            inputs=json.loads(str(row["inputs_json"])),
            status=row["status"],
            state=json.loads(str(row["state_json"])),
            state_revision=int(row["state_revision"]),
        )

    def patch_state(self, run_id: str, patch: dict[str, Any], expected_revision: int) -> RunRecord:
        current = self.get_run(run_id)
        if current.status in TERMINAL_RUN_STATUSES:
            raise RunTerminalStateError(run_id, current.status)
        if current.state_revision != expected_revision:
            raise StateConflictError(run_id, expected_revision, current.state_revision)

        next_state = deepcopy(current.state)
        stack: list[tuple[dict[str, Any], dict[str, Any]]] = [(next_state, patch)]
        while stack:
            target, source = stack.pop()
            for key, value in source.items():
                if value is None:
                    target.pop(key, None)
                elif isinstance(value, dict) and isinstance(target.get(key), dict):
                    stack.append((target[key], value))
                else:
                    target[key] = deepcopy(value)

        updated_revision = current.state_revision + 1
        cursor = self.connection.execute(
            """
            UPDATE runs
            SET state_json = ?, state_revision = ?
            WHERE run_id = ? AND state_revision = ?
            """,
            (
                json.dumps(next_state, sort_keys=True, separators=(",", ":")),
                updated_revision,
                run_id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            refreshed = self.get_run(run_id)
            raise StateConflictError(run_id, expected_revision, refreshed.state_revision)
        self.connection.commit()
        return self.get_run(run_id)

    def set_status(self, run_id: str, status: MutableRunStatus) -> RunRecord:
        current = self.get_run(run_id)
        if current.status in TERMINAL_RUN_STATUSES:
            raise RunTerminalStateError(run_id, current.status)
        cursor = self.connection.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))
        if cursor.rowcount != 1:
            raise KeyError(run_id)
        self.connection.commit()
        return self.get_run(run_id)

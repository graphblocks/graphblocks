from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal


class StateConflictError(RuntimeError):
    def __init__(self, run_id: str, expected_revision: int, current_revision: int) -> None:
        super().__init__(
            f"state revision conflict for {run_id}: expected {expected_revision}, current {current_revision}"
        )
        self.run_id = run_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    graph_hash: str
    inputs: dict[str, Any]
    status: Literal["created", "running", "succeeded", "failed", "cancelled"] = "created"
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

    def set_status(self, run_id: str, status: Literal["running", "succeeded", "failed", "cancelled"]) -> RunRecord:
        current = self.runs[run_id]
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

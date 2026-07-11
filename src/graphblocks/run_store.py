from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Literal

from .evaluation import ModelVisibleToolRef


RunInvocationMode = Literal["sync", "accepted", "background"]
RunStatus = Literal[
    "created",
    "admitted",
    "running",
    "waiting_input",
    "waiting_approval",
    "waiting_review",
    "waiting_callback",
    "paused_budget",
    "paused_callback_delivery",
    "paused_policy",
    "paused_operator",
    "resuming",
    "completed",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
    "policy_stopped",
]
MutableRunStatus = Literal[
    "admitted",
    "running",
    "waiting_input",
    "waiting_approval",
    "waiting_review",
    "waiting_callback",
    "paused_budget",
    "paused_callback_delivery",
    "paused_policy",
    "paused_operator",
    "resuming",
    "completed",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
    "policy_stopped",
]
TERMINAL_RUN_STATUSES = frozenset({"completed", "succeeded", "failed", "cancelled", "expired", "policy_stopped"})
_TERMINAL_RUN_STATUSES_SQL = tuple(sorted(TERMINAL_RUN_STATUSES))
_TERMINAL_RUN_STATUS_PLACEHOLDERS = ",".join("?" for _ in _TERMINAL_RUN_STATUSES_SQL)
VALID_RUN_STATUSES = frozenset({
    "created",
    "admitted",
    "running",
    "waiting_input",
    "waiting_approval",
    "waiting_review",
    "waiting_callback",
    "paused_budget",
    "paused_callback_delivery",
    "paused_policy",
    "paused_operator",
    "resuming",
    "completed",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
    "policy_stopped",
})
VALID_RUN_INVOCATION_MODES = frozenset({"sync", "accepted", "background"})


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{owner} {field_name} must not contain surrounding whitespace")
    return value


def _validate_optional_non_empty_string(owner: str, field_name: str, value: object | None) -> str | None:
    if value is None:
        return None
    return _validate_non_empty_string(owner, field_name, value)


def _validate_json_object(owner: str, field_name: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{owner} {field_name} must be an object")
    snapshot: dict[str, Any] = {}
    for key, item in value.items():
        key_text = _validate_non_empty_string(owner, f"{field_name} key", key)
        snapshot[key_text] = _validate_json_value(owner, f"{field_name}.{key_text}", item)
    return snapshot


def _validate_json_value(owner: str, path: str, value: object) -> Any:
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{owner} {path} must not contain non-finite numbers")
        return value
    if isinstance(value, list):
        return [_validate_json_value(owner, path, item) for item in value]
    if isinstance(value, dict):
        return _validate_json_object(owner, path, value)
    raise ValueError(f"{owner} {path} must contain only JSON values")


def _loads_strict_json(owner: str, field_name: str, value: str) -> Any:
    try:
        return json.loads(
            value,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
        )
    except ValueError as error:
        raise ValueError(f"{owner} {field_name} must be valid strict JSON") from error


def _dumps_strict_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validate_invocation_mode(owner: str, value: object) -> RunInvocationMode:
    if not isinstance(value, str):
        raise ValueError(f"{owner} invocation_mode must be a string")
    if value not in VALID_RUN_INVOCATION_MODES:
        if owner == "run":
            raise ValueError(f"invalid run invocation mode {value}")
        raise ValueError(f"invalid {owner} invocation_mode {value}")
    return value  # type: ignore[return-value]


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
class RunDeploymentProvenance:
    release_digest: str | None = None
    deployment_revision_id: str | None = None
    physical_plan_hash: str | None = None
    release_signature_digest: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "release_digest",
            "deployment_revision_id",
            "physical_plan_hash",
            "release_signature_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_optional_non_empty_string("run deployment provenance", field_name, getattr(self, field_name)),
            )

    def canonical_value(self) -> dict[str, str | None]:
        return {
            "release_digest": self.release_digest,
            "deployment_revision_id": self.deployment_revision_id,
            "physical_plan_hash": self.physical_plan_hash,
            "release_signature_digest": self.release_signature_digest,
        }

    def validate_for_production(self) -> RunDeploymentProvenance:
        for field_name in (
            "release_digest",
            "deployment_revision_id",
            "physical_plan_hash",
            "release_signature_digest",
        ):
            if getattr(self, field_name) is None:
                raise ValueError(f"production deployment provenance {field_name} is required")
        for field_name in (
            "release_digest",
            "physical_plan_hash",
            "release_signature_digest",
        ):
            value = getattr(self, field_name)
            assert isinstance(value, str)
            digest = value.removeprefix("sha256:")
            if (
                not value.startswith("sha256:")
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(
                    f"production deployment provenance {field_name} must be a canonical sha256 digest"
                )
        return self

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> RunDeploymentProvenance:
        if not isinstance(value, dict):
            raise ValueError("run deployment provenance mapping must be an object")
        return cls(
            release_digest=_validate_optional_non_empty_string(
                "run deployment provenance",
                "release_digest",
                value.get("release_digest"),
            ),
            deployment_revision_id=_validate_optional_non_empty_string(
                "run deployment provenance",
                "deployment_revision_id",
                value.get("deployment_revision_id"),
            ),
            physical_plan_hash=_validate_optional_non_empty_string(
                "run deployment provenance",
                "physical_plan_hash",
                value.get("physical_plan_hash"),
            ),
            release_signature_digest=_validate_optional_non_empty_string(
                "run deployment provenance",
                "release_signature_digest",
                value.get("release_signature_digest"),
            ),
        )


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    graph_hash: str
    inputs: dict[str, Any]
    deployment_provenance: RunDeploymentProvenance = field(default_factory=RunDeploymentProvenance)
    invocation_mode: RunInvocationMode = "sync"
    status: RunStatus = "created"
    state: dict[str, Any] = field(default_factory=dict)
    state_revision: int = 0
    model_visible_tools: tuple[ModelVisibleToolRef, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _validate_non_empty_string("run record", "run_id", self.run_id))
        object.__setattr__(self, "graph_hash", _validate_non_empty_string("run record", "graph_hash", self.graph_hash))
        object.__setattr__(self, "inputs", _validate_json_object("run record", "inputs", self.inputs))
        if not isinstance(self.deployment_provenance, RunDeploymentProvenance):
            raise ValueError("run record deployment_provenance must be RunDeploymentProvenance")
        object.__setattr__(self, "invocation_mode", _validate_invocation_mode("run record", self.invocation_mode))
        if self.status not in VALID_RUN_STATUSES:
            raise ValueError(f"invalid run record status {self.status}")
        object.__setattr__(self, "state", _validate_json_object("run record", "state", self.state))
        if not isinstance(self.state_revision, int) or isinstance(self.state_revision, bool):
            raise ValueError("run record state_revision must be an integer")
        if self.state_revision < 0:
            raise ValueError("run record state_revision must be non-negative")
        tools = tuple(self.model_visible_tools)
        if any(not isinstance(tool, ModelVisibleToolRef) for tool in tools):
            raise ValueError("run record model_visible_tools must be ModelVisibleToolRef")
        object.__setattr__(self, "model_visible_tools", tuple(sorted(tools)))


@dataclass(slots=True)
class InMemoryRunStore:
    runs: dict[str, RunRecord] = field(default_factory=dict)
    next_id: int = 1

    def create_run(
        self,
        graph_hash: str,
        inputs: dict[str, Any],
        *,
        run_id: str | None = None,
        deployment_provenance: RunDeploymentProvenance | None = None,
        invocation_mode: RunInvocationMode = "sync",
        model_visible_tools: Iterable[ModelVisibleToolRef] = (),
    ) -> RunRecord:
        _validate_non_empty_string("run store", "graph_hash", graph_hash)
        inputs = _validate_json_object("run store", "inputs", inputs)
        requested_run_id = _validate_optional_non_empty_string("run store", "run_id", run_id)
        if deployment_provenance is not None and not isinstance(deployment_provenance, RunDeploymentProvenance):
            raise ValueError("run store deployment_provenance must be RunDeploymentProvenance")
        invocation_mode = _validate_invocation_mode("run", invocation_mode)
        if requested_run_id is None:
            while f"run-{self.next_id:06d}" in self.runs:
                self.next_id += 1
            run_id = f"run-{self.next_id:06d}"
        else:
            run_id = requested_run_id
            if run_id in self.runs:
                raise ValueError(f"run store run_id {run_id!r} already exists")
        self.next_id += 1
        record = RunRecord(
            run_id=run_id,
            graph_hash=graph_hash,
            inputs=deepcopy(inputs),
            deployment_provenance=deployment_provenance or RunDeploymentProvenance(),
            invocation_mode=invocation_mode,
            model_visible_tools=tuple(model_visible_tools),
        )
        self.runs[run_id] = record
        return deepcopy(record)

    def get_run(self, run_id: str) -> RunRecord:
        return deepcopy(self.runs[run_id])

    def patch_state(self, run_id: str, patch: dict[str, Any], expected_revision: int) -> RunRecord:
        run_id = _validate_non_empty_string("run store", "run_id", run_id)
        patch = _validate_json_object("run store", "patch", patch)
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ValueError("run store expected_revision must be an integer")
        if expected_revision < 0:
            raise ValueError("run store expected_revision must be non-negative")
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
            deployment_provenance=current.deployment_provenance,
            invocation_mode=current.invocation_mode,
            model_visible_tools=current.model_visible_tools,
            status=current.status,
            state=next_state,
            state_revision=current.state_revision + 1,
        )
        self.runs[run_id] = updated
        return deepcopy(updated)

    def record_model_visible_tools(
        self,
        run_id: str,
        tools: Iterable[ModelVisibleToolRef],
    ) -> RunRecord:
        run_id = _validate_non_empty_string("run store", "run_id", run_id)
        current = self.runs[run_id]
        if current.status in TERMINAL_RUN_STATUSES:
            raise RunTerminalStateError(run_id, current.status)
        updated = RunRecord(
            run_id=current.run_id,
            graph_hash=current.graph_hash,
            inputs=deepcopy(current.inputs),
            deployment_provenance=current.deployment_provenance,
            invocation_mode=current.invocation_mode,
            model_visible_tools=tuple(tools),
            status=current.status,
            state=deepcopy(current.state),
            state_revision=current.state_revision,
        )
        self.runs[run_id] = updated
        return deepcopy(updated)

    def set_status(self, run_id: str, status: MutableRunStatus) -> RunRecord:
        run_id = _validate_non_empty_string("run store", "run_id", run_id)
        if status not in VALID_RUN_STATUSES or status == "created":
            raise ValueError(f"invalid mutable run status {status}")
        current = self.runs[run_id]
        if current.status in TERMINAL_RUN_STATUSES:
            raise RunTerminalStateError(run_id, current.status)
        updated = RunRecord(
            run_id=current.run_id,
            graph_hash=current.graph_hash,
            inputs=deepcopy(current.inputs),
            deployment_provenance=current.deployment_provenance,
            invocation_mode=current.invocation_mode,
            model_visible_tools=current.model_visible_tools,
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
              deployment_provenance_json TEXT NOT NULL,
              invocation_mode TEXT NOT NULL DEFAULT 'sync',
              model_visible_tools_json TEXT NOT NULL,
              status TEXT NOT NULL,
              state_json TEXT NOT NULL,
              state_revision INTEGER NOT NULL
            )
            """
        )
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "deployment_provenance_json" not in columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN deployment_provenance_json TEXT")
            self.connection.execute(
                """
                UPDATE runs
                SET deployment_provenance_json = ?
                WHERE deployment_provenance_json IS NULL
                """,
                (_deployment_provenance_json(RunDeploymentProvenance()),),
            )
        if "model_visible_tools_json" not in columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN model_visible_tools_json TEXT")
            self.connection.execute(
                """
                UPDATE runs
                SET model_visible_tools_json = ?
                WHERE model_visible_tools_json IS NULL
                """,
                (_model_visible_tools_json(()),),
            )
        if "invocation_mode" not in columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN invocation_mode TEXT NOT NULL DEFAULT 'sync'")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def create_run(
        self,
        graph_hash: str,
        inputs: dict[str, Any],
        *,
        run_id: str | None = None,
        deployment_provenance: RunDeploymentProvenance | None = None,
        invocation_mode: RunInvocationMode = "sync",
        model_visible_tools: Iterable[ModelVisibleToolRef] = (),
    ) -> RunRecord:
        _validate_non_empty_string("run store", "graph_hash", graph_hash)
        inputs = _validate_json_object("run store", "inputs", inputs)
        requested_run_id = _validate_optional_non_empty_string("run store", "run_id", run_id)
        if deployment_provenance is not None and not isinstance(deployment_provenance, RunDeploymentProvenance):
            raise ValueError("run store deployment_provenance must be RunDeploymentProvenance")
        invocation_mode = _validate_invocation_mode("run", invocation_mode)
        row = self.connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM runs").fetchone()
        sequence = int(row[0])
        if requested_run_id is None:
            while True:
                run_id = f"run-{sequence:06d}"
                existing = self.connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if existing is None:
                    break
                sequence += 1
        else:
            run_id = requested_run_id
            existing = self.connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"run store run_id {run_id!r} already exists")
        record = RunRecord(
            run_id=run_id,
            graph_hash=graph_hash,
            inputs=deepcopy(inputs),
            deployment_provenance=deployment_provenance or RunDeploymentProvenance(),
            invocation_mode=invocation_mode,
            model_visible_tools=tuple(model_visible_tools),
        )
        self.connection.execute(
            """
            INSERT INTO runs (
              run_id,
              sequence,
              graph_hash,
              inputs_json,
              deployment_provenance_json,
              invocation_mode,
              model_visible_tools_json,
              status,
              state_json,
              state_revision
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.run_id,
                sequence,
                record.graph_hash,
                _dumps_strict_json(record.inputs),
                _deployment_provenance_json(record.deployment_provenance),
                record.invocation_mode,
                _model_visible_tools_json(record.model_visible_tools),
                record.status,
                _dumps_strict_json(record.state),
                record.state_revision,
            ),
        )
        self.connection.commit()
        return deepcopy(record)

    def get_run(self, run_id: str) -> RunRecord:
        run_id = _validate_non_empty_string("run store", "run_id", run_id)
        row = self.connection.execute(
            """
            SELECT
              run_id,
              graph_hash,
              inputs_json,
              deployment_provenance_json,
              invocation_mode,
              model_visible_tools_json,
              status,
              state_json,
              state_revision
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
            inputs=_loads_strict_json("run store", "stored inputs_json", str(row["inputs_json"])),
            deployment_provenance=_parse_deployment_provenance_json(
                str(row["deployment_provenance_json"] or "{}")
            ),
            invocation_mode=str(row["invocation_mode"] or "sync"),
            model_visible_tools=_parse_model_visible_tools_json(
                str(row["model_visible_tools_json"] or "[]")
            ),
            status=row["status"],
            state=_loads_strict_json("run store", "stored state_json", str(row["state_json"])),
            state_revision=int(row["state_revision"]),
        )

    def patch_state(self, run_id: str, patch: dict[str, Any], expected_revision: int) -> RunRecord:
        run_id = _validate_non_empty_string("run store", "run_id", run_id)
        patch = _validate_json_object("run store", "patch", patch)
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ValueError("run store expected_revision must be an integer")
        if expected_revision < 0:
            raise ValueError("run store expected_revision must be non-negative")
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
            f"""
            UPDATE runs
            SET state_json = ?, state_revision = ?
            WHERE run_id = ? AND state_revision = ?
              AND status NOT IN ({_TERMINAL_RUN_STATUS_PLACEHOLDERS})
            """,
            (
                _dumps_strict_json(next_state),
                updated_revision,
                run_id,
                expected_revision,
                *_TERMINAL_RUN_STATUSES_SQL,
            ),
        )
        if cursor.rowcount != 1:
            refreshed = self.get_run(run_id)
            if refreshed.status in TERMINAL_RUN_STATUSES:
                raise RunTerminalStateError(run_id, refreshed.status)
            raise StateConflictError(run_id, expected_revision, refreshed.state_revision)
        self.connection.commit()
        return self.get_run(run_id)

    def record_model_visible_tools(
        self,
        run_id: str,
        tools: Iterable[ModelVisibleToolRef],
    ) -> RunRecord:
        run_id = _validate_non_empty_string("run store", "run_id", run_id)
        current = self.get_run(run_id)
        if current.status in TERMINAL_RUN_STATUSES:
            raise RunTerminalStateError(run_id, current.status)
        cursor = self.connection.execute(
            f"""
            UPDATE runs
            SET model_visible_tools_json = ?
            WHERE run_id = ?
              AND status NOT IN ({_TERMINAL_RUN_STATUS_PLACEHOLDERS})
            """,
            (_model_visible_tools_json(tools), run_id, *_TERMINAL_RUN_STATUSES_SQL),
        )
        if cursor.rowcount != 1:
            refreshed = self.get_run(run_id)
            if refreshed.status in TERMINAL_RUN_STATUSES:
                raise RunTerminalStateError(run_id, refreshed.status)
            raise KeyError(run_id)
        self.connection.commit()
        return self.get_run(run_id)

    def set_status(self, run_id: str, status: MutableRunStatus) -> RunRecord:
        run_id = _validate_non_empty_string("run store", "run_id", run_id)
        if status not in VALID_RUN_STATUSES or status == "created":
            raise ValueError(f"invalid mutable run status {status}")
        current = self.get_run(run_id)
        if current.status in TERMINAL_RUN_STATUSES:
            raise RunTerminalStateError(run_id, current.status)
        cursor = self.connection.execute(
            f"""
            UPDATE runs
            SET status = ?
            WHERE run_id = ?
              AND status NOT IN ({_TERMINAL_RUN_STATUS_PLACEHOLDERS})
            """,
            (status, run_id, *_TERMINAL_RUN_STATUSES_SQL),
        )
        if cursor.rowcount != 1:
            refreshed = self.get_run(run_id)
            if refreshed.status in TERMINAL_RUN_STATUSES:
                raise RunTerminalStateError(run_id, refreshed.status)
            raise KeyError(run_id)
        self.connection.commit()
        return self.get_run(run_id)


def _deployment_provenance_json(provenance: RunDeploymentProvenance) -> str:
    return _dumps_strict_json(provenance.canonical_value())


def _model_visible_tools_json(tools: Iterable[ModelVisibleToolRef]) -> str:
    return _dumps_strict_json(
        [
            {
                "tool_name": tool.tool_name,
                "resolved_tool_id": tool.resolved_tool_id,
                "definition_digest": tool.definition_digest,
                "binding_digest": tool.binding_digest,
                "effective_policy_snapshot_id": tool.effective_policy_snapshot_id,
                "allowed_for_principal": tool.allowed_for_principal,
                "valid_until": tool.valid_until,
            }
            for tool in sorted(tools)
        ]
    )


def _parse_deployment_provenance_json(value: str) -> RunDeploymentProvenance:
    parsed = _loads_strict_json("run store", "stored deployment_provenance_json", value)
    if not isinstance(parsed, dict):
        raise ValueError("run deployment provenance mapping must be an object")
    return RunDeploymentProvenance.from_mapping(parsed)


def _parse_model_visible_tools_json(value: str) -> tuple[ModelVisibleToolRef, ...]:
    parsed = _loads_strict_json("run store", "stored model_visible_tools_json", value)
    if not isinstance(parsed, list):
        raise ValueError("run model visible tools must be a list")
    tools: list[ModelVisibleToolRef] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValueError("run model visible tools items must be objects")
        allowed_for_principal = item.get("allowed_for_principal")
        if not isinstance(allowed_for_principal, bool):
            raise ValueError("run model visible tools allowed_for_principal must be a boolean")
        tools.append(
            ModelVisibleToolRef(
                tool_name=_validate_non_empty_string(
                    "run model visible tools",
                    f"{index}.tool_name",
                    item.get("tool_name"),
                ),
                resolved_tool_id=_validate_non_empty_string(
                    "run model visible tools",
                    f"{index}.resolved_tool_id",
                    item.get("resolved_tool_id"),
                ),
                definition_digest=_validate_non_empty_string(
                    "run model visible tools",
                    f"{index}.definition_digest",
                    item.get("definition_digest"),
                ),
                binding_digest=_validate_non_empty_string(
                    "run model visible tools",
                    f"{index}.binding_digest",
                    item.get("binding_digest"),
                ),
                effective_policy_snapshot_id=_validate_non_empty_string(
                    "run model visible tools",
                    f"{index}.effective_policy_snapshot_id",
                    item.get("effective_policy_snapshot_id"),
                ),
                allowed_for_principal=allowed_for_principal,
                valid_until=(
                    _validate_non_empty_string("run model visible tools", f"{index}.valid_until", item["valid_until"])
                    if item.get("valid_until") is not None
                    else None
                ),
            )
        )
    return tuple(sorted(tools))

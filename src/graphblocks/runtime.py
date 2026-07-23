from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
from threading import Lock
import time
from typing import Any, Callable, Literal, Protocol

from .async_operation import VALID_ASYNC_OPERATION_KINDS
from .canonical import canonical_dumps, canonical_hash, canonical_loads
from .compiler import (
    MAX_NODE_RETRY_ATTEMPTS,
    STATE_CHANGING_TOOL_EFFECTS,
    compile_graph,
)
from .duration import parse_duration_milliseconds, parse_duration_seconds
from .documents import FrozenDict, FrozenList
from .evaluation import ModelVisibleToolRef
from .leases import InMemoryLeasePool
from .plugins import BlockCatalog, builtin_block_catalog
from .run_store import InMemoryRunStore, RunDeploymentProvenance
from .tools import (
    BlockToolImplementation,
    GraphToolImplementation,
    McpToolImplementation,
    OpenApiToolImplementation,
    RemoteToolImplementation,
    ToolBinding,
    ToolCatalog,
    ToolDefinition,
    ToolResolutionScope,
)

JournalKind = Literal[
    "run_started",
    "run_waiting_callback",
    "external_callback_received",
    "run_resuming",
    "node_started",
    "node_retry",
    "node_succeeded",
    "node_completed",
    "node_failed",
    "run_succeeded",
    "run_failed",
    "run_cancelled",
]
LocalJournalKind = Literal[
    "run_started",
    "node_started",
    "node_retry",
    "node_succeeded",
    "node_failed",
    "run_succeeded",
    "run_failed",
    "run_cancelled",
]
LocalTerminalJournalKind = Literal[
    "run_succeeded",
    "run_failed",
    "run_cancelled",
]
_LOCAL_JOURNAL_KINDS = frozenset(
    {
        "run_started",
        "node_started",
        "node_retry",
        "node_succeeded",
        "node_failed",
        "run_succeeded",
        "run_failed",
        "run_cancelled",
    }
)
_LOCAL_TERMINAL_JOURNAL_KINDS = frozenset(
    {"run_succeeded", "run_failed", "run_cancelled"}
)
_JOURNAL_KINDS = _LOCAL_JOURNAL_KINDS | frozenset(
    {
        "run_waiting_callback",
        "external_callback_received",
        "run_resuming",
        "node_completed",
    }
)
_TERMINAL_JOURNAL_KINDS = frozenset(
    {"run_succeeded", "run_failed", "run_cancelled"}
)
BlockCallable = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]
MAX_U64 = (1 << 64) - 1


class JournalLike(Protocol):
    @property
    def records(self) -> Sequence[JournalRecord]:
        ...

    @property
    def terminal_kind(self) -> JournalKind | None:
        ...

    def append(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        ...

    def append_terminal(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        ...


JournalFactory = Callable[[str], JournalLike]


class JournalStateError(RuntimeError):
    pass


def _configured_retry_attempts(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        if value > MAX_NODE_RETRY_ATTEMPTS:
            raise ValueError(
                f"node retry attempts must not exceed {MAX_NODE_RETRY_ATTEMPTS}"
            )
        return max(value, 1)
    return 1


def _freeze_json_like(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenDict(
            {
                key: _freeze_json_like(nested)
                for key, nested in value.items()
            }
        )
    if isinstance(value, list):
        return FrozenList(_freeze_json_like(nested) for nested in value)
    if isinstance(value, tuple):
        return FrozenList(_freeze_json_like(nested) for nested in value)
    return value


def _mutable_json_like(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _mutable_json_like(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json_like(nested) for nested in value]
    if isinstance(value, list):
        return [_mutable_json_like(nested) for nested in value]
    return value


def _loads_strict_json(owner: str, value: str) -> Any:
    try:
        return canonical_loads(value)
    except ValueError as error:
        raise ValueError(f"{owner} must be valid strict JSON") from error


def _dumps_strict_json(owner: str, value: Any) -> str:
    try:
        return canonical_dumps(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{owner} must be valid strict JSON") from error


@dataclass(slots=True)
class CancellationToken:
    cancelled: bool = False
    reason: str | None = None

    def cancel(self, reason: str = "cancelled") -> None:
        if self.cancelled:
            return
        self.cancelled = True
        self.reason = reason


@dataclass(slots=True)
class _DeadlineCancellationToken:
    parent: CancellationToken
    deadline_monotonic: float
    deadline_reason: str

    @property
    def cancelled(self) -> bool:
        return self.parent.cancelled or time.perf_counter() >= self.deadline_monotonic

    @property
    def reason(self) -> str | None:
        if self.parent.cancelled:
            return self.parent.reason
        if time.perf_counter() >= self.deadline_monotonic:
            return self.deadline_reason
        return None

    def cancel(self, reason: str = "cancelled") -> None:
        self.parent.cancel(reason)


@dataclass(frozen=True, slots=True)
class JournalRecord:
    sequence: int
    kind: JournalKind
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 1
        ):
            raise ValueError("execution journal sequence must be a positive integer")
        if not isinstance(self.kind, str) or self.kind not in _JOURNAL_KINDS:
            raise ValueError(f"unsupported journal kind {self.kind!r}")
        if not isinstance(self.payload, Mapping):
            raise TypeError("execution journal payload must be a mapping")
        snapshot = _loads_strict_json(
            "execution journal payload",
            _dumps_strict_json("execution journal payload", self.payload),
        )
        if not isinstance(snapshot, dict):
            raise TypeError("execution journal payload must be a mapping")
        object.__setattr__(self, "payload", _freeze_json_like(snapshot))

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "payload": _mutable_json_like(self.payload),
        }


@dataclass(frozen=True, slots=True)
class ExecutionJournal:
    run_id: str
    records: tuple[JournalRecord, ...] = field(default_factory=tuple)
    terminal_kind: JournalKind | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.run_id, str)
            or not self.run_id.strip()
            or self.run_id != self.run_id.strip()
        ):
            raise ValueError("execution journal run id must be an exact nonempty string")
        if isinstance(self.records, (str, bytes, bytearray, Mapping)):
            raise ValueError(
                "execution journal records must be JournalRecord values"
            )
        try:
            records = tuple(self.records)
        except TypeError as error:
            raise ValueError(
                "execution journal records must be JournalRecord values"
            ) from error
        if any(not isinstance(record, JournalRecord) for record in records):
            raise ValueError("execution journal records must be JournalRecord values")
        for expected_sequence, record in enumerate(records, start=1):
            if record.sequence != expected_sequence:
                raise JournalStateError(
                    "execution journal record sequences must be contiguous"
                )
        terminal_records = [
            record for record in records if record.kind in _TERMINAL_JOURNAL_KINDS
        ]
        if len(terminal_records) > 1:
            raise JournalStateError(
                "execution journal must not contain multiple terminal records"
            )
        if terminal_records and terminal_records[0] is not records[-1]:
            raise JournalStateError(
                "execution journal terminal record must be last"
            )
        inferred_terminal = (
            terminal_records[0].kind if terminal_records else None
        )
        if self.terminal_kind is not None:
            if (
                not isinstance(self.terminal_kind, str)
                or self.terminal_kind not in _TERMINAL_JOURNAL_KINDS
            ):
                raise ValueError(
                    f"journal terminal kind is invalid: {self.terminal_kind!r}"
                )
            if self.terminal_kind != inferred_terminal:
                raise JournalStateError(
                    "execution journal terminal_kind must match its terminal record"
                )
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "terminal_kind", inferred_terminal)

    def append(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        if kind not in _JOURNAL_KINDS:
            raise ValueError(f"unsupported journal kind {kind!r}")
        if kind in _TERMINAL_JOURNAL_KINDS:
            raise JournalStateError(
                f"terminal journal kind {kind!r} must be recorded with append_terminal"
            )
        if self.terminal_kind is not None:
            raise JournalStateError(f"cannot append {kind} after terminal {self.terminal_kind}")
        record = JournalRecord(len(self.records) + 1, kind, payload)
        object.__setattr__(self, "records", (*self.records, record))
        return record

    def append_terminal(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        if kind not in _TERMINAL_JOURNAL_KINDS:
            raise ValueError(f"journal terminal kind is invalid: {kind!r}")
        if self.terminal_kind is not None:
            raise JournalStateError(f"terminal already recorded as {self.terminal_kind}")
        record = JournalRecord(len(self.records) + 1, kind, payload)
        object.__setattr__(self, "records", (*self.records, record))
        object.__setattr__(self, "terminal_kind", kind)
        return record


@dataclass(frozen=True, slots=True)
class LocalJournalRecord:
    """One stable C1 execution-journal record."""

    sequence: int
    kind: LocalJournalKind
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 1
        ):
            raise ValueError("local journal sequence must be a positive integer")
        if self.kind not in _LOCAL_JOURNAL_KINDS:
            raise ValueError(f"unsupported local journal kind {self.kind!r}")
        if not isinstance(self.payload, Mapping):
            raise TypeError("local journal payload must be a mapping")
        object.__setattr__(self, "payload", _freeze_json_like(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "payload": _mutable_json_like(self.payload),
        }


@dataclass(frozen=True, slots=True)
class LocalExecutionJournal:
    """In-memory journal restricted to stable C1 lifecycle events."""

    run_id: str
    records: tuple[LocalJournalRecord, ...] = field(default_factory=tuple, init=False)
    terminal_kind: LocalTerminalJournalKind | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("local journal run id must be a nonempty string")

    def append(
        self,
        kind: LocalJournalKind,
        payload: dict[str, Any],
    ) -> LocalJournalRecord:
        if kind not in _LOCAL_JOURNAL_KINDS:
            raise ValueError(f"unsupported local journal kind {kind!r}")
        if kind in _LOCAL_TERMINAL_JOURNAL_KINDS:
            raise JournalStateError(
                f"terminal local journal kind {kind!r} must be recorded with append_terminal"
            )
        if self.terminal_kind is not None:
            raise JournalStateError(
                f"cannot append {kind} after terminal {self.terminal_kind}"
            )
        record = LocalJournalRecord(len(self.records) + 1, kind, payload)
        object.__setattr__(self, "records", (*self.records, record))
        return record

    def append_terminal(
        self,
        kind: LocalTerminalJournalKind,
        payload: dict[str, Any],
    ) -> LocalJournalRecord:
        if kind not in _LOCAL_TERMINAL_JOURNAL_KINDS:
            raise ValueError(f"local terminal journal kind is invalid: {kind!r}")
        if self.terminal_kind is not None:
            raise JournalStateError(
                f"terminal already recorded as {self.terminal_kind}"
            )
        record = LocalJournalRecord(len(self.records) + 1, kind, payload)
        object.__setattr__(self, "records", (*self.records, record))
        object.__setattr__(self, "terminal_kind", kind)
        return record


@dataclass(slots=True)
class SQLiteExecutionJournal:
    path: Path | str
    run_id: str
    connection: sqlite3.Connection = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.run_id, str)
            or not self.run_id.strip()
            or self.run_id != self.run_id.strip()
        ):
            raise ValueError(
                "SQLite execution journal run id must be an exact nonempty string"
            )
        self.path = Path(self.path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS journal_records (
              run_id TEXT NOT NULL,
              sequence INTEGER NOT NULL,
              kind TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              terminal INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (run_id, sequence)
            )
            """
        )
        self.connection.commit()

    def _columns(self) -> set[str]:
        return {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(journal_records)").fetchall()
        }

    def _sequence_column(self) -> str:
        columns = self._columns()
        if "sequence" in columns:
            return "sequence"
        if "run_sequence" in columns:
            return "run_sequence"
        raise JournalStateError("journal_records must include sequence or run_sequence")

    @property
    def terminal_kind(self) -> JournalKind | None:
        sequence_column = self._sequence_column()
        row = self.connection.execute(
            f"""
            SELECT kind FROM journal_records
            WHERE run_id = ? AND terminal = 1
            ORDER BY {sequence_column} DESC
            LIMIT 1
            """,
            (self.run_id,),
        ).fetchone()
        return None if row is None else row["kind"]

    @property
    def records(self) -> list[JournalRecord]:
        sequence_column = self._sequence_column()
        rows = self.connection.execute(
            f"""
            SELECT {sequence_column} AS sequence, kind, payload_json FROM journal_records
            WHERE run_id = ?
            ORDER BY {sequence_column}
            """,
            (self.run_id,),
        ).fetchall()
        return [
            JournalRecord(
                int(row["sequence"]),
                row["kind"],
                _loads_strict_json("execution journal payload_json", str(row["payload_json"]))
                if row["payload_json"] is not None
                else {},
            )
            for row in rows
        ]

    def _append_in_transaction(
        self,
        kind: JournalKind,
        payload: dict[str, Any],
        *,
        terminal: bool,
    ) -> JournalRecord:
        if kind not in _JOURNAL_KINDS:
            raise ValueError(f"unsupported journal kind {kind!r}")
        if terminal and kind not in _TERMINAL_JOURNAL_KINDS:
            raise ValueError(f"journal terminal kind is invalid: {kind!r}")
        if not terminal and kind in _TERMINAL_JOURNAL_KINDS:
            raise JournalStateError(
                f"terminal journal kind {kind!r} must be recorded with append_terminal"
            )
        terminal_kind = self.terminal_kind
        if terminal_kind is not None:
            action = "record terminal" if terminal else f"append {kind}"
            raise JournalStateError(f"cannot {action} after terminal {terminal_kind}")
        sequence_column = self._sequence_column()
        row = self.connection.execute(
            f"SELECT COALESCE(MAX({sequence_column}), 0) + 1 FROM journal_records WHERE run_id = ?",
            (self.run_id,),
        ).fetchone()
        sequence = int(row[0])
        payload_json = _dumps_strict_json("execution journal payload", payload)
        columns = self._columns()
        if "record_id" in columns:
            self.connection.execute(
                """
                INSERT INTO journal_records (
                  run_id,
                  run_sequence,
                  record_id,
                  kind,
                  causation_id,
                  node_id,
                  attempt_id,
                  lease_epoch,
                  payload_json,
                  terminal
                )
                VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    self.run_id,
                    sequence,
                    f"{self.run_id}:{sequence}",
                    kind,
                    payload_json,
                    int(terminal),
                ),
            )
        else:
            self.connection.execute(
                """
                INSERT INTO journal_records (run_id, sequence, kind, payload_json, terminal)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self.run_id, sequence, kind, payload_json, int(terminal)),
            )
        return JournalRecord(sequence, kind, dict(payload))

    def append(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            record = self._append_in_transaction(kind, payload, terminal=False)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return record

    def append_terminal(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            record = self._append_in_transaction(kind, payload, terminal=True)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return record

    def close(self) -> None:
        self.connection.close()


@dataclass(frozen=True, slots=True)
class RuntimeCheckpoint:
    checkpoint_id: str
    run_id: str
    graph_hash: str
    wait_node: str
    remaining_nodes: tuple[str, ...]
    inputs: Mapping[str, object]
    node_outputs: Mapping[str, object]
    output_values: Mapping[str, object]
    operation: Mapping[str, object]
    state_digest: str

    def __post_init__(self) -> None:
        for field_name in (
            "checkpoint_id",
            "run_id",
            "graph_hash",
            "wait_node",
            "state_digest",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"runtime checkpoint {field_name} must be a non-empty string"
                )
            if value != value.strip():
                raise ValueError(
                    f"runtime checkpoint {field_name} must not contain surrounding whitespace"
                )
        if isinstance(
            self.remaining_nodes,
            (str, bytes, bytearray, Mapping),
        ):
            raise ValueError(
                "runtime checkpoint remaining_nodes must contain exact non-empty strings"
            )
        try:
            remaining_nodes = tuple(self.remaining_nodes)
        except TypeError as error:
            raise ValueError(
                "runtime checkpoint remaining_nodes must contain exact non-empty strings"
            ) from error
        if any(
            not isinstance(node, str)
            or not node.strip()
            or node != node.strip()
            for node in remaining_nodes
        ):
            raise ValueError(
                "runtime checkpoint remaining_nodes must contain exact non-empty strings"
            )
        if len(set(remaining_nodes)) != len(remaining_nodes):
            raise ValueError(
                "runtime checkpoint remaining_nodes must not contain duplicates"
            )
        if self.wait_node not in remaining_nodes:
            raise ValueError(
                "runtime checkpoint wait_node must be present in remaining_nodes"
            )
        object.__setattr__(self, "remaining_nodes", tuple(sorted(remaining_nodes)))
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.state_digest) is None:
            raise ValueError(
                "runtime checkpoint state_digest must be a canonical sha256 digest"
            )
        for field_name in ("inputs", "node_outputs", "output_values", "operation"):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise ValueError(
                    f"runtime checkpoint {field_name} must be a JSON object"
                )
            try:
                snapshot = canonical_loads(canonical_dumps(_mutable_json_like(value)))
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"runtime checkpoint {field_name} must contain only JSON values"
                ) from error
            object.__setattr__(self, field_name, _freeze_json_like(snapshot))
        operation_run_id = self.operation.get("run_id")
        if operation_run_id != self.run_id:
            raise ValueError(
                "runtime checkpoint operation run_id must match checkpoint run_id"
            )
        for field_name in (
            "operation_id",
            "run_id",
            "node_id",
            "attempt_id",
            "kind",
            "resume_token_hash",
            "idempotency_key",
            "expected_schema",
        ):
            value = self.operation.get(field_name)
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError(
                    f"runtime checkpoint operation {field_name} must be an exact non-empty string"
                )
        if self.operation.get("state") != "waiting_callback":
            raise ValueError(
                "runtime checkpoint operation state must be waiting_callback"
            )
        checkpoint_node_names = set(self.remaining_nodes) | set(self.node_outputs)
        if self.operation["node_id"] not in checkpoint_node_names:
            raise ValueError(
                "runtime checkpoint operation node_id must belong to checkpoint graph state"
            )
        if self.operation["kind"] not in VALID_ASYNC_OPERATION_KINDS:
            raise ValueError(
                "runtime checkpoint operation kind must be a valid async operation kind"
            )
        resume_token_hash = self.operation["resume_token_hash"]
        if re.fullmatch(r"sha256:[0-9a-f]{64}", resume_token_hash) is None:
            raise ValueError(
                "runtime checkpoint operation resume_token_hash must be a canonical sha256 digest"
            )
        for field_name in (
            "provider_operation_id",
            "infinite_wait_policy",
        ):
            value = self.operation.get(field_name)
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError(
                    f"runtime checkpoint operation {field_name} must be an exact non-empty string"
                )
        timestamps: dict[str, int | None] = {}
        for field_name in (
            "created_at_unix_ms",
            "submitted_at_unix_ms",
            "expires_at_unix_ms",
            "completed_at_unix_ms",
        ):
            value = self.operation.get(field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > MAX_U64
            ):
                raise ValueError(
                    f"runtime checkpoint operation {field_name} must be an unsigned 64-bit integer"
                )
            timestamps[field_name] = value
        created_at_unix_ms = timestamps["created_at_unix_ms"]
        submitted_at_unix_ms = timestamps["submitted_at_unix_ms"]
        if created_at_unix_ms is None:
            raise ValueError(
                "runtime checkpoint operation created_at_unix_ms must be an unsigned 64-bit integer"
            )
        if submitted_at_unix_ms is None:
            raise ValueError(
                "runtime checkpoint operation submitted_at_unix_ms must be an unsigned 64-bit integer"
            )
        if submitted_at_unix_ms < created_at_unix_ms:
            raise ValueError(
                "runtime checkpoint operation submitted_at_unix_ms must not precede created_at_unix_ms"
            )
        expires_at_unix_ms = timestamps["expires_at_unix_ms"]
        if (
            expires_at_unix_ms is not None
            and expires_at_unix_ms <= submitted_at_unix_ms
        ):
            raise ValueError(
                "runtime checkpoint operation expires_at_unix_ms must be after submitted_at_unix_ms"
            )
        if timestamps["completed_at_unix_ms"] is not None:
            raise ValueError(
                "runtime checkpoint waiting operation must not have completed_at_unix_ms"
            )
        infinite_wait_policy = self.operation.get("infinite_wait_policy")
        if expires_at_unix_ms is None and infinite_wait_policy is None:
            raise ValueError(
                "runtime checkpoint waiting operation requires expires_at_unix_ms or infinite_wait_policy"
            )
        if expires_at_unix_ms is not None and infinite_wait_policy is not None:
            raise ValueError(
                "runtime checkpoint waiting operation must not define both expires_at_unix_ms and infinite_wait_policy"
            )
        if self.content_digest() != self.state_digest:
            raise ValueError(
                "runtime checkpoint state does not match the issuing runtime"
            )

    def to_json(self) -> dict[str, object]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "graph_hash": self.graph_hash,
            "wait_node": self.wait_node,
            "remaining_nodes": list(self.remaining_nodes),
            "inputs": _mutable_json_like(self.inputs),
            "node_outputs": _mutable_json_like(self.node_outputs),
            "output_values": _mutable_json_like(self.output_values),
            "operation": _mutable_json_like(self.operation),
            "state_digest": self.state_digest,
        }

    def content_digest(self) -> str:
        return canonical_hash(
            {
                "checkpoint_id": self.checkpoint_id,
                "run_id": self.run_id,
                "graph_hash": self.graph_hash,
                "wait_node": self.wait_node,
                "remaining_nodes": list(self.remaining_nodes),
                "inputs": _mutable_json_like(self.inputs),
                "node_outputs": _mutable_json_like(self.node_outputs),
                "output_values": _mutable_json_like(self.output_values),
                "operation": _mutable_json_like(self.operation),
            }
        )


class CallbackReceiptVerifier(Protocol):
    """Trusted boundary for authorizing a callback receipt before resume."""

    def __call__(
        self,
        receipt: Mapping[str, object],
        *,
        checkpoint: RuntimeCheckpoint,
        expected_checkpoint_digest: str,
        expected_release_digest: str,
    ) -> bool:
        ...


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    status: Literal["succeeded", "failed", "cancelled", "waiting_callback"]
    outputs: Mapping[str, Any]
    journal: JournalLike
    checkpoint: RuntimeCheckpoint | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.run_id, str)
            or not self.run_id.strip()
            or self.run_id != self.run_id.strip()
        ):
            raise ValueError("runtime result run_id must be an exact non-empty string")
        if self.status not in {
            "succeeded",
            "failed",
            "cancelled",
            "waiting_callback",
        }:
            raise ValueError(f"invalid runtime result status {self.status!r}")
        if not isinstance(self.outputs, Mapping):
            raise TypeError("runtime result outputs must be a mapping")
        try:
            output_snapshot = canonical_loads(
                canonical_dumps(_mutable_json_like(self.outputs))
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                "runtime result outputs must contain only JSON values"
            ) from error
        if not isinstance(output_snapshot, dict):
            raise TypeError("runtime result outputs must be a mapping")
        object.__setattr__(self, "outputs", _freeze_json_like(output_snapshot))
        if self.checkpoint is not None and not isinstance(
            self.checkpoint,
            RuntimeCheckpoint,
        ):
            raise TypeError("runtime result checkpoint must be a RuntimeCheckpoint")
        if self.status == "waiting_callback" and self.checkpoint is None:
            raise ValueError(
                "waiting_callback runtime result requires a checkpoint"
            )
        if self.status != "waiting_callback" and self.checkpoint is not None:
            raise ValueError(
                "terminal runtime result must not retain a checkpoint"
            )
        if self.checkpoint is not None and self.checkpoint.run_id != self.run_id:
            raise ValueError("runtime result and checkpoint run ids must match")
        journal_run_id = getattr(self.journal, "run_id", None)
        if journal_run_id is not None and journal_run_id != self.run_id:
            raise ValueError("runtime result and journal run ids must match")
        expected_terminal_kind = {
            "succeeded": "run_succeeded",
            "failed": "run_failed",
            "cancelled": "run_cancelled",
            "waiting_callback": None,
        }[self.status]
        if getattr(self.journal, "terminal_kind", None) != expected_terminal_kind:
            raise ValueError(
                "runtime result status must match its terminal journal record"
            )


@dataclass(frozen=True, slots=True)
class LocalRunResult:
    """Terminal result exposed by the stable C1-only local runtime facade."""

    run_id: str
    status: Literal["succeeded", "failed", "cancelled"]
    outputs: Mapping[str, Any]
    journal: LocalExecutionJournal

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"invalid local result status {self.status!r}")
        if not isinstance(self.outputs, Mapping):
            raise TypeError("local result outputs must be a mapping")
        if not isinstance(self.journal, LocalExecutionJournal):
            raise TypeError("local result journal must be LocalExecutionJournal")
        if self.journal.run_id != self.run_id:
            raise ValueError("local result and journal run ids must match")
        expected_terminal_kind: LocalTerminalJournalKind
        if self.status == "succeeded":
            expected_terminal_kind = "run_succeeded"
        elif self.status == "failed":
            expected_terminal_kind = "run_failed"
        else:
            expected_terminal_kind = "run_cancelled"
        if self.journal.terminal_kind != expected_terminal_kind:
            raise ValueError(
                "local result status must match its terminal journal record"
            )
        object.__setattr__(self, "outputs", _freeze_json_like(self.outputs))


@dataclass(slots=True)
class RuntimeRegistry:
    blocks: dict[str, BlockCallable] = field(default_factory=dict)
    block_catalog: BlockCatalog = field(default_factory=lambda: BlockCatalog({}))
    allow_untyped: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.allow_untyped, bool):
            raise TypeError("allow_untyped must be a boolean")
        if self.allow_untyped:
            return
        undeclared = sorted(set(self.blocks) - set(self.block_catalog.descriptors))
        if undeclared:
            raise ValueError(
                "runtime blocks are not declared in the block catalog: "
                + ", ".join(undeclared)
            )

    def register(self, block_id: str, block: BlockCallable) -> None:
        if block_id in self.blocks:
            raise ValueError(f"runtime block {block_id!r} is already registered")
        if not self.allow_untyped and self.block_catalog.get(block_id) is None:
            raise ValueError(
                f"runtime block {block_id!r} is not declared in the block catalog"
            )
        self.blocks[block_id] = block

    def replace(self, block_id: str, block: BlockCallable) -> None:
        if block_id not in self.blocks:
            raise ValueError(f"runtime block {block_id!r} is not registered")
        if not self.allow_untyped and self.block_catalog.get(block_id) is None:
            raise ValueError(
                f"runtime block {block_id!r} is not declared in the block catalog"
            )
        self.blocks[block_id] = block

    def compilation_catalog(self) -> BlockCatalog:
        if not self.allow_untyped:
            if self.block_catalog.allow_unknown_blocks:
                return BlockCatalog(
                    self.block_catalog.descriptors,
                    allow_unknown_blocks=False,
                )
            return self.block_catalog
        if self.block_catalog.allow_unknown_blocks:
            return self.block_catalog
        return BlockCatalog(
            self.block_catalog.descriptors,
            allow_unknown_blocks=True,
        )

    def resolve(self, block_id: str) -> BlockCallable:
        if not self.allow_untyped and self.block_catalog.get(block_id) is None:
            raise ValueError(
                f"runtime block {block_id!r} is not declared in the block catalog"
            )
        return self.blocks[block_id]


@dataclass(slots=True)
class InProcessRuntime:
    """Preview runtime with explicit trust injection for callback continuation."""

    registry: RuntimeRegistry
    run_store: InMemoryRunStore | None = None
    cancellation_token: CancellationToken | None = None
    journal_factory: JournalFactory | None = None
    lease_pool: InMemoryLeasePool | None = None
    callback_receipt_verifier: CallbackReceiptVerifier | None = field(
        default=None,
        repr=False,
    )
    _checkpoint_state_digests: dict[str, str] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _checkpoint_lock: Lock = field(
        default_factory=Lock,
        init=False,
        repr=False,
    )
    _next_checkpoint_sequence: int = field(
        default=1,
        init=False,
        repr=False,
    )

    def run(
        self,
        graph: dict[str, Any],
        inputs: dict[str, Any],
        run_id: str = "run-000001",
        deployment_provenance: RunDeploymentProvenance | None = None,
        *,
        checkpoint: RuntimeCheckpoint | None = None,
        callback_receipt: Mapping[str, object] | None = None,
    ) -> RunResult:
        if deployment_provenance is not None and not isinstance(
            deployment_provenance,
            RunDeploymentProvenance,
        ):
            raise ValueError("deployment_provenance must be RunDeploymentProvenance")
        if deployment_provenance is not None:
            deployment_provenance.validate_for_production()
        plan = compile_graph(
            graph,
            block_catalog=self.registry.compilation_catalog(),
            allow_unknown_blocks=self.registry.allow_untyped,
        )
        errors = [item for item in plan.diagnostics.diagnostics if item.severity == "error"]
        if errors:
            message = "; ".join(f"{item.code} {item.path}: {item.message}" for item in errors)
            raise ValueError(message)

        normalized = plan.normalized
        if checkpoint is not None and not isinstance(checkpoint, RuntimeCheckpoint):
            raise ValueError("runtime checkpoint must be RuntimeCheckpoint")
        if checkpoint is None and callback_receipt is not None:
            raise ValueError("runtime callback_receipt requires a checkpoint")
        expected_checkpoint_digest: str | None = None
        if checkpoint is not None:
            if checkpoint.run_id != run_id:
                raise ValueError("runtime checkpoint run_id must match requested run_id")
            if checkpoint.graph_hash != plan.graph_hash:
                raise ValueError("runtime checkpoint graph_hash must match compiled graph")
            if not isinstance(callback_receipt, Mapping):
                raise ValueError("runtime checkpoint resume requires callback_receipt")
            with self._checkpoint_lock:
                expected_checkpoint_digest = self._checkpoint_state_digests.get(
                    checkpoint.checkpoint_id
                )
            if (
                expected_checkpoint_digest is None
                or checkpoint.content_digest() != checkpoint.state_digest
                or checkpoint.state_digest != expected_checkpoint_digest
            ):
                raise ValueError(
                    "runtime checkpoint state does not match the issuing runtime"
                )
            if canonical_dumps(inputs) != canonical_dumps(
                _mutable_json_like(checkpoint.inputs)
            ):
                raise ValueError(
                    "runtime checkpoint inputs must match original run inputs"
                )
        if self.run_store is not None and checkpoint is None:
            stored = self.run_store.create_run(
                plan.graph_hash,
                inputs,
                run_id=run_id,
                deployment_provenance=deployment_provenance,
            )
            run_id = stored.run_id
            self.run_store.set_status(run_id, "running")
        spec = normalized.get("spec", {})
        nodes = spec.get("nodes", {})
        edges = spec.get("edges", [])
        if checkpoint is not None:
            node_names = set(nodes)
            remaining_node_names = set(checkpoint.remaining_nodes)
            if not remaining_node_names.issubset(node_names):
                raise ValueError(
                    "runtime checkpoint remaining_nodes must belong to compiled graph"
                )
            wait_node = nodes.get(checkpoint.wait_node)
            if (
                not isinstance(wait_node, Mapping)
                or wait_node.get("block") != "async.await_callback@1"
            ):
                raise ValueError(
                    "runtime checkpoint wait_node must be async.await_callback@1"
                )
            if set(checkpoint.node_outputs) != node_names - remaining_node_names:
                raise ValueError(
                    "runtime checkpoint completed node outputs must match remaining nodes"
                )
        journal = self.journal_factory(run_id) if self.journal_factory is not None else ExecutionJournal(run_id)
        if checkpoint is None:
            run_started_payload: dict[str, Any] = {"graphHash": plan.graph_hash}
            if deployment_provenance is not None:
                run_started_payload["deploymentProvenance"] = deployment_provenance.canonical_value()
            journal.append("run_started", run_started_payload)

        node_inputs: dict[str, dict[str, Any]] = {name: {} for name in nodes}
        node_outputs: dict[str, dict[str, Any]] = {}
        output_values: dict[str, Any] = {}
        remaining = set(nodes)
        if checkpoint is None:
            for edge in edges:
                if not (
                    isinstance(edge, dict)
                    and isinstance(edge.get("from"), str)
                    and isinstance(edge.get("to"), str)
                    and edge["from"].startswith("$input.")
                    and edge["to"].startswith("$output.")
                ):
                    continue
                value: Any = inputs
                for part in edge["from"].partition(".")[2].split("."):
                    value = value[part]
                current = output_values
                parts = edge["to"].partition(".")[2].split(".")
                for part in parts[:-1]:
                    nested = current.setdefault(part, {})
                    if not isinstance(nested, dict):
                        raise RuntimeError(f"output path conflict at {edge['to']}")
                    current = nested
                current[parts[-1]] = value
        context = {
            "run_id": run_id,
            "turn_id": "turn-000001",
            "conversation_id": "conversation-default",
            "cancellation_token": self.cancellation_token or CancellationToken(),
            "lease_pool": self.lease_pool,
            "run_store": self.run_store,
            "deployment_provenance": deployment_provenance,
        }
        if checkpoint is not None:
            assert callback_receipt is not None
            operation = _mutable_json_like(checkpoint.operation)
            assert isinstance(operation, dict)
            receipt = _mutable_json_like(callback_receipt)
            if not isinstance(receipt, dict):
                raise ValueError("runtime callback_receipt must be a JSON object")
            assert expected_checkpoint_digest is not None
            expected_release_digest = (
                deployment_provenance.release_digest
                if deployment_provenance is not None
                and deployment_provenance.release_digest is not None
                else plan.graph_hash
            )
            verifier = self.callback_receipt_verifier
            if verifier is None:
                raise ValueError(
                    "runtime checkpoint resume requires a trusted "
                    "callback_receipt_verifier"
                )
            frozen_receipt = _freeze_json_like(receipt)
            assert isinstance(frozen_receipt, Mapping)
            try:
                receipt_verified = verifier(
                    frozen_receipt,
                    checkpoint=checkpoint,
                    expected_checkpoint_digest=expected_checkpoint_digest,
                    expected_release_digest=expected_release_digest,
                )
            except Exception as error:
                raise ValueError(
                    "runtime callback_receipt trusted verifier failed"
                ) from error
            if receipt_verified is not True:
                raise ValueError(
                    "runtime callback_receipt was rejected by the trusted verifier"
                )
            verified_by = receipt.get("verified_by")
            if (
                not isinstance(verified_by, str)
                or not verified_by.strip()
                or verified_by != verified_by.strip()
                or verified_by == "unauthenticated"
            ):
                raise ValueError(
                    "runtime callback_receipt verified_by must identify an authenticated principal"
                )
            for field_name in ("operation_id", "run_id", "node_id", "attempt_id"):
                if receipt.get(field_name) != operation.get(field_name):
                    raise ValueError(
                        f"runtime callback_receipt {field_name} must match checkpoint operation"
                    )
            if receipt.get("provider_operation_id") != operation.get(
                "provider_operation_id"
            ):
                raise ValueError(
                    "runtime callback_receipt provider_operation_id must match checkpoint operation"
                )
            for receipt_field, operation_field in (
                ("operation_idempotency_key", "idempotency_key"),
                ("resume_token_hash", "resume_token_hash"),
                ("schema_id", "expected_schema"),
            ):
                if receipt.get(receipt_field) != operation.get(operation_field):
                    raise ValueError(
                        f"runtime callback_receipt {receipt_field} must match checkpoint operation"
                    )
            callback_idempotency_key = receipt.get(
                "callback_idempotency_key"
            )
            if (
                not isinstance(callback_idempotency_key, str)
                or not callback_idempotency_key.strip()
                or callback_idempotency_key != callback_idempotency_key.strip()
            ):
                raise ValueError(
                    "runtime callback_receipt callback_idempotency_key must be an exact non-empty string"
                )
            if receipt.get("schema_validated") is not True:
                raise ValueError(
                    "runtime callback_receipt must carry successful schema validation evidence"
                )
            callback_payload = receipt.get("payload")
            if not isinstance(callback_payload, Mapping):
                raise ValueError(
                    "runtime callback_receipt payload must be a JSON object"
                )
            try:
                callback_payload = canonical_loads(canonical_dumps(callback_payload))
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "runtime callback_receipt payload must contain only JSON values"
                ) from error
            if receipt.get("payload_digest") != canonical_hash(callback_payload):
                raise ValueError(
                    "runtime callback_receipt payload_digest must match payload"
                )
            received_at_unix_ms = receipt.get("received_at_unix_ms")
            if (
                not isinstance(received_at_unix_ms, int)
                or isinstance(received_at_unix_ms, bool)
                or received_at_unix_ms < 1
            ):
                raise ValueError(
                    "runtime callback_receipt received_at_unix_ms must be a positive integer"
                )
            submitted_at_unix_ms = operation.get("submitted_at_unix_ms")
            if (
                isinstance(submitted_at_unix_ms, int)
                and not isinstance(submitted_at_unix_ms, bool)
                and received_at_unix_ms < submitted_at_unix_ms
            ):
                raise ValueError(
                    "runtime callback_receipt must not precede operation submission"
                )
            expires_at_unix_ms = operation.get("expires_at_unix_ms")
            if (
                isinstance(expires_at_unix_ms, int)
                and not isinstance(expires_at_unix_ms, bool)
                and received_at_unix_ms >= expires_at_unix_ms
            ):
                raise ValueError(
                    "runtime callback_receipt must be before operation expiration"
                )
            resume_admission = receipt.get("resume_admission")
            if not isinstance(resume_admission, Mapping):
                raise ValueError(
                    "runtime callback_receipt requires policy, budget, release, and ownership resume admission"
                )
            if (
                resume_admission.get("contract")
                == "graphblocks.trusted-callback-resume-admission.v1"
            ):
                ownership = resume_admission.get("ownership")
                schema_verification = resume_admission.get(
                    "schema_verification"
                )
                required_admission_strings = (
                    "authentication_decision_id",
                    "policy_decision_id",
                    "budget_reservation_id",
                    "compatible_release_digest",
                    "run_id",
                    "operation_id",
                    "node_id",
                    "attempt_id",
                    "checkpoint_id",
                    "checkpoint_state_digest",
                )
                required_ownership_strings = (
                    "owner_id",
                    "lease_id",
                    "fence_token",
                )
                required_schema_strings = (
                    "verification_id",
                    "schema_id",
                    "payload_digest",
                    "verified_by",
                )
                admission_strings_valid = all(
                    isinstance(resume_admission.get(field_name), str)
                    and bool(resume_admission[field_name])
                    and resume_admission[field_name]
                    == resume_admission[field_name].strip()
                    for field_name in required_admission_strings
                )
                ownership_strings_valid = isinstance(
                    ownership, Mapping
                ) and all(
                    isinstance(ownership.get(field_name), str)
                    and bool(ownership[field_name])
                    and ownership[field_name] == ownership[field_name].strip()
                    for field_name in required_ownership_strings
                )
                schema_strings_valid = isinstance(
                    schema_verification, Mapping
                ) and all(
                    isinstance(schema_verification.get(field_name), str)
                    and bool(schema_verification[field_name])
                    and schema_verification[field_name]
                    == schema_verification[field_name].strip()
                    for field_name in required_schema_strings
                )
                fencing_epoch = (
                    ownership.get("fencing_epoch")
                    if isinstance(ownership, Mapping)
                    else None
                )
                if (
                    resume_admission.get("outcome") != "authorized"
                    or not admission_strings_valid
                    or not ownership_strings_valid
                    or not schema_strings_valid
                    or not isinstance(fencing_epoch, int)
                    or isinstance(fencing_epoch, bool)
                    or fencing_epoch < 1
                    or resume_admission.get("compatible_release_digest")
                    != expected_release_digest
                    or resume_admission.get("run_id") != run_id
                    or resume_admission.get("operation_id")
                    != operation.get("operation_id")
                    or resume_admission.get("node_id")
                    != operation.get("node_id")
                    or resume_admission.get("attempt_id")
                    != operation.get("attempt_id")
                    or resume_admission.get("checkpoint_id")
                    != checkpoint.checkpoint_id
                    or resume_admission.get("checkpoint_state_digest")
                    != expected_checkpoint_digest
                    or not isinstance(schema_verification, Mapping)
                    or schema_verification.get("schema_id")
                    != receipt.get("schema_id")
                    or schema_verification.get("payload_digest")
                    != receipt.get("payload_digest")
                    or schema_verification.get("verified_by") != verified_by
                ):
                    raise ValueError(
                        "runtime callback_receipt trusted resume admission is invalid"
                    )
            else:
                required_resume_admission = {
                    "policy_reevaluated",
                    "budget_reserved",
                    "release_compatible",
                    "ownership_fenced",
                }
                if any(
                    resume_admission.get(field_name) is not True
                    for field_name in required_resume_admission
                ):
                    raise ValueError(
                        "runtime callback_receipt requires policy, budget, release, and ownership resume admission"
                    )
            node_outputs = {
                str(node): _mutable_json_like(output)
                for node, output in checkpoint.node_outputs.items()
            }
            output_values = _mutable_json_like(checkpoint.output_values)
            assert isinstance(output_values, dict)
            remaining = set(checkpoint.remaining_nodes)
            if checkpoint.wait_node not in remaining:
                raise ValueError(
                    "runtime checkpoint wait_node must remain pending"
                )
            operation["state"] = "resuming"
            wait_result = {
                "wait": {
                    "state": "resumed",
                    "operation": operation,
                    "checkpoint": False,
                },
                "callback": callback_payload,
                "operation": operation,
            }
            with self._checkpoint_lock:
                claimed_checkpoint_digest = self._checkpoint_state_digests.pop(
                    checkpoint.checkpoint_id,
                    None,
                )
            if claimed_checkpoint_digest != expected_checkpoint_digest:
                raise ValueError(
                    "runtime checkpoint state does not match the issuing runtime"
                )
            try:
                descriptor = self.registry.block_catalog.get(
                    "async.await_callback@1"
                )
                if descriptor is not None:
                    declared_outputs = {
                        port.name for port in descriptor.outputs
                    }
                    unexpected_outputs = sorted(
                        set(wait_result) - declared_outputs
                    )
                    if unexpected_outputs:
                        raise TypeError(
                            "async.await_callback@1 returned undeclared output(s): "
                            + ", ".join(unexpected_outputs)
                        )
                    missing_outputs = sorted(
                        port.name
                        for port in descriptor.outputs
                        if port.required_for(
                            wait_node.get("config", {}),
                            phase="resumed",
                        )
                        and port.name not in wait_result
                    )
                    if missing_outputs:
                        raise TypeError(
                            "async.await_callback@1 omitted required output(s): "
                            + ", ".join(missing_outputs)
                        )
                node_outputs[checkpoint.wait_node] = wait_result
                for edge in edges:
                    if not (
                        isinstance(edge, dict)
                        and isinstance(edge.get("from"), str)
                        and isinstance(edge.get("to"), str)
                        and edge["from"].split(".", 1)[0]
                        == checkpoint.wait_node
                        and edge["to"].startswith("$output.")
                    ):
                        continue
                    value: Any = wait_result
                    source_path = edge["from"].partition(".")[2]
                    if source_path:
                        for part in source_path.split("."):
                            value = value[part]
                    target_path = edge["to"].partition(".")[2]
                    current = output_values
                    parts = target_path.split(".")
                    for part in parts[:-1]:
                        nested = current.setdefault(part, {})
                        if not isinstance(nested, dict):
                            raise RuntimeError(
                                f"output path conflict at {edge['to']}"
                            )
                        current = nested
                    current[parts[-1]] = value
            except Exception as exc:
                journal.append(
                    "node_failed",
                    {
                        "node": checkpoint.wait_node,
                        "error": str(exc),
                        "attempt": 1,
                    },
                )
                journal.append_terminal(
                    "run_failed",
                    {"node": checkpoint.wait_node, "error": str(exc)},
                )
                if self.run_store is not None:
                    self.run_store.set_status(run_id, "failed")
                if self.lease_pool is not None:
                    self.lease_pool.release_all(run_id)
                return RunResult(run_id, "failed", output_values, journal)
            remaining.remove(checkpoint.wait_node)
            if self.run_store is not None:
                try:
                    self.run_store.set_status(run_id, "resuming")
                except Exception:
                    with self._checkpoint_lock:
                        self._checkpoint_state_digests.setdefault(
                            checkpoint.checkpoint_id,
                            claimed_checkpoint_digest,
                        )
                    raise
            journal.append(
                "external_callback_received",
                {
                    "operationId": operation.get("operation_id"),
                    "callbackIdempotencyKey": callback_idempotency_key,
                    "payloadDigest": receipt.get("payload_digest"),
                    "verifiedBy": verified_by,
                },
            )
            journal.append(
                "run_resuming",
                {
                    "operationId": operation.get("operation_id"),
                    "node": checkpoint.wait_node,
                },
            )
            journal.append(
                "node_succeeded",
                {
                    "node": checkpoint.wait_node,
                    "outputs": sorted(wait_result),
                },
            )

        while remaining:
            token = context["cancellation_token"]
            if isinstance(token, CancellationToken) and token.cancelled:
                journal.append_terminal("run_cancelled", {"reason": token.reason})
                if self.run_store is not None:
                    self.run_store.set_status(run_id, "cancelled")
                if self.lease_pool is not None:
                    self.lease_pool.release_all(run_id)
                return RunResult(run_id, "cancelled", output_values, journal)
            progressed = False
            for node_name in sorted(remaining):
                node = nodes[node_name]
                guard = node.get("when")
                if isinstance(guard, str):
                    guard_owner, _, guard_path = guard.partition(".")
                    guard_ready = True
                    if guard_owner == "$input":
                        guard_value: Any = inputs
                    elif guard_owner in node_outputs:
                        guard_value = node_outputs[guard_owner]
                    else:
                        guard_ready = False
                        guard_value = None
                    if guard_ready:
                        for part in guard_path.split("."):
                            if isinstance(guard_value, dict) and part in guard_value:
                                guard_value = guard_value[part]
                            else:
                                guard_ready = False
                                break
                    if not guard_ready:
                        continue
                    if not isinstance(guard_value, bool):
                        error = f"node {node_name!r} when guard must resolve to a boolean"
                        journal.append("node_failed", {"node": node_name, "error": error, "attempt": 0})
                        journal.append_terminal("run_failed", {"node": node_name, "error": error})
                        if self.run_store is not None:
                            self.run_store.set_status(run_id, "failed")
                        if self.lease_pool is not None:
                            self.lease_pool.release_all(run_id)
                        return RunResult(run_id, "failed", output_values, journal)
                    if not guard_value:
                        node_outputs[node_name] = {}
                        journal.append(
                            "node_succeeded",
                            {
                                "node": node_name,
                                "outputs": [],
                                "skipped": True,
                                "reason": "condition_false",
                            },
                        )
                        remaining.remove(node_name)
                        progressed = True
                        break
                inbound = [
                    edge
                    for edge in edges
                    if isinstance(edge, dict)
                    and isinstance(edge.get("to"), str)
                    and edge["to"].split(".", 1)[0] == node_name
                ]
                ready = True
                resolved_inputs: dict[str, Any] = {}
                for edge in inbound:
                    source = edge["from"]
                    source_owner, _, source_path = source.partition(".")
                    if source_owner == "$input":
                        value: Any = inputs
                        if source_path:
                            for part in source_path.split("."):
                                if isinstance(value, dict) and part in value:
                                    value = value[part]
                                else:
                                    ready = False
                                    break
                        if not ready:
                            break
                    elif source_owner in node_outputs:
                        value = node_outputs[source_owner]
                        source_value_missing = False
                        if source_path:
                            for part in source_path.split("."):
                                if isinstance(value, dict) and part in value:
                                    value = value[part]
                                else:
                                    source_value_missing = True
                                    break
                            if source_value_missing:
                                target_optional = False
                                target_descriptor = self.registry.block_catalog.get(
                                    str(node.get("block"))
                                )
                                if target_descriptor is not None:
                                    target_path = edge["to"].partition(".")[2]
                                    target_port_name = target_path.split(".", 1)[0]
                                    target_port = next(
                                        (
                                            port
                                            for port in target_descriptor.inputs
                                            if port.name == target_port_name
                                        ),
                                        None,
                                    )
                                    target_config = node.get("config", {})
                                    if not isinstance(target_config, Mapping):
                                        target_config = {}
                                    target_optional = (
                                        target_port is not None
                                        and not target_port.required_for(
                                            target_config,
                                            phase="initial",
                                        )
                                    )
                                if target_optional:
                                    continue
                                ready = False
                                break
                        if not ready:
                            break
                    else:
                        ready = False
                        break

                    _, _, target_path = edge["to"].partition(".")
                    if not target_path:
                        ready = False
                        break
                    current = resolved_inputs
                    parts = target_path.split(".")
                    for part in parts[:-1]:
                        next_value = current.setdefault(part, {})
                        if not isinstance(next_value, dict):
                            ready = False
                            break
                        current = next_value
                    if not ready:
                        break
                    current[parts[-1]] = value

                if not ready:
                    continue

                block_id = str(node["block"])
                flow = node.get("flow", {})
                retry = flow.get("retry", {}) if isinstance(flow, dict) else {}
                timeout_seconds = parse_duration_seconds(flow.get("timeout")) if isinstance(flow, dict) else None
                max_attempts = 1
                idempotency_key = None
                if isinstance(retry, dict):
                    max_attempts = _configured_retry_attempts(
                        retry.get("maxAttempts", retry.get("max_attempts", 1))
                    )
                    idempotency_key = retry.get("idempotencyKey") or retry.get("idempotency_key")
                else:
                    max_attempts = _configured_retry_attempts(retry)
                if not (
                    isinstance(idempotency_key, str)
                    and bool(idempotency_key.strip())
                    and idempotency_key == idempotency_key.strip()
                ):
                    idempotency_key = None
                    effects = node.get("effects", [])
                    if isinstance(effects, str):
                        effects = [effects]
                    if isinstance(effects, list) and STATE_CHANGING_TOOL_EFFECTS & {
                        str(effect) for effect in effects
                    }:
                        max_attempts = 1
                result: dict[str, Any] | None = None
                for attempt in range(1, max_attempts + 1):
                    started_payload: dict[str, Any] = {"node": node_name, "block": block_id, "attempt": attempt}
                    if idempotency_key is not None:
                        started_payload["idempotencyKey"] = str(idempotency_key)
                    journal.append("node_started", started_payload)
                    try:
                        block = self.registry.resolve(block_id)
                        merged_inputs = canonical_loads(
                            _dumps_strict_json(
                                f"{block_id} input",
                                {**node_inputs[node_name], **resolved_inputs},
                            )
                        )
                        if not isinstance(merged_inputs, dict):
                            raise TypeError("block received non-mapping input")
                        started_at = time.perf_counter()
                        deadline = None if timeout_seconds is None else started_at + timeout_seconds
                        timeout_reason = f"node {node_name!r} exceeded timeout {flow.get('timeout')}"
                        run_token = context["cancellation_token"]
                        attempt_token = (
                            run_token
                            if deadline is None
                            else _DeadlineCancellationToken(
                                parent=run_token,
                                deadline_monotonic=deadline,
                                deadline_reason=timeout_reason,
                            )
                        )
                        attempt_context = {
                            **context,
                            "node": node_name,
                            "attempt": attempt,
                            "deadline_monotonic": deadline,
                            "cancellation_token": attempt_token,
                        }
                        if idempotency_key is not None:
                            attempt_context["idempotency_key"] = str(idempotency_key)
                            attempt_context["idempotencyKey"] = str(idempotency_key)
                        attempt_result = block(
                            merged_inputs,
                            node.get("config", {}),
                            attempt_context,
                        )
                        if deadline is not None and time.perf_counter() >= deadline:
                            raise TimeoutError(timeout_reason)
                        if not isinstance(attempt_result, dict):
                            raise TypeError("block returned non-mapping output")
                        attempt_result = canonical_loads(
                            _dumps_strict_json(
                                f"{block_id} output",
                                attempt_result,
                            )
                        )
                        if not isinstance(attempt_result, dict):
                            raise TypeError("block returned non-mapping output")
                        descriptor = self.registry.block_catalog.get(block_id)
                        if descriptor is not None:
                            declared_outputs = {
                                port.name for port in descriptor.outputs
                            }
                            unexpected_outputs = sorted(
                                set(attempt_result) - declared_outputs
                            )
                            if unexpected_outputs:
                                raise TypeError(
                                    f"{block_id} returned undeclared output(s): "
                                    + ", ".join(unexpected_outputs)
                                )
                            missing_outputs = sorted(
                                port.name
                                for port in descriptor.outputs
                                if port.required_for(
                                    node.get("config", {}),
                                    phase="initial",
                                )
                                and port.name not in attempt_result
                            )
                            if missing_outputs:
                                raise TypeError(
                                    f"{block_id} omitted required output(s): "
                                    + ", ".join(missing_outputs)
                                )
                        result = attempt_result
                        break
                    except Exception as exc:
                        token = context["cancellation_token"]
                        if isinstance(token, CancellationToken) and token.cancelled:
                            journal.append_terminal(
                                "run_cancelled",
                                {"reason": token.reason, "node": node_name, "attempt": attempt},
                            )
                            if self.run_store is not None:
                                self.run_store.set_status(run_id, "cancelled")
                            if self.lease_pool is not None:
                                self.lease_pool.release_all(run_id)
                            return RunResult(run_id, "cancelled", output_values, journal)
                        if attempt < max_attempts:
                            retry_payload: dict[str, Any] = {
                                "node": node_name,
                                "block": block_id,
                                "attempt": attempt,
                                "error": str(exc),
                            }
                            if idempotency_key is not None:
                                retry_payload["idempotencyKey"] = str(idempotency_key)
                            journal.append(
                                "node_retry",
                                retry_payload,
                            )
                            continue
                        journal.append("node_failed", {"node": node_name, "error": str(exc), "attempt": attempt})
                        journal.append_terminal("run_failed", {"node": node_name, "error": str(exc)})
                        if self.run_store is not None:
                            self.run_store.set_status(run_id, "failed")
                        if self.lease_pool is not None:
                            self.lease_pool.release_all(run_id)
                        return RunResult(run_id, "failed", output_values, journal)

                wait_descriptor = result.get("wait")
                if (
                    block_id == "async.await_callback@1"
                    and isinstance(wait_descriptor, Mapping)
                    and wait_descriptor.get("state") == "waiting_callback"
                    and wait_descriptor.get("checkpoint") is True
                ):
                    checkpoint_id: str | None = None
                    try:
                        operation = wait_descriptor.get("operation")
                        if not isinstance(operation, Mapping):
                            raise ValueError(
                                "async callback wait checkpoint requires operation object"
                            )
                        with self._checkpoint_lock:
                            checkpoint_sequence = self._next_checkpoint_sequence
                            self._next_checkpoint_sequence += 1
                        checkpoint_id = (
                            f"{run_id}:{node_name}:{checkpoint_sequence}"
                        )
                        checkpoint_inputs = canonical_loads(canonical_dumps(inputs))
                        checkpoint_remaining_nodes = tuple(sorted(remaining))
                        checkpoint_node_outputs = canonical_loads(
                            canonical_dumps(node_outputs)
                        )
                        checkpoint_output_values = canonical_loads(
                            canonical_dumps(output_values)
                        )
                        checkpoint_operation = canonical_loads(
                            canonical_dumps(dict(operation))
                        )
                        checkpoint_state_digest = canonical_hash(
                            {
                                "checkpoint_id": checkpoint_id,
                                "run_id": run_id,
                                "graph_hash": plan.graph_hash,
                                "wait_node": node_name,
                                "remaining_nodes": list(
                                    checkpoint_remaining_nodes
                                ),
                                "inputs": checkpoint_inputs,
                                "node_outputs": checkpoint_node_outputs,
                                "output_values": checkpoint_output_values,
                                "operation": checkpoint_operation,
                            }
                        )
                        runtime_checkpoint = RuntimeCheckpoint(
                            checkpoint_id=checkpoint_id,
                            run_id=run_id,
                            graph_hash=plan.graph_hash,
                            wait_node=node_name,
                            remaining_nodes=checkpoint_remaining_nodes,
                            inputs=checkpoint_inputs,
                            node_outputs=checkpoint_node_outputs,
                            output_values=checkpoint_output_values,
                            operation=checkpoint_operation,
                            state_digest=checkpoint_state_digest,
                        )
                        with self._checkpoint_lock:
                            self._checkpoint_state_digests[
                                checkpoint_id
                            ] = checkpoint_state_digest
                        if self.run_store is not None:
                            self.run_store.set_status(run_id, "waiting_callback")
                        journal.append(
                            "run_waiting_callback",
                            {
                                "operationId": operation.get("operation_id"),
                                "node": node_name,
                                "graphHash": plan.graph_hash,
                            },
                        )
                        return RunResult(
                            run_id,
                            "waiting_callback",
                            dict(output_values),
                            journal,
                            runtime_checkpoint,
                        )
                    except Exception as exc:
                        if checkpoint_id is not None:
                            with self._checkpoint_lock:
                                self._checkpoint_state_digests.pop(checkpoint_id, None)
                        journal.append(
                            "node_failed",
                            {"node": node_name, "error": str(exc), "attempt": attempt},
                        )
                        journal.append_terminal(
                            "run_failed",
                            {"node": node_name, "error": str(exc)},
                        )
                        if self.run_store is not None:
                            self.run_store.set_status(run_id, "failed")
                        if self.lease_pool is not None:
                            self.lease_pool.release_all(run_id)
                        return RunResult(run_id, "failed", output_values, journal)

                node_outputs[node_name] = result
                try:
                    for edge in edges:
                        if not (
                            isinstance(edge, dict)
                            and isinstance(edge.get("from"), str)
                            and isinstance(edge.get("to"), str)
                            and edge["from"].split(".", 1)[0] == node_name
                            and edge["to"].startswith("$output.")
                        ):
                            continue
                        value = result
                        source_path = edge["from"].partition(".")[2]
                        if source_path:
                            for part in source_path.split("."):
                                value = value[part]
                        target_path = edge["to"].partition(".")[2]
                        current = output_values
                        parts = target_path.split(".")
                        for part in parts[:-1]:
                            nested = current.setdefault(part, {})
                            if not isinstance(nested, dict):
                                raise RuntimeError(f"output path conflict at {edge['to']}")
                            current = nested
                        current[parts[-1]] = value
                    journal.append(
                        "node_succeeded",
                        {"node": node_name, "outputs": sorted(result)},
                    )
                except Exception as exc:
                    journal.append(
                        "node_failed",
                        {"node": node_name, "error": str(exc), "attempt": attempt},
                    )
                    journal.append_terminal(
                        "run_failed",
                        {"node": node_name, "error": str(exc)},
                    )
                    if self.run_store is not None:
                        self.run_store.set_status(run_id, "failed")
                    if self.lease_pool is not None:
                        self.lease_pool.release_all(run_id)
                    return RunResult(run_id, "failed", output_values, journal)
                remaining.remove(node_name)
                progressed = True
                break

            if not progressed:
                unresolved = ", ".join(sorted(remaining))
                journal.append_terminal("run_failed", {"error": f"unresolved dependencies: {unresolved}"})
                if self.run_store is not None:
                    self.run_store.set_status(run_id, "failed")
                if self.lease_pool is not None:
                    self.lease_pool.release_all(run_id)
                return RunResult(run_id, "failed", output_values, journal)

        token = context["cancellation_token"]
        if isinstance(token, CancellationToken) and token.cancelled:
            journal.append_terminal("run_cancelled", {"reason": token.reason})
            if self.run_store is not None:
                self.run_store.set_status(run_id, "cancelled")
            if self.lease_pool is not None:
                self.lease_pool.release_all(run_id)
            return RunResult(run_id, "cancelled", output_values, journal)
        journal.append_terminal("run_succeeded", {"outputs": output_values})
        if self.run_store is not None:
            self.run_store.set_status(run_id, "succeeded")
        if self.lease_pool is not None:
            self.lease_pool.release_all(run_id)
        return RunResult(run_id, "succeeded", output_values, journal)


@dataclass(slots=True)
class LocalRuntime:
    """C1 local runtime facade without checkpoint, callback, or provenance APIs."""

    registry: RuntimeRegistry
    cancellation_token: CancellationToken | None = None

    def run(
        self,
        graph: dict[str, Any],
        inputs: dict[str, Any],
        run_id: str = "run-000001",
    ) -> LocalRunResult:
        result = InProcessRuntime(
            self.registry,
            cancellation_token=self.cancellation_token,
        ).run(graph, inputs, run_id)
        if result.status == "waiting_callback":
            raise RuntimeError(
                "LocalRuntime does not support callback continuation; "
                "use the preview InProcessRuntime API"
            )
        if not isinstance(result.journal, ExecutionJournal):
            raise RuntimeError("LocalRuntime requires the in-memory execution journal")
        journal = LocalExecutionJournal(result.journal.run_id)
        for record in result.journal.records:
            if record.kind in {"run_succeeded", "run_failed", "run_cancelled"}:
                journal.append_terminal(record.kind, _mutable_json_like(record.payload))
            elif record.kind in {
                "run_started",
                "node_started",
                "node_retry",
                "node_succeeded",
                "node_failed",
            }:
                journal.append(record.kind, _mutable_json_like(record.payload))
            else:
                raise RuntimeError(
                    f"LocalRuntime encountered preview journal event {record.kind!r}"
                )
        return LocalRunResult(
            run_id=result.run_id,
            status=result.status,
            outputs=result.outputs,
            journal=journal,
        )


def _stdlib_registry(
    *,
    allow_untyped: bool,
    included_block_ids: frozenset[str] | None,
) -> RuntimeRegistry:
    from .stdlib_governance import GOVERNANCE_BLOCKS
    from .stdlib_rag import RAG_BLOCKS

    catalog = builtin_block_catalog(
        profile="stable" if included_block_ids is not None else "preview"
    )
    if included_block_ids is not None:
        descriptors = {
            block_id: catalog.descriptors[block_id]
            for block_id in sorted(included_block_ids)
        }
        catalog = BlockCatalog(descriptors)
    registry = RuntimeRegistry(
        block_catalog=catalog,
        allow_untyped=allow_untyped,
    )

    def begin_turn(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        conversation = inputs.get("conversation")
        if isinstance(conversation, Mapping):
            snapshot = dict(conversation)
            conversation_id = str(
                inputs.get("conversationId")
                or conversation.get("conversationId")
                or conversation.get("conversation_id")
                or conversation.get("id")
                or config.get("conversationId")
                or context["conversation_id"]
            )
        else:
            conversation_id = str(
                inputs.get("conversationId")
                or conversation
                or config.get("conversationId")
                or context["conversation_id"]
            )
            snapshot = {"conversationId": conversation_id}
        snapshot["conversationId"] = conversation_id
        messages = snapshot.get("messages", [])
        if not isinstance(messages, list):
            raise TypeError("conversation.begin_turn@1 input 'conversation.messages' must be a list")
        messages = list(messages)
        if "message" in inputs:
            messages.append(inputs["message"])
        snapshot["messages"] = messages
        transaction = {
            "conversationId": conversation_id,
            "turnId": context["turn_id"],
        }
        if "message" in inputs:
            transaction["message"] = inputs["message"]
        return {
            "transaction": transaction,
            "snapshot": snapshot,
            "conversation": snapshot,
            "turn": transaction,
        }

    def prompt_render(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        template = str(config.get("template", "{message.text}"))

        def replace(match: re.Match[str]) -> str:
            value: Any = inputs
            for part in match.group(1).split("."):
                if isinstance(value, dict):
                    value = value[part]
                else:
                    value = getattr(value, part)
            return str(value)

        return {"prompt": re.sub(r"\{([A-Za-z0-9_.]+)\}", replace, template)}

    def scripted_generate(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        prompt = str(inputs.get("prompt", ""))
        script = config.get("script", {})
        if isinstance(script, dict) and prompt in script:
            text = str(script[prompt])
        else:
            text = str(config.get("response", prompt))
        return {"response": text}

    def resolve_tools(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        definitions = []
        definition_configs = config.get("definitions", [])
        if not isinstance(definition_configs, list | tuple):
            raise TypeError("tools.resolve@1 config.definitions must be a sequence")
        for index, item in enumerate(definition_configs):
            if not isinstance(item, dict):
                raise TypeError("tools.resolve@1 config.definitions entries must be mappings")
            definitions.append(
                ToolDefinition(
                    name=_required_string(item, "name", "name", f"config.definitions[{index}].name"),
                    description=_string_with_default(
                        item,
                        "description",
                        "description",
                        "",
                        f"config.definitions[{index}].description",
                    ),
                    input_schema=_required_string(
                        item,
                        "inputSchema",
                        "input_schema",
                        f"config.definitions[{index}].inputSchema",
                    ),
                    output_schema=_optional_string(
                        item,
                        "outputSchema",
                        "output_schema",
                        f"config.definitions[{index}].outputSchema",
                    ),
                    tags=_string_collection(item.get("tags", ()), f"config.definitions[{index}].tags"),
                    version=_optional_string(item, "version", "version", f"config.definitions[{index}].version"),
                )
            )

        bindings = []
        binding_configs = config.get("bindings", [])
        if not isinstance(binding_configs, list | tuple):
            raise TypeError("tools.resolve@1 config.bindings must be a sequence")
        for index, item in enumerate(binding_configs):
            if not isinstance(item, dict):
                raise TypeError("tools.resolve@1 config.bindings entries must be mappings")
            implementation_config = item.get("implementation")
            if not isinstance(implementation_config, dict):
                raise TypeError("tools.resolve@1 binding implementation must be a mapping")
            kind = _required_string(
                implementation_config,
                "kind",
                "kind",
                f"config.bindings[{index}].implementation.kind",
            )
            if kind == "block":
                implementation = BlockToolImplementation(
                    block=_required_string(
                        implementation_config,
                        "block",
                        "block",
                        f"config.bindings[{index}].implementation.block",
                    ),
                    input_mapping=_string_mapping(
                        implementation_config,
                        "inputMapping",
                        "input_mapping",
                        f"config.bindings[{index}].implementation.inputMapping",
                    ),
                    output_mapping=_string_mapping(
                        implementation_config,
                        "outputMapping",
                        "output_mapping",
                        f"config.bindings[{index}].implementation.outputMapping",
                    ),
                )
            elif kind == "graph":
                implementation = GraphToolImplementation(
                    graph=_required_string(
                        implementation_config,
                        "graph",
                        "graph",
                        f"config.bindings[{index}].implementation.graph",
                    ),
                    input_mapping=_string_mapping(
                        implementation_config,
                        "inputMapping",
                        "input_mapping",
                        f"config.bindings[{index}].implementation.inputMapping",
                    ),
                    output_mapping=_string_mapping(
                        implementation_config,
                        "outputMapping",
                        "output_mapping",
                        f"config.bindings[{index}].implementation.outputMapping",
                    ),
                )
            elif kind == "remote":
                implementation = RemoteToolImplementation(
                    connection=_required_string(
                        implementation_config,
                        "connection",
                        "connection",
                        f"config.bindings[{index}].implementation.connection",
                    ),
                    operation=_required_string(
                        implementation_config,
                        "operation",
                        "operation",
                        f"config.bindings[{index}].implementation.operation",
                    ),
                )
            elif kind == "mcp":
                implementation = McpToolImplementation(
                    server=_required_string(
                        implementation_config,
                        "server",
                        "server",
                        f"config.bindings[{index}].implementation.server",
                    ),
                    remote_name=_required_string(
                        implementation_config,
                        "remoteName",
                        "remote_name",
                        f"config.bindings[{index}].implementation.remoteName",
                    ),
                )
            elif kind == "openapi":
                implementation = OpenApiToolImplementation(
                    connection=_required_string(
                        implementation_config,
                        "connection",
                        "connection",
                        f"config.bindings[{index}].implementation.connection",
                    ),
                    operation_id=_required_string(
                        implementation_config,
                        "operationId",
                        "operation_id",
                        f"config.bindings[{index}].implementation.operationId",
                    ),
                )
            else:
                raise TypeError(f"tools.resolve@1 unsupported implementation kind {kind!r}")
            timeout_ms = item.get("timeoutMs", item.get("timeout_ms"))
            if timeout_ms is not None and (
                not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 0
            ):
                raise TypeError(
                    f"tools.resolve@1 config.bindings[{index}].timeoutMs must be a non-negative integer"
                )
            bindings.append(
                ToolBinding(
                    binding_id=_required_string(
                        item,
                        "bindingId",
                        "binding_id",
                        f"config.bindings[{index}].bindingId",
                    ),
                    tool_name=_required_string(
                        item,
                        "toolName",
                        "tool_name",
                        f"config.bindings[{index}].toolName",
                    ),
                    implementation=implementation,
                    effects=_string_collection(item.get("effects", ()), f"config.bindings[{index}].effects"),
                    approval=_string_with_default(
                        item,
                        "approval",
                        "approval",
                        "policy",
                        f"config.bindings[{index}].approval",
                    ),
                    idempotency=_string_with_default(
                        item,
                        "idempotency",
                        "idempotency",
                        "optional",
                        f"config.bindings[{index}].idempotency",
                    ),
                    cancellation=_string_with_default(
                        item,
                        "cancellation",
                        "cancellation",
                        "cooperative",
                        f"config.bindings[{index}].cancellation",
                    ),
                    result_mode=_string_with_default(
                        item,
                        "resultMode",
                        "result_mode",
                        "value",
                        f"config.bindings[{index}].resultMode",
                    ),
                    timeout_ms=timeout_ms,
                    retry_policy_ref=_optional_string(
                        item,
                        "retryPolicyRef",
                        "retry_policy_ref",
                        f"config.bindings[{index}].retryPolicyRef",
                    ),
                    policy_profile_ref=_optional_string(
                        item,
                        "policyProfileRef",
                        "policy_profile_ref",
                        f"config.bindings[{index}].policyProfileRef",
                    ),
                    execution_class=_optional_string(
                        item,
                        "executionClass",
                        "execution_class",
                        f"config.bindings[{index}].executionClass",
                    ),
                )
            )

        scope_config = config.get("scope", {})
        if not isinstance(scope_config, dict):
            raise TypeError("tools.resolve@1 config.scope must be a mapping")
        scope = ToolResolutionScope(
            application_tools=_string_set(scope_config, "applicationTools", "application_tools"),
            graph_tools=_string_set(scope_config, "graphTools", "graph_tools"),
            principal_tools=_string_set(scope_config, "principalTools", "principal_tools"),
            tenant_policy_tools=_string_set(scope_config, "tenantPolicyTools", "tenant_policy_tools"),
            conversation_policy_tools=_string_set(
                scope_config,
                "conversationPolicyTools",
                "conversation_policy_tools",
            ),
            data_classification_tools=_string_set(
                scope_config,
                "dataClassificationTools",
                "data_classification_tools",
            ),
            deployment_tools=_string_set(scope_config, "deploymentTools", "deployment_tools"),
            budget_tools=_string_set(scope_config, "budgetTools", "budget_tools"),
        )
        policy_snapshot = inputs.get("policySnapshot")
        effective_policy_snapshot_id = str(config.get("effectivePolicySnapshotId") or "policy-snapshot-local")
        if isinstance(policy_snapshot, dict):
            effective_policy_snapshot_id = str(
                policy_snapshot.get("snapshot_id")
                or policy_snapshot.get("snapshotId")
                or effective_policy_snapshot_id
            )
        resolved = ToolCatalog(tuple(definitions), tuple(bindings)).resolve(
            scope,
            effective_policy_snapshot_id=effective_policy_snapshot_id,
        )
        return {
            "tools": [
                {
                    "resolved_tool_id": tool.resolved_tool_id,
                    "definition": tool.definition.model_contract(),
                    "binding": tool.binding.binding_contract(),
                    "definition_digest": tool.definition_digest,
                    "binding_digest": tool.binding_digest,
                    "effective_policy_snapshot_id": tool.effective_policy_snapshot_id,
                    "allowed_for_principal": tool.allowed_for_principal,
                    "valid_until": tool.valid_until,
                }
                for tool in resolved
            ]
        }

    def scripted_agent_run(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        tools = inputs.get("tools", [])
        if not isinstance(tools, list):
            raise TypeError("agent.run@1 input 'tools' must be a list")
        model_visible_tools: list[dict[str, Any]] = []
        provenance_tools: list[ModelVisibleToolRef] = []
        for index, tool in enumerate(tools):
            if not isinstance(tool, dict):
                raise TypeError(f"agent.run@1 input 'tools[{index}]' must be a mapping")
            definition = tool.get("definition")
            if not isinstance(definition, dict):
                raise TypeError(f"agent.run@1 input 'tools[{index}].definition' must be a mapping")
            tool_name = definition.get("name")
            if not isinstance(tool_name, str):
                raise TypeError(f"agent.run@1 input 'tools[{index}].definition.name' must be a string")
            if not tool_name.strip():
                raise TypeError(f"agent.run@1 input 'tools[{index}].definition.name' must not be empty")
            resolved_tool_id = tool.get("resolved_tool_id", tool.get("resolvedToolId"))
            if not isinstance(resolved_tool_id, str):
                raise TypeError(f"agent.run@1 input 'tools[{index}].resolved_tool_id' must be a string")
            if not resolved_tool_id.strip():
                raise TypeError(f"agent.run@1 input 'tools[{index}].resolved_tool_id' must not be empty")
            definition_digest = tool.get("definition_digest", tool.get("definitionDigest"))
            if not isinstance(definition_digest, str):
                raise TypeError(f"agent.run@1 input 'tools[{index}].definition_digest' must be a string")
            if not definition_digest.strip():
                raise TypeError(f"agent.run@1 input 'tools[{index}].definition_digest' must not be empty")
            binding_digest = tool.get("binding_digest", tool.get("bindingDigest"))
            if not isinstance(binding_digest, str):
                raise TypeError(f"agent.run@1 input 'tools[{index}].binding_digest' must be a string")
            if not binding_digest.strip():
                raise TypeError(f"agent.run@1 input 'tools[{index}].binding_digest' must not be empty")
            effective_policy_snapshot_id = tool.get(
                "effective_policy_snapshot_id",
                tool.get("effectivePolicySnapshotId"),
            )
            if not isinstance(effective_policy_snapshot_id, str):
                raise TypeError(
                    f"agent.run@1 input 'tools[{index}].effective_policy_snapshot_id' must be a string"
                )
            if not effective_policy_snapshot_id.strip():
                raise TypeError(
                    f"agent.run@1 input 'tools[{index}].effective_policy_snapshot_id' must not be empty"
                )
            allowed_for_principal = tool.get("allowed_for_principal", tool.get("allowedForPrincipal"))
            if not isinstance(allowed_for_principal, bool):
                raise TypeError(f"agent.run@1 input 'tools[{index}].allowed_for_principal' must be a boolean")
            if not allowed_for_principal:
                raise PermissionError(f"agent.run@1 input 'tools[{index}]' is not allowed for principal")
            valid_until = tool.get("valid_until", tool.get("validUntil"))
            model_visible_tools.append(
                {
                    "toolName": tool_name,
                    "resolvedToolId": resolved_tool_id,
                    "definitionDigest": definition_digest,
                    "bindingDigest": binding_digest,
                    "effectivePolicySnapshotId": effective_policy_snapshot_id,
                    "allowedForPrincipal": allowed_for_principal,
                    "validUntil": valid_until,
                }
            )
            provenance_tools.append(
                ModelVisibleToolRef(
                    tool_name=tool_name,
                    resolved_tool_id=resolved_tool_id,
                    definition_digest=definition_digest,
                    binding_digest=binding_digest,
                    effective_policy_snapshot_id=effective_policy_snapshot_id,
                    allowed_for_principal=allowed_for_principal,
                    valid_until=str(valid_until) if valid_until is not None else None,
                )
            )
        model_visible_tools.sort(
            key=lambda tool: (
                str(tool["toolName"]),
                str(tool["resolvedToolId"]),
            )
        )
        run_store = context.get("run_store")
        if run_store is not None:
            run_store.record_model_visible_tools(str(context["run_id"]), provenance_tools)
        messages = inputs.get("messages")
        if messages is None:
            conversation = inputs.get("conversation")
            if isinstance(conversation, Mapping):
                messages = conversation.get("messages", [])
            else:
                messages = []
        if not isinstance(messages, list):
            raise TypeError("agent.run@1 input 'messages' must be a list")
        if "response" in config:
            text = str(config["response"])
            finish_reason = "scripted"
        elif messages:
            last_message = messages[-1]
            if isinstance(last_message, dict):
                text = str(last_message.get("content", last_message.get("text", "")))
            else:
                text = str(last_message)
            finish_reason = "echo"
        else:
            text = ""
            finish_reason = "empty"
        output_policy = config.get("outputPolicy", config.get("output_policy"))
        output_policy = output_policy if isinstance(output_policy, dict) else {}
        output_policy_profile_ref = output_policy.get("profileRef", output_policy.get("profile_ref"))
        if not isinstance(output_policy_profile_ref, str) or not output_policy_profile_ref.strip():
            output_policy_profile_ref = None
        candidate = {
            "text": text,
            "finishReason": finish_reason,
            "toolCount": len(tools),
            "modelVisibleTools": model_visible_tools,
        }
        if output_policy_profile_ref is not None:
            candidate["outputPolicyProfileRef"] = output_policy_profile_ref
        return {
            "candidate": candidate,
            "result": candidate,
            "message": candidate,
        }

    def commit_turn(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        transaction = inputs.get("transaction", inputs.get("turn"))
        if not isinstance(transaction, Mapping):
            raise TypeError(
                "conversation.commit_turn@1 requires transaction or turn mapping"
            )
        if transaction.get("status") == "policy_stopped":
            raise RuntimeError("conversation.commit_turn@1 cannot commit policy-stopped turn")
        if "candidate" in inputs:
            candidate = inputs["candidate"]
        elif "response" in inputs:
            candidate = inputs["response"]
        else:
            raise TypeError(
                "conversation.commit_turn@1 requires candidate or response input"
            )
        text = candidate["text"] if isinstance(candidate, Mapping) and "text" in candidate else str(candidate)
        answer = {
            "conversationId": transaction["conversationId"],
            "text": text,
            "turnId": transaction["turnId"],
        }
        return {
            "answer": answer,
            "result": candidate,
        }

    def policy_stop_turn(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        transaction = inputs["transaction"]
        if not isinstance(transaction, dict):
            raise TypeError("conversation.policy_stop_turn@1 requires transaction mapping")
        stopped = {
            "conversationId": transaction["conversationId"],
            "turnId": transaction["turnId"],
            "status": "policy_stopped",
            "draftDisposition": str(config.get("draftDisposition", "retract")),
            "committedMessageIds": [],
        }
        return {"transaction": stopped, "turn": stopped}

    def control_map(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        items = inputs.get("items", [])
        if not isinstance(items, list):
            raise TypeError("control.map@2 input 'items' must be a list")
        block_id = config.get("block")
        if not isinstance(block_id, str) or not block_id:
            raise TypeError("control.map@2 config.block must be a nonempty string")
        input_name = str(config.get("inputName", "item"))
        output_name = config.get("outputName")
        block_config = config.get("config", {})
        if not isinstance(block_config, dict):
            raise TypeError("control.map@2 config.config must be a mapping")
        block = registry.resolve(block_id)
        outcomes: list[dict[str, Any]] = []
        values: list[Any] = []
        for index, item in enumerate(items):
            try:
                result = block({input_name: item}, block_config, {**context, "map_index": index})
                if not isinstance(result, dict):
                    raise TypeError("mapped block returned non-mapping output")
                value = result if output_name is None else result[str(output_name)]
                values.append(value)
                outcomes.append({"status": "succeeded", "value": value})
            except Exception as exc:
                if config.get("onError") != "collect":
                    raise
                outcomes.append({"status": "failed", "error": str(exc)})
        if config.get("onError") == "collect":
            return {"outcomes": outcomes, "values": values}
        return {"values": values}

    def control_select(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        cases = inputs.get("cases", {})
        if not isinstance(cases, dict):
            raise TypeError("control.select@1 input 'cases' must be a mapping")
        order = config.get("order")
        if order is None:
            order = list(cases)
        if not isinstance(order, list):
            raise TypeError("control.select@1 config.order must be a list")
        for key in order:
            if key in cases:
                return {"value": cases[key], "selected": key}
        if "default" in config:
            return {"value": config["default"], "selected": "default"}
        raise KeyError("control.select@1 found no present case")

    def async_start_operation(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        operation_id = _required_async_string(config, "operationId", "operation_id", "operationId")
        run_id = _required_async_string(config, "runId", "run_id", "runId")
        node_id = _required_async_string(config, "nodeId", "node_id", "nodeId")
        attempt_id = _required_async_string(config, "attemptId", "attempt_id", "attemptId")
        kind = _required_async_string(config, "kind", "kind", "kind")
        resume_token_hash = _required_async_string(
            config,
            "resumeTokenHash",
            "resume_token_hash",
            "resumeTokenHash",
        )
        resume_token_hash = _validate_async_resume_token_hash(
            "async.start_operation@1 config",
            "resumeTokenHash",
            resume_token_hash,
        )
        idempotency_key = _required_async_string(
            config,
            "idempotencyKey",
            "idempotency_key",
            "idempotencyKey",
        )
        expected_schema = _required_async_string(config, "expectedSchema", "expected_schema", "expectedSchema")
        created_at_unix_ms = _required_async_u64(config, "createdAtUnixMs", "created_at_unix_ms", "createdAtUnixMs")
        operation: dict[str, Any] = {
            "operation_id": operation_id,
            "run_id": run_id,
            "node_id": node_id,
            "attempt_id": attempt_id,
            "kind": kind,
            "provider_operation_id": None,
            "state": "created",
            "resume_token_hash": resume_token_hash,
            "idempotency_key": idempotency_key,
            "expected_schema": expected_schema,
            "created_at_unix_ms": created_at_unix_ms,
            "submitted_at_unix_ms": None,
            "expires_at_unix_ms": None,
            "infinite_wait_policy": None,
            "completed_at_unix_ms": None,
        }
        provider_operation_id = _optional_async_string(
            config,
            "providerOperationId",
            "provider_operation_id",
            "providerOperationId",
        )
        if provider_operation_id is not None:
            operation["provider_operation_id"] = provider_operation_id
            submitted_at_unix_ms = _required_async_u64(
                config,
                "submittedAtUnixMs",
                "submitted_at_unix_ms",
                "submittedAtUnixMs",
            )
            if submitted_at_unix_ms < created_at_unix_ms:
                raise ValueError("async.start_operation@1 invalid operation: submitted_at precedes created_at")
            operation["submitted_at_unix_ms"] = submitted_at_unix_ms
            operation["state"] = "submitted"
        expires_at_unix_ms = _optional_async_u64(config, "expiresAtUnixMs", "expires_at_unix_ms", "expiresAtUnixMs")
        timeout_ms = _optional_duration_ms(config, ("timeoutMs", "timeout_ms", "timeout"), "timeout")
        if expires_at_unix_ms is not None and timeout_ms is not None:
            raise ValueError("async.start_operation@1 must not define both expiresAtUnixMs and timeout")
        if expires_at_unix_ms is None and timeout_ms is not None:
            if created_at_unix_ms > MAX_U64 - timeout_ms:
                raise ValueError("async.start_operation@1 timeout exceeds timestamp range")
            expires_at_unix_ms = created_at_unix_ms + timeout_ms
        infinite_wait_policy = _optional_async_string(
            config,
            "infiniteWaitPolicy",
            "infinite_wait_policy",
            "infiniteWaitPolicy",
        )
        if expires_at_unix_ms is not None and infinite_wait_policy is not None:
            raise ValueError("async.start_operation@1 must not define both timeout and infiniteWaitPolicy")
        if infinite_wait_policy is not None:
            operation["infinite_wait_policy"] = infinite_wait_policy
        if expires_at_unix_ms is not None:
            submitted_at_unix_ms = operation.get("submitted_at_unix_ms")
            if not isinstance(submitted_at_unix_ms, int) or isinstance(submitted_at_unix_ms, bool):
                raise ValueError("async.start_operation@1 invalid operation: non-created operations require submitted_at")
            if (
                expires_at_unix_ms <= submitted_at_unix_ms
            ):
                raise ValueError("async.start_operation@1 invalid operation: expires_at must be after submitted_at")
            operation["expires_at_unix_ms"] = expires_at_unix_ms
            operation["state"] = "waiting_callback"
        elif infinite_wait_policy is not None:
            submitted_at_unix_ms = operation.get("submitted_at_unix_ms")
            if not isinstance(submitted_at_unix_ms, int) or isinstance(submitted_at_unix_ms, bool):
                raise ValueError("async.start_operation@1 invalid operation: non-created operations require submitted_at")
            operation["state"] = "waiting_callback"
        if "subject" in inputs:
            operation["subject"] = inputs["subject"]
        if "changeset" in inputs:
            operation["changeset"] = inputs["changeset"]
        return {"operation": operation}

    def async_await_callback(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        operation = _required_async_operation_input(inputs, "async.await_callback@1")
        if operation.get("state") != "waiting_callback":
            raise RuntimeError(
                f"async.await_callback@1 operation must be waiting_callback, got {operation.get('state')!r}"
            )
        on_timeout = str(config.get("onTimeout", config.get("on_timeout", "fail")))
        if on_timeout not in {"fail", "cancel", "expire"}:
            raise ValueError("async.await_callback@1 onTimeout must be one of fail, cancel, or expire")
        checkpoint = config.get("checkpoint", True)
        if not isinstance(checkpoint, bool):
            raise ValueError("async.await_callback@1 checkpoint must be a boolean")
        wait: dict[str, Any] = {
            "state": "waiting_callback",
            "operation": operation,
            "checkpoint": checkpoint,
            "onTimeout": on_timeout,
        }
        timeout_ms = _optional_duration_ms(config, ("timeoutMs", "timeout_ms", "timeout"), "timeout")
        if timeout_ms is not None:
            wait["timeoutMs"] = timeout_ms
        infinite_wait_policy = _optional_async_string(
            config,
            "infiniteWaitPolicy",
            "infinite_wait_policy",
            "infiniteWaitPolicy",
        )
        if timeout_ms is not None and infinite_wait_policy is not None:
            raise ValueError("async.await_callback@1 must not define both timeout and infiniteWaitPolicy")
        if infinite_wait_policy is not None:
            wait["infiniteWaitPolicy"] = infinite_wait_policy
        return {"wait": wait}

    def async_poll_operation(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        operation = dict(_required_async_operation_input(inputs, "async.poll_operation@1"))
        interval_ms = _optional_duration_ms(
            config,
            ("intervalMs", "interval_ms", "interval"),
            "interval",
        )
        if interval_ms is None:
            interval_ms = 30_000
        max_interval_ms = _optional_duration_ms(
            config,
            ("maxIntervalMs", "max_interval_ms", "maxInterval", "max_interval"),
            "maxInterval",
        )
        if max_interval_ms is None:
            max_interval_ms = interval_ms
        if max_interval_ms < interval_ms:
            raise ValueError("async.poll_operation@1 maxInterval must not be less than interval")
        timeout_ms = _optional_duration_ms(config, ("timeoutMs", "timeout_ms", "timeout"), "timeout")
        infinite_wait_policy = _optional_async_string(
            config,
            "infiniteWaitPolicy",
            "infinite_wait_policy",
            "infiniteWaitPolicy",
        )
        if timeout_ms is None and infinite_wait_policy is None:
            raise ValueError("async.poll_operation@1 requires timeoutMs")
        if timeout_ms is not None and infinite_wait_policy is not None:
            raise ValueError("async.poll_operation@1 must not define both timeout and infiniteWaitPolicy")
        operation["state"] = "polling"
        poll = {
            "state": "polling",
            "operation": operation,
            "intervalMs": interval_ms,
            "maxIntervalMs": max_interval_ms,
        }
        if timeout_ms is not None:
            poll["timeoutMs"] = timeout_ms
        if infinite_wait_policy is not None:
            poll["infiniteWaitPolicy"] = infinite_wait_policy
        return {"poll": poll}

    def async_complete_operation(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        config = _required_async_config_mapping(config, "async.complete_operation@1")
        operation = _required_async_operation_input(inputs, "async.complete_operation@1")
        completed_at_unix_ms = _optional_async_u64(
            config,
            "completedAtUnixMs",
            "completed_at_unix_ms",
            "completedAtUnixMs",
        )
        _validate_async_terminal_timestamp(operation, completed_at_unix_ms, "async.complete_operation@1")
        return {
            "result": _async_operation_result(
                str(operation["operation_id"]),
                "completed",
                output=inputs.get("output"),
                result_projections=_async_result_projections(config, "async.complete_operation@1"),
                external_effects=_async_external_effects(config, "async.complete_operation@1"),
                completed_at_unix_ms=completed_at_unix_ms,
            )
        }

    def async_cancel_operation(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        config = _required_async_config_mapping(config, "async.cancel_operation@1")
        operation = _required_async_operation_input(inputs, "async.cancel_operation@1")
        completed_at_unix_ms = _optional_async_u64(
            config,
            "cancelledAtUnixMs",
            "cancelled_at_unix_ms",
            "cancelledAtUnixMs",
        )
        _validate_async_terminal_timestamp(operation, completed_at_unix_ms, "async.cancel_operation@1")
        return {
            "result": _async_operation_result(
                str(operation["operation_id"]),
                "cancelled",
                result_projections=_async_result_projections(config, "async.cancel_operation@1"),
                external_effects=_async_external_effects(config, "async.cancel_operation@1"),
                completed_at_unix_ms=completed_at_unix_ms,
            )
        }

    def async_expire_operation(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        config = _required_async_config_mapping(config, "async.expire_operation@1")
        operation = _required_async_operation_input(inputs, "async.expire_operation@1")
        completed_at_unix_ms = _optional_async_u64(
            config,
            "expiredAtUnixMs",
            "expired_at_unix_ms",
            "expiredAtUnixMs",
        )
        _validate_async_terminal_timestamp(operation, completed_at_unix_ms, "async.expire_operation@1")
        return {
            "result": _async_operation_result(
                str(operation["operation_id"]),
                "expired",
                result_projections=_async_result_projections(config, "async.expire_operation@1"),
                external_effects=_async_external_effects(config, "async.expire_operation@1"),
                completed_at_unix_ms=completed_at_unix_ms,
            )
        }

    def _config_value(config: Mapping[str, Any], camel_key: str, snake_key: str) -> tuple[bool, Any]:
        if camel_key in config:
            return True, config[camel_key]
        if snake_key in config:
            return True, config[snake_key]
        return False, None

    def _required_async_config_mapping(config: Any, block_label: str) -> Mapping[str, Any]:
        if not isinstance(config, Mapping):
            raise TypeError(f"{block_label} config must be a mapping")
        return config

    def _validate_config_string(value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"tools.resolve@1 {label} must be a string")
        if not value.strip():
            raise TypeError(f"tools.resolve@1 {label} must not be empty")
        return value

    def _required_string(config: Mapping[str, Any], camel_key: str, snake_key: str, label: str) -> str:
        found, value = _config_value(config, camel_key, snake_key)
        if not found:
            raise TypeError(f"tools.resolve@1 {label} is required")
        return _validate_config_string(value, label)

    def _optional_string(config: Mapping[str, Any], camel_key: str, snake_key: str, label: str) -> str | None:
        found, value = _config_value(config, camel_key, snake_key)
        if not found or value is None:
            return None
        return _validate_config_string(value, label)

    def _string_with_default(
        config: Mapping[str, Any],
        camel_key: str,
        snake_key: str,
        default: str,
        label: str,
    ) -> str:
        found, value = _config_value(config, camel_key, snake_key)
        if not found:
            return default
        return _validate_config_string(value, label)

    def _string_collection(value: Any, label: str) -> frozenset[str]:
        if not isinstance(value, list | tuple | set | frozenset):
            raise TypeError(f"tools.resolve@1 {label} must be a sequence")
        if any(not isinstance(item, str) for item in value):
            raise TypeError(f"tools.resolve@1 {label} entries must be strings")
        if any(not item.strip() for item in value):
            raise TypeError(f"tools.resolve@1 {label} entries must not be empty")
        return frozenset(value)

    def _string_mapping(
        config: Mapping[str, Any],
        camel_key: str,
        snake_key: str,
        label: str,
    ) -> dict[str, str]:
        found, value = _config_value(config, camel_key, snake_key)
        if not found:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError(f"tools.resolve@1 {label} must be a mapping")
        mapping = dict(value)
        if any(not isinstance(key, str) or not isinstance(item, str) for key, item in mapping.items()):
            raise TypeError(f"tools.resolve@1 {label} entries must be strings")
        return mapping

    def _string_set(config: dict[str, Any], camel_key: str, snake_key: str) -> frozenset[str] | None:
        value = config.get(camel_key, config.get(snake_key))
        if value is None:
            return None
        return _string_collection(value, f"scope {camel_key}")

    def _required_async_string(config: Mapping[str, Any], camel_key: str, snake_key: str, label: str) -> str:
        found, value = _config_value(config, camel_key, snake_key)
        if not found:
            raise TypeError(f"async.start_operation@1 config.{label} is required")
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"async.start_operation@1 config.{label} must be a non-empty string")
        return value

    def _validate_async_resume_token_hash(owner: str, field_name: str, value: str) -> str:
        if not value.startswith("sha256:"):
            raise ValueError(f"{owner} {field_name} must be a canonical sha256 digest")
        digest = value.removeprefix("sha256:")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError(f"{owner} {field_name} must be a canonical sha256 digest")
        return value

    def _optional_async_string(config: Mapping[str, Any], camel_key: str, snake_key: str, label: str) -> str | None:
        found, value = _config_value(config, camel_key, snake_key)
        if not found or value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"async operation config.{label} must be a non-empty string")
        return value

    def _required_async_u64(config: Mapping[str, Any], camel_key: str, snake_key: str, label: str) -> int:
        found, value = _config_value(config, camel_key, snake_key)
        if not found:
            raise TypeError(f"async.start_operation@1 config.{label} is required")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_U64:
            raise TypeError(f"async operation config.{label} must be an unsigned 64-bit integer")
        return value

    def _optional_async_u64(config: Mapping[str, Any], camel_key: str, snake_key: str, label: str) -> int | None:
        found, value = _config_value(config, camel_key, snake_key)
        if not found or value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_U64:
            raise TypeError(f"async operation config.{label} must be an unsigned 64-bit integer")
        return value

    def _optional_duration_ms(config: Mapping[str, Any], keys: tuple[str, ...], label: str) -> int | None:
        value = None
        for key in keys:
            if key in config:
                value = config[key]
                break
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"async operation config.{label} must be a positive duration")
        if isinstance(value, int):
            if value <= 0:
                raise ValueError(f"async operation config.{label} must be a positive duration")
            if value > MAX_U64:
                raise ValueError(f"async operation config.{label} must be an unsigned 64-bit duration")
            return value
        duration_ms = parse_duration_milliseconds(value)
        if duration_ms is None:
            seconds = parse_duration_seconds(value)
            if seconds is not None and seconds * 1000 > MAX_U64:
                raise ValueError(
                    f"async operation config.{label} must be an unsigned 64-bit duration"
                )
            raise ValueError(f"async operation config.{label} must be a positive duration")
        return duration_ms

    def _required_async_operation_input(inputs: Mapping[str, Any], block_label: str) -> dict[str, Any]:
        operation = inputs.get("operation")
        if not isinstance(operation, dict):
            raise TypeError(f"{block_label} requires operation input")
        normalized = dict(operation)
        for snake_key, camel_key in (
            ("operation_id", "operationId"),
            ("run_id", "runId"),
            ("node_id", "nodeId"),
            ("attempt_id", "attemptId"),
            ("kind", "kind"),
            ("state", "state"),
            ("resume_token_hash", "resumeTokenHash"),
            ("idempotency_key", "idempotencyKey"),
            ("expected_schema", "expectedSchema"),
        ):
            value = normalized.get(snake_key, normalized.get(camel_key))
            if not isinstance(value, str) or not value.strip():
                raise TypeError(f"{block_label} input operation.{snake_key} must be a non-empty string")
            if snake_key == "resume_token_hash":
                value = _validate_async_resume_token_hash(
                    f"{block_label} input operation",
                    snake_key,
                    value,
                )
            normalized[snake_key] = value
        for snake_key, camel_key in (
            ("provider_operation_id", "providerOperationId"),
            ("infinite_wait_policy", "infiniteWaitPolicy"),
        ):
            value = normalized.get(snake_key, normalized.get(camel_key))
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise TypeError(f"{block_label} input operation.{snake_key} must be a non-empty string")
            normalized[snake_key] = value
        for snake_key, camel_key in (
            ("created_at_unix_ms", "createdAtUnixMs"),
            ("submitted_at_unix_ms", "submittedAtUnixMs"),
            ("expires_at_unix_ms", "expiresAtUnixMs"),
            ("completed_at_unix_ms", "completedAtUnixMs"),
        ):
            value = normalized.get(snake_key, normalized.get(camel_key))
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_U64:
                raise TypeError(f"{block_label} input operation.{snake_key} must be an unsigned 64-bit integer")
            normalized[snake_key] = value
        return normalized

    def _validate_async_terminal_timestamp(
        operation: Mapping[str, Any],
        completed_at_unix_ms: int | None,
        block_label: str,
    ) -> None:
        if completed_at_unix_ms is None:
            return
        if completed_at_unix_ms == 0:
            raise ValueError(f"{block_label} terminal timestamp must be positive")
        submitted_at_unix_ms = operation.get("submitted_at_unix_ms")
        if isinstance(submitted_at_unix_ms, int) and not isinstance(submitted_at_unix_ms, bool):
            if completed_at_unix_ms < submitted_at_unix_ms:
                raise ValueError(f"{block_label} terminal timestamp must not be earlier than submitted_at_unix_ms")
        expires_at_unix_ms = operation.get("expires_at_unix_ms")
        if isinstance(expires_at_unix_ms, int) and not isinstance(expires_at_unix_ms, bool):
            if completed_at_unix_ms >= expires_at_unix_ms:
                raise ValueError(f"{block_label} terminal timestamp must be earlier than expires_at_unix_ms")

    def _async_operation_result(
        operation_id: str,
        status: str,
        *,
        output: Any = None,
        result_projections: Mapping[str, list[dict[str, Any]]] | None = None,
        external_effects: list[dict[str, Any]] | None = None,
        completed_at_unix_ms: int | None,
    ) -> dict[str, Any]:
        projections = result_projections or {}
        return {
            "operation_id": operation_id,
            "status": status,
            "output": output,
            "artifacts": projections.get("artifacts", []),
            "diagnostics": projections.get("diagnostics", []),
            "metrics": projections.get("metrics", []),
            "checks": projections.get("checks", []),
            "usage": projections.get("usage", []),
            "external_effects": [] if external_effects is None else external_effects,
            "completed_at_unix_ms": completed_at_unix_ms,
        }

    def _async_result_projections(config: Mapping[str, Any], block_label: str) -> dict[str, list[dict[str, Any]]]:
        return {
            "artifacts": _async_result_projection(config, "artifacts", block_label),
            "diagnostics": _async_result_projection(config, "diagnostics", block_label),
            "metrics": _async_result_projection(config, "metrics", block_label),
            "checks": _async_result_projection(config, "checks", block_label),
            "usage": _async_result_projection(config, "usage", block_label),
        }

    def _async_result_projection(config: Mapping[str, Any], field: str, block_label: str) -> list[dict[str, Any]]:
        raw_items = config.get(field, [])
        if raw_items is None:
            return []
        if isinstance(raw_items, Mapping) or isinstance(raw_items, (str, bytes, bytearray, memoryview)):
            raise TypeError(f"{block_label} config.{field} must be a sequence")
        if not isinstance(raw_items, list | tuple):
            raise TypeError(f"{block_label} config.{field} must be a sequence")
        items = []
        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, Mapping):
                raise TypeError(f"{block_label} config.{field}[{index}] must be a mapping")
            items.append(dict(raw_item))
        return items

    def _async_external_effects(config: Mapping[str, Any], block_label: str) -> list[dict[str, Any]]:
        raw_effects = config.get("externalEffects", config.get("external_effects", []))
        if not isinstance(raw_effects, list | tuple):
            raise TypeError(f"{block_label} config.externalEffects must be a sequence")
        effects = []
        for index, raw_effect in enumerate(raw_effects):
            if not isinstance(raw_effect, Mapping):
                raise TypeError(f"{block_label} config.externalEffects[{index}] must be a mapping")
            effect = {
                "effect_id": _required_effect_string(raw_effect, "effectId", "effect_id", "effectId", block_label),
                "target": _required_effect_string(raw_effect, "target", "target", "target", block_label),
                "operation": _required_effect_string(raw_effect, "operation", "operation", "operation", block_label),
                "outcome": _required_effect_string(raw_effect, "outcome", "outcome", "outcome", block_label),
                "idempotency_key": None,
                "provider_effect_id": None,
            }
            if effect["outcome"] not in {"no_external_effect", "committed", "not_committed", "unknown"}:
                raise ValueError(f"{block_label} config.externalEffects[{index}].outcome is unsupported")
            idempotency_key = _optional_effect_string(
                raw_effect,
                "idempotencyKey",
                "idempotency_key",
                "idempotencyKey",
                block_label,
            )
            if idempotency_key is not None:
                effect["idempotency_key"] = idempotency_key
            provider_effect_id = _optional_effect_string(
                raw_effect,
                "providerEffectId",
                "provider_effect_id",
                "providerEffectId",
                block_label,
            )
            if provider_effect_id is not None:
                if effect["outcome"] != "committed":
                    raise ValueError(f"{block_label} provider identity but no committed external effect")
                effect["provider_effect_id"] = provider_effect_id
            effects.append(effect)
        return effects

    def _required_effect_string(
        config: Mapping[str, Any],
        camel_key: str,
        snake_key: str,
        label: str,
        block_label: str,
    ) -> str:
        found, value = _config_value(config, camel_key, snake_key)
        if not found:
            raise TypeError(f"{block_label} config.externalEffects.{label} is required")
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"{block_label} config.externalEffects.{label} must be a non-empty string")
        return value

    def _optional_effect_string(
        config: Mapping[str, Any],
        camel_key: str,
        snake_key: str,
        label: str,
        block_label: str,
    ) -> str | None:
        found, value = _config_value(config, camel_key, snake_key)
        if not found or value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"{block_label} config.externalEffects.{label} must be a non-empty string")
        return value

    handlers = [
        ("conversation.begin_turn@1", begin_turn),
        ("prompt.render@1", prompt_render),
        ("model.generate@1", scripted_generate),
        ("tools.resolve@1", resolve_tools),
        ("agent.run@1", scripted_agent_run),
        ("conversation.commit_turn@1", commit_turn),
        ("conversation.policy_stop_turn@1", policy_stop_turn),
        ("control.map@2", control_map),
        ("control.select@1", control_select),
        ("async.start_operation@1", async_start_operation),
        ("async.await_callback@1", async_await_callback),
        ("async.poll_operation@1", async_poll_operation),
        ("async.complete_operation@1", async_complete_operation),
        ("async.cancel_operation@1", async_cancel_operation),
        ("async.expire_operation@1", async_expire_operation),
        *RAG_BLOCKS.items(),
        *GOVERNANCE_BLOCKS.items(),
    ]
    for block_id, block in handlers:
        if included_block_ids is not None and block_id not in included_block_ids:
            continue
        registry.register(block_id, block)
    return registry


def stdlib_registry(*, allow_untyped: bool = False) -> RuntimeRegistry:
    """Return the full preview stdlib across all implemented profiles."""

    return _stdlib_registry(
        allow_untyped=allow_untyped,
        included_block_ids=None,
    )


_CORE_STDLIB_BLOCK_IDS = frozenset(
    {
        "control.map@2",
        "control.select@1",
        "model.generate@1",
        "prompt.render@1",
    }
)


def core_stdlib_registry(*, allow_untyped: bool = False) -> RuntimeRegistry:
    """Return the stable C1 handler and descriptor subset of the stdlib."""

    return _stdlib_registry(
        allow_untyped=allow_untyped,
        included_block_ids=_CORE_STDLIB_BLOCK_IDS,
    )

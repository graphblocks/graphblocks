from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
import sqlite3
from threading import Lock, RLock
import time
from typing import Any, Callable, Literal, ParamSpec, Protocol, TypeVar, cast

from .async_operation import VALID_ASYNC_OPERATION_KINDS
from ._canonical_reference import canonical_dumps, canonical_hash, canonical_loads
from .compiler import (
    MAX_NODE_RETRY_ATTEMPTS,
    STATE_CHANGING_TOOL_EFFECTS,
    compile_graph_reference as compile_graph,
)
from .duration import parse_duration_seconds
from .documents import FrozenDict, FrozenList
from .leases import InMemoryLeasePool
from .plugins import (
    BlockCatalog,
    builtin_block_catalog,
    builtin_block_implementations,
)
from .run_store import InMemoryRunStore, RunDeploymentProvenance

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
_TERMINAL_JOURNAL_KINDS = frozenset({"run_succeeded", "run_failed", "run_cancelled"})
BlockCallable = Callable[
    [dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]
]
MAX_U64 = (1 << 64) - 1
_SQLITE_JOURNAL_BUSY_TIMEOUT_MS = 5_000
_JournalP = ParamSpec("_JournalP")
_JournalR = TypeVar("_JournalR")


def _with_sqlite_execution_journal_lock(
    method: Callable[_JournalP, _JournalR],
) -> Callable[_JournalP, _JournalR]:
    @wraps(method)
    def locked(*args: _JournalP.args, **kwargs: _JournalP.kwargs) -> _JournalR:
        journal = cast("SQLiteExecutionJournal", args[0])
        with journal._lock:
            if journal._closed:
                raise JournalClosedError("SQLite execution journal is closed")
            return method(*args, **kwargs)

    return locked


class JournalLike(Protocol):
    @property
    def records(self) -> Sequence[JournalRecord]: ...

    @property
    def terminal_kind(self) -> JournalKind | None: ...

    def append(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord: ...

    def append_terminal(
        self, kind: JournalKind, payload: dict[str, Any]
    ) -> JournalRecord: ...


JournalFactory = Callable[[str], JournalLike]


class JournalStateError(RuntimeError):
    pass


class JournalClosedError(RuntimeError):
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
            {key: _freeze_json_like(nested) for key, nested in value.items()}
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


def _canonical_json_object(owner: str, value: object) -> FrozenDict:
    if not isinstance(value, Mapping):
        raise TypeError(f"{owner} must be a mapping")
    snapshot = _loads_strict_json(
        owner,
        _dumps_strict_json(owner, value),
    )
    if not isinstance(snapshot, dict):
        raise TypeError(f"{owner} must be a mapping")
    frozen = _freeze_json_like(snapshot)
    if not isinstance(frozen, FrozenDict):
        raise TypeError(f"{owner} must be a mapping")
    return frozen


def _require_exact_nonempty_string(owner: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{owner} must be an exact nonempty string")


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
        object.__setattr__(
            self,
            "payload",
            _canonical_json_object("execution journal payload", self.payload),
        )

    @classmethod
    def _from_canonical_payload(
        cls,
        sequence: int,
        kind: JournalKind,
        payload: FrozenDict,
    ) -> JournalRecord:
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
        ):
            raise ValueError(
                "execution journal sequence must be a positive integer"
            )
        if not isinstance(kind, str) or kind not in _JOURNAL_KINDS:
            raise ValueError(f"unsupported journal kind {kind!r}")
        if not isinstance(payload, FrozenDict):
            raise TypeError(
                "execution journal canonical payload must be a FrozenDict"
            )
        record = object.__new__(cls)
        object.__setattr__(record, "sequence", sequence)
        object.__setattr__(record, "kind", kind)
        object.__setattr__(record, "payload", payload)
        return record

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "payload": _mutable_json_like(self.payload),
        }


def _validated_execution_journal_state(
    records_value: object,
    terminal_kind_value: object,
) -> tuple[tuple[JournalRecord, ...], JournalKind | None]:
    if isinstance(records_value, (str, bytes, bytearray, Mapping)):
        raise ValueError("execution journal records must be JournalRecord values")
    try:
        raw_records: tuple[object, ...] = tuple(
            records_value  # type: ignore[arg-type]
        )
    except TypeError as error:
        raise ValueError(
            "execution journal records must be JournalRecord values"
        ) from error
    if any(not isinstance(record, JournalRecord) for record in raw_records):
        raise ValueError("execution journal records must be JournalRecord values")
    records = cast(tuple[JournalRecord, ...], raw_records)
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
        raise JournalStateError("execution journal terminal record must be last")
    inferred_terminal = terminal_records[0].kind if terminal_records else None
    if terminal_kind_value is not None:
        if (
            not isinstance(terminal_kind_value, str)
            or terminal_kind_value not in _TERMINAL_JOURNAL_KINDS
        ):
            raise ValueError(
                f"journal terminal kind is invalid: {terminal_kind_value!r}"
            )
        if terminal_kind_value != inferred_terminal:
            raise JournalStateError(
                "execution journal terminal_kind must match its terminal record"
            )
    return records, inferred_terminal


@dataclass(frozen=True, slots=True)
class JournalSnapshot:
    """Immutable point-in-time state captured from an execution journal."""

    run_id: str
    records: tuple[JournalRecord, ...] = field(default_factory=tuple)
    terminal_kind: JournalKind | None = None
    __hash__ = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        _require_exact_nonempty_string(
            "execution journal snapshot run id",
            self.run_id,
        )
        records, terminal_kind = _validated_execution_journal_state(
            self.records,
            self.terminal_kind,
        )
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "terminal_kind", terminal_kind)


@dataclass(slots=True, init=False, eq=False, repr=False)
class ExecutionJournal:
    """Mutable in-memory journal with atomic immutable snapshots."""

    _run_id: str = field(init=False, repr=False)
    _records: list[JournalRecord] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    _terminal_kind: JournalKind | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _lock: RLock = field(
        default_factory=RLock,
        init=False,
        repr=False,
        compare=False,
    )
    __hash__ = None  # type: ignore[assignment]

    def __init__(
        self,
        run_id: str,
        records: Sequence[JournalRecord] = (),
        terminal_kind: JournalKind | None = None,
    ) -> None:
        _require_exact_nonempty_string(
            "execution journal run id",
            run_id,
        )
        normalized_records, normalized_terminal_kind = (
            _validated_execution_journal_state(records, terminal_kind)
        )
        self._run_id = run_id
        self._records = list(normalized_records)
        self._terminal_kind = normalized_terminal_kind
        self._lock = RLock()

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def records(self) -> tuple[JournalRecord, ...]:
        with self._lock:
            return tuple(self._records)

    @property
    def terminal_kind(self) -> JournalKind | None:
        with self._lock:
            return self._terminal_kind

    def __repr__(self) -> str:
        snapshot = self.snapshot()
        return (
            f"{type(self).__name__}(run_id={snapshot.run_id!r}, "
            f"records={snapshot.records!r}, "
            f"terminal_kind={snapshot.terminal_kind!r})"
        )

    def __reduce__(
        self,
    ) -> tuple[
        type[ExecutionJournal],
        tuple[str, tuple[JournalRecord, ...], JournalKind | None],
    ]:
        snapshot = self.snapshot()
        return type(self), (
            snapshot.run_id,
            snapshot.records,
            snapshot.terminal_kind,
        )

    def __setstate__(self, state: object) -> None:
        if (
            not isinstance(state, (list, tuple))
            or len(state) != 3
        ):
            raise TypeError("invalid legacy execution journal pickle state")
        run_id, records, terminal_kind = state
        ExecutionJournal.__init__(
            self,
            cast(str, run_id),
            cast(Sequence[JournalRecord], records),
            cast(JournalKind | None, terminal_kind),
        )

    def snapshot(self) -> JournalSnapshot:
        with self._lock:
            run_id = self._run_id
            records = tuple(self._records)
            terminal_kind = self._terminal_kind
        return JournalSnapshot(
            run_id=run_id,
            records=records,
            terminal_kind=terminal_kind,
        )

    def append(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        if kind not in _JOURNAL_KINDS:
            raise ValueError(f"unsupported journal kind {kind!r}")
        if kind in _TERMINAL_JOURNAL_KINDS:
            raise JournalStateError(
                f"terminal journal kind {kind!r} must be recorded with append_terminal"
            )
        with self._lock:
            if self._terminal_kind is not None:
                raise JournalStateError(
                    f"cannot append {kind} after terminal {self._terminal_kind}"
                )
        canonical_payload = _canonical_json_object(
            "execution journal payload",
            payload,
        )
        with self._lock:
            if self._terminal_kind is not None:
                raise JournalStateError(
                    f"cannot append {kind} after terminal {self._terminal_kind}"
                )
            record = JournalRecord._from_canonical_payload(
                len(self._records) + 1,
                kind,
                canonical_payload,
            )
            self._records.append(record)
            return record

    def append_terminal(
        self, kind: JournalKind, payload: dict[str, Any]
    ) -> JournalRecord:
        if kind not in _TERMINAL_JOURNAL_KINDS:
            raise ValueError(f"journal terminal kind is invalid: {kind!r}")
        with self._lock:
            if self._terminal_kind is not None:
                raise JournalStateError(
                    f"terminal already recorded as {self._terminal_kind}"
                )
        canonical_payload = _canonical_json_object(
            "execution journal payload",
            payload,
        )
        with self._lock:
            if self._terminal_kind is not None:
                raise JournalStateError(
                    f"terminal already recorded as {self._terminal_kind}"
                )
            record = JournalRecord._from_canonical_payload(
                len(self._records) + 1,
                kind,
                canonical_payload,
            )
            self._records.append(record)
            self._terminal_kind = kind
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
        object.__setattr__(
            self,
            "payload",
            _canonical_json_object("local journal payload", self.payload),
        )

    @classmethod
    def _from_canonical_payload(
        cls,
        sequence: int,
        kind: LocalJournalKind,
        payload: FrozenDict,
    ) -> LocalJournalRecord:
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
        ):
            raise ValueError("local journal sequence must be a positive integer")
        if kind not in _LOCAL_JOURNAL_KINDS:
            raise ValueError(f"unsupported local journal kind {kind!r}")
        if not isinstance(payload, FrozenDict):
            raise TypeError("local journal canonical payload must be a FrozenDict")
        record = object.__new__(cls)
        object.__setattr__(record, "sequence", sequence)
        object.__setattr__(record, "kind", kind)
        object.__setattr__(record, "payload", payload)
        return record

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "payload": _mutable_json_like(self.payload),
        }


def _validated_local_execution_journal_state(
    records_value: object,
    terminal_kind_value: object,
) -> tuple[tuple[LocalJournalRecord, ...], LocalTerminalJournalKind | None]:
    if isinstance(records_value, (str, bytes, bytearray, Mapping)):
        raise ValueError("local journal records must be LocalJournalRecord values")
    try:
        raw_records: tuple[object, ...] = tuple(
            records_value  # type: ignore[arg-type]
        )
    except TypeError as error:
        raise ValueError(
            "local journal records must be LocalJournalRecord values"
        ) from error
    if any(not isinstance(record, LocalJournalRecord) for record in raw_records):
        raise ValueError("local journal records must be LocalJournalRecord values")
    records = cast(tuple[LocalJournalRecord, ...], raw_records)
    for expected_sequence, record in enumerate(records, start=1):
        if record.sequence != expected_sequence:
            raise JournalStateError(
                "local journal record sequences must be contiguous"
            )
    terminal_records = [
        record
        for record in records
        if record.kind in _LOCAL_TERMINAL_JOURNAL_KINDS
    ]
    if len(terminal_records) > 1:
        raise JournalStateError(
            "local journal must not contain multiple terminal records"
        )
    if terminal_records and terminal_records[0] is not records[-1]:
        raise JournalStateError("local journal terminal record must be last")
    inferred_terminal = terminal_records[0].kind if terminal_records else None
    if terminal_kind_value is not None:
        if (
            not isinstance(terminal_kind_value, str)
            or terminal_kind_value not in _LOCAL_TERMINAL_JOURNAL_KINDS
        ):
            raise ValueError(
                f"local journal terminal kind is invalid: {terminal_kind_value!r}"
            )
        if terminal_kind_value != inferred_terminal:
            raise JournalStateError(
                "local journal terminal_kind must match its terminal record"
            )
    return records, cast(LocalTerminalJournalKind | None, inferred_terminal)


@dataclass(frozen=True, slots=True)
class LocalJournalSnapshot:
    """Immutable point-in-time state captured from a stable local journal."""

    run_id: str
    records: tuple[LocalJournalRecord, ...] = field(default_factory=tuple)
    terminal_kind: LocalTerminalJournalKind | None = None
    __hash__ = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        _require_exact_nonempty_string(
            "local journal snapshot run id",
            self.run_id,
        )
        records, terminal_kind = _validated_local_execution_journal_state(
            self.records,
            self.terminal_kind,
        )
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "terminal_kind", terminal_kind)


@dataclass
class LocalExecutionJournal:
    """Mutable stable C1 lifecycle journal with atomic immutable snapshots."""

    __slots__ = ("run_id", "_records", "_terminal_kind", "_lock")

    run_id: str
    __hash__ = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        _require_exact_nonempty_string(
            "local journal run id",
            self.run_id,
        )
        self._records: list[LocalJournalRecord] = []
        self._terminal_kind: LocalTerminalJournalKind | None = None
        self._lock = RLock()

    def __setattr__(self, name: str, value: object) -> None:
        if name == "run_id":
            try:
                object.__getattribute__(self, "run_id")
            except AttributeError:
                pass
            else:
                raise AttributeError("local execution journal run_id is read-only")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name == "run_id":
            raise AttributeError("local execution journal run_id is read-only")
        object.__delattr__(self, name)

    def __eq__(self, other: object) -> bool:
        if other.__class__ is not self.__class__:
            return NotImplemented
        return self.snapshot() == other.snapshot()

    @property
    def records(self) -> tuple[LocalJournalRecord, ...]:
        with self._lock:
            return tuple(self._records)

    @property
    def terminal_kind(self) -> LocalTerminalJournalKind | None:
        with self._lock:
            return self._terminal_kind

    def __repr__(self) -> str:
        snapshot = self.snapshot()
        return (
            f"{type(self).__name__}(run_id={snapshot.run_id!r}, "
            f"records={snapshot.records!r}, "
            f"terminal_kind={snapshot.terminal_kind!r})"
        )

    def __reduce__(
        self,
    ) -> tuple[
        type[LocalExecutionJournal],
        tuple[str],
        tuple[
            tuple[LocalJournalRecord, ...],
            LocalTerminalJournalKind | None,
        ],
    ]:
        snapshot = self.snapshot()
        return type(self), (snapshot.run_id,), (
            snapshot.records,
            snapshot.terminal_kind,
        )

    def __setstate__(self, state: object) -> None:
        if not isinstance(state, (list, tuple)):
            raise TypeError("invalid local execution journal pickle state")
        if len(state) == 2:
            run_id = self.run_id
            records, terminal_kind = state
        elif len(state) == 3:
            run_id, records, terminal_kind = state
            _require_exact_nonempty_string(
                "local journal run id",
                run_id,
            )
            try:
                current_run_id = object.__getattribute__(self, "run_id")
            except AttributeError:
                object.__setattr__(self, "run_id", run_id)
            else:
                if current_run_id != run_id:
                    raise JournalStateError(
                        "local journal pickle run id does not match its constructor"
                    )
        else:
            raise TypeError("invalid local execution journal pickle state")
        normalized_records, normalized_terminal_kind = (
            _validated_local_execution_journal_state(records, terminal_kind)
        )
        object.__setattr__(self, "_records", list(normalized_records))
        object.__setattr__(self, "_terminal_kind", normalized_terminal_kind)
        object.__setattr__(self, "_lock", RLock())

    def snapshot(self) -> LocalJournalSnapshot:
        with self._lock:
            run_id = self.run_id
            records = tuple(self._records)
            terminal_kind = self._terminal_kind
        return LocalJournalSnapshot(
            run_id=run_id,
            records=records,
            terminal_kind=terminal_kind,
        )

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
        with self._lock:
            if self._terminal_kind is not None:
                raise JournalStateError(
                    f"cannot append {kind} after terminal {self._terminal_kind}"
                )
        canonical_payload = _canonical_json_object(
            "local journal payload",
            payload,
        )
        with self._lock:
            if self._terminal_kind is not None:
                raise JournalStateError(
                    f"cannot append {kind} after terminal {self._terminal_kind}"
                )
            record = LocalJournalRecord._from_canonical_payload(
                len(self._records) + 1,
                kind,
                canonical_payload,
            )
            self._records.append(record)
            return record

    def append_terminal(
        self,
        kind: LocalTerminalJournalKind,
        payload: dict[str, Any],
    ) -> LocalJournalRecord:
        if kind not in _LOCAL_TERMINAL_JOURNAL_KINDS:
            raise ValueError(f"local terminal journal kind is invalid: {kind!r}")
        with self._lock:
            if self._terminal_kind is not None:
                raise JournalStateError(
                    f"terminal already recorded as {self._terminal_kind}"
                )
        canonical_payload = _canonical_json_object(
            "local journal payload",
            payload,
        )
        with self._lock:
            if self._terminal_kind is not None:
                raise JournalStateError(
                    f"terminal already recorded as {self._terminal_kind}"
                )
            record = LocalJournalRecord._from_canonical_payload(
                len(self._records) + 1,
                kind,
                canonical_payload,
            )
            self._records.append(record)
            self._terminal_kind = kind
            return record


@dataclass(slots=True)
class SQLiteExecutionJournal:
    path: Path | str
    run_id: str
    connection: sqlite3.Connection = field(init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        _require_exact_nonempty_string(
            "SQLite execution journal run id",
            self.run_id,
        )
        self.path = Path(self.path)
        self.connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=_SQLITE_JOURNAL_BUSY_TIMEOUT_MS / 1_000,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            f"PRAGMA busy_timeout = {_SQLITE_JOURNAL_BUSY_TIMEOUT_MS}"
        )
        if str(self.path) != ":memory:":
            try:
                self.connection.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).casefold():
                    raise
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
            for row in self.connection.execute(
                "PRAGMA table_info(journal_records)"
            ).fetchall()
        }

    def _sequence_column(self) -> str:
        columns = self._columns()
        if "sequence" in columns:
            return "sequence"
        if "run_sequence" in columns:
            return "run_sequence"
        raise JournalStateError("journal_records must include sequence or run_sequence")

    @property
    @_with_sqlite_execution_journal_lock
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
    @_with_sqlite_execution_journal_lock
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
                _loads_strict_json(
                    "execution journal payload_json", str(row["payload_json"])
                )
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
        return JournalRecord(
            sequence,
            kind,
            _loads_strict_json("execution journal payload", payload_json),
        )

    @_with_sqlite_execution_journal_lock
    def append(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            record = self._append_in_transaction(kind, payload, terminal=False)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return record

    @_with_sqlite_execution_journal_lock
    def append_terminal(
        self, kind: JournalKind, payload: dict[str, Any]
    ) -> JournalRecord:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            record = self._append_in_transaction(kind, payload, terminal=True)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return record

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.connection.close()
            self._closed = True


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
            not isinstance(node, str) or not node.strip() or node != node.strip()
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
    ) -> bool: ...


class CheckpointAuthorityVerifier(Protocol):
    """Trusted boundary for restoring a checkpoint issued before restart."""

    def __call__(
        self,
        checkpoint: RuntimeCheckpoint,
        *,
        expected_graph_hash: str,
    ) -> bool: ...


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
            raise ValueError("waiting_callback runtime result requires a checkpoint")
        if self.status != "waiting_callback" and self.checkpoint is not None:
            raise ValueError("terminal runtime result must not retain a checkpoint")
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
        _require_exact_nonempty_string(
            "local result run_id",
            self.run_id,
        )
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
        object.__setattr__(
            self,
            "outputs",
            _canonical_json_object("local result outputs", self.outputs),
        )


_FULL_STDLIB_REGISTRY_MARKER = object()


@dataclass(slots=True)
class RuntimeRegistry:
    blocks: dict[str, BlockCallable] = field(default_factory=dict)
    block_catalog: BlockCatalog = field(default_factory=lambda: BlockCatalog({}))
    allow_untyped: bool = False
    _profile_marker: object | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _profile_blocks: tuple[tuple[str, BlockCallable], ...] = field(
        default=(),
        init=False,
        repr=False,
        compare=False,
    )
    _profile_block_catalog: BlockCatalog | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _profile_allow_untyped: bool | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

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
        self._profile_marker = None
        if block_id in self.blocks:
            raise ValueError(f"runtime block {block_id!r} is already registered")
        if not self.allow_untyped and self.block_catalog.get(block_id) is None:
            raise ValueError(
                f"runtime block {block_id!r} is not declared in the block catalog"
            )
        self.blocks[block_id] = block

    def replace(self, block_id: str, block: BlockCallable) -> None:
        self._profile_marker = None
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
    checkpoint_authority_verifier: CheckpointAuthorityVerifier | None = field(
        default=None,
        repr=False,
    )
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
        errors = [
            item for item in plan.diagnostics.diagnostics if item.severity == "error"
        ]
        if errors:
            message = "; ".join(
                f"{item.code} {item.path}: {item.message}" for item in errors
            )
            raise ValueError(message)

        normalized = plan.normalized
        if checkpoint is not None and not isinstance(checkpoint, RuntimeCheckpoint):
            raise ValueError("runtime checkpoint must be RuntimeCheckpoint")
        if checkpoint is None and callback_receipt is not None:
            raise ValueError("runtime callback_receipt requires a checkpoint")
        expected_checkpoint_digest: str | None = None
        if checkpoint is not None:
            if checkpoint.run_id != run_id:
                raise ValueError(
                    "runtime checkpoint run_id must match requested run_id"
                )
            if checkpoint.graph_hash != plan.graph_hash:
                raise ValueError(
                    "runtime checkpoint graph_hash must match compiled graph"
                )
            if not isinstance(callback_receipt, Mapping):
                raise ValueError("runtime checkpoint resume requires callback_receipt")
            with self._checkpoint_lock:
                expected_checkpoint_digest = self._checkpoint_state_digests.get(
                    checkpoint.checkpoint_id
                )
            if (
                expected_checkpoint_digest is None
                and self.checkpoint_authority_verifier is not None
            ):
                try:
                    checkpoint_verified = self.checkpoint_authority_verifier(
                        checkpoint,
                        expected_graph_hash=plan.graph_hash,
                    )
                except Exception as error:
                    raise ValueError(
                        "runtime checkpoint trusted authority failed"
                    ) from error
                if checkpoint_verified is not True:
                    raise ValueError(
                        "runtime checkpoint was rejected by the trusted authority"
                    )
                with self._checkpoint_lock:
                    expected_checkpoint_digest = (
                        self._checkpoint_state_digests.setdefault(
                            checkpoint.checkpoint_id,
                            checkpoint.state_digest,
                        )
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
        journal = (
            self.journal_factory(run_id)
            if self.journal_factory is not None
            else ExecutionJournal(run_id)
        )
        if checkpoint is None:
            run_started_payload: dict[str, Any] = {"graphHash": plan.graph_hash}
            if deployment_provenance is not None:
                run_started_payload["deploymentProvenance"] = (
                    deployment_provenance.canonical_value()
                )
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
            callback_idempotency_key = receipt.get("callback_idempotency_key")
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
                schema_verification = resume_admission.get("schema_verification")
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
                ownership_strings_valid = isinstance(ownership, Mapping) and all(
                    isinstance(ownership.get(field_name), str)
                    and bool(ownership[field_name])
                    and ownership[field_name] == ownership[field_name].strip()
                    for field_name in required_ownership_strings
                )
                schema_strings_valid = isinstance(schema_verification, Mapping) and all(
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
                    or resume_admission.get("node_id") != operation.get("node_id")
                    or resume_admission.get("attempt_id") != operation.get("attempt_id")
                    or resume_admission.get("checkpoint_id") != checkpoint.checkpoint_id
                    or resume_admission.get("checkpoint_state_digest")
                    != expected_checkpoint_digest
                    or not isinstance(schema_verification, Mapping)
                    or schema_verification.get("schema_id") != receipt.get("schema_id")
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
                raise ValueError("runtime checkpoint wait_node must remain pending")
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
                descriptor = self.registry.block_catalog.get("async.await_callback@1")
                if descriptor is not None:
                    declared_outputs = {port.name for port in descriptor.outputs}
                    unexpected_outputs = sorted(set(wait_result) - declared_outputs)
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
                        and edge["from"].split(".", 1)[0] == checkpoint.wait_node
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
                            raise RuntimeError(f"output path conflict at {edge['to']}")
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
                        error = (
                            f"node {node_name!r} when guard must resolve to a boolean"
                        )
                        journal.append(
                            "node_failed",
                            {"node": node_name, "error": error, "attempt": 0},
                        )
                        journal.append_terminal(
                            "run_failed", {"node": node_name, "error": error}
                        )
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
                timeout_seconds = (
                    parse_duration_seconds(flow.get("timeout"))
                    if isinstance(flow, dict)
                    else None
                )
                max_attempts = 1
                idempotency_key = None
                if isinstance(retry, dict):
                    max_attempts = _configured_retry_attempts(
                        retry.get("maxAttempts", retry.get("max_attempts", 1))
                    )
                    idempotency_key = retry.get("idempotencyKey") or retry.get(
                        "idempotency_key"
                    )
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
                configured_callback_checkpoint: object = True
                if block_id == "async.await_callback@1":
                    callback_wait_config = node.get("config", {})
                    if isinstance(callback_wait_config, Mapping):
                        configured_callback_checkpoint = (
                            callback_wait_config.get("checkpoint", True)
                        )
                result: dict[str, Any] | None = None
                for attempt in range(1, max_attempts + 1):
                    started_payload: dict[str, Any] = {
                        "node": node_name,
                        "block": block_id,
                        "attempt": attempt,
                    }
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
                        deadline = (
                            None
                            if timeout_seconds is None
                            else started_at + timeout_seconds
                        )
                        timeout_reason = (
                            f"node {node_name!r} exceeded timeout {flow.get('timeout')}"
                        )
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
                        if block_id == "async.await_callback@1":
                            wait_descriptor = attempt_result.get("wait")
                            if (
                                isinstance(wait_descriptor, Mapping)
                                and wait_descriptor.get("checkpoint")
                                is not configured_callback_checkpoint
                            ):
                                raise TypeError(
                                    "async.await_callback@1 returned checkpoint "
                                    "inconsistent with config"
                                )
                        result = attempt_result
                        break
                    except Exception as exc:
                        token = context["cancellation_token"]
                        if isinstance(token, CancellationToken) and token.cancelled:
                            journal.append_terminal(
                                "run_cancelled",
                                {
                                    "reason": token.reason,
                                    "node": node_name,
                                    "attempt": attempt,
                                },
                            )
                            if self.run_store is not None:
                                self.run_store.set_status(run_id, "cancelled")
                            if self.lease_pool is not None:
                                self.lease_pool.release_all(run_id)
                            return RunResult(
                                run_id, "cancelled", output_values, journal
                            )
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
                        journal.append(
                            "node_failed",
                            {"node": node_name, "error": str(exc), "attempt": attempt},
                        )
                        journal.append_terminal(
                            "run_failed", {"node": node_name, "error": str(exc)}
                        )
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
                        checkpoint_id = f"{run_id}:{node_name}:{checkpoint_sequence}"
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
                                "remaining_nodes": list(checkpoint_remaining_nodes),
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
                            self._checkpoint_state_digests[checkpoint_id] = (
                                checkpoint_state_digest
                            )
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
                                raise RuntimeError(
                                    f"output path conflict at {edge['to']}"
                                )
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
                journal.append_terminal(
                    "run_failed", {"error": f"unresolved dependencies: {unresolved}"}
                )
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
    profile: Literal["preview", "stable"],
) -> RuntimeRegistry:
    from .stdlib_governance import GOVERNANCE_IMPLEMENTATIONS
    from .stdlib_rag import RAG_IMPLEMENTATIONS
    from .stdlib_runtime_handlers import core_stdlib_implementations

    catalog = builtin_block_catalog(profile=profile)
    registry = RuntimeRegistry(
        block_catalog=catalog,
        allow_untyped=allow_untyped,
    )
    implementation_handlers: dict[str, BlockCallable] = {}
    for source in (
        core_stdlib_implementations(registry.resolve),
        RAG_IMPLEMENTATIONS,
        GOVERNANCE_IMPLEMENTATIONS,
    ):
        duplicate_implementations = set(implementation_handlers).intersection(source)
        if duplicate_implementations:
            raise RuntimeError(
                "stdlib implementation handlers are duplicated: "
                + ", ".join(sorted(duplicate_implementations))
            )
        implementation_handlers.update(source)

    manifest_bindings = builtin_block_implementations()
    catalog_block_ids = set(catalog.descriptors)
    manifest_block_ids = set(manifest_bindings)
    if profile == "preview" and catalog_block_ids != manifest_block_ids:
        missing_from_catalog = sorted(manifest_block_ids - catalog_block_ids)
        missing_from_manifest = sorted(catalog_block_ids - manifest_block_ids)
        raise RuntimeError(
            "preview stdlib catalog and manifest block inventory differ; "
            f"missing from catalog={missing_from_catalog}, "
            f"missing from manifest={missing_from_manifest}"
        )
    missing_manifest_bindings = sorted(catalog_block_ids - manifest_block_ids)
    if missing_manifest_bindings:
        raise RuntimeError(
            "stdlib catalog blocks lack manifest implementation bindings: "
            + ", ".join(missing_manifest_bindings)
        )

    required_implementations = {
        manifest_bindings[block_id] for block_id in catalog_block_ids
    }
    missing_handlers = sorted(
        required_implementations - set(implementation_handlers)
    )
    if missing_handlers:
        raise RuntimeError(
            "stdlib manifest implementations lack Python handlers: "
            + ", ".join(missing_handlers)
        )
    if profile == "preview":
        unreferenced_handlers = sorted(
            set(implementation_handlers) - required_implementations
        )
        if unreferenced_handlers:
            raise RuntimeError(
                "Python stdlib handlers are absent from the manifest: "
                + ", ".join(unreferenced_handlers)
            )

    for block_id in catalog.descriptors:
        implementation_id = manifest_bindings[block_id]
        registry.register(block_id, implementation_handlers[implementation_id])
    if profile == "preview" and not allow_untyped:
        registry._profile_marker = _FULL_STDLIB_REGISTRY_MARKER
        registry._profile_blocks = tuple(registry.blocks.items())
        registry._profile_block_catalog = registry.block_catalog
        registry._profile_allow_untyped = registry.allow_untyped
    return registry


def stdlib_registry(*, allow_untyped: bool = False) -> RuntimeRegistry:
    """Return the full preview stdlib across all implemented profiles."""

    return _stdlib_registry(
        allow_untyped=allow_untyped,
        profile="preview",
    )


def is_full_stdlib_registry(registry: object) -> bool:
    """Return whether a registry is an unmodified full stdlib factory result."""

    if (
        not isinstance(registry, RuntimeRegistry)
        or registry._profile_marker is not _FULL_STDLIB_REGISTRY_MARKER
        or type(registry.blocks) is not dict
        or registry.block_catalog is not registry._profile_block_catalog
        or registry.allow_untyped is not registry._profile_allow_untyped
        or len(registry.blocks) != len(registry._profile_blocks)
    ):
        return False
    return all(
        registry.blocks.get(block_id) is handler
        for block_id, handler in registry._profile_blocks
    )


def core_stdlib_registry(*, allow_untyped: bool = False) -> RuntimeRegistry:
    """Return the stable C1 handler and descriptor subset of the stdlib."""

    return _stdlib_registry(
        allow_untyped=allow_untyped,
        profile="stable",
    )

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
import pickle
from threading import Barrier, Event

import pytest

from graphblocks.runtime import (
    ExecutionJournal,
    JournalSnapshot,
    JournalRecord,
    JournalStateError,
    LocalExecutionJournal,
    LocalJournalRecord,
    SQLiteExecutionJournal,
)


def _invalid_journal_payload(case: str) -> dict[object, object]:
    if case == "object":
        return {"value": object()}
    if case == "bytes":
        return {"value": b"\x00\x01"}
    if case == "set":
        return {"value": {1, 2, 3}}
    if case == "cycle":
        payload: dict[object, object] = {}
        payload["self"] = payload
        return payload
    if case == "non_string_key":
        return {1: "value"}
    if case == "nan":
        return {"value": math.nan}
    if case == "positive_infinity":
        return {"value": math.inf}
    if case == "negative_infinity":
        return {"value": -math.inf}
    raise AssertionError(f"unknown invalid journal payload case: {case}")


def test_execution_journal_records_snapshot_payloads_and_freeze_nested_values() -> None:
    journal = ExecutionJournal("run-000001")
    payload = {
        "outputs": {"answer": "ok"},
        "events": [{"kind": "RunStarted"}],
    }

    record = journal.append("node_succeeded", payload)
    payload["outputs"]["answer"] = "mutated"
    payload["events"][0]["kind"] = "mutated"

    assert record.payload["outputs"] == {"answer": "ok"}
    assert record.payload["events"] == ({"kind": "RunStarted"},)
    assert record.to_dict() == {
        "sequence": 1,
        "kind": "node_succeeded",
        "payload": {
            "outputs": {"answer": "ok"},
            "events": [{"kind": "RunStarted"}],
        },
    }
    with pytest.raises(TypeError):
        record.payload["outputs"]["answer"] = "mutated"
    with pytest.raises(TypeError):
        record.payload["events"][0]["kind"] = "mutated"


@pytest.mark.parametrize(
    ("journal_type", "storage_name"),
    (
        (ExecutionJournal, "_records"),
        (LocalExecutionJournal, "records"),
    ),
)
def test_in_memory_journal_append_keeps_constant_time_internal_storage(
    journal_type: type[ExecutionJournal] | type[LocalExecutionJournal],
    storage_name: str,
) -> None:
    journal = journal_type("run-000001")
    initial_snapshot = journal.records
    storage = object.__getattribute__(journal, storage_name)

    for index in range(1_000):
        journal.append("node_started", {"node": f"node-{index}"})

    assert isinstance(storage, list)
    assert object.__getattribute__(journal, storage_name) is storage
    assert initial_snapshot == ()
    assert isinstance(journal.records, tuple)
    assert len(journal.records) == 1_000


def test_execution_journal_is_unhashable_and_uses_identity_equality() -> None:
    journal = ExecutionJournal("run-000001")
    same_state = ExecutionJournal("run-000001")

    assert journal is not same_state
    assert journal != same_state
    with pytest.raises(TypeError):
        hash(journal)

    journal.append("run_started", {})

    with pytest.raises(TypeError):
        hash(journal)


def test_execution_journal_snapshot_is_detached_and_immutable() -> None:
    journal = ExecutionJournal("run-000001")
    journal.append("run_started", {"input": {"value": 1}})
    running_snapshot = journal.snapshot()

    journal.append("node_started", {"node": "first"})
    journal.append_terminal("run_succeeded", {"outputs": {"answer": "ok"}})
    terminal_snapshot = journal.snapshot()

    assert isinstance(running_snapshot, JournalSnapshot)
    assert running_snapshot == JournalSnapshot(
        "run-000001",
        running_snapshot.records,
    )
    assert [record.kind for record in running_snapshot.records] == ["run_started"]
    assert running_snapshot.terminal_kind is None
    assert [record.kind for record in terminal_snapshot.records] == [
        "run_started",
        "node_started",
        "run_succeeded",
    ]
    assert terminal_snapshot.terminal_kind == "run_succeeded"
    with pytest.raises(AttributeError):
        running_snapshot.terminal_kind = "run_succeeded"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        running_snapshot.records += terminal_snapshot.records  # type: ignore[misc]
    with pytest.raises(TypeError):
        running_snapshot.records[0].payload["input"]["value"] = 2
    with pytest.raises(TypeError):
        hash(running_snapshot)
    with pytest.raises(AttributeError):
        journal.run_id = "different-run"  # type: ignore[misc]


def test_execution_journal_serializes_concurrent_appends_and_terminal_commit() -> None:
    journal = ExecutionJournal("run-000001")
    append_barrier = Barrier(9)

    def append_record(index: int) -> int:
        append_barrier.wait(timeout=5)
        return journal.append("node_started", {"node": index}).sequence

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(append_record, index) for index in range(8)]
        append_barrier.wait(timeout=5)
        sequences = [future.result(timeout=5) for future in futures]

    terminal_barrier = Barrier(3)

    def append_terminal(kind: str) -> str:
        terminal_barrier.wait(timeout=5)
        try:
            return journal.append_terminal(kind, {"kind": kind}).kind  # type: ignore[arg-type]
        except JournalStateError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        terminal_futures = [
            executor.submit(append_terminal, "run_succeeded"),
            executor.submit(append_terminal, "run_failed"),
        ]
        terminal_barrier.wait(timeout=5)
        outcomes = [future.result(timeout=5) for future in terminal_futures]

    snapshot = journal.snapshot()

    assert sorted(sequences) == list(range(1, 9))
    assert [record.sequence for record in snapshot.records] == list(range(1, 10))
    assert outcomes.count("rejected") == 1
    assert snapshot.terminal_kind in {"run_succeeded", "run_failed"}
    assert snapshot.records[-1].kind == snapshot.terminal_kind


def test_execution_journal_canonicalizes_payload_without_holding_state_lock() -> None:
    journal = ExecutionJournal("run-000001")
    canonicalization_started = Event()
    release_canonicalization = Event()

    class SnapshotDependentPayload(dict[str, object]):
        def items(self):
            canonicalization_started.set()
            if not release_canonicalization.wait(timeout=5):
                raise RuntimeError("snapshot did not complete")
            return super().items()

    with ThreadPoolExecutor(max_workers=2) as executor:
        terminal_future = executor.submit(
            journal.append_terminal,
            "run_succeeded",
            SnapshotDependentPayload({"outputs": {}}),
        )
        assert canonicalization_started.wait(timeout=5)
        snapshot_future = executor.submit(journal.snapshot)
        try:
            running_snapshot = snapshot_future.result(timeout=2)
        finally:
            release_canonicalization.set()
        terminal_record = terminal_future.result(timeout=5)

    terminal_snapshot = journal.snapshot()

    assert running_snapshot.records == ()
    assert running_snapshot.terminal_kind is None
    assert terminal_record.sequence == 1
    assert terminal_snapshot.records == (terminal_record,)
    assert terminal_snapshot.terminal_kind == "run_succeeded"


@pytest.mark.parametrize("journal_type", (ExecutionJournal, LocalExecutionJournal))
def test_in_memory_journal_restores_mutable_storage_after_pickle_round_trip(
    journal_type: type[ExecutionJournal] | type[LocalExecutionJournal],
) -> None:
    journal = journal_type("run-000001")
    journal.append("node_started", {"node": "first"})

    restored = pickle.loads(pickle.dumps(journal))
    appended = restored.append("node_started", {"node": "second"})

    assert appended.sequence == 2
    assert [record.payload["node"] for record in restored.records] == [
        "first",
        "second",
    ]


def test_execution_journal_pickle_preserves_terminal_seal() -> None:
    journal = ExecutionJournal("run-000001")
    journal.append("run_started", {})
    journal.append_terminal("run_succeeded", {"outputs": {}})

    restored = pickle.loads(pickle.dumps(journal))

    assert restored.snapshot() == journal.snapshot()
    with pytest.raises(JournalStateError, match="after terminal"):
        restored.append("node_started", {"node": "late"})


def test_execution_journal_restores_legacy_slot_pickle_state() -> None:
    records = (
        JournalRecord(1, "run_started", {}),
        JournalRecord(2, "run_succeeded", {"outputs": {}}),
    )

    class LegacyExecutionJournal:
        def __reduce__(self):
            return (
                object.__new__,
                (ExecutionJournal,),
                ["run-legacy", records, "run_succeeded"],
            )

    restored = pickle.loads(pickle.dumps(LegacyExecutionJournal()))

    assert isinstance(restored, ExecutionJournal)
    assert restored.run_id == "run-legacy"
    assert restored.records == records
    assert restored.terminal_kind == "run_succeeded"
    with pytest.raises(JournalStateError, match="after terminal"):
        restored.append("node_started", {"node": "late"})


def test_journal_record_backends_share_canonical_nested_payload_snapshot(
    tmp_path,
) -> None:
    payload = {
        "items": (
            {
                "values": [("alpha", "beta")],
            },
        ),
    }
    expected = {
        "sequence": 1,
        "kind": "node_succeeded",
        "payload": {
            "items": [
                {
                    "values": [["alpha", "beta"]],
                },
            ],
        },
    }
    local_record = LocalJournalRecord(1, "node_succeeded", payload)
    execution_record = JournalRecord(1, "node_succeeded", payload)
    database = tmp_path / "canonical-journal.sqlite3"
    sqlite_journal = SQLiteExecutionJournal(database, "run-000001")
    sqlite_journal.append("node_succeeded", payload)
    sqlite_journal.close()

    reopened = SQLiteExecutionJournal(database, "run-000001")
    try:
        persisted_record = reopened.records[0]
    finally:
        reopened.close()

    assert local_record.to_dict() == expected
    assert execution_record.to_dict() == expected
    assert persisted_record.to_dict() == expected


def test_sqlite_execution_journal_returns_its_single_persisted_payload_snapshot(
    tmp_path,
) -> None:
    class StatefulDict(dict[str, object]):
        def __init__(self) -> None:
            super().__init__({"unsafe": object()})
            self.calls = 0

        def items(self):
            self.calls += 1
            return (("safe", {"value": 1}),)

    payload = StatefulDict()
    database = tmp_path / "snapshot-once.sqlite3"
    journal = SQLiteExecutionJournal(database, "run-000001")
    record = journal.append("node_succeeded", payload)
    journal.close()

    reopened = SQLiteExecutionJournal(database, "run-000001")
    try:
        persisted_record = reopened.records[0]
    finally:
        reopened.close()

    assert payload.calls == 1
    assert record.to_dict()["payload"] == {"safe": {"value": 1}}
    assert persisted_record.to_dict() == record.to_dict()


@pytest.mark.parametrize(
    "case",
    (
        "object",
        "bytes",
        "set",
        "cycle",
        "non_string_key",
        "nan",
        "positive_infinity",
        "negative_infinity",
    ),
)
@pytest.mark.parametrize(
    "target", ("local_record", "execution_record", "sqlite_append")
)
def test_journal_record_backends_reject_non_json_payloads(
    tmp_path,
    target: str,
    case: str,
) -> None:
    payload = _invalid_journal_payload(case)

    with pytest.raises(ValueError, match="must be valid strict JSON"):
        if target == "local_record":
            LocalJournalRecord(1, "node_succeeded", payload)  # type: ignore[arg-type]
        elif target == "execution_record":
            JournalRecord(1, "node_succeeded", payload)  # type: ignore[arg-type]
        else:
            journal = SQLiteExecutionJournal(tmp_path / "journal.sqlite3", "run-000001")
            try:
                journal.append("node_succeeded", payload)  # type: ignore[arg-type]
            finally:
                journal.close()


def test_sqlite_execution_journal_persists_records_across_instances(tmp_path) -> None:
    database = tmp_path / "journal.sqlite3"
    first = SQLiteExecutionJournal(database, "run-000001")
    first.append("run_started", {"graphHash": "sha256:test"})
    first.append("node_started", {"node": "render"})
    first.append_terminal("run_succeeded", {"outputs": {"answer": "ok"}})
    first.close()

    second = SQLiteExecutionJournal(database, "run-000001")

    assert [record.kind for record in second.records] == ["run_started", "node_started", "run_succeeded"]
    assert second.records[2].payload == {"outputs": {"answer": "ok"}}
    assert second.terminal_kind == "run_succeeded"


def test_sqlite_execution_journal_serializes_concurrent_sequence_assignment(
    tmp_path,
) -> None:
    database = tmp_path / "concurrent-journal.sqlite3"
    writer_count = 8
    barrier = Barrier(writer_count)

    def append(index: int) -> int:
        journal = SQLiteExecutionJournal(database, "run-000001")
        try:
            barrier.wait()
            return journal.append("node_started", {"writer": index}).sequence
        finally:
            journal.close()

    with ThreadPoolExecutor(max_workers=writer_count) as executor:
        sequences = list(executor.map(append, range(writer_count)))

    assert sorted(sequences) == list(range(1, writer_count + 1))


def test_sqlite_execution_journal_rejects_non_standard_payload_json_on_replay(tmp_path) -> None:
    database = tmp_path / "journal.sqlite3"
    journal = SQLiteExecutionJournal(database, "run-000001")
    journal.append("run_started", {"graphHash": "sha256:test"})
    journal.connection.execute(
        "UPDATE journal_records SET payload_json = ? WHERE run_id = ? AND sequence = ?",
        ('{"value": NaN}', "run-000001", 1),
    )
    journal.connection.commit()

    with pytest.raises(ValueError, match="execution journal payload_json must be valid strict JSON"):
        journal.records


def test_sqlite_execution_journal_rejects_non_finite_payloads_on_append(tmp_path) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "journal.sqlite3", "run-000001")

    with pytest.raises(ValueError, match="execution journal payload must be valid strict JSON"):
        journal.append("node_succeeded", {"value": math.nan})


def test_sqlite_execution_journal_rejects_second_terminal(tmp_path) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "journal.sqlite3", "run-000001")
    journal.append_terminal("run_failed", {"error": "first"})

    with pytest.raises(JournalStateError):
        journal.append_terminal("run_succeeded", {"outputs": {}})


@pytest.mark.parametrize("sqlite", (False, True))
def test_execution_journal_enforces_terminal_kind_api(tmp_path, sqlite: bool) -> None:
    journal = (
        SQLiteExecutionJournal(tmp_path / "journal.sqlite3", "run-000001")
        if sqlite
        else ExecutionJournal("run-000001")
    )

    with pytest.raises(JournalStateError, match="must be recorded with append_terminal"):
        journal.append("run_succeeded", {"outputs": {}})
    with pytest.raises(ValueError, match="terminal kind is invalid"):
        journal.append_terminal("node_started", {"node": "render"})  # type: ignore[arg-type]

    assert tuple(journal.records) == ()


def test_sqlite_execution_journal_rejects_append_after_terminal_on_reopen(tmp_path) -> None:
    database = tmp_path / "journal.sqlite3"
    first = SQLiteExecutionJournal(database, "run-000001")
    first.append_terminal("run_cancelled", {"reason": "user"})
    first.close()

    reopened = SQLiteExecutionJournal(database, "run-000001")
    with pytest.raises(JournalStateError):
        reopened.append("node_succeeded", {"node": "late"})

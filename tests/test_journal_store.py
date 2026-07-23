from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
from threading import Barrier

import pytest

from graphblocks.runtime import ExecutionJournal, JournalStateError, SQLiteExecutionJournal


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

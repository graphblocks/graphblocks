from __future__ import annotations

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


def test_sqlite_execution_journal_rejects_second_terminal(tmp_path) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "journal.sqlite3", "run-000001")
    journal.append_terminal("run_failed", {"error": "first"})

    with pytest.raises(JournalStateError):
        journal.append_terminal("run_succeeded", {"outputs": {}})


def test_sqlite_execution_journal_rejects_append_after_terminal_on_reopen(tmp_path) -> None:
    database = tmp_path / "journal.sqlite3"
    first = SQLiteExecutionJournal(database, "run-000001")
    first.append_terminal("run_cancelled", {"reason": "user"})
    first.close()

    reopened = SQLiteExecutionJournal(database, "run-000001")
    with pytest.raises(JournalStateError):
        reopened.append("node_succeeded", {"node": "late"})

from __future__ import annotations

import pytest

from graphblocks.run_store import InMemoryRunStore, RunTerminalStateError, SQLiteRunStore, StateConflictError


def test_run_store_applies_state_patch_with_revision_cas() -> None:
    store = InMemoryRunStore()
    record = store.create_run("sha256:test", {"message": {"text": "hello"}})

    updated = store.patch_state(record.run_id, {"conversation": {"turns": 1}}, expected_revision=0)

    assert updated.state_revision == 1
    assert updated.state == {"conversation": {"turns": 1}}


def test_run_store_rejects_stale_state_patch() -> None:
    store = InMemoryRunStore()
    record = store.create_run("sha256:test", {})
    store.patch_state(record.run_id, {"count": 1}, expected_revision=0)

    with pytest.raises(StateConflictError) as error:
        store.patch_state(record.run_id, {"count": 2}, expected_revision=0)

    assert error.value.current_revision == 1


def test_run_store_returns_defensive_copies() -> None:
    store = InMemoryRunStore()
    record = store.create_run("sha256:test", {})
    updated = store.patch_state(record.run_id, {"nested": {"value": 1}}, expected_revision=0)

    updated.state["nested"]["value"] = 99

    assert store.get_run(record.run_id).state == {"nested": {"value": 1}}


def test_state_patch_deletes_key_with_none_value() -> None:
    store = InMemoryRunStore()
    record = store.create_run("sha256:test", {})
    store.patch_state(record.run_id, {"kept": True, "removed": True}, expected_revision=0)

    updated = store.patch_state(record.run_id, {"removed": None}, expected_revision=1)

    assert updated.state == {"kept": True}
    assert updated.state_revision == 2


def test_run_store_rejects_state_and_status_mutation_after_terminal_status() -> None:
    store = InMemoryRunStore()
    record = store.create_run("sha256:test", {})
    store.set_status(record.run_id, "succeeded")

    with pytest.raises(RunTerminalStateError) as patch_error:
        store.patch_state(record.run_id, {"late": True}, expected_revision=0)

    assert patch_error.value.run_id == record.run_id
    assert patch_error.value.status == "succeeded"
    assert str(patch_error.value) == f"run {record.run_id} is terminal with status succeeded"

    with pytest.raises(RunTerminalStateError):
        store.set_status(record.run_id, "failed")


def test_sqlite_run_store_persists_records_across_instances(tmp_path) -> None:
    database = tmp_path / "runs.sqlite3"
    first = SQLiteRunStore(database)
    record = first.create_run("sha256:test", {"message": {"text": "hello"}})
    first.patch_state(record.run_id, {"conversation": {"turns": 1}}, expected_revision=0)
    first.set_status(record.run_id, "succeeded")
    first.close()

    second = SQLiteRunStore(database)
    loaded = second.get_run(record.run_id)

    assert loaded.graph_hash == "sha256:test"
    assert loaded.inputs == {"message": {"text": "hello"}}
    assert loaded.status == "succeeded"
    assert loaded.state == {"conversation": {"turns": 1}}
    assert loaded.state_revision == 1


def test_sqlite_run_store_enforces_state_revision_cas(tmp_path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    record = store.create_run("sha256:test", {})
    store.patch_state(record.run_id, {"count": 1}, expected_revision=0)

    with pytest.raises(StateConflictError) as error:
        store.patch_state(record.run_id, {"count": 2}, expected_revision=0)

    assert error.value.current_revision == 1
    assert store.get_run(record.run_id).state == {"count": 1}


def test_sqlite_run_store_rejects_state_and_status_mutation_after_terminal_status(tmp_path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    record = store.create_run("sha256:test", {})
    store.set_status(record.run_id, "cancelled")

    with pytest.raises(RunTerminalStateError) as patch_error:
        store.patch_state(record.run_id, {"late": True}, expected_revision=0)

    assert patch_error.value.run_id == record.run_id
    assert patch_error.value.status == "cancelled"
    assert store.get_run(record.run_id).state == {}

    with pytest.raises(RunTerminalStateError):
        store.set_status(record.run_id, "running")

    assert store.get_run(record.run_id).status == "cancelled"


def test_sqlite_run_store_allocates_monotonic_run_ids_after_reopen(tmp_path) -> None:
    database = tmp_path / "runs.sqlite3"
    first = SQLiteRunStore(database)
    assert first.create_run("sha256:one", {}).run_id == "run-000001"
    first.close()

    second = SQLiteRunStore(database)
    assert second.create_run("sha256:two", {}).run_id == "run-000002"

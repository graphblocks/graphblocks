from __future__ import annotations

import pytest

from graphblocks.run_store import InMemoryRunStore, StateConflictError


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


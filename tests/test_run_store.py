from __future__ import annotations

import pytest

from graphblocks.evaluation import ModelVisibleToolRef
from graphblocks.run_store import (
    InMemoryRunStore,
    RunDeploymentProvenance,
    RunTerminalStateError,
    SQLiteRunStore,
    StateConflictError,
)


def test_run_store_applies_state_patch_with_revision_cas() -> None:
    store = InMemoryRunStore()
    record = store.create_run("sha256:test", {"message": {"text": "hello"}})

    updated = store.patch_state(record.run_id, {"conversation": {"turns": 1}}, expected_revision=0)

    assert updated.state_revision == 1
    assert updated.state == {"conversation": {"turns": 1}}


def test_run_store_records_deployment_provenance_and_preserves_it_across_mutations() -> None:
    store = InMemoryRunStore()
    provenance = RunDeploymentProvenance(
        release_digest="sha256:release",
        deployment_revision_id="rev-1",
        physical_plan_hash="sha256:physical",
        release_signature_digest="sha256:signature",
    )

    record = store.create_run("sha256:test", {}, deployment_provenance=provenance)
    patched = store.patch_state(record.run_id, {"step": 1}, expected_revision=0)
    running = store.set_status(record.run_id, "running")

    assert record.deployment_provenance.canonical_value() == {
        "release_digest": "sha256:release",
        "deployment_revision_id": "rev-1",
        "physical_plan_hash": "sha256:physical",
        "release_signature_digest": "sha256:signature",
    }
    assert patched.deployment_provenance == provenance
    assert running.deployment_provenance == provenance


def test_run_store_records_model_visible_tools_and_preserves_them_across_mutations() -> None:
    store = InMemoryRunStore()
    ticket_tool = _model_visible_tool("ticket.create", "resolved-ticket", False)
    search_tool = _model_visible_tool("knowledge.search", "resolved-search", True)

    record = store.create_run(
        "sha256:test",
        {},
        model_visible_tools=(ticket_tool, search_tool),
    )
    patched = store.patch_state(record.run_id, {"step": 1}, expected_revision=0)
    running = store.set_status(record.run_id, "running")

    assert record.model_visible_tools == (search_tool, ticket_tool)
    assert patched.model_visible_tools == record.model_visible_tools
    assert running.model_visible_tools == record.model_visible_tools


def test_run_store_records_model_visible_tools_after_run_creation() -> None:
    store = InMemoryRunStore()
    record = store.create_run("sha256:test", {})
    ticket_tool = _model_visible_tool("ticket.create", "resolved-ticket", False)
    search_tool = _model_visible_tool("knowledge.search", "resolved-search", True)

    updated = store.record_model_visible_tools(
        record.run_id,
        (ticket_tool, search_tool),
    )

    assert updated.model_visible_tools == (search_tool, ticket_tool)
    assert updated.state_revision == 0
    assert store.get_run(record.run_id).model_visible_tools == (search_tool, ticket_tool)


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

    with pytest.raises(RunTerminalStateError):
        store.record_model_visible_tools(
            record.run_id,
            (_model_visible_tool("knowledge.search", "resolved-search", True),),
        )


def test_run_store_treats_policy_stopped_as_terminal_status() -> None:
    store = InMemoryRunStore()
    record = store.create_run("sha256:test", {})
    stopped = store.set_status(record.run_id, "policy_stopped")

    assert stopped.status == "policy_stopped"
    with pytest.raises(RunTerminalStateError) as patch_error:
        store.patch_state(record.run_id, {"late": True}, expected_revision=0)

    assert patch_error.value.status == "policy_stopped"
    with pytest.raises(RunTerminalStateError):
        store.set_status(record.run_id, "failed")


def test_sqlite_run_store_persists_records_across_instances(tmp_path) -> None:
    database = tmp_path / "runs.sqlite3"
    first = SQLiteRunStore(database)
    provenance = RunDeploymentProvenance(
        release_digest="sha256:release",
        deployment_revision_id="rev-1",
        physical_plan_hash="sha256:physical",
        release_signature_digest="sha256:signature",
    )
    record = first.create_run(
        "sha256:test",
        {"message": {"text": "hello"}},
        deployment_provenance=provenance,
        model_visible_tools=(
            _model_visible_tool("ticket.create", "resolved-ticket", False),
            _model_visible_tool("knowledge.search", "resolved-search", True),
        ),
    )
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
    assert loaded.deployment_provenance == provenance
    assert loaded.model_visible_tools == (
        _model_visible_tool("knowledge.search", "resolved-search", True),
        _model_visible_tool("ticket.create", "resolved-ticket", False),
    )


def test_sqlite_run_store_records_model_visible_tools_after_run_creation(tmp_path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    record = store.create_run("sha256:test", {})
    ticket_tool = _model_visible_tool("ticket.create", "resolved-ticket", False)
    search_tool = _model_visible_tool("knowledge.search", "resolved-search", True)

    updated = store.record_model_visible_tools(
        record.run_id,
        (ticket_tool, search_tool),
    )

    assert updated.model_visible_tools == (search_tool, ticket_tool)
    assert updated.state_revision == 0
    assert store.get_run(record.run_id).model_visible_tools == (search_tool, ticket_tool)


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
    with pytest.raises(RunTerminalStateError):
        store.record_model_visible_tools(
            record.run_id,
            (_model_visible_tool("knowledge.search", "resolved-search", True),),
        )


def _model_visible_tool(
    tool_name: str,
    resolved_tool_id: str,
    allowed_for_principal: bool,
) -> ModelVisibleToolRef:
    return ModelVisibleToolRef(
        tool_name=tool_name,
        resolved_tool_id=resolved_tool_id,
        definition_digest="sha256:definition",
        binding_digest="sha256:binding",
        effective_policy_snapshot_id="policy-snapshot-1",
        allowed_for_principal=allowed_for_principal,
        valid_until="2026-06-30T00:00:00Z",
    )


def test_sqlite_run_store_treats_policy_stopped_as_terminal_status(tmp_path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    record = store.create_run("sha256:test", {})
    stopped = store.set_status(record.run_id, "policy_stopped")

    assert stopped.status == "policy_stopped"
    with pytest.raises(RunTerminalStateError) as patch_error:
        store.patch_state(record.run_id, {"late": True}, expected_revision=0)

    assert patch_error.value.status == "policy_stopped"
    assert store.get_run(record.run_id).state == {}
    with pytest.raises(RunTerminalStateError):
        store.set_status(record.run_id, "running")


def test_sqlite_run_store_allocates_monotonic_run_ids_after_reopen(tmp_path) -> None:
    database = tmp_path / "runs.sqlite3"
    first = SQLiteRunStore(database)
    assert first.create_run("sha256:one", {}).run_id == "run-000001"
    first.close()

    second = SQLiteRunStore(database)
    assert second.create_run("sha256:two", {}).run_id == "run-000002"

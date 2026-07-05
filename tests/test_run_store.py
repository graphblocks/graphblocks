from __future__ import annotations

import math

import pytest

import graphblocks
from graphblocks.evaluation import ModelVisibleToolRef
from graphblocks.run_store import (
    InMemoryRunStore,
    RunDeploymentProvenance,
    RunRecord,
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


def test_run_store_records_invocation_mode_and_preserves_it_across_mutations() -> None:
    store = InMemoryRunStore()
    accepted = store.create_run("sha256:test", {}, invocation_mode="accepted")
    patched = store.patch_state(accepted.run_id, {"step": 1}, expected_revision=0)
    running = store.set_status(accepted.run_id, "running")

    assert accepted.invocation_mode == "accepted"
    assert patched.invocation_mode == "accepted"
    assert running.invocation_mode == "accepted"

    background = store.create_run("sha256:test", {}, invocation_mode="background")
    assert background.invocation_mode == "background"
    assert "RunInvocationMode" in graphblocks.__all__


def test_run_records_validate_identity_status_revision_and_payload_shapes() -> None:
    with pytest.raises(ValueError, match="run deployment provenance release_digest must not be empty"):
        RunDeploymentProvenance(release_digest=" ")
    with pytest.raises(ValueError, match="run record run_id must not be empty"):
        RunRecord(" ", "sha256:test", {})
    with pytest.raises(ValueError, match="run record graph_hash must not be empty"):
        RunRecord("run-1", " ", {})
    with pytest.raises(ValueError, match="run record inputs must be an object"):
        RunRecord("run-1", "sha256:test", [])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid run record status"):
        RunRecord("run-1", "sha256:test", {}, status="paused")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid run record invocation_mode"):
        RunRecord("run-1", "sha256:test", {}, invocation_mode="deferred")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="run record state_revision must be non-negative"):
        RunRecord("run-1", "sha256:test", {}, state_revision=-1)
    with pytest.raises(ValueError, match="run record model_visible_tools must be ModelVisibleToolRef"):
        RunRecord("run-1", "sha256:test", {}, model_visible_tools=(object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="run record inputs.payload must contain only JSON values"):
        RunRecord("run-1", "sha256:test", {"payload": object()})
    with pytest.raises(ValueError, match="run record state.value must not contain non-finite numbers"):
        RunRecord("run-1", "sha256:test", {}, state={"value": math.nan})


def test_run_store_validates_create_patch_status_and_copies_inputs() -> None:
    store = InMemoryRunStore()
    inputs = {"message": {"text": "hello"}}
    record = store.create_run(" sha256:test ", inputs)
    inputs["message"]["text"] = "mutated"

    assert record.graph_hash == "sha256:test"
    assert store.get_run(record.run_id).inputs == {"message": {"text": "hello"}}
    with pytest.raises(ValueError, match="run store graph_hash must not be empty"):
        store.create_run(" ", {})
    with pytest.raises(ValueError, match="run store inputs must be an object"):
        store.create_run("sha256:test", [])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="run store inputs.payload must contain only JSON values"):
        store.create_run("sha256:test", {"payload": object()})
    with pytest.raises(ValueError, match="invalid run invocation mode"):
        store.create_run("sha256:test", {}, invocation_mode="deferred")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="run store patch must be an object"):
        store.patch_state(record.run_id, [], expected_revision=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="run store patch.value must not contain non-finite numbers"):
        store.patch_state(record.run_id, {"value": math.inf}, expected_revision=0)
    with pytest.raises(ValueError, match="run store expected_revision must be non-negative"):
        store.patch_state(record.run_id, {}, expected_revision=-1)
    with pytest.raises(ValueError, match="invalid mutable run status"):
        store.set_status(record.run_id, "created")  # type: ignore[arg-type]


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


def test_run_store_accepts_durable_async_lifecycle_statuses() -> None:
    mutable_statuses = (
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
    )

    for status in mutable_statuses:
        store = InMemoryRunStore()
        record = store.create_run("sha256:test", {})
        updated = store.set_status(record.run_id, status)  # type: ignore[arg-type]
        patched = store.patch_state(record.run_id, {"status": status}, expected_revision=0)

        assert updated.status == status
        assert patched.state == {"status": status}


def test_run_store_treats_completed_and_expired_as_terminal_statuses() -> None:
    for terminal_status in ("completed", "expired"):
        store = InMemoryRunStore()
        record = store.create_run("sha256:test", {})
        terminal = store.set_status(record.run_id, terminal_status)  # type: ignore[arg-type]

        assert terminal.status == terminal_status
        with pytest.raises(RunTerminalStateError) as patch_error:
            store.patch_state(record.run_id, {"late": True}, expected_revision=0)

        assert patch_error.value.status == terminal_status
        with pytest.raises(RunTerminalStateError):
            store.set_status(record.run_id, "running")


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


def test_sqlite_run_store_persists_invocation_mode_across_instances(tmp_path) -> None:
    database = tmp_path / "runs.sqlite3"
    first = SQLiteRunStore(database)
    accepted = first.create_run("sha256:accepted", {}, invocation_mode="accepted")
    background = first.create_run("sha256:background", {}, invocation_mode="background")
    first.patch_state(accepted.run_id, {"step": 1}, expected_revision=0)
    first.set_status(background.run_id, "running")
    first.close()

    second = SQLiteRunStore(database)

    assert second.get_run(accepted.run_id).invocation_mode == "accepted"
    assert second.get_run(background.run_id).invocation_mode == "background"


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


def test_sqlite_run_store_validates_create_patch_and_status_arguments(tmp_path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    record = store.create_run("sha256:test", {})

    with pytest.raises(ValueError, match="run store graph_hash must not be empty"):
        store.create_run(" ", {})
    with pytest.raises(ValueError, match="run store inputs must be an object"):
        store.create_run("sha256:test", [])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="run store run_id must not be empty"):
        store.get_run(" ")
    with pytest.raises(ValueError, match="run store patch must be an object"):
        store.patch_state(record.run_id, [], expected_revision=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="run store expected_revision must be an integer"):
        store.patch_state(record.run_id, {}, expected_revision=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid mutable run status"):
        store.set_status(record.run_id, "created")  # type: ignore[arg-type]


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


def test_sqlite_run_store_persists_durable_async_lifecycle_statuses(tmp_path) -> None:
    database = tmp_path / "runs.sqlite3"
    first = SQLiteRunStore(database)
    mutable_statuses = (
        "admitted",
        "waiting_input",
        "waiting_approval",
        "waiting_review",
        "waiting_callback",
        "paused_budget",
        "paused_callback_delivery",
        "paused_policy",
        "paused_operator",
        "resuming",
    )

    for index, status in enumerate(mutable_statuses, start=1):
        record = first.create_run(f"sha256:test-{index}", {})
        first.set_status(record.run_id, status)  # type: ignore[arg-type]
    first.close()

    second = SQLiteRunStore(database)
    for index, status in enumerate(mutable_statuses, start=1):
        assert second.get_run(f"run-{index:06d}").status == status


def test_sqlite_run_store_treats_completed_and_expired_as_terminal_statuses(tmp_path) -> None:
    for terminal_status in ("completed", "expired"):
        store = SQLiteRunStore(tmp_path / f"runs-{terminal_status}.sqlite3")
        record = store.create_run("sha256:test", {})
        terminal = store.set_status(record.run_id, terminal_status)  # type: ignore[arg-type]

        assert terminal.status == terminal_status
        with pytest.raises(RunTerminalStateError) as patch_error:
            store.patch_state(record.run_id, {"late": True}, expected_revision=0)

        assert patch_error.value.status == terminal_status
        with pytest.raises(RunTerminalStateError):
            store.set_status(record.run_id, "running")


def test_sqlite_run_store_allocates_monotonic_run_ids_after_reopen(tmp_path) -> None:
    database = tmp_path / "runs.sqlite3"
    first = SQLiteRunStore(database)
    assert first.create_run("sha256:one", {}).run_id == "run-000001"
    first.close()

    second = SQLiteRunStore(database)
    assert second.create_run("sha256:two", {}).run_id == "run-000002"

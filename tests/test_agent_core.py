from __future__ import annotations

import pytest

from graphblocks import (
    AgentLoopController,
    AgentLoopDecision,
    AgentSpec,
    AgentState,
    AgentStateError,
    AgentStatePatch,
    AgentStatePatchOp,
    AgentStateSchema,
)


def test_agent_spec_defaults_match_rust_runtime_contract() -> None:
    spec = AgentSpec("support-models")

    assert spec.model_pool == "support-models"
    assert spec.max_steps == 12
    assert spec.exit_conditions == ("final_message",)
    assert spec.tool_failure == "return_to_model"
    assert spec.parallel_tool_calls is True
    assert spec.tools == ()


def test_agent_state_patch_increments_revision_and_preserves_json_null() -> None:
    schema = AgentStateSchema(("profile", "note"))
    state = AgentState()

    revision = state.apply_patch(
        0,
        AgentStatePatch().set("profile", None).set("note", "keep"),
        schema=schema,
    )

    assert revision == 1
    assert state.revision == 1
    assert "profile" in state.values
    assert state.values["profile"] is None
    assert state.values["note"] == "keep"

    assert state.apply_patch(1, AgentStatePatch().delete("note"), schema=schema) == 2
    assert "profile" in state.values
    assert "note" not in state.values


def test_agent_state_patch_rejects_revision_conflict_without_mutating_state() -> None:
    state = AgentState(values={"profile": "initial"}, revision=1)

    with pytest.raises(AgentStateError, match="revision 1, not expected revision 0"):
        state.apply_patch(0, AgentStatePatch().set("profile", "updated"))

    assert state.revision == 1
    assert state.values == {"profile": "initial"}


def test_agent_state_patch_rejects_unknown_schema_keys() -> None:
    schema = AgentStateSchema(("profile",))
    state = AgentState()

    with pytest.raises(AgentStateError, match="agent state key 'unapproved' is not allowed"):
        state.apply_patch(0, AgentStatePatch().set("unapproved", "value"), schema=schema)

    assert state.revision == 0
    assert state.values == {}


def test_agent_state_patch_rejects_invalid_operations_without_mutating_state() -> None:
    state = AgentState(values={"profile": "initial"})
    patch = AgentStatePatch(
        (
            AgentStatePatchOp("set", "profile", "updated"),
            AgentStatePatchOp("merge", "extra", {"debug": True}),
        )
    )

    with pytest.raises(AgentStateError, match="unknown agent state patch operation merge"):
        state.apply_patch(0, patch)

    assert state.revision == 0
    assert state.values == {"profile": "initial"}


def test_agent_loop_controller_respects_step_and_completion_reserve_boundaries() -> None:
    controller = AgentLoopController(
        AgentSpec("support-models").with_max_steps(4).with_completion_reserve_units(100)
    )

    assert controller.decide_next_step(completed_steps=4, remaining_budget_units=1_000) == AgentLoopDecision.stop(
        "max_steps_reached"
    )
    assert controller.decide_next_step(completed_steps=3, remaining_budget_units=100) == AgentLoopDecision.finalize(
        "completion_reserve_reached"
    )
    assert controller.decide_next_step(completed_steps=3, remaining_budget_units=101) == AgentLoopDecision.continue_(
        "admitted"
    )

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
    assert spec.output_policy_profile_ref is None


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

    with pytest.raises(AgentStateError, match="unknown agent state patch operation merge"):
        AgentStatePatchOp("merge", "extra", {"debug": True})

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


def test_agent_spec_records_output_policy_profile_ref_for_agent_run() -> None:
    spec = (
        AgentSpec("support-models")
        .with_tools(("knowledge.search",))
        .with_output_policy_profile_ref("assistant-output-standard")
    )

    assert spec.output_policy_profile_ref == "assistant-output-standard"
    assert spec.tools == ("knowledge.search",)


def test_agent_constructors_reject_ambiguous_and_coerced_state() -> None:
    with pytest.raises(ValueError, match="tools must not contain duplicates"):
        AgentSpec("support-models", tools=("knowledge.search", "knowledge.search"))
    with pytest.raises(ValueError, match="max_steps must be an integer"):
        AgentSpec("support-models", max_steps=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="parallel_tool_calls must be a boolean"):
        AgentSpec("support-models", parallel_tool_calls=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="allowed_keys must not contain duplicates"):
        AgentStateSchema(("profile", "profile"))
    with pytest.raises(AgentStateError, match="delete operations must not carry a value"):
        AgentStatePatchOp("delete", "profile", "unexpected")
    with pytest.raises(ValueError, match="revision must be an integer"):
        AgentState(revision=False)


def test_agent_state_snapshots_nested_patch_values_before_commit() -> None:
    payload = {"preferences": ["short"]}
    state = AgentState()

    state.apply_patch(0, AgentStatePatch().set("profile", payload))
    payload["preferences"].append("mutable")

    assert state.values == {"profile": {"preferences": ["short"]}}

    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    with pytest.raises(AgentStateError, match="must be canonical JSON"):
        state.apply_patch(1, AgentStatePatch().set("recursive", recursive))
    assert state.revision == 1


def test_agent_state_canonical_snapshot_prevents_deepcopy_injection() -> None:
    class DeepcopyInjection(dict[str, object]):
        def __deepcopy__(self, _memo: object) -> object:
            return object()

    state = AgentState(values={"profile": DeepcopyInjection(theme="dark")})
    state.apply_patch(
        0,
        AgentStatePatch().set(
            "next",
            DeepcopyInjection(preferences=["short"]),
        ),
    )

    assert state.values == {
        "profile": {"theme": "dark"},
        "next": {"preferences": ["short"]},
    }
    with pytest.raises(TypeError, match="frozen mapping"):
        state.values["forged"] = True
    with pytest.raises(AttributeError):
        state.revision = 0

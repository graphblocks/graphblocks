from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]


def test_agents_package_exposes_tool_resolution_and_execution_plan_contracts(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-agents" / "src"))
    graphblocks_agents = importlib.import_module("graphblocks_agents")

    catalog = graphblocks_agents.ToolCatalog(
        definitions=(
            graphblocks_agents.ToolDefinition(
                name="knowledge.search",
                description="Search support documentation.",
                input_schema="schemas/SearchRequest@1",
            ),
        ),
        bindings=(
            graphblocks_agents.ToolBinding(
                binding_id="binding-knowledge",
                tool_name="knowledge.search",
                implementation=graphblocks_agents.BlockToolImplementation(block="knowledge.search@1"),
                effects=frozenset({"external_read"}),
                approval="never",
            ),
        ),
    )
    resolved = catalog.resolve(
        graphblocks_agents.ToolResolutionScope(principal_tools=frozenset({"knowledge.search"})),
        effective_policy_snapshot_id="policy-snapshot-1",
    )
    first_call = (
        graphblocks_agents.ToolCallDraft.proposed("response-1", "call-a", "knowledge.search")
        .append_argument_fragment('{"query":"runtime"}')
        .complete_arguments()
        .into_tool_call(resolved[0].resolved_tool_id, created_at="2026-06-23T00:00:00Z")
    )
    second_call = (
        graphblocks_agents.ToolCallDraft.proposed("response-1", "call-b", "knowledge.search")
        .append_argument_fragment('{"query":"policy"}')
        .complete_arguments()
        .into_tool_call(resolved[0].resolved_tool_id, created_at="2026-06-23T00:00:00Z")
    )
    plan = graphblocks_agents.ToolExecutionPlan(
        plan_id="plan-1",
        response_id="response-1",
        calls=(graphblocks_agents.ToolPlanCall(first_call), graphblocks_agents.ToolPlanCall(second_call)),
        maximum_parallelism=1,
    )

    assert [tool.definition.name for tool in resolved] == ["knowledge.search"]
    assert resolved[0].allowed_for_principal is True
    assert plan.ready_call_ids() == ["call-a"]
    plan.record_started("call-a")
    plan.record_completed("call-a")
    assert plan.ready_call_ids() == ["call-b"]
    assert "evaluate_native_tool_execution_plan" in graphblocks_agents.__all__
    assert "evaluate_native_sequential_tool_queue" in graphblocks_agents.__all__


def test_agents_package_lazy_native_helpers_delegate_to_runtime(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-agents" / "src"))
    calls: list[tuple[str, tuple[object, ...]]] = []

    def evaluate_tool_execution_plan(plan: dict[str, object], operations: object) -> dict[str, object]:
        calls.append(("plan", (plan, operations)))
        return {"kind": "plan", "plan": plan, "operations": operations}

    def decide_agent_step(spec: dict[str, object], request: dict[str, object]) -> dict[str, object]:
        calls.append(("agent_step", (spec, request)))
        return {"kind": "agent_step", "spec": spec, "request": request}

    def evaluate_sequential_tool_queue(queue: dict[str, object], operations: object) -> dict[str, object]:
        calls.append(("queue", (queue, operations)))
        return {"kind": "queue", "queue": queue, "operations": operations}

    def evaluate_tool_result_stream(state: dict[str, object], operations: object) -> dict[str, object]:
        calls.append(("tool_result_stream", (state, operations)))
        return {"kind": "tool_result_stream", "state": state, "operations": operations}

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            decide_agent_step=decide_agent_step,
            evaluate_sequential_tool_queue=evaluate_sequential_tool_queue,
            evaluate_tool_execution_plan=evaluate_tool_execution_plan,
            evaluate_tool_result_stream=evaluate_tool_result_stream,
        ),
    )
    graphblocks_agents = importlib.import_module("graphblocks_agents")

    plan = graphblocks_agents.evaluate_native_tool_execution_plan(
        {"planId": "plan-1"},
        [{"op": "ready"}],
    )
    decision = graphblocks_agents.decide_native_agent_step(
        {"maxSteps": 3},
        {"step": 1, "toolResults": []},
    )
    queue = graphblocks_agents.evaluate_native_sequential_tool_queue(
        {"planId": "plan-1", "responseId": "response-1", "calls": []},
        [{"op": "start_next_ready"}],
    )
    stream = graphblocks_agents.evaluate_native_tool_result_stream(
        {"toolCallId": "call-1"},
        [{"op": "delta", "sequence": 2}],
    )

    assert plan == {"kind": "plan", "plan": {"planId": "plan-1"}, "operations": [{"op": "ready"}]}
    assert decision == {
        "kind": "agent_step",
        "spec": {"maxSteps": 3},
        "request": {"step": 1, "toolResults": []},
    }
    assert queue == {
        "kind": "queue",
        "queue": {"planId": "plan-1", "responseId": "response-1", "calls": []},
        "operations": [{"op": "start_next_ready"}],
    }
    assert stream == {
        "kind": "tool_result_stream",
        "state": {"toolCallId": "call-1"},
        "operations": [{"op": "delta", "sequence": 2}],
    }
    assert calls == [
        ("plan", ({"planId": "plan-1"}, [{"op": "ready"}])),
        ("agent_step", ({"maxSteps": 3}, {"step": 1, "toolResults": []})),
        (
            "queue",
            (
                {"planId": "plan-1", "responseId": "response-1", "calls": []},
                [{"op": "start_next_ready"}],
            ),
        ),
        ("tool_result_stream", ({"toolCallId": "call-1"}, [{"op": "delta", "sequence": 2}])),
    ]
    assert "decide_native_agent_step" in graphblocks_agents.__all__
    assert "evaluate_native_tool_result_stream" in graphblocks_agents.__all__


def test_agents_package_exposes_policy_obligated_tool_admission(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-agents" / "src"))
    graphblocks_agents = importlib.import_module("graphblocks_agents")

    catalog = graphblocks_agents.ToolCatalog(
        definitions=(
            graphblocks_agents.ToolDefinition(
                name="process.run",
                description="Run an approved process.",
                input_schema="schemas/ProcessRun@1",
            ),
        ),
        bindings=(
            graphblocks_agents.ToolBinding(
                binding_id="binding-process",
                tool_name="process.run",
                implementation=graphblocks_agents.BlockToolImplementation(block="blocks.process"),
                effects=frozenset({"process"}),
                approval="policy",
                idempotency="required",
            ),
        ),
    )
    resolved = catalog.resolve(
        graphblocks_agents.ToolResolutionScope(principal_tools=frozenset({"process.run"})),
        effective_policy_snapshot_id="policy-snapshot-1",
    )[0]
    call = (
        graphblocks_agents.ToolCallDraft.proposed("response-1", "call-1", "process.run")
        .append_argument_fragment('{"cmd":["echo","hello"]}')
        .complete_arguments()
        .into_tool_call(resolved.resolved_tool_id, created_at="2026-06-23T00:00:00Z")
    )
    schemas = graphblocks_agents.ToolSchemaRegistry(
        schemas=(
            graphblocks_agents.JsonSchema(
                "schemas/ProcessRun@1",
                graphblocks_agents.JsonSchemaNode.object().required_property(
                    "cmd",
                    graphblocks_agents.JsonSchemaNode.array(graphblocks_agents.JsonSchemaNode.string()),
                ),
            ),
        )
    )
    decision = graphblocks_agents.PolicyDecision(
        decision_id="decision-allow-tool",
        effect="allow_with_obligations",
        reason_codes=("allow-process",),
        policy_refs=("allow-process",),
        obligations=(graphblocks_agents.PolicyObligation("obl-approval", "require_tool_approval"),),
        evaluated_at="2026-06-23T00:00:01Z",
        input_digest="sha256:before-tool",
    )

    with pytest.raises(graphblocks_agents.ToolAdmissionError, match="requires approval"):
        graphblocks_agents.admit_tool_call(
            call,
            resolved,
            schemas,
            policy_decision=decision,
            expected_policy_input_digest=decision.input_digest,
            principal_id="user-1",
            idempotency_key="idem-1",
            admitted_at="2026-06-23T00:00:01Z",
            now=1_200,
        )

    request = graphblocks_agents.ToolApprovalRequest.for_call(
        "approval-1",
        resolved,
        call,
        principal_id="user-1",
        requested_at=1_100,
        expires_at=2_000,
    )
    approval = graphblocks_agents.ToolApprovalRecord.approve(request, approver_id="admin-1", decided_at=1_150)
    admitted = graphblocks_agents.admit_tool_call(
        call,
        resolved,
        schemas,
        approval=approval,
        policy_decision=decision,
        expected_policy_input_digest=decision.input_digest,
        principal_id="user-1",
        idempotency_key="idem-1",
        admitted_at="2026-06-23T00:00:01Z",
        now=1_200,
    )

    assert admitted.call.status == "admitted"


def test_agents_package_exposes_streaming_tool_result_state(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-agents" / "src"))
    graphblocks_agents = importlib.import_module("graphblocks_agents")

    stream = graphblocks_agents.ToolResultStreamState()
    started = graphblocks_agents.ToolResultEvent.started(
        "call-1",
        1,
        started_at="2026-06-23T00:00:00Z",
    )
    delta = graphblocks_agents.ToolResultEvent.delta(
        "call-1",
        2,
        (graphblocks_agents.ContentPart(kind="text", text="draft"),),
    )
    stopped = graphblocks_agents.ToolResult.policy_stopped(
        "call-1",
        error={"code": "policy.denied", "message": "blocked"},
        started_at="2026-06-23T00:00:00Z",
        completed_at="2026-06-23T00:00:01Z",
    )
    stopped_event = graphblocks_agents.ToolResultEvent.policy_stopped("call-1", 3, stopped)

    assert stream.accept(started) == started
    assert stream.accept(delta).into_result() is None
    assert stream.accept(stopped_event).into_result() == stopped
    with pytest.raises(graphblocks_agents.ToolResultStreamError) as error:
        stream.accept(
            graphblocks_agents.ToolResultEvent.delta(
                "call-1",
                4,
                (graphblocks_agents.ContentPart(kind="text", text="late"),),
            )
        )

    assert error.value.final_status == "policy_stopped"
    assert stream.final_result_for("call-1") == stopped
    assert "ToolResultStreamState" in graphblocks_agents.__all__
    assert "ToolResultStreamError" in graphblocks_agents.__all__


def test_agents_package_exposes_agent_loop_contracts(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-agents" / "src"))
    graphblocks_agents = importlib.import_module("graphblocks_agents")

    controller = graphblocks_agents.AgentLoopController(
        graphblocks_agents.AgentSpec("support-models").with_completion_reserve_units(100)
    )

    assert graphblocks_agents.AgentSpec("support-models").max_steps == 12
    assert controller.decide_next_step(3, 100) == graphblocks_agents.AgentLoopDecision.finalize(
        "completion_reserve_reached"
    )
    assert "AgentStatePatch" in graphblocks_agents.__all__

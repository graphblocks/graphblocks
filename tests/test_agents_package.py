from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import graphblocks
import pytest

from graphblocks.agent import VALID_TOOL_FAILURE_POLICIES
from graphblocks.tools import (
    FINAL_TOOL_RESULT_EVENT_STATUSES,
    VALID_TOOL_APPROVALS,
    VALID_TOOL_APPROVAL_STATUSES,
    VALID_TOOL_CALL_DRAFT_STATUSES,
    VALID_TOOL_CALL_STATUSES,
    VALID_TOOL_CANCELLATIONS,
    VALID_TOOL_EFFECT_OUTCOMES,
    VALID_TOOL_EFFECTS,
    VALID_TOOL_EXECUTION_CANCELLATION_POLICIES,
    VALID_TOOL_EXECUTION_FAILURE_POLICIES,
    VALID_TOOL_IDEMPOTENCIES,
    VALID_TOOL_RESULT_EVENT_KINDS,
    VALID_TOOL_RESULT_MODES,
    VALID_TOOL_RESULT_STATUSES,
)


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


def test_agents_package_exposes_tool_literal_sets(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-agents" / "src"))
    graphblocks_agents = importlib.import_module("graphblocks_agents")
    expected_constants = {
        "FINAL_TOOL_RESULT_EVENT_STATUSES": FINAL_TOOL_RESULT_EVENT_STATUSES,
        "VALID_TOOL_FAILURE_POLICIES": VALID_TOOL_FAILURE_POLICIES,
        "VALID_TOOL_APPROVALS": VALID_TOOL_APPROVALS,
        "VALID_TOOL_APPROVAL_STATUSES": VALID_TOOL_APPROVAL_STATUSES,
        "VALID_TOOL_CALL_DRAFT_STATUSES": VALID_TOOL_CALL_DRAFT_STATUSES,
        "VALID_TOOL_CALL_STATUSES": VALID_TOOL_CALL_STATUSES,
        "VALID_TOOL_CANCELLATIONS": VALID_TOOL_CANCELLATIONS,
        "VALID_TOOL_EFFECT_OUTCOMES": VALID_TOOL_EFFECT_OUTCOMES,
        "VALID_TOOL_EFFECTS": VALID_TOOL_EFFECTS,
        "VALID_TOOL_EXECUTION_CANCELLATION_POLICIES": VALID_TOOL_EXECUTION_CANCELLATION_POLICIES,
        "VALID_TOOL_EXECUTION_FAILURE_POLICIES": VALID_TOOL_EXECUTION_FAILURE_POLICIES,
        "VALID_TOOL_IDEMPOTENCIES": VALID_TOOL_IDEMPOTENCIES,
        "VALID_TOOL_RESULT_EVENT_KINDS": VALID_TOOL_RESULT_EVENT_KINDS,
        "VALID_TOOL_RESULT_MODES": VALID_TOOL_RESULT_MODES,
        "VALID_TOOL_RESULT_STATUSES": VALID_TOOL_RESULT_STATUSES,
    }

    assert sorted(name for name in expected_constants if name not in graphblocks_agents.__all__) == []
    for name, value in expected_constants.items():
        assert getattr(graphblocks_agents, name) is value
    assert graphblocks.VALID_TOOL_FAILURE_POLICIES is VALID_TOOL_FAILURE_POLICIES
    assert "VALID_TOOL_FAILURE_POLICIES" in graphblocks.__all__


def test_agents_package_lazy_native_helpers_delegate_to_runtime(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-agents" / "src"))
    calls: list[tuple[str, tuple[object, ...]]] = []

    def evaluate_tool_execution_plan(plan: dict[str, object], operations: object) -> dict[str, object]:
        calls.append(("plan", (plan, operations)))
        return {"kind": "plan", "plan": plan, "operations": operations}

    def finalize_tool_call(
        draft: dict[str, object],
        *,
        resolved_tool_id: str,
        created_at_unix_ms: int,
    ) -> dict[str, object]:
        calls.append(("finalize", (draft, resolved_tool_id, created_at_unix_ms)))
        return {
            "kind": "finalized",
            "draft": draft,
            "resolvedToolId": resolved_tool_id,
            "createdAtUnixMs": created_at_unix_ms,
        }

    def prepare_tool_result_for_model(
        call: dict[str, object],
        result: dict[str, object],
        resolved_tool: dict[str, object],
        schema_registry: object,
        *,
        content_policy: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append(("prepare_result", (call, result, resolved_tool, schema_registry, content_policy)))
        return {
            "kind": "prepared_result",
            "call": call,
            "result": result,
            "resolvedTool": resolved_tool,
            "schemaRegistry": schema_registry,
            "contentPolicy": content_policy,
        }

    def decide_agent_step(spec: dict[str, object], request: dict[str, object]) -> dict[str, object]:
        calls.append(("agent_step", (spec, request)))
        return {"kind": "agent_step", "spec": spec, "request": request}

    def evaluate_sequential_tool_queue(queue: dict[str, object], operations: object) -> dict[str, object]:
        calls.append(("queue", (queue, operations)))
        return {"kind": "queue", "queue": queue, "operations": operations}

    def evaluate_tool_result_stream(state: dict[str, object], operations: object) -> dict[str, object]:
        calls.append(("tool_result_stream", (state, operations)))
        return {"kind": "tool_result_stream", "state": state, "operations": operations}

    def evaluate_tool_approval(
        record: dict[str, object],
        resolved_tool: dict[str, object],
        call: dict[str, object],
        *,
        principal_id: str,
        now_unix_ms: int,
    ) -> dict[str, object]:
        calls.append(("tool_approval", (record, resolved_tool, call, principal_id, now_unix_ms)))
        return {
            "kind": "tool_approval",
            "record": record,
            "resolvedTool": resolved_tool,
            "call": call,
            "principalId": principal_id,
            "nowUnixMs": now_unix_ms,
        }

    def evaluate_tool_admission(request: dict[str, object]) -> dict[str, object]:
        calls.append(("tool_admission", (request,)))
        return {"kind": "tool_admission", "request": request}

    def evaluate_tool_resolution(
        catalog: dict[str, object],
        scope: dict[str, object],
        *,
        effective_policy_snapshot_id: str,
    ) -> dict[str, object]:
        calls.append(("tool_resolution", (catalog, scope, effective_policy_snapshot_id)))
        return {
            "kind": "tool_resolution",
            "catalog": catalog,
            "scope": scope,
            "effectivePolicySnapshotId": effective_policy_snapshot_id,
        }

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            decide_agent_step=decide_agent_step,
            evaluate_sequential_tool_queue=evaluate_sequential_tool_queue,
            evaluate_tool_admission=evaluate_tool_admission,
            evaluate_tool_approval=evaluate_tool_approval,
            evaluate_tool_execution_plan=evaluate_tool_execution_plan,
            evaluate_tool_resolution=evaluate_tool_resolution,
            evaluate_tool_result_stream=evaluate_tool_result_stream,
            finalize_tool_call=finalize_tool_call,
            prepare_tool_result_for_model=prepare_tool_result_for_model,
        ),
    )
    graphblocks_agents = importlib.import_module("graphblocks_agents")

    plan = graphblocks_agents.evaluate_native_tool_execution_plan(
        {"planId": "plan-1"},
        [{"op": "ready"}],
    )
    finalized = graphblocks_agents.finalize_native_tool_call(
        {"toolCallId": "call-1", "status": "arguments_complete"},
        resolved_tool_id="resolved-tool-1",
        created_at_unix_ms=1_782_300_001_000,
    )
    prepared_result = graphblocks_agents.prepare_native_tool_result_for_model(
        {"toolCallId": "call-1"},
        {"toolCallId": "call-1", "status": "completed"},
        {"resolvedToolId": "resolved-tool-1"},
        {"schemas": []},
        content_policy={"maxOutputBytes": 1024},
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
    approval = graphblocks_agents.evaluate_native_tool_approval(
        {"approvalId": "approval-1", "status": "approved"},
        {"resolvedToolId": "resolved-tool-1"},
        {"toolCallId": "call-1"},
        principal_id="user-1",
        now_unix_ms=1_500,
    )
    admission = graphblocks_agents.evaluate_native_tool_admission(
        {"call": {"toolCallId": "call-1"}, "principalId": "user-1"},
    )
    resolution = graphblocks_agents.evaluate_native_tool_resolution(
        {"definitions": [{"name": "knowledge.search"}]},
        {"principalTools": ["knowledge.search"]},
        effective_policy_snapshot_id="policy-snapshot-1",
    )

    assert plan == {"kind": "plan", "plan": {"planId": "plan-1"}, "operations": [{"op": "ready"}]}
    assert finalized == {
        "kind": "finalized",
        "draft": {"toolCallId": "call-1", "status": "arguments_complete"},
        "resolvedToolId": "resolved-tool-1",
        "createdAtUnixMs": 1_782_300_001_000,
    }
    assert prepared_result == {
        "kind": "prepared_result",
        "call": {"toolCallId": "call-1"},
        "result": {"toolCallId": "call-1", "status": "completed"},
        "resolvedTool": {"resolvedToolId": "resolved-tool-1"},
        "schemaRegistry": {"schemas": []},
        "contentPolicy": {"maxOutputBytes": 1024},
    }
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
    assert approval == {
        "kind": "tool_approval",
        "record": {"approvalId": "approval-1", "status": "approved"},
        "resolvedTool": {"resolvedToolId": "resolved-tool-1"},
        "call": {"toolCallId": "call-1"},
        "principalId": "user-1",
        "nowUnixMs": 1_500,
    }
    assert admission == {
        "kind": "tool_admission",
        "request": {"call": {"toolCallId": "call-1"}, "principalId": "user-1"},
    }
    assert resolution == {
        "kind": "tool_resolution",
        "catalog": {"definitions": [{"name": "knowledge.search"}]},
        "scope": {"principalTools": ["knowledge.search"]},
        "effectivePolicySnapshotId": "policy-snapshot-1",
    }
    assert calls == [
        ("plan", ({"planId": "plan-1"}, [{"op": "ready"}])),
        (
            "finalize",
            (
                {"toolCallId": "call-1", "status": "arguments_complete"},
                "resolved-tool-1",
                1_782_300_001_000,
            ),
        ),
        (
            "prepare_result",
            (
                {"toolCallId": "call-1"},
                {"toolCallId": "call-1", "status": "completed"},
                {"resolvedToolId": "resolved-tool-1"},
                {"schemas": []},
                {"maxOutputBytes": 1024},
            ),
        ),
        ("agent_step", ({"maxSteps": 3}, {"step": 1, "toolResults": []})),
        (
            "queue",
            (
                {"planId": "plan-1", "responseId": "response-1", "calls": []},
                [{"op": "start_next_ready"}],
            ),
        ),
        ("tool_result_stream", ({"toolCallId": "call-1"}, [{"op": "delta", "sequence": 2}])),
        (
            "tool_approval",
            (
                {"approvalId": "approval-1", "status": "approved"},
                {"resolvedToolId": "resolved-tool-1"},
                {"toolCallId": "call-1"},
                "user-1",
                1_500,
            ),
        ),
        (
            "tool_admission",
            ({"call": {"toolCallId": "call-1"}, "principalId": "user-1"},),
        ),
        (
            "tool_resolution",
            (
                {"definitions": [{"name": "knowledge.search"}]},
                {"principalTools": ["knowledge.search"]},
                "policy-snapshot-1",
            ),
        ),
    ]
    assert "decide_native_agent_step" in graphblocks_agents.__all__
    assert "evaluate_native_tool_admission" in graphblocks_agents.__all__
    assert "evaluate_native_tool_approval" in graphblocks_agents.__all__
    assert "evaluate_native_tool_resolution" in graphblocks_agents.__all__
    assert "evaluate_native_tool_result_stream" in graphblocks_agents.__all__
    assert "finalize_native_tool_call" in graphblocks_agents.__all__
    assert "prepare_native_tool_result_for_model" in graphblocks_agents.__all__


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

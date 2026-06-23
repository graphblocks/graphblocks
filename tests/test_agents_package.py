from __future__ import annotations

import importlib
from pathlib import Path


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

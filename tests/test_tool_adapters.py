from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from graphblocks import (
    AdmittedToolCall,
    McpToolImplementation,
    OpenApiToolImplementation,
    ResolvedTool,
    ToolBinding,
    ToolCall,
    ToolDefinition,
    canonical_hash,
)


ROOT = Path(__file__).parents[1]


def _admitted_call_for(
    implementation: McpToolImplementation | OpenApiToolImplementation,
    *,
    tool_name: str,
    binding_id: str,
    arguments: dict[str, object],
) -> tuple[AdmittedToolCall, ResolvedTool]:
    definition = ToolDefinition(
        name=tool_name,
        description="Execute the tool.",
        input_schema="schemas/ToolRequest@1",
    )
    binding = ToolBinding(
        binding_id=binding_id,
        tool_name=tool_name,
        implementation=implementation,
        effects=frozenset({"network"}),
    )
    resolved = ResolvedTool.from_definition_and_binding(
        resolved_tool_id="resolved-tool-1",
        definition=definition,
        binding=binding,
        effective_policy_snapshot_id="policy-snapshot-1",
        allowed_for_principal=True,
    )
    call = ToolCall(
        tool_call_id="call-1",
        response_id="response-1",
        resolved_tool_id=resolved.resolved_tool_id,
        name=tool_name,
        arguments=arguments,
        arguments_digest=canonical_hash(arguments),
        status="admitted",
        admitted_at="2026-06-23T00:00:00Z",
    )
    return AdmittedToolCall(call=call, idempotency_key="idem-1"), resolved


def test_mcp_adapter_builds_tool_definition_and_binding(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")

    definition = graphblocks_mcp.define_mcp_tool(
        name="knowledge.search",
        description="Search support documentation.",
        input_schema="schemas/SearchRequest@1",
        output_schema="schemas/SearchResult@1",
        tags=frozenset({"support", "search"}),
        version="1.0.0",
    )
    binding = graphblocks_mcp.bind_mcp_tool(
        binding_id="binding-mcp-search",
        tool_name="knowledge.search",
        server="support-mcp",
        remote_name="search",
        effects=frozenset({"external_read", "network"}),
        approval="never",
        idempotency="not_applicable",
    )

    assert definition.model_contract()["tags"] == ["search", "support"]
    assert binding.binding_contract()["implementation"] == {
        "kind": "mcp",
        "server": "support-mcp",
        "remote_name": "search",
    }
    assert binding.binding_contract()["effects"] == ["external_read", "network"]


def test_mcp_adapter_prepares_admitted_invocation_contract(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    arguments = {"query": "billing", "limit": 5}
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments=arguments,
    )

    invocation = graphblocks_mcp.prepare_mcp_tool_invocation(admitted, resolved)
    arguments["query"] = "mutated"

    assert invocation.request_contract() == {
        "kind": "mcp",
        "binding_id": "binding-mcp-search",
        "tool_call_id": "call-1",
        "server": "support-mcp",
        "remote_name": "search",
        "arguments": {"limit": 5, "query": "billing"},
        "arguments_digest": admitted.call.arguments_digest,
        "idempotency_key": "idem-1",
    }


def test_mcp_adapter_rejects_non_mcp_binding(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
    )

    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="requires an MCP tool binding"):
        graphblocks_mcp.prepare_mcp_tool_invocation(admitted, resolved)


def test_openapi_adapter_builds_tool_definition_and_binding(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")

    definition = graphblocks_openapi.define_openapi_tool(
        name="ticket.create",
        description="Create a support ticket.",
        input_schema="schemas/TicketCreateRequest@1",
        output_schema="schemas/Ticket@1",
        version="1.0.0",
    )
    binding = graphblocks_openapi.bind_openapi_operation(
        binding_id="binding-ticket-create",
        tool_name="ticket.create",
        connection="ticket-system",
        operation_id="createTicket",
        effects=frozenset({"external_write", "network"}),
        approval="policy",
        idempotency="required",
    )

    assert definition.name == "ticket.create"
    assert binding.binding_contract()["implementation"] == {
        "kind": "openapi",
        "connection": "ticket-system",
        "operation_id": "createTicket",
    }
    assert binding.binding_contract()["idempotency"] == "required"


def test_openapi_adapter_prepares_admitted_invocation_contract(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    arguments = {"title": "Need help", "priority": "normal"}
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments=arguments,
    )

    invocation = graphblocks_openapi.prepare_openapi_operation_invocation(admitted, resolved)
    arguments["title"] = "mutated"

    assert invocation.request_contract() == {
        "kind": "openapi",
        "binding_id": "binding-ticket-create",
        "tool_call_id": "call-1",
        "connection": "ticket-system",
        "operation_id": "createTicket",
        "arguments": {"priority": "normal", "title": "Need help"},
        "arguments_digest": admitted.call.arguments_digest,
        "idempotency_key": "idem-1",
    }


def test_openapi_adapter_rejects_non_openapi_binding(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
    )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="requires an OpenAPI tool binding"):
        graphblocks_openapi.prepare_openapi_operation_invocation(admitted, resolved)

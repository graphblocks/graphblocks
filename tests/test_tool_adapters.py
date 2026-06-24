from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from graphblocks import (
    AdmittedToolCall,
    ContentPart,
    JsonSchema,
    JsonSchemaNode,
    McpToolImplementation,
    OpenApiToolImplementation,
    ResolvedTool,
    ToolBinding,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolSchemaRegistry,
    canonical_hash,
)


ROOT = Path(__file__).parents[1]


def _admitted_call_for(
    implementation: McpToolImplementation | OpenApiToolImplementation,
    *,
    tool_name: str,
    binding_id: str,
    arguments: dict[str, object],
    output_schema: str | None = None,
) -> tuple[AdmittedToolCall, ResolvedTool]:
    definition = ToolDefinition(
        name=tool_name,
        description="Execute the tool.",
        input_schema="schemas/ToolRequest@1",
        output_schema=output_schema,
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


def _tool_output_registry() -> ToolSchemaRegistry:
    return ToolSchemaRegistry(
        (
            JsonSchema(
                "schemas/SearchResult@1",
                JsonSchemaNode.object().required_property(
                    "items",
                    JsonSchemaNode.array(JsonSchemaNode.string()),
                ),
            ),
            JsonSchema(
                "schemas/Ticket@1",
                JsonSchemaNode.object().required_property("ticket_id", JsonSchemaNode.string()),
            ),
        )
    )


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


def test_mcp_adapter_rejects_stale_argument_digest(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
    )
    stale = AdmittedToolCall(
        call=ToolCall(
            tool_call_id=admitted.call.tool_call_id,
            response_id=admitted.call.response_id,
            resolved_tool_id=admitted.call.resolved_tool_id,
            name=admitted.call.name,
            arguments={"query": "mutated"},
            arguments_digest=admitted.call.arguments_digest,
            status="admitted",
            admitted_at=admitted.call.admitted_at,
        ),
        idempotency_key=admitted.idempotency_key,
    )

    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="arguments digest does not match"):
        graphblocks_mcp.prepare_mcp_tool_invocation(stale, resolved)


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


def test_mcp_adapter_converts_valid_response_to_tool_result(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
        output_schema="schemas/SearchResult@1",
    )

    result = graphblocks_mcp.mcp_tool_result_from_response(
        admitted,
        resolved,
        _tool_output_registry(),
        output={"items": ["billing"]},
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
        effect_outcome="no_external_effect",
    )

    assert result.tool_call_id == "call-1"
    assert result.status == "completed"
    assert result.output[0].kind == "json"
    assert result.output[0].data == {"items": ["billing"]}
    assert result.output[0].metadata["trust_designation"] == "untrusted_external"
    assert result.output_digest.startswith("sha256:")
    assert result.effect_outcome == "no_external_effect"


def test_mcp_adapter_prepares_tool_result_with_redactions_and_capture_policy(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
    )
    result = ToolResult.completed(
        "call-1",
        (ContentPart(kind="text", text="safe secret suffix", metadata={"adapter": "mcp"}),),
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
    )

    prepared = graphblocks_mcp.prepare_mcp_tool_result_for_model(
        admitted,
        resolved,
        _tool_output_registry(),
        result,
        redactions=(
            {"path": "/parts/0/text", "start": 5, "end": 11, "replacement": "[redacted]"},
        ),
        capture_policy={
            "mode": "hash_only",
            "retention_policy": "records-30d",
            "consent_ref": "consent-1",
        },
    )

    assert prepared[0].text == "safe [redacted] suffix"
    assert prepared[0].metadata["capture"]["mode"] == "hash_only"
    assert "secret" not in repr(prepared[0].metadata["capture"])
    assert result.output[0].text == "safe secret suffix"


def test_mcp_adapter_converts_error_to_failed_tool_result(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
    )
    error = {"code": "mcp.timeout", "message": "MCP call timed out"}

    result = graphblocks_mcp.mcp_tool_result_from_error(
        admitted,
        resolved,
        error=error,
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
        effect_outcome="not_committed",
    )
    error["message"] = "mutated"

    assert result.status == "failed"
    assert result.error == {"code": "mcp.timeout", "message": "MCP call timed out"}
    assert result.effect_outcome == "not_committed"


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


def test_openapi_adapter_rejects_stale_argument_digest(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
    )
    stale = AdmittedToolCall(
        call=ToolCall(
            tool_call_id=admitted.call.tool_call_id,
            response_id=admitted.call.response_id,
            resolved_tool_id=admitted.call.resolved_tool_id,
            name=admitted.call.name,
            arguments={"title": "mutated"},
            arguments_digest=admitted.call.arguments_digest,
            status="admitted",
            admitted_at=admitted.call.admitted_at,
        ),
        idempotency_key=admitted.idempotency_key,
    )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="arguments digest does not match"):
        graphblocks_openapi.prepare_openapi_operation_invocation(stale, resolved)


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


def test_openapi_adapter_converts_valid_response_to_tool_result(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
        output_schema="schemas/Ticket@1",
    )

    result = graphblocks_openapi.openapi_tool_result_from_response(
        admitted,
        resolved,
        _tool_output_registry(),
        output={"ticket_id": "ticket-1"},
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
        effect_outcome="committed",
    )

    assert result.status == "completed"
    assert result.output[0].data == {"ticket_id": "ticket-1"}
    assert result.output[0].metadata["trust_designation"] == "untrusted_external"
    assert result.effect_was_committed()


def test_openapi_adapter_enforces_result_policy_size_limit(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
        output_schema="schemas/Ticket@1",
    )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="failed validation"):
        graphblocks_openapi.openapi_tool_result_from_response(
            admitted,
            resolved,
            _tool_output_registry(),
            output={"ticket_id": "ticket-1"},
            started_at="2026-06-23T00:00:01Z",
            completed_at="2026-06-23T00:00:02Z",
            max_output_bytes=4,
        )


def test_openapi_adapter_rejects_response_that_fails_output_schema(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
        output_schema="schemas/Ticket@1",
    )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="failed validation"):
        graphblocks_openapi.openapi_tool_result_from_response(
            admitted,
            resolved,
            _tool_output_registry(),
            output={"id": "ticket-1"},
            started_at="2026-06-23T00:00:01Z",
            completed_at="2026-06-23T00:00:02Z",
        )


def test_openapi_adapter_converts_error_to_failed_tool_result(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
    )
    error = {"code": "openapi.conflict", "message": "duplicate ticket"}

    result = graphblocks_openapi.openapi_tool_result_from_error(
        admitted,
        resolved,
        error=error,
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
        effect_outcome="unknown",
    )
    error["message"] = "mutated"

    assert result.status == "failed"
    assert result.error == {"code": "openapi.conflict", "message": "duplicate ticket"}
    assert result.effect_outcome == "unknown"

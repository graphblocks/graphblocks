from __future__ import annotations

from dataclasses import replace
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from graphblocks import (
    AdmittedToolCall,
    ArtifactRef,
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


def test_mcp_and_openapi_adapters_expose_native_connector_capability_helper(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    calls: list[tuple[dict[str, object], object]] = []

    def evaluate_connector_capabilities(
        connection: dict[str, object],
        required_capabilities: object,
    ) -> dict[str, object]:
        calls.append((connection, required_capabilities))
        return {
            "ok": True,
            "connection": connection,
            "requiredCapabilities": required_capabilities,
            "supportedCapabilities": ["http_json", "oauth2"],
            "missingCapabilities": [],
        }

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(evaluate_connector_capabilities=evaluate_connector_capabilities),
    )
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")

    mcp_result = graphblocks_mcp.evaluate_native_connector_capabilities(
        {"connectionId": "support-mcp", "kind": "mcp", "provider": "stdio"},
        ["stdio"],
    )
    openapi_result = graphblocks_openapi.evaluate_native_connector_capabilities(
        {"connectionId": "ticket-system", "kind": "openapi", "provider": "zendesk"},
        {"required": ["http_json"]},
    )

    assert mcp_result["ok"] is True
    assert openapi_result["ok"] is True
    assert calls == [
        ({"connectionId": "support-mcp", "kind": "mcp", "provider": "stdio"}, ["stdio"]),
        (
            {"connectionId": "ticket-system", "kind": "openapi", "provider": "zendesk"},
            {"required": ["http_json"]},
        ),
    ]
    assert "evaluate_native_connector_capabilities" in graphblocks_mcp.__all__
    assert "evaluate_native_connector_capabilities" in graphblocks_openapi.__all__


def _admitted_call_for(
    implementation: McpToolImplementation | OpenApiToolImplementation,
    *,
    tool_name: str,
    binding_id: str,
    arguments: dict[str, object],
    idempotency: str = "optional",
    idempotency_key: str | None = "idem-1",
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
        idempotency=idempotency,
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
    return AdmittedToolCall(call=call, idempotency_key=idempotency_key), resolved


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


def test_mcp_adapter_discovers_tool_definitions_from_capabilities(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")

    definitions = graphblocks_mcp.discover_mcp_tool_definitions(
        {
            "tools": [
                {
                    "name": "ticket.create",
                    "description": "Create a ticket.",
                    "inputSchema": {"type": "object"},
                },
                {
                    "name": "knowledge.search",
                    "description": "Search support documentation.",
                    "inputSchema": {"$id": "schemas/KnowledgeSearchRequest@1"},
                    "outputSchema": "schemas/KnowledgeSearchResult@1",
                    "tags": ["search"],
                },
            ]
        },
        tags=("support",),
        version="1.0.0",
    )

    assert [definition.name for definition in definitions] == ["knowledge.search", "ticket.create"]
    assert definitions[0].input_schema == "schemas/KnowledgeSearchRequest@1"
    assert definitions[0].output_schema == "schemas/KnowledgeSearchResult@1"
    assert definitions[0].tags == frozenset({"search", "support"})
    assert definitions[1].input_schema == "schemas/mcp/ticket-create/input@1"
    assert definitions[1].output_schema is None
    assert "discover_mcp_tool_definitions" in graphblocks_mcp.__all__


def test_mcp_adapter_discovery_rejects_blank_tool_metadata(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")

    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="name"):
        graphblocks_mcp.discover_mcp_tool_definitions({"tools": [{"name": " "}]})

    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="tags"):
        graphblocks_mcp.discover_mcp_tool_definitions(
            {"tools": [{"name": "knowledge.search", "tags": ["support", " "]}]},
        )

    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="schema"):
        graphblocks_mcp.discover_mcp_tool_definitions(
            {"tools": [{"name": "knowledge.search", "inputSchema": " "}]},
        )

    definitions = graphblocks_mcp.discover_mcp_tool_definitions(
        {
            "tools": [
                {
                    "name": " knowledge.search ",
                    "description": " Search support documentation. ",
                    "inputSchema": " schemas/KnowledgeSearchRequest@1 ",
                    "tags": [" support "],
                },
            ]
        },
        tags=(" global ",),
    )

    assert definitions[0].name == "knowledge.search"
    assert definitions[0].description == "Search support documentation."
    assert definitions[0].input_schema == "schemas/KnowledgeSearchRequest@1"
    assert definitions[0].tags == frozenset({"global", "support"})


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
        "resolved_tool_id": "resolved-tool-1",
        "tool_name": "knowledge.search",
        "tool_call_id": "call-1",
        "server": "support-mcp",
        "remote_name": "search",
        "arguments": {"limit": 5, "query": "billing"},
        "arguments_digest": admitted.call.arguments_digest,
        "definition_digest": resolved.definition_digest,
        "binding_digest": resolved.binding_digest,
        "effective_policy_snapshot_id": "policy-snapshot-1",
        "idempotency_key": "idem-1",
    }
    direct_kwargs = {
        "binding_id": invocation.binding_id,
        "resolved_tool_id": invocation.resolved_tool_id,
        "tool_name": invocation.tool_name,
        "tool_call_id": invocation.tool_call_id,
        "server": invocation.server,
        "remote_name": invocation.remote_name,
        "arguments_json": '{"query":"billing","limit":5}',
        "arguments_digest": invocation.arguments_digest,
        "definition_digest": invocation.definition_digest,
        "binding_digest": invocation.binding_digest,
        "effective_policy_snapshot_id": invocation.effective_policy_snapshot_id,
        "idempotency_key": invocation.idempotency_key,
    }
    direct = graphblocks_mcp.McpToolInvocation(**direct_kwargs)

    assert direct.arguments_json == '{"limit":5,"query":"billing"}'
    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="server must not be empty"):
        graphblocks_mcp.McpToolInvocation(**{**direct_kwargs, "server": " "})
    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="must decode to an object"):
        graphblocks_mcp.McpToolInvocation(**{**direct_kwargs, "arguments_json": "[]"})
    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="digest does not match"):
        graphblocks_mcp.McpToolInvocation(**{**direct_kwargs, "arguments_json": '{"query":"changed"}'})


def test_mcp_adapter_rejects_stale_argument_digest(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
    )
    object.__setattr__(admitted.call, "arguments", {"query": "mutated"})
    stale = AdmittedToolCall(
        call=admitted.call,
        idempotency_key=admitted.idempotency_key,
    )

    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="arguments digest does not match"):
        graphblocks_mcp.prepare_mcp_tool_invocation(stale, resolved)


def test_mcp_adapter_rechecks_resolved_tool_capability_before_invocation(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
    )

    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="not allowed for principal"):
        graphblocks_mcp.prepare_mcp_tool_invocation(
            admitted,
            replace(resolved, allowed_for_principal=False),
        )

    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="expired at 2026-06-23T00:00:01Z"):
        graphblocks_mcp.prepare_mcp_tool_invocation(
            admitted,
            replace(resolved, valid_until="2026-06-23T00:00:01Z"),
            validation_time="2026-06-23T00:00:02Z",
        )


def test_mcp_adapter_requires_required_idempotency_key_before_execution(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
        idempotency="required",
        idempotency_key=None,
    )

    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="requires an idempotency key"):
        graphblocks_mcp.prepare_mcp_tool_invocation(admitted, resolved)


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


def test_mcp_adapter_converts_denied_terminal_result(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
    )
    error = {"code": "mcp.denied", "message": "MCP server denied the tool call"}

    result = graphblocks_mcp.mcp_tool_result_denied(
        admitted,
        resolved,
        error=error,
        completed_at="2026-06-23T00:00:02Z",
    )
    error["message"] = "mutated"
    prepared = graphblocks_mcp.prepare_mcp_tool_result_for_model(
        admitted,
        resolved,
        _tool_output_registry(),
        result,
    )

    assert result.status == "denied"
    assert result.error == {"code": "mcp.denied", "message": "MCP server denied the tool call"}
    assert result.effect_outcome == "not_committed"
    assert prepared == ()
    assert "mcp_tool_result_denied" in graphblocks_mcp.__all__


def test_mcp_adapter_converts_policy_stopped_terminal_result(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
    )
    error = {"code": "policy.denied", "message": "tool output violated policy"}

    result = graphblocks_mcp.mcp_tool_result_policy_stopped(
        admitted,
        resolved,
        error=error,
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
        effect_outcome="committed",
    )
    error["message"] = "mutated"
    prepared = graphblocks_mcp.prepare_mcp_tool_result_for_model(
        admitted,
        resolved,
        _tool_output_registry(),
        result,
    )

    assert result.status == "policy_stopped"
    assert result.error == {"code": "policy.denied", "message": "tool output violated policy"}
    assert result.effect_outcome == "committed"
    assert prepared == ()


def test_mcp_adapter_converts_cancelled_and_incomplete_terminal_results(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
    )

    cancelled = graphblocks_mcp.mcp_tool_result_cancelled(
        admitted,
        resolved,
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
        effect_outcome="not_committed",
    )
    incomplete = graphblocks_mcp.mcp_tool_result_incomplete(
        admitted,
        resolved,
        started_at="2026-06-23T00:00:03Z",
        completed_at="2026-06-23T00:00:04Z",
        effect_outcome="unknown",
    )

    assert cancelled.status == "cancelled"
    assert cancelled.effect_outcome == "not_committed"
    assert incomplete.status == "incomplete"
    assert incomplete.effect_outcome == "unknown"


def test_mcp_adapter_builds_streaming_tool_result_events(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
    )

    started = graphblocks_mcp.mcp_tool_result_started(
        admitted,
        resolved,
        sequence=1,
        started_at="2026-06-23T00:00:01Z",
    )
    delta = graphblocks_mcp.mcp_tool_result_delta(
        admitted,
        resolved,
        sequence=2,
        output=(
            "partial",
            {"kind": "json", "data": {"count": 1}, "metadata": {"phase": "draft"}},
        ),
    )
    artifact = graphblocks_mcp.mcp_tool_result_artifact_ready(
        admitted,
        resolved,
        sequence=3,
        artifact={
            "artifactId": "artifact-1",
            "uri": "blob://tool-results/1",
            "mediaType": "text/plain",
            "metadata": {"source": "mcp"},
        },
    )

    assert started.kind == "started"
    assert started.started_at == "2026-06-23T00:00:01Z"
    assert delta.kind == "delta"
    assert delta.output[0] == ContentPart(
        kind="text",
        text="partial",
        metadata={"adapter": "mcp", "trust_designation": "untrusted_external"},
    )
    assert delta.output[1].data == {"count": 1}
    assert delta.output[1].metadata == {
        "phase": "draft",
        "adapter": "mcp",
        "trust_designation": "untrusted_external",
    }
    assert delta.into_result() is None
    assert artifact.kind == "artifact_ready"
    assert artifact.artifact == ArtifactRef(
        "artifact-1",
        "blob://tool-results/1",
        media_type="text/plain",
        metadata={"source": "mcp"},
    )
    assert "mcp_tool_result_delta" in graphblocks_mcp.__all__


def test_mcp_adapter_builds_validated_completed_tool_result_event(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
        output_schema="schemas/SearchResult@1",
    )
    result = ToolResult.completed(
        "call-1",
        (ContentPart(kind="json", data={"items": ["billing"]}),),
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
    )

    event = graphblocks_mcp.mcp_tool_result_completed(
        admitted,
        resolved,
        _tool_output_registry(),
        sequence=4,
        result=result,
    )

    assert event.kind == "completed"
    assert event.into_result() == result
    assert event.is_final_durable_result() is True
    assert "mcp_tool_result_completed" in graphblocks_mcp.__all__


def test_mcp_adapter_completed_tool_result_event_requires_result_validation(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
        output_schema="schemas/SearchResult@1",
    )
    result = ToolResult.completed(
        "call-1",
        (ContentPart(kind="json", data={"items": ["billing"]}),),
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
    )

    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="failed validation"):
        graphblocks_mcp.mcp_tool_result_completed(
            admitted,
            resolved,
            _tool_output_registry(),
            sequence=4,
            result=result,
            max_output_bytes=4,
        )

    failed = ToolResult.failed(
        "call-1",
        error={"code": "mcp.failed", "message": "failed"},
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
    )
    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="requires a completed tool result"):
        graphblocks_mcp.mcp_tool_result_completed(
            admitted,
            resolved,
            _tool_output_registry(),
            sequence=5,
            result=failed,
        )


def test_mcp_adapter_builds_terminal_tool_result_events(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
    )
    policy_stopped = ToolResult.policy_stopped(
        "call-1",
        error={"code": "policy.denied", "message": "tool output was stopped"},
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
    ).with_effect_outcome("committed")

    event = graphblocks_mcp.mcp_tool_result_terminal_event(
        admitted,
        resolved,
        _tool_output_registry(),
        sequence=6,
        result=policy_stopped,
    )

    assert event.kind == "policy_stopped"
    assert event.into_result() == policy_stopped
    assert event.is_final_durable_result() is True
    assert "mcp_tool_result_terminal_event" in graphblocks_mcp.__all__


def test_mcp_adapter_terminal_event_rejects_mismatched_result(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
    )
    denied = ToolResult.denied(
        "call-other",
        error={"code": "mcp.denied", "message": "denied"},
        completed_at="2026-06-23T00:00:02Z",
    )

    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="terminal event is invalid"):
        graphblocks_mcp.mcp_tool_result_terminal_event(
            admitted,
            resolved,
            _tool_output_registry(),
            sequence=6,
            result=denied,
        )


def test_mcp_adapter_rejects_invalid_streaming_tool_result_events(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-mcp" / "src"))
    graphblocks_mcp = importlib.import_module("graphblocks_mcp")
    admitted, resolved = _admitted_call_for(
        McpToolImplementation(server="support-mcp", remote_name="search"),
        tool_name="knowledge.search",
        binding_id="binding-mcp-search",
        arguments={"query": "billing"},
    )

    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="delta output must be a sequence"):
        graphblocks_mcp.mcp_tool_result_delta(admitted, resolved, sequence=1, output="draft")

    with pytest.raises(graphblocks_mcp.McpToolAdapterError, match="requires artifact_id and uri"):
        graphblocks_mcp.mcp_tool_result_artifact_ready(
            admitted,
            resolved,
            sequence=2,
            artifact={"artifact_id": "artifact-1"},
        )


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


def test_openapi_adapter_discovers_tool_definitions_from_operation_schemas(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")

    definitions = graphblocks_openapi.define_openapi_tools_from_spec(
        {
            "openapi": "3.1.0",
            "paths": {
                "/tickets": {
                    "post": {
                        "operationId": "createTicket",
                        "summary": "Create a support ticket.",
                        "tags": ["tickets"],
                        "x-graphblocks-tool-name": "ticket.create",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$id": "schemas/TicketCreateRequest@1"}
                                }
                            }
                        },
                        "responses": {
                            "201": {
                                "description": "Created.",
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/Ticket"}
                                    }
                                },
                            }
                        },
                    }
                }
            },
        },
        schema_prefix="schemas/openapi",
        tags=("support",),
        version="1.0.0",
    )

    assert len(definitions) == 1
    assert definitions[0].name == "ticket.create"
    assert definitions[0].description == "Create a support ticket."
    assert definitions[0].input_schema == "schemas/TicketCreateRequest@1"
    assert definitions[0].output_schema == "schemas/openapi/ticket@1"
    assert definitions[0].tags == frozenset({"support", "tickets"})
    assert "define_openapi_tools_from_spec" in graphblocks_openapi.__all__


def test_openapi_adapter_discovery_rejects_blank_operation_metadata(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="operationId"):
        graphblocks_openapi.define_openapi_tools_from_spec(
            {"paths": {"/tickets": {"post": {"operationId": " "}}}},
        )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="tags"):
        graphblocks_openapi.define_openapi_tools_from_spec(
            {"paths": {"/tickets": {"post": {"operationId": "createTicket", "tags": ["tickets", " "]}}}},
        )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="input schema"):
        graphblocks_openapi.define_openapi_tools_from_spec(
            {
                "paths": {
                    "/tickets": {
                        "post": {
                            "operationId": "createTicket",
                            "x-graphblocks-input-schema": " ",
                        }
                    }
                }
            },
        )

    definitions = graphblocks_openapi.define_openapi_tools_from_spec(
        {
            "paths": {
                "/tickets": {
                    "post": {
                        "operationId": " createTicket ",
                        "summary": " Create a support ticket. ",
                        "tags": [" tickets "],
                    }
                }
            }
        },
        tags=(" support ",),
    )

    assert definitions[0].name == "createTicket"
    assert definitions[0].description == "Create a support ticket."
    assert definitions[0].tags == frozenset({"support", "tickets"})


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
        "resolved_tool_id": "resolved-tool-1",
        "tool_name": "ticket.create",
        "tool_call_id": "call-1",
        "connection": "ticket-system",
        "operation_id": "createTicket",
        "arguments": {"priority": "normal", "title": "Need help"},
        "arguments_digest": admitted.call.arguments_digest,
        "definition_digest": resolved.definition_digest,
        "binding_digest": resolved.binding_digest,
        "effective_policy_snapshot_id": "policy-snapshot-1",
        "idempotency_key": "idem-1",
    }
    direct_kwargs = {
        "binding_id": invocation.binding_id,
        "resolved_tool_id": invocation.resolved_tool_id,
        "tool_name": invocation.tool_name,
        "tool_call_id": invocation.tool_call_id,
        "connection": invocation.connection,
        "operation_id": invocation.operation_id,
        "arguments_json": '{"title":"Need help","priority":"normal"}',
        "arguments_digest": invocation.arguments_digest,
        "definition_digest": invocation.definition_digest,
        "binding_digest": invocation.binding_digest,
        "effective_policy_snapshot_id": invocation.effective_policy_snapshot_id,
        "idempotency_key": invocation.idempotency_key,
    }
    direct = graphblocks_openapi.OpenApiOperationInvocation(**direct_kwargs)

    assert direct.arguments_json == '{"priority":"normal","title":"Need help"}'
    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="connection must not be empty"):
        graphblocks_openapi.OpenApiOperationInvocation(**{**direct_kwargs, "connection": " "})
    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="must decode to an object"):
        graphblocks_openapi.OpenApiOperationInvocation(**{**direct_kwargs, "arguments_json": "[]"})
    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="digest does not match"):
        graphblocks_openapi.OpenApiOperationInvocation(**{**direct_kwargs, "arguments_json": '{"title":"changed"}'})


def test_openapi_adapter_rejects_stale_argument_digest(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
    )
    object.__setattr__(admitted.call, "arguments", {"title": "mutated"})
    stale = AdmittedToolCall(
        call=admitted.call,
        idempotency_key=admitted.idempotency_key,
    )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="arguments digest does not match"):
        graphblocks_openapi.prepare_openapi_operation_invocation(stale, resolved)


def test_openapi_adapter_rechecks_resolved_tool_capability_before_invocation(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
    )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="not allowed for principal"):
        graphblocks_openapi.prepare_openapi_operation_invocation(
            admitted,
            replace(resolved, allowed_for_principal=False),
        )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="expired at 2026-06-23T00:00:01Z"):
        graphblocks_openapi.prepare_openapi_operation_invocation(
            admitted,
            replace(resolved, valid_until="2026-06-23T00:00:01Z"),
            validation_time="2026-06-23T00:00:02Z",
        )


def test_openapi_adapter_requires_required_idempotency_key_before_execution(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
        idempotency="required",
        idempotency_key=None,
    )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="requires an idempotency key"):
        graphblocks_openapi.prepare_openapi_operation_invocation(admitted, resolved)


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


def test_openapi_adapter_converts_denied_terminal_result(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
    )
    error = {"code": "openapi.denied", "message": "ticket system denied the operation"}

    result = graphblocks_openapi.openapi_tool_result_denied(
        admitted,
        resolved,
        error=error,
        completed_at="2026-06-23T00:00:02Z",
    )
    error["message"] = "mutated"
    prepared = graphblocks_openapi.prepare_openapi_tool_result_for_model(
        admitted,
        resolved,
        _tool_output_registry(),
        result,
    )

    assert result.status == "denied"
    assert result.error == {"code": "openapi.denied", "message": "ticket system denied the operation"}
    assert result.effect_outcome == "not_committed"
    assert prepared == ()
    assert "openapi_tool_result_denied" in graphblocks_openapi.__all__


def test_openapi_adapter_converts_policy_stopped_terminal_result(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
    )

    result = graphblocks_openapi.openapi_tool_result_policy_stopped(
        admitted,
        resolved,
        error={"code": "policy.denied", "message": "ticket output violated policy"},
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
        effect_outcome="committed",
    )
    prepared = graphblocks_openapi.prepare_openapi_tool_result_for_model(
        admitted,
        resolved,
        _tool_output_registry(),
        result,
    )

    assert result.status == "policy_stopped"
    assert result.effect_was_committed()
    assert prepared == ()


def test_openapi_adapter_converts_cancelled_and_incomplete_terminal_results(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
    )

    cancelled = graphblocks_openapi.openapi_tool_result_cancelled(
        admitted,
        resolved,
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
        effect_outcome="not_committed",
    )
    incomplete = graphblocks_openapi.openapi_tool_result_incomplete(
        admitted,
        resolved,
        started_at="2026-06-23T00:00:03Z",
        completed_at="2026-06-23T00:00:04Z",
        effect_outcome="unknown",
    )

    assert cancelled.status == "cancelled"
    assert cancelled.effect_outcome == "not_committed"
    assert incomplete.status == "incomplete"
    assert incomplete.effect_outcome == "unknown"


def test_openapi_adapter_builds_streaming_tool_result_events(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
    )

    started = graphblocks_openapi.openapi_tool_result_started(
        admitted,
        resolved,
        sequence=1,
        started_at="2026-06-23T00:00:01Z",
    )
    delta = graphblocks_openapi.openapi_tool_result_delta(
        admitted,
        resolved,
        sequence=2,
        output=(
            {"text": "draft ticket"},
            ContentPart(kind="text", text="continuation", metadata={"phase": "draft"}),
        ),
    )
    artifact = graphblocks_openapi.openapi_tool_result_artifact_ready(
        admitted,
        resolved,
        sequence=3,
        artifact=ArtifactRef("artifact-2", "blob://tool-results/2", checksum="sha256:artifact"),
    )

    assert started.kind == "started"
    assert delta.kind == "delta"
    assert delta.output[0] == ContentPart(
        kind="text",
        text="draft ticket",
        metadata={"adapter": "openapi", "trust_designation": "untrusted_external"},
    )
    assert delta.output[1].metadata == {
        "phase": "draft",
        "adapter": "openapi",
        "trust_designation": "untrusted_external",
    }
    assert delta.into_result() is None
    assert artifact.kind == "artifact_ready"
    assert artifact.artifact == ArtifactRef("artifact-2", "blob://tool-results/2", checksum="sha256:artifact")
    assert "openapi_tool_result_artifact_ready" in graphblocks_openapi.__all__


def test_openapi_adapter_builds_validated_completed_tool_result_event(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
        output_schema="schemas/Ticket@1",
    )
    result = ToolResult.completed(
        "call-1",
        (ContentPart(kind="json", data={"ticket_id": "ticket-1"}),),
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
    )

    event = graphblocks_openapi.openapi_tool_result_completed(
        admitted,
        resolved,
        _tool_output_registry(),
        sequence=4,
        result=result,
    )

    assert event.kind == "completed"
    assert event.into_result() == result
    assert event.is_final_durable_result() is True
    assert "openapi_tool_result_completed" in graphblocks_openapi.__all__


def test_openapi_adapter_completed_tool_result_event_requires_result_validation(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
        output_schema="schemas/Ticket@1",
    )
    result = ToolResult.completed(
        "call-1",
        (ContentPart(kind="json", data={"id": "ticket-1"}),),
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
    )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="failed validation"):
        graphblocks_openapi.openapi_tool_result_completed(
            admitted,
            resolved,
            _tool_output_registry(),
            sequence=4,
            result=result,
        )

    denied = ToolResult.denied(
        "call-1",
        error={"code": "openapi.denied", "message": "denied"},
        completed_at="2026-06-23T00:00:02Z",
    )
    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="requires a completed tool result"):
        graphblocks_openapi.openapi_tool_result_completed(
            admitted,
            resolved,
            _tool_output_registry(),
            sequence=5,
            result=denied,
        )


def test_openapi_adapter_builds_terminal_tool_result_events(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
    )
    cancelled = ToolResult.cancelled(
        "call-1",
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
    ).with_effect_outcome("not_committed")

    event = graphblocks_openapi.openapi_tool_result_terminal_event(
        admitted,
        resolved,
        _tool_output_registry(),
        sequence=6,
        result=cancelled,
    )

    assert event.kind == "cancelled"
    assert event.into_result() == cancelled
    assert event.is_final_durable_result() is True
    assert "openapi_tool_result_terminal_event" in graphblocks_openapi.__all__


def test_openapi_adapter_terminal_event_routes_completed_results_through_validation(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
        output_schema="schemas/Ticket@1",
    )
    result = ToolResult.completed(
        "call-1",
        (ContentPart(kind="json", data={"id": "ticket-1"}),),
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
    )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="failed validation"):
        graphblocks_openapi.openapi_tool_result_terminal_event(
            admitted,
            resolved,
            _tool_output_registry(),
            sequence=6,
            result=result,
        )


def test_openapi_adapter_rejects_invalid_streaming_tool_result_events(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openapi" / "src"))
    graphblocks_openapi = importlib.import_module("graphblocks_openapi")
    admitted, resolved = _admitted_call_for(
        OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
        tool_name="ticket.create",
        binding_id="binding-ticket-create",
        arguments={"title": "Need help"},
    )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="metadata must be an object"):
        graphblocks_openapi.openapi_tool_result_delta(
            admitted,
            resolved,
            sequence=1,
            output=({"kind": "text", "text": "draft", "metadata": "bad"},),
        )

    with pytest.raises(graphblocks_openapi.OpenApiToolAdapterError, match="sizeBytes must be an integer"):
        graphblocks_openapi.openapi_tool_result_artifact_ready(
            admitted,
            resolved,
            sequence=2,
            artifact={"artifactId": "artifact-1", "uri": "blob://tool-results/1", "sizeBytes": "large"},
        )

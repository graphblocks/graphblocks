from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


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

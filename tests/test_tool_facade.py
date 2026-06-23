from __future__ import annotations

from graphblocks import (
    BlockToolImplementation,
    OpenApiToolImplementation,
    ResolvedTool,
    ToolBinding,
    ToolDefinition,
)


def test_tool_definition_is_model_visible_contract_only() -> None:
    definition = ToolDefinition(
        name="knowledge.search",
        description="Search support documentation.",
        input_schema="schemas/SearchRequest@1",
        output_schema="schemas/SearchResult@1",
        tags=frozenset({"support", "search"}),
        version="1.0.0",
    )

    assert definition.model_contract() == {
        "name": "knowledge.search",
        "description": "Search support documentation.",
        "input_schema": "schemas/SearchRequest@1",
        "output_schema": "schemas/SearchResult@1",
        "tags": ["search", "support"],
        "version": "1.0.0",
    }
    assert "connection" not in definition.model_contract()
    assert definition.digest().startswith("sha256:")


def test_tool_definition_digest_is_stable_for_tag_order() -> None:
    left = ToolDefinition(
        name="knowledge.search",
        description="Search support documentation.",
        input_schema="schemas/SearchRequest@1",
        tags=frozenset({"support", "search"}),
    )
    right = ToolDefinition(
        name="knowledge.search",
        description="Search support documentation.",
        input_schema="schemas/SearchRequest@1",
        tags=frozenset({"search", "support"}),
    )

    assert left.digest() == right.digest()


def test_tool_binding_digest_includes_execution_contract_not_definition_text() -> None:
    binding = ToolBinding(
        binding_id="binding-ticket-create",
        tool_name="ticket.create",
        implementation=OpenApiToolImplementation(
            connection="ticket-system",
            operation_id="createTicket",
        ),
        effects=frozenset({"external_write", "network"}),
        approval="policy",
        idempotency="required",
        policy_profile_ref="assistant-output-standard",
    )

    canonical = binding.binding_contract()

    assert canonical["implementation"] == {
        "kind": "openapi",
        "connection": "ticket-system",
        "operation_id": "createTicket",
    }
    assert canonical["effects"] == ["external_write", "network"]
    assert "description" not in canonical
    assert binding.digest().startswith("sha256:")


def test_resolved_tool_records_definition_binding_and_policy_identity() -> None:
    definition = ToolDefinition(
        name="knowledge.search",
        description="Search support documentation.",
        input_schema="schemas/SearchRequest@1",
    )
    binding = ToolBinding(
        binding_id="binding-knowledge-search",
        tool_name="knowledge.search",
        implementation=BlockToolImplementation(block="knowledge.search@1"),
        effects=frozenset({"external_read"}),
        approval="never",
        idempotency="not_applicable",
    )
    resolved = ResolvedTool.from_definition_and_binding(
        resolved_tool_id="resolved-1",
        definition=definition,
        binding=binding,
        effective_policy_snapshot_id="policy-snapshot-1",
        allowed_for_principal=True,
    )

    assert resolved.definition_digest == definition.digest()
    assert resolved.binding_digest == binding.digest()
    assert resolved.effective_policy_snapshot_id == "policy-snapshot-1"
    assert resolved.allowed_for_principal is True

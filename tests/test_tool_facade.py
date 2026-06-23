from __future__ import annotations

from graphblocks import (
    BlockToolImplementation,
    ContentPart,
    OpenApiToolImplementation,
    ResolvedTool,
    ToolBinding,
    ToolCallDraft,
    ToolCallError,
    ToolResult,
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


def test_tool_call_draft_requires_complete_json_arguments_before_final_call() -> None:
    draft = ToolCallDraft.proposed("response-1", "call-1", "knowledge.search")
    draft = draft.append_argument_fragment('{"query":')
    draft = draft.append_argument_fragment('"runtime policy"}')

    assert draft.status == "arguments_streaming"
    try:
        draft.into_tool_call("resolved-tool-1", created_at="2026-06-23T00:00:00Z")
    except ToolCallError as error:
        assert str(error) == "tool arguments are not complete"
    else:
        raise AssertionError("streaming arguments must not create a final ToolCall")

    call = draft.complete_arguments().into_tool_call("resolved-tool-1", created_at="2026-06-23T00:00:00Z")

    assert call.tool_call_id == "call-1"
    assert call.response_id == "response-1"
    assert call.resolved_tool_id == "resolved-tool-1"
    assert call.name == "knowledge.search"
    assert call.arguments == {"query": "runtime policy"}
    assert call.arguments_digest.startswith("sha256:")
    assert call.revision == 1
    assert call.status == "validated"


def test_tool_call_argument_digest_is_stable_and_revision_resets_admission_state() -> None:
    left = (
        ToolCallDraft.proposed("response-1", "call-1", "ticket.create")
        .append_argument_fragment('{"b":2,"a":1}')
        .complete_arguments()
        .into_tool_call("resolved-tool-1", created_at="2026-06-23T00:00:00Z")
    )
    right = (
        ToolCallDraft.proposed("response-1", "call-2", "ticket.create")
        .append_argument_fragment('{"a":1,"b":2}')
        .complete_arguments()
        .into_tool_call("resolved-tool-1", created_at="2026-06-23T00:00:01Z")
    )

    assert left.arguments_digest == right.arguments_digest

    admitted = left.with_status("admitted", admitted_at="2026-06-23T00:00:02Z")
    try:
        admitted.revise_arguments({"title": "new"})
    except ToolCallError as error:
        assert str(error) == "tool arguments cannot be revised after validation"
    else:
        raise AssertionError("admitted calls must not be revised")

    revised = left.revise_arguments({"title": "new"})
    assert revised.revision == 2
    assert revised.status == "validated"
    assert revised.admitted_at is None
    assert revised.completed_at is None
    assert revised.arguments_digest != left.arguments_digest


def test_completed_tool_result_computes_stable_output_digest() -> None:
    left = ToolResult.completed(
        "call-1",
        (
            ContentPart(kind="text", text="policy summary"),
            ContentPart(kind="json", data={"b": 2, "a": 1}),
        ),
        started_at="2026-06-23T00:00:00Z",
        completed_at="2026-06-23T00:00:01Z",
    )
    right = ToolResult.completed(
        "call-1",
        (
            ContentPart(kind="text", text="policy summary"),
            ContentPart(kind="json", data={"a": 1, "b": 2}),
        ),
        started_at="2026-06-23T00:00:00Z",
        completed_at="2026-06-23T00:00:01Z",
    )

    assert left.status == "completed"
    assert left.output_digest == right.output_digest
    assert left.output_digest is not None and left.output_digest.startswith("sha256:")
    assert left.started_at == "2026-06-23T00:00:00Z"
    assert left.completed_at == "2026-06-23T00:00:01Z"


def test_policy_stopped_tool_result_is_final_but_incomplete() -> None:
    result = ToolResult.policy_stopped(
        "call-1",
        error={"code": "policy.denied", "message": "tool output was stopped by policy"},
        started_at="2026-06-23T00:00:00Z",
        completed_at="2026-06-23T00:00:01Z",
    )

    assert result.status == "policy_stopped"
    assert result.output_digest is None
    assert result.error == {"code": "policy.denied", "message": "tool output was stopped by policy"}

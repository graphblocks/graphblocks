from __future__ import annotations

import pytest

from graphblocks import (
    ArtifactRef,
    BlockToolImplementation,
    ContentPart,
    OpenApiToolImplementation,
    ResolvedTool,
    ToolBinding,
    ToolCatalog,
    ToolCallDraft,
    ToolCallError,
    ToolResult,
    ToolDefinition,
    ToolExecutionPlan,
    ToolExecutionPlanError,
    ToolPlanCall,
    ToolResolutionError,
    ToolResolutionScope,
    ToolResultEvent,
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


def test_tool_catalog_resolution_intersects_scoped_capabilities() -> None:
    knowledge = ToolDefinition(
        name="knowledge.search",
        description="Search support documentation.",
        input_schema="schemas/SearchRequest@1",
    )
    ticket = ToolDefinition(
        name="ticket.create",
        description="Create a support ticket.",
        input_schema="schemas/TicketCreateRequest@1",
    )
    catalog = ToolCatalog(
        definitions=(knowledge, ticket),
        bindings=(
            ToolBinding(
                binding_id="binding-knowledge",
                tool_name="knowledge.search",
                implementation=BlockToolImplementation(block="knowledge.search@1"),
                effects=frozenset({"external_read"}),
            ),
            ToolBinding(
                binding_id="binding-ticket",
                tool_name="ticket.create",
                implementation=OpenApiToolImplementation(connection="ticket-system", operation_id="createTicket"),
                effects=frozenset({"external_write", "network"}),
            ),
        ),
    )
    scope = ToolResolutionScope(
        application_tools=frozenset({"knowledge.search", "ticket.create"}),
        graph_tools=frozenset({"knowledge.search", "ticket.create"}),
        principal_tools=frozenset({"knowledge.search"}),
        budget_tools=frozenset({"knowledge.search", "ticket.create"}),
    )

    resolved = catalog.resolve(scope, effective_policy_snapshot_id="policy-snapshot-1")

    assert [tool.definition.name for tool in resolved] == ["knowledge.search"]
    assert resolved[0].allowed_for_principal is True
    assert resolved[0].resolved_tool_id.startswith("sha256:")


def test_tool_catalog_reports_visible_tool_without_binding() -> None:
    catalog = ToolCatalog(
        definitions=(
            ToolDefinition(
                name="knowledge.search",
                description="Search support documentation.",
                input_schema="schemas/SearchRequest@1",
            ),
        ),
        bindings=(),
    )

    with pytest.raises(ToolResolutionError) as error:
        catalog.resolve(ToolResolutionScope(), effective_policy_snapshot_id="policy-snapshot-1")

    assert str(error.value) == "tool binding missing for knowledge.search"


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


def test_streaming_tool_result_delta_is_not_a_durable_result() -> None:
    event = ToolResultEvent.delta("call-1", 3, (ContentPart(kind="text", text="draft chunk"),))

    assert event.kind == "delta"
    assert event.tool_call_id == "call-1"
    assert event.sequence == 3
    assert event.output == (ContentPart(kind="text", text="draft chunk"),)
    assert event.is_final_durable_result() is False
    assert event.into_result() is None


def test_tool_result_events_carry_artifacts_and_final_result() -> None:
    artifact = ArtifactRef("artifact-1", "file:///tmp/out.txt", checksum="sha256:out")
    artifact_event = ToolResultEvent.artifact_ready("call-1", 4, artifact)
    result = ToolResult.completed(
        "call-1",
        (ContentPart(kind="text", text="done"),),
        started_at="2026-06-23T00:00:00Z",
        completed_at="2026-06-23T00:00:01Z",
    )
    completed = ToolResultEvent.completed("call-1", 7, result)

    assert artifact_event.kind == "artifact_ready"
    assert artifact_event.artifact == artifact
    assert artifact_event.is_final_durable_result() is False
    assert completed.kind == "completed"
    assert completed.is_final_durable_result() is True
    assert completed.into_result() == result


def _tool_call(tool_call_id: str, arguments: str = '{"resource_id":"a"}'):
    return (
        ToolCallDraft.proposed("response-1", tool_call_id, "ticket.create")
        .append_argument_fragment(arguments)
        .complete_arguments()
        .into_tool_call("resolved-tool-1", created_at="2026-06-23T00:00:00Z")
    )


def test_tool_execution_plan_readies_independent_calls_up_to_parallelism() -> None:
    plan = ToolExecutionPlan(
        plan_id="plan-1",
        response_id="response-1",
        calls=(
            ToolPlanCall(_tool_call("call-a")),
            ToolPlanCall(_tool_call("call-b", '{"resource_id":"b"}')),
        ),
        maximum_parallelism=1,
    )

    assert plan.ready_call_ids() == ["call-a"]
    plan.record_started("call-a")
    assert plan.ready_call_ids() == []
    plan.record_completed("call-a")
    assert plan.ready_call_ids() == ["call-b"]


def test_tool_execution_plan_waits_for_dependencies_and_serializes_effect_keys() -> None:
    dependent = _tool_call("call-b", '{"resource_id":"ticket-1"}')
    dependent = dependent.__class__(
        tool_call_id=dependent.tool_call_id,
        response_id=dependent.response_id,
        resolved_tool_id=dependent.resolved_tool_id,
        name=dependent.name,
        arguments=dependent.arguments,
        arguments_digest=dependent.arguments_digest,
        revision=dependent.revision,
        status=dependent.status,
        depends_on=("call-a",),
        created_at=dependent.created_at,
    )
    plan = ToolExecutionPlan(
        plan_id="plan-1",
        response_id="response-1",
        calls=(
            ToolPlanCall(_tool_call("call-a", '{"resource_id":"ticket-1"}'), effect_key="ticket:ticket-1"),
            ToolPlanCall(dependent, effect_key="ticket:ticket-1"),
            ToolPlanCall(_tool_call("call-c", '{"resource_id":"ticket-2"}'), effect_key="ticket:ticket-2"),
        ),
        maximum_parallelism=3,
    )

    assert plan.ready_call_ids() == ["call-a", "call-c"]
    plan.record_started("call-a")
    assert plan.ready_call_ids() == ["call-c"]
    with pytest.raises(ToolExecutionPlanError) as error:
        plan.record_started("call-b")
    assert str(error.value) == "tool call call-b dependencies are not ready"
    plan.record_completed("call-a")
    assert plan.ready_call_ids() == ["call-b", "call-c"]


def test_tool_execution_plan_policy_stop_denies_pending_and_can_cancel_running() -> None:
    plan = ToolExecutionPlan(
        plan_id="plan-1",
        response_id="response-1",
        calls=(ToolPlanCall(_tool_call("call-a")), ToolPlanCall(_tool_call("call-b"))),
        maximum_parallelism=2,
    )

    plan.record_started("call-a")
    assert plan.apply_policy_stop("deny") == ["call-b"]
    assert plan.state("call-a") == "running"
    assert plan.state("call-b") == "denied"
    with pytest.raises(ToolExecutionPlanError) as error:
        plan.record_started("call-b")
    assert str(error.value) == "tool call call-b is denied, not pending"

    cancel_plan = ToolExecutionPlan(
        plan_id="plan-2",
        response_id="response-1",
        calls=(ToolPlanCall(_tool_call("call-a")), ToolPlanCall(_tool_call("call-b"))),
        maximum_parallelism=2,
    )
    cancel_plan.record_started("call-a")

    assert cancel_plan.apply_policy_stop("cancel_admitted") == ["call-a", "call-b"]
    assert cancel_plan.state("call-a") == "cancelled"
    assert cancel_plan.state("call-b") == "denied"

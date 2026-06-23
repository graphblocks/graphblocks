from __future__ import annotations

from dataclasses import replace

import pytest

from graphblocks import (
    ArtifactRef,
    AdmittedToolCall,
    BlockToolImplementation,
    ContentPart,
    JsonSchema,
    JsonSchemaNode,
    OpenApiToolImplementation,
    ResolvedTool,
    ToolAdmissionError,
    ToolApprovalError,
    ToolApprovalRecord,
    ToolApprovalRequest,
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
    ToolSchemaRegistry,
    ToolSchemaRegistryError,
    ToolSchemaValidationError,
    admit_tool_call,
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


def test_tool_schema_registry_validates_required_nested_arguments() -> None:
    registry = ToolSchemaRegistry(
        schemas=(
            JsonSchema(
                "schemas/ProcessRun@1",
                JsonSchemaNode.object().required_property(
                    "cmd",
                    JsonSchemaNode.array(JsonSchemaNode.string()),
                ),
            ),
        )
    )

    registry.validate("schemas/ProcessRun@1", {"cmd": ["echo", "hello"]})

    with pytest.raises(ToolSchemaValidationError) as missing:
        registry.validate("schemas/ProcessRun@1", {})
    assert str(missing.value) == "schemas/ProcessRun@1 missing required property cmd at $"

    with pytest.raises(ToolSchemaValidationError) as mismatch:
        registry.validate("schemas/ProcessRun@1", {"cmd": ["echo", 7]})
    assert str(mismatch.value) == "schemas/ProcessRun@1 expected string at $.cmd[1]"


def test_tool_schema_registry_reports_missing_and_duplicate_schemas() -> None:
    with pytest.raises(ToolSchemaRegistryError) as duplicate:
        ToolSchemaRegistry(
            schemas=(
                JsonSchema("schemas/ProcessRun@1", JsonSchemaNode.object()),
                JsonSchema("schemas/ProcessRun@1", JsonSchemaNode.object()),
            )
        )
    assert str(duplicate.value) == "duplicate schema schemas/ProcessRun@1"

    registry = ToolSchemaRegistry(schemas=())
    with pytest.raises(ToolSchemaValidationError) as missing:
        registry.validate("schemas/Missing@1", {})
    assert str(missing.value) == "schema schemas/Missing@1 is not registered"


def _resolved_search_tool() -> ResolvedTool:
    catalog = ToolCatalog(
        definitions=(
            ToolDefinition(
                name="knowledge.search",
                description="Search documentation.",
                input_schema="schemas/Search@1",
            ),
        ),
        bindings=(
            ToolBinding(
                binding_id="binding-search",
                tool_name="knowledge.search",
                implementation=BlockToolImplementation(block="blocks.search"),
                effects=frozenset({"external_read"}),
            ),
        ),
    )
    return catalog.resolve(ToolResolutionScope(), effective_policy_snapshot_id="policy-snapshot-1")[0]


def _search_call(resolved: ResolvedTool, query: str = "runtime"):
    return (
        ToolCallDraft.proposed("response-1", "call-1", "knowledge.search")
        .append_argument_fragment(f'{{"query":"{query}"}}')
        .complete_arguments()
        .into_tool_call(resolved.resolved_tool_id, created_at="2026-06-23T00:00:00Z")
    )


def test_tool_approval_record_is_valid_only_for_same_call_arguments_and_principal() -> None:
    resolved = _resolved_search_tool()
    call = _search_call(resolved)
    request = ToolApprovalRequest.for_call(
        "approval-1",
        resolved,
        call,
        principal_id="user-1",
        requested_at=1_000,
        expires_at=2_000,
    )
    record = ToolApprovalRecord.approve(request, approver_id="admin-1", decided_at=1_100)

    assert record.status == "approved"
    assert record.request.revision == 1
    assert record.is_valid_for(resolved, call, principal_id="user-1", now=1_500) is True
    assert record.is_valid_for(resolved, call, principal_id="user-2", now=1_500) is False
    assert record.is_valid_for(resolved, call, principal_id="user-1", now=2_001) is False

    changed = _search_call(resolved, query="changed")
    assert record.is_valid_for(resolved, changed, principal_id="user-1", now=1_500) is False


def test_tool_approval_record_is_invalid_after_argument_revision() -> None:
    resolved = _resolved_search_tool()
    call = _search_call(resolved)
    request = ToolApprovalRequest.for_call(
        "approval-1",
        resolved,
        call,
        principal_id="user-1",
        requested_at=1_000,
        expires_at=2_000,
    )
    record = ToolApprovalRecord.approve(request, approver_id="admin-1", decided_at=1_100)
    revised = call.revise_arguments({"query": "changed"})

    assert revised.revision == 2
    assert record.is_valid_for(resolved, revised, principal_id="user-1", now=1_500) is False


def test_tool_approval_request_rejects_mismatch_and_invalid_expiration() -> None:
    resolved = _resolved_search_tool()
    call = _search_call(resolved)

    with pytest.raises(ToolApprovalError) as expiration:
        ToolApprovalRequest.for_call(
            "approval-1",
            resolved,
            call,
            principal_id="user-1",
            requested_at=2_000,
            expires_at=1_000,
        )
    assert str(expiration.value) == "approval expiration must be after request time"

    mismatched = call.__class__(
        tool_call_id=call.tool_call_id,
        response_id=call.response_id,
        resolved_tool_id="resolved-tool-other",
        name=call.name,
        arguments=call.arguments,
        arguments_digest=call.arguments_digest,
        revision=call.revision,
        status=call.status,
        created_at=call.created_at,
    )
    with pytest.raises(ToolApprovalError) as mismatch:
        ToolApprovalRequest.for_call(
            "approval-1",
            resolved,
            mismatched,
            principal_id="user-1",
            requested_at=1_000,
            expires_at=2_000,
        )
    assert str(mismatch.value) == "tool call references a different resolved tool"


def _resolved_process_tool() -> ResolvedTool:
    catalog = ToolCatalog(
        definitions=(
            ToolDefinition(
                name="process.run",
                description="Run an approved process.",
                input_schema="schemas/ProcessRun@1",
            ),
        ),
        bindings=(
            ToolBinding(
                binding_id="binding-process",
                tool_name="process.run",
                implementation=BlockToolImplementation(block="blocks.process"),
                effects=frozenset({"process"}),
                approval="always",
                idempotency="required",
            ),
        ),
    )
    return catalog.resolve(ToolResolutionScope(), effective_policy_snapshot_id="policy-snapshot-1")[0]


def _process_call(resolved: ResolvedTool, arguments: str = '{"cmd":["echo","hello"]}'):
    return (
        ToolCallDraft.proposed("response-1", "call-1", "process.run")
        .append_argument_fragment(arguments)
        .complete_arguments()
        .into_tool_call(resolved.resolved_tool_id, created_at="2026-06-23T00:00:00Z")
    )


def _process_schema_registry() -> ToolSchemaRegistry:
    return ToolSchemaRegistry(
        schemas=(
            JsonSchema(
                "schemas/ProcessRun@1",
                JsonSchemaNode.object().required_property("cmd", JsonSchemaNode.array(JsonSchemaNode.string())),
            ),
        )
    )


def test_tool_admission_validates_arguments_before_approval() -> None:
    resolved = _resolved_process_tool()
    call = _process_call(resolved, arguments='{"cmd":"echo hello"}')

    with pytest.raises(ToolAdmissionError) as error:
        admit_tool_call(
            call,
            resolved,
            _process_schema_registry(),
            principal_id="user-1",
            idempotency_key="idem-1",
            admitted_at="2026-06-23T00:00:01Z",
            now=1_200,
        )

    assert str(error.value) == "tool call call-1 arguments invalid: schemas/ProcessRun@1 expected array at $.cmd"


def test_tool_admission_denies_tool_no_longer_allowed_for_principal() -> None:
    resolved = replace(_resolved_process_tool(), allowed_for_principal=False)
    call = _process_call(resolved)

    with pytest.raises(ToolAdmissionError) as error:
        admit_tool_call(
            call,
            resolved,
            _process_schema_registry(),
            principal_id="user-1",
            idempotency_key="idem-1",
            admitted_at="2026-06-23T00:00:01Z",
            now=1_200,
        )

    assert str(error.value) == "resolved tool process.run is not allowed for principal user-1"


def test_tool_admission_denies_expired_resolved_tool() -> None:
    resolved = replace(_resolved_process_tool(), valid_until="2026-06-23T00:00:00Z")
    call = _process_call(resolved)

    with pytest.raises(ToolAdmissionError) as error:
        admit_tool_call(
            call,
            resolved,
            _process_schema_registry(),
            principal_id="user-1",
            idempotency_key="idem-1",
            admitted_at="2026-06-23T00:00:01Z",
            now=1_200,
        )

    assert str(error.value) == "resolved tool process.run expired at 2026-06-23T00:00:00Z"


def test_tool_admission_requires_approval_and_idempotency_key() -> None:
    resolved = _resolved_process_tool()
    call = _process_call(resolved)

    with pytest.raises(ToolAdmissionError) as approval_error:
        admit_tool_call(
            call,
            resolved,
            _process_schema_registry(),
            principal_id="user-1",
            idempotency_key="idem-1",
            admitted_at="2026-06-23T00:00:01Z",
            now=1_200,
        )
    assert str(approval_error.value) == "tool call call-1 requires approval"

    request = ToolApprovalRequest.for_call(
        "approval-1",
        resolved,
        call,
        principal_id="user-1",
        requested_at=1_100,
        expires_at=2_000,
    )
    approval = ToolApprovalRecord.approve(request, approver_id="admin-1", decided_at=1_150)

    with pytest.raises(ToolAdmissionError) as idempotency_error:
        admit_tool_call(
            call,
            resolved,
            _process_schema_registry(),
            approval=approval,
            principal_id="user-1",
            admitted_at="2026-06-23T00:00:01Z",
            now=1_200,
        )
    assert str(idempotency_error.value) == "tool call call-1 requires an idempotency key"


def test_tool_admission_returns_admitted_call_with_idempotency_key() -> None:
    resolved = _resolved_process_tool()
    call = _process_call(resolved)
    request = ToolApprovalRequest.for_call(
        "approval-1",
        resolved,
        call,
        principal_id="user-1",
        requested_at=1_100,
        expires_at=2_000,
    )
    approval = ToolApprovalRecord.approve(request, approver_id="admin-1", decided_at=1_150)

    admitted = admit_tool_call(
        call,
        resolved,
        _process_schema_registry(),
        approval=approval,
        principal_id="user-1",
        idempotency_key="idem-1",
        admitted_at="2026-06-23T00:00:01Z",
        now=1_200,
    )

    assert isinstance(admitted, AdmittedToolCall)
    assert admitted.call.status == "admitted"
    assert admitted.call.admitted_at == "2026-06-23T00:00:01Z"
    assert admitted.idempotency_key == "idem-1"


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


def test_denied_tool_result_records_pre_execution_denial() -> None:
    result = ToolResult.denied(
        "call-1",
        error={"code": "tool.denied", "message": "tool was denied before execution"},
        completed_at="2026-06-23T00:00:01Z",
    )

    assert result.status == "denied"
    assert result.output_digest is None
    assert result.started_at is None
    assert result.completed_at == "2026-06-23T00:00:01Z"
    assert result.error == {"code": "tool.denied", "message": "tool was denied before execution"}


def test_policy_stopped_tool_result_can_report_committed_effect_outcome() -> None:
    result = ToolResult.policy_stopped(
        "call-1",
        error={"code": "policy.denied", "message": "tool output was stopped after a write committed"},
        started_at="2026-06-23T00:00:00Z",
        completed_at="2026-06-23T00:00:01Z",
    ).with_effect_outcome("committed")

    assert result.status == "policy_stopped"
    assert result.effect_outcome == "committed"
    assert result.effect_was_committed() is True


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


def test_terminal_tool_result_events_preserve_partial_terminal_kind() -> None:
    policy_stopped = ToolResult.policy_stopped(
        "call-1",
        error={"code": "policy.denied", "message": "tool output was stopped by policy"},
        started_at="2026-06-23T00:00:00Z",
        completed_at="2026-06-23T00:00:01Z",
    )
    cancelled = ToolResult.cancelled(
        "call-2",
        started_at="2026-06-23T00:00:02Z",
        completed_at="2026-06-23T00:00:03Z",
    )
    incomplete = ToolResult.incomplete(
        "call-3",
        started_at="2026-06-23T00:00:04Z",
        completed_at="2026-06-23T00:00:05Z",
    )

    policy_event = ToolResultEvent.policy_stopped("call-1", 8, policy_stopped)
    cancelled_event = ToolResultEvent.cancelled("call-2", 9, cancelled)
    incomplete_event = ToolResultEvent.incomplete("call-3", 10, incomplete)

    assert policy_event.kind == "policy_stopped"
    assert cancelled_event.kind == "cancelled"
    assert incomplete_event.kind == "incomplete"
    assert policy_event.is_final_durable_result() is True
    assert cancelled_event.is_final_durable_result() is True
    assert incomplete_event.is_final_durable_result() is True
    assert policy_event.into_result() == policy_stopped
    assert cancelled_event.into_result() == cancelled
    assert incomplete_event.into_result() == incomplete


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
    dependent = replace(dependent, depends_on=("call-a",))
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


def test_tool_execution_plan_skips_dependents_after_dependency_failure() -> None:
    dependent = replace(_tool_call("call-b", '{"resource_id":"b"}'), depends_on=("call-a",))
    transitive = replace(_tool_call("call-c", '{"resource_id":"c"}'), depends_on=("call-b",))
    plan = ToolExecutionPlan(
        plan_id="plan-1",
        response_id="response-1",
        calls=(
            ToolPlanCall(_tool_call("call-a", '{"resource_id":"a"}')),
            ToolPlanCall(dependent),
            ToolPlanCall(transitive),
        ),
        maximum_parallelism=3,
    )

    assert plan.ready_call_ids() == ["call-a"]
    plan.record_started("call-a")
    plan.record_failed("call-a")

    assert plan.state("call-a") == "failed"
    assert plan.state("call-b") == "skipped"
    assert plan.state("call-c") == "skipped"
    assert plan.ready_call_ids() == []


def test_tool_execution_fail_fast_cancels_pending_calls_after_failure() -> None:
    plan = ToolExecutionPlan(
        plan_id="plan-1",
        response_id="response-1",
        calls=(
            ToolPlanCall(_tool_call("call-a", '{"resource_id":"a"}')),
            ToolPlanCall(_tool_call("call-b", '{"resource_id":"b"}')),
            ToolPlanCall(_tool_call("call-c", '{"resource_id":"c"}')),
        ),
        maximum_parallelism=3,
        failure_policy="fail_fast",
    )

    plan.record_started("call-a")
    plan.record_started("call-b")
    plan.record_failed("call-a")

    assert plan.state("call-a") == "failed"
    assert plan.state("call-b") == "running"
    assert plan.state("call-c") == "cancelled"
    assert plan.ready_call_ids() == []


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


def test_tool_execution_cancelled_call_cancels_dependents_by_default() -> None:
    dependent = replace(_tool_call("call-b", '{"resource_id":"b"}'), depends_on=("call-a",))
    plan = ToolExecutionPlan(
        plan_id="plan-1",
        response_id="response-1",
        calls=(
            ToolPlanCall(_tool_call("call-a", '{"resource_id":"a"}')),
            ToolPlanCall(dependent),
            ToolPlanCall(_tool_call("call-c", '{"resource_id":"c"}')),
        ),
        maximum_parallelism=3,
    )

    plan.record_started("call-a")
    plan.record_cancelled("call-a")

    assert plan.state("call-a") == "cancelled"
    assert plan.state("call-b") == "cancelled"
    assert plan.state("call-c") == "pending"
    assert plan.ready_call_ids() == ["call-c"]


def test_tool_execution_cancelled_call_can_skip_dependents_and_allow_independent_calls() -> None:
    dependent = replace(_tool_call("call-b", '{"resource_id":"b"}'), depends_on=("call-a",))
    plan = ToolExecutionPlan(
        plan_id="plan-1",
        response_id="response-1",
        calls=(
            ToolPlanCall(_tool_call("call-a", '{"resource_id":"a"}')),
            ToolPlanCall(dependent),
            ToolPlanCall(_tool_call("call-c", '{"resource_id":"c"}')),
        ),
        maximum_parallelism=3,
        cancellation_policy="allow_independent_calls",
    )

    plan.record_started("call-a")
    plan.record_cancelled("call-a")

    assert plan.state("call-a") == "cancelled"
    assert plan.state("call-b") == "skipped"
    assert plan.state("call-c") == "pending"
    assert plan.ready_call_ids() == ["call-c"]


def test_tool_execution_cancel_all_policy_cancels_every_nonterminal_call() -> None:
    plan = ToolExecutionPlan(
        plan_id="plan-1",
        response_id="response-1",
        calls=(
            ToolPlanCall(_tool_call("call-a", '{"resource_id":"a"}')),
            ToolPlanCall(_tool_call("call-b", '{"resource_id":"b"}')),
            ToolPlanCall(_tool_call("call-c", '{"resource_id":"c"}')),
        ),
        maximum_parallelism=3,
        cancellation_policy="cancel_all",
    )

    plan.record_started("call-a")
    plan.record_started("call-c")
    plan.record_completed("call-c")
    plan.record_cancelled("call-a")

    assert plan.state("call-a") == "cancelled"
    assert plan.state("call-b") == "cancelled"
    assert plan.state("call-c") == "completed"
    assert plan.ready_call_ids() == []

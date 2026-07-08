from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

import graphblocks
from graphblocks.approval import VALID_APPROVAL_STATUSES


ROOT = Path(__file__).parents[1]


def test_audit_package_exposes_append_only_event_and_enforcement_records(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-audit" / "src"))
    graphblocks_audit = importlib.import_module("graphblocks_audit")

    metadata = graphblocks_audit.ApplicationEventMetadata(
        event_id="event-1",
        run_id="run-1",
        response_id="response-1",
        sequence=1,
        release_id="release-1",
        policy_snapshot_id="policy-1",
        occurred_at="2026-06-23T00:00:00Z",
        turn_id="turn-1",
    )
    event = graphblocks_audit.ApplicationEvent.new(
        "OutputPolicyAllowed",
        metadata,
        payload={"decision_id": "decision-1"},
    )
    decision = graphblocks_audit.PolicyDecision(
        decision_id="decision-1",
        effect="allow",
        reason_codes=("allow-output",),
        policy_refs=("policy/output",),
        input_digest="sha256:input",
    )
    enforcement = graphblocks_audit.PolicyEnforcementRecord.from_decision(
        record_id="enforcement-1",
        decision=decision,
        enforcement_point="before_client_delivery",
        status="enforced",
    )

    assert event.metadata.event_id == "event-1"
    assert enforcement.decision_id == "decision-1"
    assert enforcement.enforcement_point == "before_client_delivery"
    assert enforcement.status == "enforced"
    assert graphblocks_audit.VALID_APPROVAL_STATUSES is VALID_APPROVAL_STATUSES
    assert "VALID_APPROVAL_STATUSES" in graphblocks_audit.__all__
    assert graphblocks.VALID_APPROVAL_STATUSES is VALID_APPROVAL_STATUSES
    assert "VALID_APPROVAL_STATUSES" in graphblocks.__all__


def test_audit_package_exposes_native_audit_helpers(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-audit" / "src"))
    calls: list[tuple[str, dict[str, object]]] = []

    def record_tool_effect_precondition(
        resolved_tool: dict[str, object],
        call: dict[str, object],
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(("precondition", {"resolved_tool": resolved_tool, "call": call, **kwargs}))
        return {"digest": "sha256:precondition", "payload": {"tool_call_id": call["toolCallId"]}}

    def record_tool_effect_audit_event(**kwargs: object) -> dict[str, object]:
        calls.append(("audit_event", dict(kwargs)))
        return {"eventId": kwargs["event_id"], "payloadDigest": "sha256:audit-event"}

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            record_tool_effect_audit_event=record_tool_effect_audit_event,
            record_tool_effect_precondition=record_tool_effect_precondition,
        ),
    )
    graphblocks_audit = importlib.import_module("graphblocks_audit")

    precondition = graphblocks_audit.record_native_tool_effect_precondition(
        {"resolvedToolId": "resolved-tool-1"},
        {"toolCallId": "call-1"},
        effect_key="ticket.create:cust-1",
        idempotency_key="idem-ticket-1",
    )
    event = graphblocks_audit.record_native_tool_effect_audit_event(
        event_id="audit-effect-1",
        occurred_at="2026-06-23T00:00:02Z",
        actor={"principalId": "user-1"},
        resolved_tool={"resolvedToolId": "resolved-tool-1"},
        call={"toolCallId": "call-1"},
        result={"toolCallId": "call-1", "status": "completed"},
        precondition_digest=precondition["digest"],
    )

    assert precondition == {"digest": "sha256:precondition", "payload": {"tool_call_id": "call-1"}}
    assert event == {"eventId": "audit-effect-1", "payloadDigest": "sha256:audit-event"}
    assert calls == [
        (
            "precondition",
            {
                "resolved_tool": {"resolvedToolId": "resolved-tool-1"},
                "call": {"toolCallId": "call-1"},
                "effect_key": "ticket.create:cust-1",
                "idempotency_key": "idem-ticket-1",
                "policy_decision_id": None,
                "execution_target": None,
                "sandbox_id": None,
            },
        ),
        (
            "audit_event",
            {
                "event_id": "audit-effect-1",
                "occurred_at": "2026-06-23T00:00:02Z",
                "actor": {"principalId": "user-1"},
                "resolved_tool": {"resolvedToolId": "resolved-tool-1"},
                "call": {"toolCallId": "call-1"},
                "result": {"toolCallId": "call-1", "status": "completed"},
                "effect_key": None,
                "precondition_digest": "sha256:precondition",
                "idempotency_key": None,
                "policy_decision_id": None,
            },
        ),
    ]
    assert "record_native_tool_effect_precondition" in graphblocks_audit.__all__
    assert "record_native_tool_effect_audit_event" in graphblocks_audit.__all__


def test_audit_package_records_tool_effect_precondition_and_outcome(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-audit" / "src"))
    graphblocks = importlib.import_module("graphblocks")
    graphblocks_audit = importlib.import_module("graphblocks_audit")

    catalog = graphblocks.ToolCatalog(
        definitions=(
            graphblocks.ToolDefinition(
                "ticket.create",
                "Create a support ticket.",
                "schemas/TicketCreate@1",
            ),
        ),
        bindings=(
            graphblocks.ToolBinding(
                "binding-ticket-create",
                "ticket.create",
                graphblocks.BlockToolImplementation("blocks.ticket_create"),
                effects=frozenset({"destructive", "external_write", "network"}),
            ),
        ),
    )
    resolved_tool = catalog.resolve(
        graphblocks.ToolResolutionScope(),
        effective_policy_snapshot_id="policy-snapshot-1",
    )[0]
    draft = graphblocks.ToolCallDraft.proposed("response-1", "call-1", "ticket.create")
    call = draft.append_argument_fragment(
        '{"customer_id":"cust-1","title":"Help"}'
    ).complete_arguments().into_tool_call(
        resolved_tool.resolved_tool_id,
        created_at="2026-06-23T00:00:00Z",
    ).with_status("admitted", admitted_at="2026-06-23T00:00:00Z")
    precondition = graphblocks_audit.ToolEffectPrecondition.from_admitted_call(
        resolved_tool=resolved_tool,
        call=call,
        effect_key="ticket.create:cust-1",
        idempotency_key="idem-ticket-1",
        policy_decision_id="decision-tool-1",
        execution_target="worker:local",
        sandbox_id="sandbox-1",
    )
    result = graphblocks.ToolResult.completed(
        "call-1",
        (graphblocks.ContentPart(kind="json", data={"ticket_id": "T-1"}),),
        started_at="2026-06-23T00:00:01Z",
        completed_at="2026-06-23T00:00:02Z",
    ).with_effect_outcome("committed")

    record = graphblocks_audit.ToolEffectAuditRecord.from_tool_result(
        event_id="audit-effect-1",
        occurred_at="2026-06-23T00:00:03Z",
        actor=graphblocks_audit.PrincipalRef("user-1", tenant_id="tenant-a"),
        resolved_tool=resolved_tool,
        call=call,
        result=result,
        effect_key="ticket.create:cust-1",
        precondition_digest=precondition.digest,
        idempotency_key="idem-ticket-1",
        policy_decision_id="decision-tool-1",
    )

    assert record.target_kind == "destructive_effect"
    assert record.resource.resource_id == "tool:ticket.create"
    assert record.reason_codes == ("tool_effect.committed",)
    assert record.payload == {
        "tool_call_id": "call-1",
        "response_id": "response-1",
        "resolved_tool_id": resolved_tool.resolved_tool_id,
        "tool_name": "ticket.create",
        "tool_call_revision": 1,
        "arguments_digest": call.arguments_digest,
        "definition_digest": resolved_tool.definition_digest,
        "binding_digest": resolved_tool.binding_digest,
        "effective_policy_snapshot_id": "policy-snapshot-1",
        "effects": ["destructive", "external_write", "network"],
        "effect_key": "ticket.create:cust-1",
        "precondition_digest": precondition.digest,
        "idempotency_key": "idem-ticket-1",
        "policy_decision_id": "decision-tool-1",
        "result_status": "completed",
        "effect_outcome": "committed",
        "output_digest": result.output_digest,
        "started_at": "2026-06-23T00:00:01Z",
        "completed_at": "2026-06-23T00:00:02Z",
    }
    assert record.payload_digest().startswith("sha256:")


def test_audit_package_builds_tool_effect_precondition(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-audit" / "src"))
    graphblocks = importlib.import_module("graphblocks")
    graphblocks_audit = importlib.import_module("graphblocks_audit")

    catalog = graphblocks.ToolCatalog(
        definitions=(
            graphblocks.ToolDefinition(
                "ticket.create",
                "Create a support ticket.",
                "schemas/TicketCreate@1",
            ),
        ),
        bindings=(
            graphblocks.ToolBinding(
                "binding-ticket-create",
                "ticket.create",
                graphblocks.BlockToolImplementation("blocks.ticket_create"),
                effects=frozenset({"destructive", "external_write", "network"}),
            ),
        ),
    )
    resolved_tool = catalog.resolve(
        graphblocks.ToolResolutionScope(),
        effective_policy_snapshot_id="policy-snapshot-1",
    )[0]
    call = graphblocks.ToolCallDraft.proposed("response-1", "call-1", "ticket.create").append_argument_fragment(
        '{"customer_id":"cust-1","title":"Help"}'
    ).complete_arguments().into_tool_call(
        resolved_tool.resolved_tool_id,
        created_at="2026-06-23T00:00:00Z",
    ).with_status(
        "admitted",
        admitted_at="2026-06-23T00:00:00Z",
    )

    precondition = graphblocks_audit.ToolEffectPrecondition.from_admitted_call(
        resolved_tool=resolved_tool,
        call=call,
        effect_key="ticket.create:cust-1",
        idempotency_key="idem-ticket-1",
        policy_decision_id="decision-tool-1",
        execution_target="worker:local",
        sandbox_id="sandbox-1",
    )
    same_precondition = graphblocks_audit.ToolEffectPrecondition.from_admitted_call(
        resolved_tool=resolved_tool,
        call=call,
        effect_key="ticket.create:cust-1",
        idempotency_key="idem-ticket-1",
        policy_decision_id="decision-tool-1",
        execution_target="worker:local",
        sandbox_id="sandbox-1",
    )

    assert precondition.digest == same_precondition.digest
    assert precondition.digest.startswith("sha256:")
    assert dict(precondition.payload) == {
        "tool_call_id": "call-1",
        "response_id": "response-1",
        "resolved_tool_id": resolved_tool.resolved_tool_id,
        "binding_id": "binding-ticket-create",
        "tool_name": "ticket.create",
        "tool_call_revision": 1,
        "arguments_digest": call.arguments_digest,
        "definition_digest": resolved_tool.definition_digest,
        "binding_digest": resolved_tool.binding_digest,
        "effective_policy_snapshot_id": "policy-snapshot-1",
        "effects": ["destructive", "external_write", "network"],
        "effect_key": "ticket.create:cust-1",
        "idempotency_key": "idem-ticket-1",
        "policy_decision_id": "decision-tool-1",
        "execution_target": "worker:local",
        "sandbox_id": "sandbox-1",
        "admitted_at": "2026-06-23T00:00:00Z",
    }
    assert "ToolEffectPrecondition" in graphblocks_audit.__all__


def test_audit_package_rejects_precondition_before_admission(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-audit" / "src"))
    graphblocks = importlib.import_module("graphblocks")
    graphblocks_audit = importlib.import_module("graphblocks_audit")

    definition = graphblocks.ToolDefinition(
        "knowledge.search",
        "Search documentation.",
        "schemas/Search@1",
    )
    binding = graphblocks.ToolBinding(
        "binding-search",
        "knowledge.search",
        graphblocks.BlockToolImplementation("blocks.search"),
    )
    resolved_tool = graphblocks.ResolvedTool.from_definition_and_binding(
        resolved_tool_id="resolved-search",
        definition=definition,
        binding=binding,
        effective_policy_snapshot_id="policy-snapshot-1",
        allowed_for_principal=True,
    )
    call = graphblocks.ToolCall(
        tool_call_id="call-1",
        response_id="response-1",
        resolved_tool_id="resolved-search",
        name="knowledge.search",
        arguments={},
        arguments_digest=graphblocks.canonical_hash({}),
    )

    try:
        graphblocks_audit.ToolEffectPrecondition.from_admitted_call(
            resolved_tool=resolved_tool,
            call=call,
        )
    except graphblocks_audit.ToolEffectAuditError as error:
        assert "must be admitted" in str(error)
    else:
        raise AssertionError("precondition should require admitted tool call")


def test_audit_package_rejects_mismatched_tool_effect_record_inputs(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-audit" / "src"))
    graphblocks = importlib.import_module("graphblocks")
    graphblocks_audit = importlib.import_module("graphblocks_audit")

    definition = graphblocks.ToolDefinition(
        "knowledge.search",
        "Search documentation.",
        "schemas/Search@1",
    )
    binding = graphblocks.ToolBinding(
        "binding-search",
        "knowledge.search",
        graphblocks.BlockToolImplementation("blocks.search"),
    )
    resolved_tool = graphblocks.ResolvedTool.from_definition_and_binding(
        resolved_tool_id="resolved-search",
        definition=definition,
        binding=binding,
        effective_policy_snapshot_id="policy-snapshot-1",
        allowed_for_principal=True,
    )
    call = graphblocks.ToolCall(
        tool_call_id="call-1",
        response_id="response-1",
        resolved_tool_id="resolved-search",
        name="knowledge.search",
        arguments={},
        arguments_digest=graphblocks.canonical_hash({}),
    )

    try:
        graphblocks_audit.ToolEffectAuditRecord.from_tool_result(
            event_id="audit-effect-1",
            occurred_at="2026-06-23T00:00:03Z",
            actor=graphblocks_audit.PrincipalRef("user-1"),
            resolved_tool=resolved_tool,
            call=call,
            result=graphblocks.ToolResult.completed(
                "other-call",
                (graphblocks.ContentPart(kind="text", text="ok"),),
                started_at="2026-06-23T00:00:01Z",
                completed_at="2026-06-23T00:00:02Z",
            ),
        )
    except graphblocks_audit.ToolEffectAuditError as error:
        assert "other-call" in str(error)
        assert "call-1" in str(error)
    else:
        raise AssertionError("mismatched tool result should be rejected")


def test_audit_package_persists_outbox_records(monkeypatch, tmp_path) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-audit" / "src"))
    graphblocks_audit = importlib.import_module("graphblocks_audit")
    path = tmp_path / "audit.sqlite3"

    outbox = graphblocks_audit.SQLiteAuditOutbox(path)
    first = outbox.append(
        "application_event",
        {"event_id": "event-1", "kind": "OutputPolicyAllowed"},
        occurred_at="2026-06-23T00:00:00Z",
        record_id="audit-1",
    )
    second = outbox.append(
        "policy_enforcement",
        {"record_id": "enforcement-1", "status": "blocked"},
        occurred_at="2026-06-23T00:00:01Z",
        record_id="audit-2",
    )
    outbox.close()

    reopened = graphblocks_audit.SQLiteAuditOutbox(path)
    assert reopened.get("audit-1") == first
    assert [record.record_id for record in reopened.pending()] == ["audit-1", "audit-2"]
    assert reopened.pending(limit=1) == [first]

    published = reopened.mark_published("audit-1", published_at="2026-06-23T00:00:02Z")
    failed = reopened.mark_failed("audit-2", error="sink unavailable")

    assert published.status == "published"
    assert published.published_at == "2026-06-23T00:00:02Z"
    assert failed.status == "failed"
    assert failed.attempts == second.attempts + 1
    assert failed.last_error == "sink unavailable"
    assert reopened.pending() == [failed]
    reopened.close()


def test_audit_outbox_rejects_non_standard_payload_json_on_replay(monkeypatch, tmp_path) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-audit" / "src"))
    graphblocks_audit = importlib.import_module("graphblocks_audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox(tmp_path / "audit.sqlite3")
    outbox.append("application_event", {"event_id": "event-1"}, occurred_at="2026-06-23T00:00:00Z", record_id="audit-1")
    outbox._connection.execute(  # noqa: SLF001
        "UPDATE audit_outbox_records SET payload_json = ? WHERE record_id = ?",
        ('{"value": NaN}', "audit-1"),
    )
    outbox._connection.commit()  # noqa: SLF001

    try:
        outbox.get("audit-1")
    except ValueError as error:
        assert "audit outbox payload_json must be valid strict JSON" in str(error)
    else:
        raise AssertionError("audit outbox replay should reject non-standard JSON constants")
    finally:
        outbox.close()


def test_audit_outbox_rejects_payload_digest_drift_on_replay(monkeypatch, tmp_path) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-audit" / "src"))
    graphblocks_audit = importlib.import_module("graphblocks_audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox(tmp_path / "audit.sqlite3")
    outbox.append("application_event", {"event_id": "event-1"}, occurred_at="2026-06-23T00:00:00Z", record_id="audit-1")
    outbox._connection.execute(  # noqa: SLF001
        "UPDATE audit_outbox_records SET payload_json = ? WHERE record_id = ?",
        ('{"event_id":"event-mutated"}', "audit-1"),
    )
    outbox._connection.commit()  # noqa: SLF001

    try:
        outbox.get("audit-1")
    except ValueError as error:
        assert "audit outbox payload_digest does not match payload_json" in str(error)
    else:
        raise AssertionError("audit outbox replay should reject payload digest drift")
    finally:
        outbox.close()


def test_audit_package_rejects_duplicate_outbox_record_ids(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-audit" / "src"))
    graphblocks_audit = importlib.import_module("graphblocks_audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()
    outbox.append("application_event", {"event_id": "event-1"}, occurred_at="2026-06-23T00:00:00Z", record_id="audit-1")

    try:
        outbox.append(
            "application_event",
            {"event_id": "event-2"},
            occurred_at="2026-06-23T00:00:01Z",
            record_id="audit-1",
        )
    except graphblocks_audit.AuditOutboxConflictError as error:
        assert "audit-1" in str(error)
    else:
        raise AssertionError("duplicate audit outbox record should be rejected")
    finally:
        outbox.close()


def test_audit_outbox_treats_published_records_as_terminal(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-audit" / "src"))
    graphblocks_audit = importlib.import_module("graphblocks_audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()
    outbox.append("application_event", {"event_id": "event-1"}, occurred_at="2026-06-23T00:00:00Z", record_id="audit-1")
    published = outbox.mark_published("audit-1", published_at="2026-06-23T00:00:01Z")

    try:
        outbox.mark_failed("audit-1", error="sink unavailable")
    except graphblocks_audit.AuditOutboxError as error:
        assert "already published" in str(error)
    else:
        raise AssertionError("published audit records should not be marked failed")

    assert outbox.mark_published("audit-1", published_at="2026-06-23T00:00:01Z") == published
    try:
        outbox.mark_published("audit-1", published_at="2026-06-23T00:00:02Z")
    except graphblocks_audit.AuditOutboxError as error:
        assert "already published" in str(error)
    else:
        raise AssertionError("published audit records should not change terminal timestamp")

    assert outbox.get("audit-1").status == "published"
    assert outbox.get("audit-1").published_at == "2026-06-23T00:00:01Z"
    assert outbox.pending() == []
    outbox.close()

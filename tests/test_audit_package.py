from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
from threading import Barrier
from types import SimpleNamespace

import graphblocks
import pytest
from graphblocks.approval import VALID_APPROVAL_STATUSES


ROOT = Path(__file__).parents[1]


def test_tool_effect_audit_record_validates_identity_and_deep_freezes_payload() -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    record = graphblocks_audit.ToolEffectAuditRecord(
        event_id="event-1",
        target_kind="tool_effect",
        occurred_at="2026-06-23T00:00:00Z",
        actor=graphblocks_audit.PrincipalRef("worker-1"),
        resource=graphblocks_audit.ResourceRef("tool:search", resource_kind="tool"),
        reason_codes=("tool_effect.applied",),
        payload={"items": ["initial"]},
    )

    with pytest.raises(TypeError):
        list.__setitem__(record.payload["items"], 0, "mutated")
    with pytest.raises(graphblocks_audit.ToolEffectAuditError, match="actor must be"):
        graphblocks_audit.ToolEffectAuditRecord(
            event_id="event-2",
            target_kind="tool_effect",
            occurred_at="2026-06-23T00:00:00Z",
            actor=object(),
            resource=graphblocks_audit.ResourceRef("tool:search"),
            reason_codes=("tool_effect.applied",),
        )
    with pytest.raises(
        graphblocks_audit.ToolEffectAuditError,
        match="reason_codes must not contain duplicates",
    ):
        graphblocks_audit.ToolEffectAuditRecord(
            event_id="event-3",
            target_kind="tool_effect",
            occurred_at="2026-06-23T00:00:00Z",
            actor=graphblocks_audit.PrincipalRef("worker-1"),
            resource=graphblocks_audit.ResourceRef("tool:search"),
            reason_codes=("tool_effect.applied", "tool_effect.applied"),
        )


def test_audit_outbox_rejects_attempt_counter_overflow() -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()
    outbox.append(
        "application_event",
        {"event_id": "event-1"},
        occurred_at="2026-06-23T00:00:00Z",
        record_id="audit-overflow",
    )
    outbox._connection.execute(
        """
        UPDATE audit_outbox_records
        SET status = 'failed', attempts = ?, last_error = 'still unavailable'
        WHERE record_id = 'audit-overflow'
        """,
        ((1 << 63) - 1,),
    )
    outbox._connection.commit()

    assert outbox.claim_pending(
        claim_id="claim-overflow",
        claimed_by="publisher-1",
        lease_duration_ms=60_000,
    ) == []


def test_audit_outbox_normalizes_unstable_payload_mappings() -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")

    class BrokenPayload(dict[str, object]):
        def items(self):
            raise RuntimeError("mapping changed during iteration")

    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()
    with pytest.raises(ValueError, match="must contain strict canonical JSON"):
        outbox.append(
            "application_event",
            BrokenPayload(event_id="event-1"),
            occurred_at="2026-06-23T00:00:00Z",
        )


def test_audit_package_exposes_append_only_event_and_enforcement_records(monkeypatch) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")

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
        evaluated_at="2026-06-23T00:00:01Z",
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
    assert "VALID_APPROVAL_STATUSES" not in graphblocks.__all__


def test_audit_package_exposes_native_audit_helpers(monkeypatch) -> None:
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
    graphblocks_audit = importlib.import_module("graphblocks.audit")

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
    graphblocks = importlib.import_module("graphblocks")
    graphblocks_audit = importlib.import_module("graphblocks.audit")

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
    graphblocks = importlib.import_module("graphblocks")
    graphblocks_audit = importlib.import_module("graphblocks.audit")

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


def test_audit_package_rejects_forged_tool_effect_precondition_digest(monkeypatch) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")

    with pytest.raises(
        graphblocks_audit.ToolEffectAuditError,
        match="digest does not match payload",
    ):
        graphblocks_audit.ToolEffectPrecondition(
            payload={"tool_call_id": "call-1"},
            digest="sha256:" + "0" * 64,
        )


def test_audit_package_rejects_precondition_before_admission(monkeypatch) -> None:
    graphblocks = importlib.import_module("graphblocks")
    graphblocks_audit = importlib.import_module("graphblocks.audit")

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
    graphblocks = importlib.import_module("graphblocks")
    graphblocks_audit = importlib.import_module("graphblocks.audit")

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
    graphblocks_audit = importlib.import_module("graphblocks.audit")
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

    claims = reopened.claim_pending(
        claim_id="claim-batch-1",
        claimed_by="publisher-1",
        lease_duration_ms=60_000,
        limit=2,
    )
    published = reopened.mark_published(
        claims[0],
        published_at="2026-06-23T00:00:02Z",
    )
    failed = reopened.mark_failed(
        claims[1],
        error="sink unavailable",
    )

    assert published.status == "published"
    assert published.published_at == "2026-06-23T00:00:02Z"
    assert failed.status == "failed"
    assert failed.attempts == second.attempts + 1
    assert failed.last_error == "sink unavailable"
    assert reopened.pending() == [failed]
    reopened.close()


def test_audit_outbox_claims_each_record_once_across_publishers(
    tmp_path,
) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    path = tmp_path / "audit.sqlite3"
    seed = graphblocks_audit.SQLiteAuditOutbox(path)
    seed.append(
        "application_event",
        {"event_id": "event-1"},
        occurred_at="2026-07-29T00:00:00Z",
        record_id="audit-1",
    )
    seed.close()
    barrier = Barrier(2)

    def claim(worker_id: str):
        outbox = graphblocks_audit.SQLiteAuditOutbox(
            path,
            clock_ms=lambda: 1_000,
        )
        barrier.wait()
        try:
            return outbox.claim_pending(
                claim_id=f"claim-{worker_id}",
                claimed_by=worker_id,
                lease_duration_ms=60_000,
            )
        finally:
            outbox.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(claim, "publisher-1")
        second = executor.submit(claim, "publisher-2")
        claims = (*first.result(), *second.result())

    assert len(claims) == 1
    assert claims[0].record.record_id == "audit-1"
    assert "AuditOutboxClaim" in graphblocks_audit.__all__
    publisher = graphblocks_audit.SQLiteAuditOutbox(
        path,
        clock_ms=lambda: 1_500,
    )
    published = publisher.mark_published(
        claims[0],
        published_at="2026-07-29T00:00:01Z",
    )
    assert published.status == "published"
    assert publisher.pending() == []
    publisher.close()


def test_audit_outbox_reclaims_expired_lease_and_fences_stale_claim(
    tmp_path,
) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    current_time_ms = [1_000]
    outbox = graphblocks_audit.SQLiteAuditOutbox(
        tmp_path / "audit.sqlite3",
        clock_ms=lambda: current_time_ms[0],
    )
    outbox.append(
        "application_event",
        {"event_id": "event-1"},
        occurred_at="2026-07-29T00:00:00Z",
        record_id="audit-1",
    )
    first = outbox.claim_pending(
        claim_id="claim-1",
        claimed_by="publisher-1",
        lease_duration_ms=100,
    )[0]
    assert outbox.claim_pending(
        claim_id="claim-too-early",
        claimed_by="publisher-2",
        lease_duration_ms=100,
    ) == []

    current_time_ms[0] = 1_100
    with pytest.raises(graphblocks_audit.AuditOutboxError, match="stale"):
        outbox.mark_published(
            first,
            published_at="2026-07-29T00:00:01Z",
        )

    second = outbox.claim_pending(
        claim_id="claim-2",
        claimed_by="publisher-2",
        lease_duration_ms=100,
    )[0]
    assert second.generation == first.generation + 1

    with pytest.raises(graphblocks_audit.AuditOutboxError, match="stale"):
        outbox.mark_published(
            first,
            published_at="2026-07-29T00:00:01Z",
        )

    current_time_ms[0] = 1_150
    published = outbox.mark_published(
        second,
        published_at="2026-07-29T00:00:02Z",
    )
    assert published.status == "published"
    with pytest.raises(graphblocks_audit.AuditOutboxError, match="already published"):
        outbox.mark_published(
            first,
            published_at="2026-07-29T00:00:02Z",
        )
    outbox.close()


def test_audit_outbox_renews_active_claim_before_completion(tmp_path) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    current_time_ms = [1_000]
    outbox = graphblocks_audit.SQLiteAuditOutbox(
        tmp_path / "audit.sqlite3",
        clock_ms=lambda: current_time_ms[0],
    )
    outbox.append(
        "application_event",
        {"event_id": "event-1"},
        occurred_at="2026-07-29T00:00:00Z",
        record_id="audit-1",
    )
    claim = outbox.claim_pending(
        claim_id="claim-1",
        claimed_by="publisher-1",
        lease_duration_ms=100,
    )[0]

    current_time_ms[0] = 999
    with pytest.raises(graphblocks_audit.AuditOutboxError, match="clock_ms moved"):
        outbox.mark_published(
            claim,
            published_at="2026-07-29T00:00:01Z",
        )

    current_time_ms[0] = 1_050
    renewed = outbox.renew_claim(
        claim,
        lease_duration_ms=100,
    )
    current_time_ms[0] = 1_100
    published = outbox.mark_published(
        renewed,
        published_at="2026-07-29T00:00:01Z",
    )

    assert renewed.generation == claim.generation
    assert renewed.claimed_at_ms == claim.claimed_at_ms
    assert renewed.lease_expires_at_ms == 1_150
    assert published.status == "published"
    outbox.close()


def test_audit_outbox_requires_claim_for_terminal_transition() -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()
    outbox.append(
        "application_event",
        {"event_id": "event-1"},
        occurred_at="2026-07-29T00:00:00Z",
        record_id="audit-1",
    )

    with pytest.raises(ValueError, match="requires an AuditOutboxClaim"):
        outbox.mark_published(  # type: ignore[arg-type]
            "audit-1",
            published_at="2026-07-29T00:00:01Z",
        )
    with pytest.raises(ValueError, match="requires an AuditOutboxClaim"):
        outbox.mark_failed(  # type: ignore[arg-type]
            "audit-1",
            error="sink unavailable",
        )

    assert outbox.get("audit-1").status == "pending"
    outbox.close()


def test_audit_outbox_failed_claim_can_be_retried_with_new_fence(
    tmp_path,
) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox(
        tmp_path / "audit.sqlite3"
    )
    outbox.append(
        "application_event",
        {"event_id": "event-1"},
        occurred_at="2026-07-29T00:00:00Z",
        record_id="audit-1",
    )
    first = outbox.claim_pending(
        claim_id="claim-1",
        claimed_by="publisher-1",
        lease_duration_ms=60_000,
    )[0]
    failed = outbox.mark_failed(
        first,
        error="sink unavailable",
    )
    second = outbox.claim_pending(
        claim_id="claim-2",
        claimed_by="publisher-2",
        lease_duration_ms=60_000,
    )[0]

    assert failed.status == "failed"
    assert failed.attempts == 1
    assert second.record == failed
    assert second.generation == first.generation + 1
    outbox.close()


def test_audit_outbox_migrates_existing_table_for_claims(tmp_path) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    path = tmp_path / "audit.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE audit_outbox_records (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT,
          record_id TEXT NOT NULL UNIQUE,
          record_type TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          payload_digest TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          status TEXT NOT NULL,
          attempts INTEGER NOT NULL,
          published_at TEXT,
          last_error TEXT
        )
        """
    )
    connection.commit()
    connection.close()

    outbox = graphblocks_audit.SQLiteAuditOutbox(path)
    outbox.append(
        "application_event",
        {"event_id": "event-1"},
        occurred_at="2026-07-29T00:00:00Z",
        record_id="audit-1",
    )
    claim = outbox.claim_pending(
        claim_id="claim-1",
        claimed_by="publisher-1",
        lease_duration_ms=60_000,
    )[0]

    assert claim.record.record_id == "audit-1"
    assert claim.generation == 1
    outbox.close()


def test_audit_outbox_serializes_concurrent_claim_schema_migrations(
    tmp_path,
) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    path = tmp_path / "audit.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE audit_outbox_records (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT,
          record_id TEXT NOT NULL UNIQUE,
          record_type TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          payload_digest TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          status TEXT NOT NULL,
          attempts INTEGER NOT NULL,
          published_at TEXT,
          last_error TEXT
        )
        """
    )
    connection.commit()
    connection.close()
    barrier = Barrier(2)

    def migrate() -> set[str]:
        barrier.wait()
        outbox = graphblocks_audit.SQLiteAuditOutbox(path)
        try:
            return {
                row["name"]
                for row in outbox._connection.execute(
                    "PRAGMA table_info(audit_outbox_records)"
                )
            }
        finally:
            outbox.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(migrate)
        second = executor.submit(migrate)
        column_sets = (first.result(), second.result())

    expected_claim_columns = {
        "claim_id",
        "claimed_by",
        "claim_generation",
        "claimed_at_ms",
        "claim_expires_at_ms",
    }
    assert all(expected_claim_columns <= columns for columns in column_sets)


def test_audit_outbox_serializes_single_instance_across_threads() -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()

    def append_record(index: int) -> str:
        return outbox.append(
            "application_event",
            {"event_id": f"event-{index}"},
            occurred_at="2026-07-29T00:00:00Z",
            record_id=f"audit-{index}",
        ).record_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        record_ids = tuple(executor.map(append_record, range(32)))

    assert record_ids == tuple(f"audit-{index}" for index in range(32))
    pending_ids = [record.record_id for record in outbox.pending()]
    assert len(pending_ids) == len(record_ids)
    assert set(pending_ids) == set(record_ids)
    outbox.close()


def test_audit_outbox_reports_cross_thread_use_after_close() -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()
    outbox.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(outbox.pending)
        with pytest.raises(
            graphblocks_audit.AuditOutboxError,
            match="audit outbox is closed",
        ):
            future.result()

    outbox.close()


@pytest.mark.parametrize("limit", (True, -1, 1.5))
def test_audit_outbox_rejects_invalid_pending_limits(monkeypatch, limit: object) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()

    with pytest.raises(
        ValueError,
        match="audit outbox pending limit must be a non-negative integer",
    ):
        outbox.pending(limit=limit)  # type: ignore[arg-type]
    assert outbox.pending(limit=0) == []
    outbox.close()


def test_audit_outbox_records_deep_freeze_payload_evidence(monkeypatch) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()
    payload = {"context": {"tenant": "acme", "labels": ["audit"]}}
    record = outbox.append(
        "application_event",
        payload,
        occurred_at="2026-06-23T00:00:00Z",
        record_id="audit-1",
    )
    payload["context"]["tenant"] = "mutated"
    payload["context"]["labels"].append("mutated")

    assert record.payload == {"context": {"tenant": "acme", "labels": ["audit"]}}
    try:
        record.payload["context"]["tenant"] = "mutated"
    except TypeError:
        pass
    else:
        raise AssertionError("nested audit payload mappings must be immutable")
    try:
        record.payload["context"]["labels"].append("mutated")
    except TypeError:
        pass
    else:
        raise AssertionError("nested audit payload lists must be immutable")
    outbox.close()


def test_audit_outbox_rejects_invalid_persisted_lifecycle_state(monkeypatch, tmp_path) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox(tmp_path / "audit.sqlite3")
    outbox.append(
        "application_event",
        {"event_id": "event-1"},
        occurred_at="2026-06-23T00:00:00Z",
        record_id="audit-1",
    )
    outbox._connection.execute(  # noqa: SLF001
        "UPDATE audit_outbox_records SET status = ?, attempts = ? WHERE record_id = ?",
        ("forged", -1, "audit-1"),
    )
    outbox._connection.commit()  # noqa: SLF001

    try:
        outbox.get("audit-1")
    except ValueError as error:
        assert "audit outbox status must be pending, published, or failed" in str(error)
    else:
        raise AssertionError("audit outbox replay must reject invalid lifecycle state")
    finally:
        outbox.close()


def test_audit_outbox_rejects_non_standard_payload_json_on_replay(monkeypatch, tmp_path) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
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


@pytest.mark.parametrize(
    "payload_json",
    (
        '{"value":1,"value":2}',
        '{"value": 1}',
    ),
)
def test_audit_outbox_rejects_duplicate_or_noncanonical_payload_json_on_replay(
    monkeypatch,
    tmp_path,
    payload_json: str,
) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox(tmp_path / "audit.sqlite3")
    outbox.append(
        "application_event",
        {"value": 1},
        occurred_at="2026-06-23T00:00:00Z",
        record_id="audit-1",
    )
    outbox._connection.execute(  # noqa: SLF001
        "UPDATE audit_outbox_records SET payload_json = ? WHERE record_id = ?",
        (payload_json, "audit-1"),
    )
    outbox._connection.commit()  # noqa: SLF001

    with pytest.raises(ValueError, match="payload_json must be valid strict JSON"):
        outbox.get("audit-1")
    outbox.close()


def test_audit_outbox_rejects_non_string_and_cyclic_payload_values(monkeypatch) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    with pytest.raises(ValueError, match="object keys must be strings"):
        outbox.append(
            "application_event",
            {1: "event-1"},  # type: ignore[dict-item]
            occurred_at="2026-06-23T00:00:00Z",
        )
    with pytest.raises(ValueError, match="cyclic"):
        outbox.append(
            "application_event",
            cyclic,
            occurred_at="2026-06-23T00:00:00Z",
        )
    outbox.close()


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"attempts": 1.5}, "attempts must be a non-negative integer"),
        ({"attempts": 1}, "pending audit outbox record must have zero attempts"),
        (
            {"status": "failed", "attempts": 1},
            "failed audit outbox record requires last_error",
        ),
        (
            {"status": "published", "published_at": "2026-06-23T00:00:01Z", "last_error": "stale"},
            "published audit outbox record must not define last_error",
        ),
    ),
)
def test_audit_outbox_rejects_inconsistent_persisted_lifecycle_fields_without_coercion(
    monkeypatch,
    tmp_path,
    updates: dict[str, object],
    message: str,
) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox(tmp_path / "audit.sqlite3")
    outbox.append(
        "application_event",
        {"event_id": "event-1"},
        occurred_at="2026-06-23T00:00:00Z",
        record_id="audit-1",
    )
    assignments = ", ".join(f"{field} = ?" for field in updates)
    outbox._connection.execute(  # noqa: SLF001
        f"UPDATE audit_outbox_records SET {assignments} WHERE record_id = ?",
        (*updates.values(), "audit-1"),
    )
    outbox._connection.commit()  # noqa: SLF001

    with pytest.raises(ValueError, match=message):
        outbox.get("audit-1")
    outbox.close()


def test_audit_outbox_rejects_payload_digest_drift_on_replay(monkeypatch, tmp_path) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
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
    graphblocks_audit = importlib.import_module("graphblocks.audit")
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


def test_audit_outbox_treats_identical_append_as_idempotent(monkeypatch) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()
    payload = {"event_id": "event-1", "kind": "OutputPolicyAllowed"}

    first = outbox.append(
        "application_event",
        payload,
        occurred_at="2026-06-23T00:00:00Z",
    )
    replayed = outbox.append(
        "application_event",
        payload,
        occurred_at="2026-06-23T00:00:00Z",
    )

    assert replayed == first
    assert outbox.pending() == [first]
    outbox.close()


def test_audit_outbox_compares_replays_by_canonical_payload_identity(monkeypatch) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()
    payload = {"items": ("a",), "score": Decimal("0.1")}

    first = outbox.append(
        "application_event",
        payload,
        occurred_at="2026-06-23T00:00:00Z",
    )
    replayed = outbox.append(
        "application_event",
        payload,
        occurred_at="2026-06-23T00:00:00Z",
    )

    assert replayed == first
    assert outbox.pending() == [first]
    outbox.close()


def test_audit_outbox_treats_published_records_as_terminal(monkeypatch) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()
    outbox.append("application_event", {"event_id": "event-1"}, occurred_at="2026-06-23T00:00:00Z", record_id="audit-1")
    claim = outbox.claim_pending(
        claim_id="claim-1",
        claimed_by="publisher-1",
        lease_duration_ms=60_000,
    )[0]
    published = outbox.mark_published(
        claim,
        published_at="2026-06-23T00:00:01Z",
    )

    try:
        outbox.mark_failed(
            claim,
            error="sink unavailable",
        )
    except graphblocks_audit.AuditOutboxError as error:
        assert "already published" in str(error)
    else:
        raise AssertionError("published audit records should not be marked failed")

    assert outbox.mark_published(
        claim,
        published_at="2026-06-23T00:00:01Z",
    ) == published
    try:
        outbox.mark_published(
            claim,
            published_at="2026-06-23T00:00:02Z",
        )
    except graphblocks_audit.AuditOutboxError as error:
        assert "already published" in str(error)
    else:
        raise AssertionError("published audit records should not change terminal timestamp")

    assert outbox.get("audit-1").status == "published"
    assert outbox.get("audit-1").published_at == "2026-06-23T00:00:01Z"
    assert outbox.pending() == []
    outbox.close()


def test_audit_outbox_rejects_invalid_transition_details_without_mutating(monkeypatch) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    outbox = graphblocks_audit.SQLiteAuditOutbox.in_memory()
    outbox.append(
        "application_event",
        {"event_id": "event-1"},
        occurred_at="2026-06-23T00:00:00Z",
        record_id="audit-1",
    )
    claim = outbox.claim_pending(
        claim_id="claim-1",
        claimed_by="publisher-1",
        lease_duration_ms=60_000,
    )[0]

    for transition in (
        lambda: outbox.mark_published(
            claim,
            published_at=" ",
        ),
        lambda: outbox.mark_failed(
            claim,
            error=" ",
        ),
    ):
        try:
            transition()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid audit transition details must be rejected")
        assert outbox.get("audit-1").status == "pending"

    outbox.close()

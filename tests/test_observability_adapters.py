from __future__ import annotations

from decimal import Decimal
import importlib
import json
import sys
from types import SimpleNamespace

import pytest

from graphblocks.output_policy import (
    VALID_DRAFT_DISPOSITIONS,
    VALID_OUTPUT_DISPOSITIONS,
    VALID_OUTPUT_DURABLE_RESULTS,
    VALID_PENDING_TOOL_CALLS_DISPOSITIONS,
    VALID_TERMINAL_REASONS,
)
from graphblocks.policy import VALID_ENFORCEMENT_POINTS
from graphblocks.tools import (
    VALID_TOOL_CALL_STATUSES,
    VALID_TOOL_EFFECT_OUTCOMES,
    VALID_TOOL_EFFECTS,
    VALID_TOOL_RESULT_MODES,
    VALID_TOOL_RESULT_STATUSES,
)


def test_telemetry_package_exposes_native_content_capture(monkeypatch) -> None:
    calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def capture_telemetry_content(
        decision: dict[str, object],
        content: dict[str, object],
    ) -> dict[str, object]:
        calls.append((decision, content))
        return {"mode": decision["mode"], "preview": "safe [redacted] suffix"}

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(capture_telemetry_content=capture_telemetry_content),
    )
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")

    captured = graphblocks_telemetry.capture_native_telemetry_content(
        {"mode": "redacted_preview", "retentionPolicy": "debug-7d"},
        {
            "contentKind": "tool_result",
            "text": "safe secret suffix",
            "redactions": [{"pattern": "secret", "replacement": "[redacted]"}],
        },
    )

    assert captured == {"mode": "redacted_preview", "preview": "safe [redacted] suffix"}
    assert calls == [
        (
            {"mode": "redacted_preview", "retentionPolicy": "debug-7d"},
            {
                "contentKind": "tool_result",
                "text": "safe secret suffix",
                "redactions": [{"pattern": "secret", "replacement": "[redacted]"}],
            },
        )
    ]
    assert "capture_native_telemetry_content" in graphblocks_telemetry.__all__


def test_telemetry_observation_contract_detaches_mutable_inputs(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    usage = {"input_tokens": 20, "output_tokens": 8}
    attributes = {"tenant": "tenant-1"}

    observation = graphblocks_telemetry.GenerationTelemetryRecord(
        record_id="gen-1",
        run_id="run-1",
        span_id="span-1",
        node_id="agent",
        provider="openai-compatible",
        model="gpt-test",
        release_id="release-1",
        input_digest="sha256:input",
        output_digest="sha256:output",
        usage=usage,
        timing_ms={"queue_wait": 4, "execution": 128},
        attributes=attributes,
    )
    usage["input_tokens"] = 999
    attributes["tenant"] = "mutated"

    assert observation.observation_contract() == {
        "record_id": "gen-1",
        "run_id": "run-1",
        "span_id": "span-1",
        "node_id": "agent",
        "provider": "openai-compatible",
        "model": "gpt-test",
        "release_id": "release-1",
        "input_digest": "sha256:input",
        "output_digest": "sha256:output",
        "usage": {"input_tokens": 20, "output_tokens": 8},
        "timing_ms": {"execution": 128, "queue_wait": 4},
        "attributes": {"tenant": "tenant-1"},
    }


def test_telemetry_policy_and_tool_records_apply_capture_policy(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    output_record = graphblocks_telemetry.OutputPolicyTelemetryRecord(
        record_id="policy-1",
        run_id="run-1",
        stream_id="stream-1",
        response_id="response-1",
        enforcement_point="before_client_delivery",
        disposition="abort_response",
        release_id="release-1",
        policy_snapshot_id="policy-snapshot-1",
        terminal_reason="policy_denied",
        draft_disposition="retract",
        pending_tool_calls="deny",
        durable_result="none",
        accepted_through_sequence=7,
        last_client_delivered_sequence=5,
        attributes={"tenant": "tenant-1", "prompt": "secret prompt", "debug": "drop me"},
    )
    tool_record = graphblocks_telemetry.ToolExecutionTelemetryRecord(
        record_id="tool-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name="ticket.create",
        status="completed",
        release_id="release-1",
        result_mode="value",
        effect_outcome="committed",
        effects=("network", "external_write"),
        duration_ms=128,
        attributes={"tenant": "tenant-1", "tool_result": "secret result", "debug": "drop me"},
    )
    policy = graphblocks_telemetry.TelemetryCapturePolicy(
        redacted_attribute_keys=("prompt", "tool_result"),
        dropped_attribute_keys=("debug",),
    )

    redacted_output = policy.apply_output_policy(output_record)
    redacted_tool = policy.apply_tool_execution(tool_record)

    assert output_record.observation_contract() == {
        "record_id": "policy-1",
        "run_id": "run-1",
        "stream_id": "stream-1",
        "response_id": "response-1",
        "enforcement_point": "before_client_delivery",
        "disposition": "abort_response",
        "release_id": "release-1",
        "policy_snapshot_id": "policy-snapshot-1",
        "terminal_reason": "policy_denied",
        "draft_disposition": "retract",
        "pending_tool_calls": "deny",
        "durable_result": "none",
        "accepted_through_sequence": 7,
        "last_client_delivered_sequence": 5,
        "attributes": {"debug": "drop me", "prompt": "secret prompt", "tenant": "tenant-1"},
    }
    assert tool_record.observation_contract() == {
        "record_id": "tool-1",
        "run_id": "run-1",
        "tool_call_id": "call-1",
        "tool_name": "ticket.create",
        "status": "completed",
        "release_id": "release-1",
        "result_mode": "value",
        "effect_outcome": "committed",
        "effects": ["external_write", "network"],
        "duration_ms": 128,
        "attributes": {"debug": "drop me", "tenant": "tenant-1", "tool_result": "secret result"},
    }
    assert redacted_output.observation_contract()["attributes"] == {
        "prompt": "[redacted]",
        "tenant": "tenant-1",
    }
    assert redacted_tool.observation_contract()["attributes"] == {
        "tenant": "tenant-1",
        "tool_result": "[redacted]",
    }
    assert "OutputPolicyTelemetryRecord" in graphblocks_telemetry.__all__
    assert "ToolExecutionTelemetryRecord" in graphblocks_telemetry.__all__


def test_telemetry_package_exposes_canonical_literal_sets(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    expected_constants = {
        "VALID_DRAFT_DISPOSITIONS": VALID_DRAFT_DISPOSITIONS,
        "VALID_ENFORCEMENT_POINTS": VALID_ENFORCEMENT_POINTS,
        "VALID_OUTPUT_DISPOSITIONS": VALID_OUTPUT_DISPOSITIONS,
        "VALID_OUTPUT_DURABLE_RESULTS": VALID_OUTPUT_DURABLE_RESULTS,
        "VALID_PENDING_TOOL_CALLS_DISPOSITIONS": VALID_PENDING_TOOL_CALLS_DISPOSITIONS,
        "VALID_TERMINAL_REASONS": VALID_TERMINAL_REASONS,
        "VALID_TOOL_CALL_STATUSES": VALID_TOOL_CALL_STATUSES,
        "VALID_TOOL_EFFECT_OUTCOMES": VALID_TOOL_EFFECT_OUTCOMES,
        "VALID_TOOL_EFFECTS": VALID_TOOL_EFFECTS,
        "VALID_TOOL_RESULT_MODES": VALID_TOOL_RESULT_MODES,
        "VALID_TOOL_RESULT_STATUSES": VALID_TOOL_RESULT_STATUSES,
    }

    for name, value in expected_constants.items():
        assert getattr(graphblocks_telemetry, name) is value
        assert name in graphblocks_telemetry.__all__


def test_telemetry_records_validate_policy_and_tool_literal_fields(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")

    with pytest.raises(graphblocks_telemetry.TelemetryProjectionError, match="enforcement_point"):
        graphblocks_telemetry.OutputPolicyTelemetryRecord(
            record_id="policy-invalid",
            run_id="run-1",
            stream_id="stream-1",
            response_id="response-1",
            enforcement_point="after_delivery",
            disposition="allow",
        )
    with pytest.raises(graphblocks_telemetry.TelemetryProjectionError, match="disposition"):
        graphblocks_telemetry.OutputPolicyTelemetryRecord(
            record_id="policy-invalid",
            run_id="run-1",
            stream_id="stream-1",
            response_id="response-1",
            enforcement_point="before_client_delivery",
            disposition="permit",
        )
    with pytest.raises(graphblocks_telemetry.TelemetryProjectionError, match="accepted_through_sequence"):
        graphblocks_telemetry.OutputPolicyTelemetryRecord(
            record_id="policy-invalid",
            run_id="run-1",
            stream_id="stream-1",
            response_id="response-1",
            enforcement_point="before_client_delivery",
            disposition="allow",
            accepted_through_sequence=-1,
        )

    running = graphblocks_telemetry.ToolExecutionTelemetryRecord(
        record_id="tool-running",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name="knowledge.search",
        status="running",
    )
    assert running.status == "running"
    with pytest.raises(graphblocks_telemetry.TelemetryProjectionError, match="status"):
        graphblocks_telemetry.ToolExecutionTelemetryRecord(
            record_id="tool-invalid",
            run_id="run-1",
            tool_call_id="call-1",
            tool_name="knowledge.search",
            status="waiting",
        )
    with pytest.raises(graphblocks_telemetry.TelemetryProjectionError, match="result_mode"):
        graphblocks_telemetry.ToolExecutionTelemetryRecord(
            record_id="tool-invalid",
            run_id="run-1",
            tool_call_id="call-1",
            tool_name="knowledge.search",
            status="completed",
            result_mode="stream",
        )
    with pytest.raises(graphblocks_telemetry.TelemetryProjectionError, match="effects"):
        graphblocks_telemetry.ToolExecutionTelemetryRecord(
            record_id="tool-invalid",
            run_id="run-1",
            tool_call_id="call-1",
            tool_name="knowledge.search",
            status="completed",
            effects=("network", "telepathy"),
        )


def test_telemetry_capture_policy_redacts_sensitive_observation_fields(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    observation = graphblocks_telemetry.GenerationTelemetryRecord(
        record_id="gen-1",
        run_id="run-1",
        span_id="span-1",
        node_id="agent",
        provider="openai-compatible",
        model="gpt-test",
        input_digest="sha256:input",
        output_digest="sha256:output",
        attributes={
            "tenant": "tenant-1",
            "prompt": "secret prompt",
            "api_key": "sk-test",
            "debug": "drop me",
        },
    )
    policy = graphblocks_telemetry.TelemetryCapturePolicy(
        redacted_attribute_keys=("api_key", "prompt"),
        dropped_attribute_keys=("debug",),
        capture_input_digest=False,
        capture_output_digest=True,
    )

    redacted = policy.apply_generation(observation)

    assert redacted.observation_contract()["input_digest"] is None
    assert redacted.observation_contract()["output_digest"] == "sha256:output"
    assert redacted.observation_contract()["attributes"] == {
        "api_key": "[redacted]",
        "prompt": "[redacted]",
        "tenant": "tenant-1",
    }
    assert observation.attributes["prompt"] == "secret prompt"
    assert "TelemetryCapturePolicy" in graphblocks_telemetry.__all__


def test_default_telemetry_capture_redacts_normalized_sensitive_keys(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    graphblocks_langfuse = importlib.import_module("graphblocks.integrations.langfuse")
    graphblocks_otel = importlib.import_module("graphblocks.integrations.otel")
    observation = graphblocks_telemetry.GenerationTelemetryRecord(
        record_id="gen-sensitive",
        run_id="run-1",
        span_id="span-1",
        node_id="agent",
        provider="openai-compatible",
        model="gpt-test",
        attributes={
            "Authorization": "Bearer secret",
            "apiKey": "sk-api",
            "openai_api_key": "sk-openai",
            "access_token": "access-secret",
            "bearer_token": "bearer-secret",
            "client_secret": "client-secret",
            "id_token": "id-secret",
            "input_tokens": 23,
            "oauth-token": "oauth-secret",
            "output_tokens": 7,
            "refreshToken": "refresh-secret",
            "session_token": "session-secret",
            "token_count": 30,
            "tenant": "tenant-1",
        },
    )

    otel_redacted = graphblocks_otel.DEFAULT_OTLP_CAPTURE_POLICY.apply_generation(observation)
    langfuse_redacted = graphblocks_langfuse.DEFAULT_LANGFUSE_CAPTURE_POLICY.apply_generation(
        observation
    )

    expected_attributes = {
        "Authorization": "[redacted]",
        "access_token": "[redacted]",
        "apiKey": "[redacted]",
        "bearer_token": "[redacted]",
        "client_secret": "[redacted]",
        "id_token": "[redacted]",
        "input_tokens": 23,
        "oauth-token": "[redacted]",
        "openai_api_key": "[redacted]",
        "output_tokens": 7,
        "refreshToken": "[redacted]",
        "session_token": "[redacted]",
        "tenant": "tenant-1",
        "token_count": 30,
    }
    assert otel_redacted.attributes == expected_attributes
    assert langfuse_redacted.attributes == expected_attributes


def test_telemetry_capture_policy_linter_flags_unprotected_secret_and_content_keys(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    linter = graphblocks_telemetry.TelemetryCapturePolicyLinter(
        sensitive_attribute_keys=("api_key", "authorization"),
        content_attribute_keys=("messages", "prompt"),
    )
    policy = graphblocks_telemetry.TelemetryCapturePolicy(
        redacted_attribute_keys=("api_key",),
        replacement=" ",
    )

    result = linter.lint_policy(policy)

    assert not result.passed
    assert result.issue_contracts() == [
        {
            "attribute_key": "api_key",
            "reason": "redaction_replacement_empty",
            "required_action": "set_non_empty_replacement",
        },
        {
            "attribute_key": "authorization",
            "reason": "sensitive_attribute_not_protected",
            "required_action": "redact_or_drop",
        },
        {
            "attribute_key": "messages",
            "reason": "content_attribute_not_protected",
            "required_action": "redact_or_drop",
        },
        {
            "attribute_key": "prompt",
            "reason": "content_attribute_not_protected",
            "required_action": "redact_or_drop",
        },
    ]


def test_telemetry_capture_policy_linter_accepts_protected_capture_policy(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    linter = graphblocks_telemetry.TelemetryCapturePolicyLinter(
        sensitive_attribute_keys=("api_key", "authorization"),
        content_attribute_keys=("messages", "prompt"),
    )
    policy = graphblocks_telemetry.TelemetryCapturePolicy(
        redacted_attribute_keys=("api_key", "authorization", "prompt"),
        dropped_attribute_keys=("messages",),
        replacement="[redacted]",
    )

    result = linter.lint_policy(policy)

    assert result.passed
    assert result.issue_contracts() == []
    assert "TelemetryCapturePolicyLinter" in graphblocks_telemetry.__all__


def test_telemetry_export_failure_is_non_fatal_to_run(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")

    result = graphblocks_telemetry.TelemetryExportResult.failed(
        exporter="otlp",
        record_ids=("gen-1",),
        error_type="TimeoutError",
        retryable=True,
    )

    assert result.result_contract() == {
        "exporter": "otlp",
        "status": "failed",
        "record_ids": ["gen-1"],
        "error_type": "TimeoutError",
        "retryable": True,
        "run_impact": "none",
    }


def test_telemetry_export_outage_preserves_durable_records_and_recovers_once(
    monkeypatch,
    tmp_path,
) -> None:
    graphblocks_audit = importlib.import_module("graphblocks.audit")
    graphblocks_langfuse = importlib.import_module("graphblocks.integrations.langfuse")
    graphblocks_otel = importlib.import_module("graphblocks.integrations.otel")
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    from graphblocks.budget import SQLiteBudgetLedger, UsageAmount
    from graphblocks.policy import ResourceRef
    from graphblocks.runtime import SQLiteExecutionJournal
    from graphblocks.usage import SQLiteUsageLedger, UsageRecord

    journal = SQLiteExecutionJournal(tmp_path / "journal.sqlite3", "run-1")
    journal.append("run_started", {"graphHash": "sha256:graph"})
    journal.append("node_succeeded", {"node": "agent", "outputDigest": "sha256:output"})
    journal.append_terminal("run_succeeded", {"outputDigest": "sha256:output"})
    audit = graphblocks_audit.SQLiteAuditOutbox(tmp_path / "audit.sqlite3")
    audit.append(
        "application_event",
        {"event_id": "event-1", "kind": "RunSucceeded", "run_id": "run-1"},
        occurred_at="2026-07-10T00:00:00Z",
        record_id="audit-1",
    )
    usage = SQLiteUsageLedger(tmp_path / "usage.sqlite3")
    usage.append(
        UsageRecord(
            record_id="usage-1",
            source="provider_reported",
            confidence="provider_exact",
            amounts=(UsageAmount("model_total_tokens", Decimal("28"), "tokens"),),
            occurred_at="2026-07-10T00:00:00Z",
            run_id="run-1",
            attempt_id="attempt-1",
            provider_response_id="response-1",
        )
    )
    budget = SQLiteBudgetLedger(tmp_path / "budget.sqlite3")
    budget.allocate(
        "budget-1",
        ResourceRef("tenant:acme", resource_kind="tenant"),
        [UsageAmount("model_total_tokens", Decimal("100"), "tokens")],
        policy_ref="policy-1",
    )
    reservation = budget.reserve(
        "budget-1",
        ResourceRef("run-1", resource_kind="run"),
        [UsageAmount("model_total_tokens", Decimal("40"), "tokens")],
        purpose="provider_call",
        expires_at="2026-07-10T01:00:00Z",
        reservation_id="reservation-1",
    )
    budget.commit(
        reservation.reservation_id,
        [UsageAmount("model_total_tokens", Decimal("28"), "tokens")],
    )
    record = graphblocks_telemetry.GenerationTelemetryRecord(
        record_id="gen-1",
        run_id="run-1",
        span_id="span-1",
        node_id="agent",
        provider="openai-compatible",
        model="gpt-test",
        release_id="release-1",
        input_digest="sha256:input",
        output_digest="sha256:output",
        usage={"input_tokens": 20, "output_tokens": 8},
        attributes={"environment": "production"},
    )
    outbox = graphblocks_telemetry.TelemetryExportOutbox()
    assert outbox.accept((record,)) == ("gen-1",)
    assert outbox.accept((record,)) == ("gen-1",)
    otlp_attempts: list[list[dict[str, object]]] = []
    langfuse_exports: list[dict[str, object]] = []

    def durable_state() -> object:
        budget_balance = budget.balance("budget-1")
        return graphblocks_telemetry.TelemetryCorrectnessSnapshot.capture(
            execution_journal=[entry.to_dict() for entry in journal.records],
            audit_log=[
                {
                    "record_id": entry.record_id,
                    "payload_digest": entry.payload_digest,
                    "status": entry.status,
                }
                for entry in audit.pending()
            ],
            usage_ledger=[
                {
                    "record_id": entry.record_id,
                    "amounts": [
                        {
                            "kind": amount.kind,
                            "amount": str(amount.amount),
                            "unit": amount.unit,
                        }
                        for amount in entry.amounts
                    ],
                }
                for entry in usage.records_for_run("run-1")
            ],
            budget_ledger={
                "budget_id": budget_balance.budget_id,
                "revision": budget_balance.revision,
                "committed": [
                    {
                        "kind": amount.kind,
                        "amount": str(amount.amount),
                        "unit": amount.unit,
                    }
                    for amount in budget_balance.committed
                ],
            },
        )

    def export_otlp(records) -> None:
        projections = [
            graphblocks_otel.otlp_span_from_generation(
                entry,
                schema_url="https://opentelemetry.io/schemas/1.27.0",
            ).span_contract()
            for entry in records
        ]
        otlp_attempts.append(projections)
        if len(otlp_attempts) == 1:
            raise TimeoutError("collector unavailable")

    def export_langfuse(records) -> None:
        langfuse_exports.extend(
            graphblocks_langfuse.langfuse_generation_from_observation(
                entry,
                trace_id="trace-1",
            ).generation_contract()
            for entry in records
        )

    baseline = durable_state()
    failed = outbox.attempt_export(
        "otlp",
        export_otlp,
        correctness_probe=durable_state,
        retryable=True,
    )
    langfuse = outbox.attempt_export(
        "langfuse",
        export_langfuse,
        correctness_probe=durable_state,
    )
    recovered = outbox.attempt_export(
        "otlp",
        export_otlp,
        correctness_probe=durable_state,
        retryable=True,
    )
    redundant_retry = outbox.attempt_export(
        "otlp",
        export_otlp,
        correctness_probe=durable_state,
        retryable=True,
    )

    assert failed.result.result_contract() == {
        "exporter": "otlp",
        "status": "failed",
        "record_ids": ["gen-1"],
        "error_type": "TimeoutError",
        "retryable": True,
        "run_impact": "none",
    }
    assert failed.evaluation_contract() == {
        "exporter": "otlp",
        "attempt": 1,
        "result": failed.result.result_contract(),
        "correctness_preserved": True,
        "correctness_before_digest": baseline.digest,
        "correctness_after_digest": baseline.digest,
        "accepted_record_ids": ["gen-1"],
        "delivered_record_ids": [],
        "pending_record_ids": ["gen-1"],
    }
    assert failed.correctness_preserved
    assert failed.pending_record_ids == ("gen-1",)
    assert langfuse.result.status == "completed"
    assert recovered.result.status == "completed"
    assert recovered.attempt == 2
    assert recovered.result.record_ids == ("gen-1",)
    assert recovered.pending_record_ids == ()
    assert redundant_retry.attempt == 3
    assert redundant_retry.result.record_ids == ()
    assert len(otlp_attempts) == 2
    assert [projection["span_id"] for projection in otlp_attempts[1]] == ["span-1"]
    assert [projection["generation_id"] for projection in langfuse_exports] == ["span-1"]
    assert outbox.accepted_record_ids == ("gen-1",)
    assert durable_state() == baseline
    assert len(journal.records) == 3
    assert [entry.record_id for entry in audit.pending()] == ["audit-1"]
    assert [entry.record_id for entry in usage.records_for_run("run-1")] == ["usage-1"]
    assert [str(amount.amount) for amount in budget.balance("budget-1").committed] == ["28"]
    assert "TelemetryExportOutbox" in graphblocks_telemetry.__all__
    assert "TelemetryCorrectnessSnapshot" in graphblocks_telemetry.__all__

    journal.close()
    audit.close()
    usage.close()
    budget.close()


def test_telemetry_export_outbox_rejects_conflicts_and_fails_closed_on_state_drift(
    monkeypatch,
) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    original = graphblocks_telemetry.GenerationTelemetryRecord(
        record_id="gen-1",
        run_id="run-1",
        span_id="span-1",
        node_id="agent",
        provider="openai-compatible",
        model="gpt-test",
    )
    conflicting = graphblocks_telemetry.GenerationTelemetryRecord(
        record_id="gen-1",
        run_id="run-1",
        span_id="span-1",
        node_id="agent",
        provider="openai-compatible",
        model="gpt-mutated",
    )
    outbox = graphblocks_telemetry.TelemetryExportOutbox()
    outbox.accept((original,))

    with pytest.raises(graphblocks_telemetry.TelemetryExportConflictError, match="gen-1"):
        outbox.accept((conflicting,))

    journal_state = [{"sequence": 1, "kind": "run_succeeded"}]

    def mutate_authoritative_state(records) -> None:
        assert [record.record_id for record in records] == ["gen-1"]
        journal_state.append({"sequence": 2, "kind": "telemetry_side_effect"})
        raise TimeoutError("collector unavailable")

    with pytest.raises(
        graphblocks_telemetry.TelemetryCorrectnessViolation,
        match="authoritative durable state",
    ):
        outbox.attempt_export(
            "otlp",
            mutate_authoritative_state,
            correctness_probe=lambda: graphblocks_telemetry.TelemetryCorrectnessSnapshot.capture(
                execution_journal=journal_state,
                audit_log=[],
                usage_ledger=[],
                budget_ledger={},
            ),
            retryable=True,
        )

    assert outbox.pending_record_ids("otlp") == ("gen-1",)


def test_telemetry_diagnostic_bundle_combines_observability_health(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    capture_lint = graphblocks_telemetry.TelemetryCapturePolicyLinter(
        sensitive_attribute_keys=("api_key",),
        content_attribute_keys=("prompt",),
    ).lint_policy(graphblocks_telemetry.TelemetryCapturePolicy())
    cardinality_lint = graphblocks_telemetry.MetricCardinalityLinter(
        max_distinct_values_per_label=1,
    ).lint_samples(
        (
            {
                "name": "graphblocks_tool_executions_total",
                "labels": {"tool_name": "ticket.create", "run_id": "run-1"},
                "value": 1,
            },
            {
                "name": "graphblocks_tool_executions_total",
                "labels": {"tool_name": "ticket.update"},
                "value": 1,
            },
        )
    )
    exporter_failure = graphblocks_telemetry.TelemetryExportResult.failed(
        exporter="otlp",
        record_ids=("policy-1", "tool-1"),
        error_type="TimeoutError",
        retryable=True,
    )
    exporter_success = graphblocks_telemetry.TelemetryExportResult.completed(
        exporter="langfuse",
        record_ids=("gen-1",),
    )

    bundle = graphblocks_telemetry.telemetry_diagnostic_bundle(
        "observability-health",
        capture_policy_result=capture_lint,
        metric_cardinality_result=cardinality_lint,
        export_results=(exporter_success, exporter_failure),
    )

    assert bundle.ok is False
    assert bundle.summary() == {"error": 2, "warning": 3, "info": 0}
    assert bundle.bundle_contract() == {
        "bundle_id": "observability-health",
        "ok": False,
        "summary": {"error": 2, "warning": 3, "info": 0},
        "sections": [
            {
                "name": "capture_policy",
                "ok": False,
                "summary": {"error": 2, "warning": 0, "info": 0},
                "diagnostics": [
                    {
                        "code": "TelemetryCapturePolicy.content_attribute_not_protected",
                        "severity": "error",
                        "path": "$.capturePolicy.attributes.prompt",
                        "message": (
                            "Telemetry attribute 'prompt' failed capture-policy lint; "
                            "required action: redact_or_drop"
                        ),
                    },
                    {
                        "code": "TelemetryCapturePolicy.sensitive_attribute_not_protected",
                        "severity": "error",
                        "path": "$.capturePolicy.attributes.api_key",
                        "message": (
                            "Telemetry attribute 'api_key' failed capture-policy lint; "
                            "required action: redact_or_drop"
                        ),
                    },
                ],
            },
            {
                "name": "exporters",
                "ok": True,
                "summary": {"error": 0, "warning": 1, "info": 0},
                "diagnostics": [
                    {
                        "code": "TelemetryExport.failed",
                        "severity": "warning",
                        "path": "$.exporters.otlp",
                        "message": (
                            "Telemetry exporter 'otlp' reported status 'failed' for 2 record(s); "
                            "retryable: True; error_type: TimeoutError"
                        ),
                    }
                ],
            },
            {
                "name": "metric_cardinality",
                "ok": True,
                "summary": {"error": 0, "warning": 2, "info": 0},
                "diagnostics": [
                    {
                        "code": "TelemetryMetricCardinality.blocked_label",
                        "severity": "warning",
                        "path": "$.metrics.graphblocks_tool_executions_total.labels.run_id",
                        "message": (
                            "Telemetry metric 'graphblocks_tool_executions_total' label 'run_id' "
                            "observed 1 distinct value(s); limit: 0"
                        ),
                    },
                    {
                        "code": "TelemetryMetricCardinality.too_many_values",
                        "severity": "warning",
                        "path": "$.metrics.graphblocks_tool_executions_total.labels.tool_name",
                        "message": (
                            "Telemetry metric 'graphblocks_tool_executions_total' label 'tool_name' "
                            "observed 2 distinct value(s); limit: 1"
                        ),
                    },
                ],
            },
        ],
    }
    assert "TelemetryDiagnosticBundle" in graphblocks_telemetry.__all__
    assert "telemetry_diagnostic_bundle" in graphblocks_telemetry.__all__


def test_metric_cardinality_linter_flags_unbounded_labels(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    linter = graphblocks_telemetry.MetricCardinalityLinter(max_distinct_values_per_label=2)

    result = linter.lint_samples(
        (
            {
                "name": "graphblocks_generation_usage_tokens_total",
                "labels": {"provider": "openai-compatible", "model": "small", "run_id": "run-1"},
                "value": 1,
            },
            {
                "name": "graphblocks_generation_usage_tokens_total",
                "labels": {"provider": "openai-compatible", "model": "medium"},
                "value": 1,
            },
            {
                "name": "graphblocks_generation_usage_tokens_total",
                "labels": {"provider": "openai-compatible", "model": "large"},
                "value": 1,
            },
        )
    )

    assert not result.passed
    assert result.issue_contracts() == [
        {
            "metric_name": "graphblocks_generation_usage_tokens_total",
            "label": "model",
            "distinct_values": 3,
            "limit": 2,
            "reason": "too_many_values",
        },
        {
            "metric_name": "graphblocks_generation_usage_tokens_total",
            "label": "run_id",
            "distinct_values": 1,
            "limit": 0,
            "reason": "blocked_label",
        },
    ]


def test_metric_cardinality_linter_normalizes_blocked_label_spelling(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    linter = graphblocks_telemetry.MetricCardinalityLinter()

    result = linter.lint_samples(
        (
            {
                "name": "graphblocks_generation_total",
                "labels": {"runId": "run-1", "traceId": "trace-1"},
                "value": 1,
            },
        )
    )

    assert [(issue.label, issue.reason) for issue in result.issues] == [
        ("runId", "blocked_label"),
        ("traceId", "blocked_label"),
    ]


def test_otel_projection_uses_versioned_schema_without_importing_sdk(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    graphblocks_otel = importlib.import_module("graphblocks.integrations.otel")
    observation = graphblocks_telemetry.GenerationTelemetryRecord(
        record_id="gen-1",
        run_id="run-1",
        span_id="span-1",
        node_id="agent",
        provider="openai-compatible",
        model="gpt-test",
        release_id="release-1",
        usage={"input_tokens": 20, "output_tokens": 8},
    )

    span = graphblocks_otel.otlp_span_from_generation(
        observation,
        schema_url="https://opentelemetry.io/schemas/1.27.0",
    )

    assert span.span_contract() == {
        "schema_url": "https://opentelemetry.io/schemas/1.27.0",
        "name": "graphblocks.generation",
        "span_id": "span-1",
        "attributes": {
            "gen_ai.request.model": "gpt-test",
            "gen_ai.system": "openai-compatible",
            "graphblocks.node_id": "agent",
            "graphblocks.record_id": "gen-1",
            "graphblocks.release_id": "release-1",
            "graphblocks.run_id": "run-1",
        },
        "metrics": {
            "usage.input_tokens": 20,
            "usage.output_tokens": 8,
        },
    }


def test_otel_projection_contracts_reject_non_standard_json_constants(monkeypatch) -> None:
    graphblocks_otel = importlib.import_module("graphblocks.integrations.otel")

    span = graphblocks_otel.OtlpSpanProjection(span_json='{"metric": NaN}')
    template = graphblocks_otel.OtelCollectorTemplate(
        name="collector",
        config_json='{"receivers": Infinity}',
    )

    with pytest.raises(graphblocks_otel.OtelCollectorTemplateError, match="strict JSON"):
        span.span_contract()
    with pytest.raises(graphblocks_otel.OtelCollectorTemplateError, match="strict JSON"):
        template.config_contract()


def test_otel_span_projection_requires_schema_url(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    graphblocks_otel = importlib.import_module("graphblocks.integrations.otel")
    generation_record = graphblocks_telemetry.GenerationTelemetryRecord(
        record_id="gen-1",
        run_id="run-1",
        span_id="span-1",
        node_id="agent",
        provider="openai-compatible",
        model="gpt-test",
    )
    output_record = graphblocks_telemetry.OutputPolicyTelemetryRecord(
        record_id="policy-1",
        run_id="run-1",
        stream_id="stream-1",
        response_id="response-1",
        enforcement_point="before_client_delivery",
        disposition="allow",
    )
    tool_record = graphblocks_telemetry.ToolExecutionTelemetryRecord(
        record_id="tool-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name="knowledge.search",
        status="completed",
    )

    with pytest.raises(graphblocks_otel.OtelCollectorTemplateError, match="schema_url"):
        graphblocks_otel.otlp_span_from_generation(generation_record, schema_url=" ")
    with pytest.raises(graphblocks_otel.OtelCollectorTemplateError, match="schema_url"):
        graphblocks_otel.otlp_span_from_output_policy(output_record, schema_url="")
    with pytest.raises(graphblocks_otel.OtelCollectorTemplateError, match="schema_url"):
        graphblocks_otel.otlp_span_from_tool_execution(tool_record, schema_url=" ")


def test_otel_projection_applies_capture_policy_before_export(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    graphblocks_otel = importlib.import_module("graphblocks.integrations.otel")
    observation = graphblocks_telemetry.GenerationTelemetryRecord(
        record_id="gen-1",
        run_id="run-1",
        span_id="span-1",
        node_id="agent",
        provider="openai-compatible",
        model="gpt-test",
        attributes={
            "tenant": "tenant-1",
            "api_key": "sk-test",
            "prompt": "secret prompt",
        },
    )

    span = graphblocks_otel.otlp_span_from_generation(
        observation,
        schema_url="https://opentelemetry.io/schemas/1.27.0",
    )

    attributes = span.span_contract()["attributes"]
    assert attributes["graphblocks.attribute.api_key"] == "[redacted]"
    assert attributes["graphblocks.attribute.tenant"] == "tenant-1"
    assert "graphblocks.attribute.prompt" not in attributes
    assert "secret prompt" not in repr(span.span_contract())


def test_otel_projects_policy_and_tool_spans_with_capture_policy(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    graphblocks_otel = importlib.import_module("graphblocks.integrations.otel")
    output_record = graphblocks_telemetry.OutputPolicyTelemetryRecord(
        record_id="policy-1",
        run_id="run-1",
        stream_id="stream-1",
        response_id="response-1",
        enforcement_point="before_client_delivery",
        disposition="abort_response",
        release_id="release-1",
        policy_snapshot_id="policy-snapshot-1",
        terminal_reason="policy_denied",
        draft_disposition="retract",
        pending_tool_calls="deny",
        durable_result="none",
        accepted_through_sequence=7,
        last_client_delivered_sequence=5,
        attributes={"tenant": "tenant-1", "api_key": "sk-test", "prompt": "secret prompt"},
    )
    tool_record = graphblocks_telemetry.ToolExecutionTelemetryRecord(
        record_id="tool-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name="ticket.create",
        status="completed",
        release_id="release-1",
        result_mode="value",
        effect_outcome="committed",
        effects=("network", "external_write"),
        duration_ms=128,
        attributes={"tenant": "tenant-1", "token": "secret-token", "tool_result": "secret result"},
    )

    output_span = graphblocks_otel.otlp_span_from_output_policy(
        output_record,
        schema_url="https://opentelemetry.io/schemas/1.27.0",
    )
    tool_span = graphblocks_otel.otlp_span_from_tool_execution(
        tool_record,
        schema_url="https://opentelemetry.io/schemas/1.27.0",
    )

    assert output_span.span_contract() == {
        "schema_url": "https://opentelemetry.io/schemas/1.27.0",
        "name": "graphblocks.output_policy",
        "span_id": "policy-1",
        "attributes": {
            "graphblocks.attribute.api_key": "[redacted]",
            "graphblocks.attribute.tenant": "tenant-1",
            "graphblocks.disposition": "abort_response",
            "graphblocks.draft_disposition": "retract",
            "graphblocks.durable_result": "none",
            "graphblocks.enforcement_point": "before_client_delivery",
            "graphblocks.pending_tool_calls": "deny",
            "graphblocks.policy_snapshot_id": "policy-snapshot-1",
            "graphblocks.record_id": "policy-1",
            "graphblocks.release_id": "release-1",
            "graphblocks.response_id": "response-1",
            "graphblocks.run_id": "run-1",
            "graphblocks.stream_id": "stream-1",
            "graphblocks.terminal_reason": "policy_denied",
        },
        "metrics": {
            "accepted_through_sequence": 7,
            "last_client_delivered_sequence": 5,
        },
    }
    assert tool_span.span_contract() == {
        "schema_url": "https://opentelemetry.io/schemas/1.27.0",
        "name": "graphblocks.tool_execution",
        "span_id": "tool-1",
        "attributes": {
            "graphblocks.attribute.tenant": "tenant-1",
            "graphblocks.attribute.token": "[redacted]",
            "graphblocks.effect_outcome": "committed",
            "graphblocks.effects": ["external_write", "network"],
            "graphblocks.record_id": "tool-1",
            "graphblocks.release_id": "release-1",
            "graphblocks.result_mode": "value",
            "graphblocks.run_id": "run-1",
            "graphblocks.tool_call_id": "call-1",
            "graphblocks.tool_name": "ticket.create",
            "graphblocks.tool_status": "completed",
        },
        "metrics": {"duration_ms": 128},
    }
    assert "secret prompt" not in repr(output_span.span_contract())
    assert "secret result" not in repr(tool_span.span_contract())
    assert "otlp_span_from_output_policy" in graphblocks_otel.__all__
    assert "otlp_span_from_tool_execution" in graphblocks_otel.__all__


def test_otel_collector_template_renders_otlp_pipeline_without_sdk_import(monkeypatch) -> None:
    graphblocks_otel = importlib.import_module("graphblocks.integrations.otel")

    template = graphblocks_otel.otlp_collector_template(
        "collector.example:4317",
        name="support-agent-collector",
        pipelines=("traces", "metrics", "logs"),
        resource_attributes={
            "service.name": "graphblocks-support",
            "deployment.environment.name": "prod",
        },
        memory_limit_mib=256,
        batch_timeout="500ms",
    )

    assert template.template_contract()["name"] == "support-agent-collector"
    assert template.config_contract() == {
        "exporters": {
            "otlp/graphblocks": {
                "endpoint": "collector.example:4317",
                "tls": {"insecure": False},
            }
        },
        "processors": {
            "batch": {"timeout": "500ms"},
            "memory_limiter": {"check_interval": "1s", "limit_mib": 256},
            "resource/graphblocks": {
                "attributes": [
                    {"action": "upsert", "key": "deployment.environment.name", "value": "prod"},
                    {"action": "upsert", "key": "service.name", "value": "graphblocks-support"},
                ]
            },
        },
        "receivers": {
            "otlp": {
                "protocols": {
                    "grpc": {"endpoint": "127.0.0.1:4317"},
                    "http": {"endpoint": "127.0.0.1:4318"},
                }
            }
        },
        "service": {
            "pipelines": {
                "logs": {
                    "exporters": ["otlp/graphblocks"],
                    "processors": ["memory_limiter", "resource/graphblocks", "batch"],
                    "receivers": ["otlp"],
                },
                "metrics": {
                    "exporters": ["otlp/graphblocks"],
                    "processors": ["memory_limiter", "resource/graphblocks", "batch"],
                    "receivers": ["otlp"],
                },
                "traces": {
                    "exporters": ["otlp/graphblocks"],
                    "processors": ["memory_limiter", "resource/graphblocks", "batch"],
                    "receivers": ["otlp"],
                },
            }
        },
    }
    assert json.loads(template.render_json()) == template.config_contract()
    assert "OtelCollectorTemplate" in graphblocks_otel.__all__
    assert "otlp_collector_template" in graphblocks_otel.__all__


def test_otel_collector_template_rejects_invalid_pipeline(monkeypatch) -> None:
    graphblocks_otel = importlib.import_module("graphblocks.integrations.otel")

    with pytest.raises(graphblocks_otel.OtelCollectorTemplateError, match="unknown collector pipeline"):
        graphblocks_otel.otlp_collector_template("collector.example:4317", pipelines=("profiles",))


def test_langfuse_projection_uses_trace_generation_contract(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    graphblocks_langfuse = importlib.import_module("graphblocks.integrations.langfuse")
    observation = graphblocks_telemetry.GenerationTelemetryRecord(
        record_id="gen-1",
        run_id="run-1",
        span_id="span-1",
        node_id="agent",
        provider="openai-compatible",
        model="gpt-test",
        release_id="release-1",
        input_digest="sha256:input",
        output_digest="sha256:output",
        usage={"input_tokens": 20, "output_tokens": 8},
    )

    generation = graphblocks_langfuse.langfuse_generation_from_observation(
        observation,
        trace_id="trace-1",
    )

    assert generation.generation_contract() == {
        "trace_id": "trace-1",
        "generation_id": "span-1",
        "name": "agent",
        "model": "gpt-test",
        "provider": "openai-compatible",
        "metadata": {
            "input_digest": "sha256:input",
            "node_id": "agent",
            "output_digest": "sha256:output",
            "record_id": "gen-1",
            "release_id": "release-1",
            "run_id": "run-1",
        },
        "usage": {"input_tokens": 20, "output_tokens": 8},
    }


def test_langfuse_projection_contracts_reject_non_standard_json_constants(monkeypatch) -> None:
    graphblocks_langfuse = importlib.import_module("graphblocks.integrations.langfuse")

    projections = (
        graphblocks_langfuse.LangfuseGenerationProjection(generation_json='{"usage": NaN}'),
        graphblocks_langfuse.LangfusePromptProjection(prompt_json='{"metadata": Infinity}'),
        graphblocks_langfuse.LangfuseScoreProjection(score_json='{"value": -Infinity}'),
        graphblocks_langfuse.LangfuseDatasetItemProjection(dataset_item_json='{"input": NaN}'),
        graphblocks_langfuse.LangfuseEventProjection(event_json='{"metadata": Infinity}'),
    )
    contract_methods = (
        "generation_contract",
        "prompt_contract",
        "score_contract",
        "dataset_item_contract",
        "event_contract",
    )

    for projection, method_name in zip(projections, contract_methods, strict=True):
        with pytest.raises(ValueError, match="strict JSON"):
            getattr(projection, method_name)()


def test_langfuse_projection_applies_capture_policy_before_export(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    graphblocks_langfuse = importlib.import_module("graphblocks.integrations.langfuse")
    observation = graphblocks_telemetry.GenerationTelemetryRecord(
        record_id="gen-1",
        run_id="run-1",
        span_id="span-1",
        node_id="agent",
        provider="openai-compatible",
        model="gpt-test",
        input_digest="sha256:input",
        output_digest="sha256:output",
        attributes={
            "tenant": "tenant-1",
            "token": "secret-token",
            "messages": [{"role": "user", "content": "private"}],
        },
    )

    generation = graphblocks_langfuse.langfuse_generation_from_observation(observation)

    metadata = generation.generation_contract()["metadata"]
    assert metadata["attributes"] == {
        "tenant": "tenant-1",
        "token": "[redacted]",
    }
    assert "messages" not in metadata["attributes"]
    assert "private" not in repr(generation.generation_contract())


def test_langfuse_projects_policy_and_tool_events_with_capture_policy(monkeypatch) -> None:
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    graphblocks_langfuse = importlib.import_module("graphblocks.integrations.langfuse")
    output_record = graphblocks_telemetry.OutputPolicyTelemetryRecord(
        record_id="policy-1",
        run_id="run-1",
        stream_id="stream-1",
        response_id="response-1",
        enforcement_point="before_client_delivery",
        disposition="abort_response",
        release_id="release-1",
        policy_snapshot_id="policy-snapshot-1",
        terminal_reason="policy_denied",
        draft_disposition="retract",
        pending_tool_calls="deny",
        durable_result="none",
        accepted_through_sequence=7,
        last_client_delivered_sequence=5,
        attributes={"tenant": "tenant-1", "api_key": "sk-test", "prompt": "secret prompt"},
    )
    tool_record = graphblocks_telemetry.ToolExecutionTelemetryRecord(
        record_id="tool-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name="ticket.create",
        status="completed",
        release_id="release-1",
        result_mode="value",
        effect_outcome="committed",
        effects=("network", "external_write"),
        duration_ms=128,
        attributes={"tenant": "tenant-1", "token": "secret-token", "tool_result": "secret result"},
    )

    output_event = graphblocks_langfuse.langfuse_event_from_output_policy(output_record, trace_id="trace-1")
    tool_event = graphblocks_langfuse.langfuse_event_from_tool_execution(tool_record, trace_id="trace-1")

    assert output_event.event_contract() == {
        "trace_id": "trace-1",
        "event_id": "policy-1",
        "name": "graphblocks.output_policy",
        "metadata": {
            "accepted_through_sequence": 7,
            "attributes": {"api_key": "[redacted]", "tenant": "tenant-1"},
            "disposition": "abort_response",
            "draft_disposition": "retract",
            "durable_result": "none",
            "enforcement_point": "before_client_delivery",
            "last_client_delivered_sequence": 5,
            "pending_tool_calls": "deny",
            "policy_snapshot_id": "policy-snapshot-1",
            "record_id": "policy-1",
            "release_id": "release-1",
            "response_id": "response-1",
            "run_id": "run-1",
            "stream_id": "stream-1",
            "terminal_reason": "policy_denied",
        },
    }
    assert tool_event.event_contract() == {
        "trace_id": "trace-1",
        "event_id": "tool-1",
        "name": "graphblocks.tool_execution",
        "metadata": {
            "attributes": {"tenant": "tenant-1", "token": "[redacted]"},
            "duration_ms": 128,
            "effect_outcome": "committed",
            "effects": ["external_write", "network"],
            "record_id": "tool-1",
            "release_id": "release-1",
            "result_mode": "value",
            "run_id": "run-1",
            "status": "completed",
            "tool_call_id": "call-1",
            "tool_name": "ticket.create",
        },
    }
    assert "secret prompt" not in repr(output_event.event_contract())
    assert "secret result" not in repr(tool_event.event_contract())
    assert "LangfuseEventProjection" in graphblocks_langfuse.__all__
    assert "langfuse_event_from_output_policy" in graphblocks_langfuse.__all__
    assert "langfuse_event_from_tool_execution" in graphblocks_langfuse.__all__


def test_langfuse_prompt_score_and_dataset_projections_are_body_free(monkeypatch) -> None:
    graphblocks_langfuse = importlib.import_module("graphblocks.integrations.langfuse")
    from graphblocks.evaluation import MetricObservation, ResourceSnapshotRef

    prompt = graphblocks_langfuse.langfuse_prompt_from_reference(
        "support.answer",
        version="2026-06-23",
        label="production",
        prompt_digest="sha256:prompt",
        variables_schema_ref="schemas/SupportPrompt@1",
        metadata={"release_id": "release-1"},
    )
    subject = ResourceSnapshotRef(
        "answer-1",
        "sha256:answer",
        resource_kind="answer",
        metadata={"split": "golden"},
    )
    metric = MetricObservation(
        "answer_grounded",
        Decimal("0.91"),
        unit="ratio",
        direction="maximize",
        baseline_value=Decimal("0.85"),
        subject=subject,
        evaluator={"name": "grounding-check", "version": "1"},
    )
    score = graphblocks_langfuse.langfuse_score_from_metric(
        metric,
        trace_id="trace-1",
        observation_id="span-1",
        comment="offline evaluation",
    )
    dataset_item = graphblocks_langfuse.langfuse_dataset_item_from_snapshots(
        "support-golden",
        "case-1",
        input_snapshot=ResourceSnapshotRef("question-1", "sha256:question", resource_kind="question"),
        expected_output=subject,
        metadata={"split": "validation"},
    )

    assert prompt.prompt_contract() == {
        "name": "support.answer",
        "version": "2026-06-23",
        "label": "production",
        "prompt_digest": "sha256:prompt",
        "variables_schema_ref": "schemas/SupportPrompt@1",
        "metadata": {"release_id": "release-1"},
    }
    assert score.score_contract() == {
        "trace_id": "trace-1",
        "observation_id": "span-1",
        "name": "answer_grounded",
        "value": "0.91",
        "comment": "offline evaluation",
        "metadata": {
            "baseline_value": "0.85",
            "direction": "maximize",
            "evaluator": {"name": "grounding-check", "version": "1"},
            "subject": {
                "resource_id": "answer-1",
                "digest": "sha256:answer",
                "resource_kind": "answer",
                "uri": None,
                "metadata": {"split": "golden"},
            },
            "unit": "ratio",
        },
    }
    assert dataset_item.dataset_item_contract() == {
        "dataset_name": "support-golden",
        "item_id": "case-1",
        "input": {
            "resource_id": "question-1",
            "digest": "sha256:question",
            "resource_kind": "question",
            "uri": None,
            "metadata": {},
        },
        "expected_output": {
            "resource_id": "answer-1",
            "digest": "sha256:answer",
            "resource_kind": "answer",
            "uri": None,
            "metadata": {"split": "golden"},
        },
        "metadata": {"split": "validation"},
    }
    assert "prompt body" not in repr(prompt.prompt_contract())
    assert "LangfusePromptProjection" in graphblocks_langfuse.__all__
    assert "langfuse_score_from_metric" in graphblocks_langfuse.__all__

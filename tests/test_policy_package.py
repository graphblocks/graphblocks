from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

import graphblocks
import yaml
from graphblocks.output_policy import (
    PendingToolCallsDisposition,
    VALID_DELIVERY_MODES,
    VALID_DRAFT_DISPOSITIONS,
    VALID_FLUSH_BOUNDARIES,
    VALID_OUTPUT_DISPOSITIONS,
    VALID_OUTPUT_DURABLE_RESULTS,
    VALID_PENDING_TOOL_CALLS_DISPOSITIONS,
    VALID_PROVIDER_CANCELLATIONS,
    VALID_TERMINAL_REASONS,
    VALID_VIOLATION_ACTIONS,
)
from graphblocks.policy import (
    EnforcementPoint,
    PolicyEffect,
    PolicyEnforcementStatus,
    RuleEffect,
    VALID_ENFORCEMENT_POINTS,
    VALID_ENFORCEMENT_STATUSES,
    VALID_POLICY_EFFECTS,
    VALID_POLICY_FAIL_MODES,
    VALID_RULE_EFFECTS,
)


ROOT = Path(__file__).parents[1]


def test_standard_policy_profiles_include_assistant_output_streaming_profile() -> None:
    profile_set = yaml.safe_load(
        (
            ROOT
            / "docs"
            / "upstream"
            / "GraphBlocks_v1.0_Final"
            / "profiles"
            / "policy-profiles.yaml"
        ).read_text(encoding="utf-8")
    )
    profile = profile_set["spec"]["profiles"]["assistant-output-standard"]

    assert profile["outputStreaming"]["delivery"] == {
        "mode": "bounded_holdback",
        "holdbackMaxTokens": 48,
        "holdbackMaxDuration": "250ms",
        "flushBoundaries": ["sentence", "paragraph", "tool_call"],
    }
    assert profile["outputStreaming"]["evaluation"]["enforcementPoints"] == [
        "on_generation_chunk",
        "before_client_delivery",
        "before_output_commit",
    ]
    assert profile["outputStreaming"]["onViolation"] == {
        "disposition": "abort_response",
        "providerCancellation": {"mode": "request"},
        "pendingToolCalls": {"disposition": "deny"},
        "deliveredDraft": {"disposition": "retract"},
        "durableResult": {"disposition": "none"},
        "replacement": {"kind": "message_ref", "ref": "messages/output-policy-blocked"},
    }


def test_policy_package_exposes_static_evaluator_and_output_gate_contract(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy" / "src"))
    graphblocks_policy = importlib.import_module("graphblocks_policy")

    evaluator = graphblocks_policy.StaticPolicyEvaluator(
        rules=[
            graphblocks_policy.PolicyRule(
                "allow-model",
                "allow",
                actions=("model.generate",),
                resource_selectors=("model",),
            )
        ]
    )
    request = graphblocks_policy.PolicyRequest(
        request_id="request-1",
        enforcement_point="on_generation_chunk",
        action="model.generate",
        resource=graphblocks_policy.ResourceRef("model:support", resource_kind="model"),
        occurred_at="2026-06-23T00:00:00Z",
    )
    decision = evaluator.evaluate(request, evaluated_at="2026-06-23T00:00:01Z")

    gate = graphblocks_policy.OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(graphblocks_policy.GenerationChunk.text("stream-1", "response-1", 1, "blocked"))
    update = gate.apply_decision(
        graphblocks_policy.OutputPolicyDecision.abort_response("output-decision-1", input_digest="sha256:output"),
        occurred_at="2026-06-23T00:00:02Z",
    )

    assert decision.effect == "allow"
    assert decision.input_digest == request.with_input_digest().input_digest
    assert update.cutoff is not None
    assert update.cutoff.policy_decision_id == "output-decision-1"
    assert update.pending_tool_calls == "deny"
    assert "PolicyUnavailableError" in graphblocks_policy.__all__
    assert "unavailable_policy_decision" in graphblocks_policy.__all__


def test_policy_package_exposes_declarative_output_policy_evaluator(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy" / "src"))
    graphblocks_policy = importlib.import_module("graphblocks_policy")
    evaluator = graphblocks_policy.DeclarativeOutputPolicyEvaluator(
        rules=(
            graphblocks_policy.DeclarativeOutputPolicyRule(
                rule_id="redact-secret",
                literal="secret",
                disposition="redact",
                replacement="[redacted]",
            ),
        )
    )

    decision = evaluator.evaluate_chunk(
        graphblocks_policy.GenerationChunk.text("stream-1", "response-1", 1, "hello secret"),
        evaluated_at="2026-06-23T00:00:00Z",
    )

    assert decision.disposition == "redact"
    assert decision.redactions == (
        {"path": "/chunks/1/text", "start": 6, "end": 12, "replacement": "[redacted]"},
    )
    assert "DeclarativeOutputPolicyEvaluator" in graphblocks_policy.__all__


def test_policy_package_exposes_policy_test_dsl(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy" / "src"))
    graphblocks_policy = importlib.import_module("graphblocks_policy")
    evaluator = graphblocks_policy.StaticPolicyEvaluator(
        rules=[
            graphblocks_policy.PolicyRule(
                "deny-tool",
                "deny",
                actions=("tool.run",),
                resource_selectors=("tool",),
            )
        ]
    )
    request = graphblocks_policy.PolicyRequest(
        request_id="request-1",
        enforcement_point="before_tool_or_effect",
        action="tool.run",
        resource=graphblocks_policy.ResourceRef("tool:shell", resource_kind="tool"),
        occurred_at="2026-06-23T00:00:00Z",
    )
    case = graphblocks_policy.PolicyTestCase(
        "deny-shell",
        request,
        graphblocks_policy.PolicyTestExpectation(effect="deny", enforcement_status="blocked"),
        evaluated_at="2026-06-23T00:00:01Z",
    )

    report = graphblocks_policy.run_policy_tests(evaluator, [case])

    assert report.passed is True
    assert "PolicyEnforcer" in graphblocks_policy.__all__
    assert "run_policy_tests" in graphblocks_policy.__all__


def test_policy_package_exposes_canonical_literal_sets(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy" / "src"))
    graphblocks_policy = importlib.import_module("graphblocks_policy")
    output_constants = {
        "VALID_DELIVERY_MODES": VALID_DELIVERY_MODES,
        "VALID_DRAFT_DISPOSITIONS": VALID_DRAFT_DISPOSITIONS,
        "VALID_FLUSH_BOUNDARIES": VALID_FLUSH_BOUNDARIES,
        "VALID_OUTPUT_DISPOSITIONS": VALID_OUTPUT_DISPOSITIONS,
        "VALID_OUTPUT_DURABLE_RESULTS": VALID_OUTPUT_DURABLE_RESULTS,
        "VALID_PENDING_TOOL_CALLS_DISPOSITIONS": VALID_PENDING_TOOL_CALLS_DISPOSITIONS,
        "VALID_PROVIDER_CANCELLATIONS": VALID_PROVIDER_CANCELLATIONS,
        "VALID_TERMINAL_REASONS": VALID_TERMINAL_REASONS,
        "VALID_VIOLATION_ACTIONS": VALID_VIOLATION_ACTIONS,
    }

    assert graphblocks_policy.VALID_ENFORCEMENT_POINTS is VALID_ENFORCEMENT_POINTS
    assert graphblocks_policy.VALID_ENFORCEMENT_STATUSES is VALID_ENFORCEMENT_STATUSES
    assert graphblocks_policy.VALID_POLICY_EFFECTS is VALID_POLICY_EFFECTS
    assert graphblocks_policy.VALID_POLICY_FAIL_MODES is VALID_POLICY_FAIL_MODES
    assert graphblocks_policy.VALID_RULE_EFFECTS is VALID_RULE_EFFECTS
    assert {
        "on_generation_chunk",
        "before_client_delivery",
        "before_output_commit",
        "before_tool_or_effect",
    }.issubset(graphblocks_policy.VALID_ENFORCEMENT_POINTS)
    assert "VALID_ENFORCEMENT_POINTS" in graphblocks_policy.__all__
    assert graphblocks.VALID_ENFORCEMENT_POINTS is VALID_ENFORCEMENT_POINTS
    assert graphblocks.VALID_ENFORCEMENT_STATUSES is VALID_ENFORCEMENT_STATUSES
    assert graphblocks.VALID_POLICY_EFFECTS is VALID_POLICY_EFFECTS
    assert graphblocks.VALID_POLICY_FAIL_MODES is VALID_POLICY_FAIL_MODES
    assert graphblocks.VALID_RULE_EFFECTS is VALID_RULE_EFFECTS
    assert "VALID_ENFORCEMENT_POINTS" in graphblocks.__all__
    assert "VALID_POLICY_EFFECTS" in graphblocks.__all__
    assert graphblocks.EnforcementPoint is EnforcementPoint
    assert graphblocks.PolicyEffect is PolicyEffect
    assert graphblocks.PolicyEnforcementStatus is PolicyEnforcementStatus
    assert graphblocks.RuleEffect is RuleEffect
    assert "EnforcementPoint" in graphblocks.__all__
    assert "PolicyEffect" in graphblocks.__all__
    assert "PolicyEnforcementStatus" in graphblocks.__all__
    assert "RuleEffect" in graphblocks.__all__
    for name, value in output_constants.items():
        assert getattr(graphblocks_policy, name) is value
        assert name in graphblocks_policy.__all__
    assert {"allow", "hold", "deny_commit"}.issubset(graphblocks_policy.VALID_OUTPUT_DISPOSITIONS)
    assert {"bounded_holdback", "immediate_draft"}.issubset(graphblocks_policy.VALID_DELIVERY_MODES)


def test_root_facade_exports_output_policy_literal_contract() -> None:
    expected_aliases = {
        "DeliveryMode",
        "DraftDisposition",
        "FlushBoundary",
        "OutputDisposition",
        "OutputDurableResult",
        "PendingToolCallsDisposition",
        "ProviderCancellation",
        "TerminalReason",
        "ViolationAction",
    }
    expected_constants = {
        "VALID_DELIVERY_MODES": VALID_DELIVERY_MODES,
        "VALID_DRAFT_DISPOSITIONS": VALID_DRAFT_DISPOSITIONS,
        "VALID_FLUSH_BOUNDARIES": VALID_FLUSH_BOUNDARIES,
        "VALID_OUTPUT_DISPOSITIONS": VALID_OUTPUT_DISPOSITIONS,
        "VALID_OUTPUT_DURABLE_RESULTS": VALID_OUTPUT_DURABLE_RESULTS,
        "VALID_PENDING_TOOL_CALLS_DISPOSITIONS": VALID_PENDING_TOOL_CALLS_DISPOSITIONS,
        "VALID_PROVIDER_CANCELLATIONS": VALID_PROVIDER_CANCELLATIONS,
        "VALID_TERMINAL_REASONS": VALID_TERMINAL_REASONS,
        "VALID_VIOLATION_ACTIONS": VALID_VIOLATION_ACTIONS,
    }
    expected_exports = expected_aliases | set(expected_constants)

    assert sorted(name for name in expected_exports if name not in graphblocks.__all__) == []
    for name in expected_aliases:
        assert hasattr(graphblocks, name)
    assert graphblocks.PendingToolCallsDisposition is PendingToolCallsDisposition
    for name, value in expected_constants.items():
        assert getattr(graphblocks, name) is value


def test_policy_package_lazy_native_output_helpers_delegate_to_runtime(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy" / "src"))
    calls: list[tuple[str, object, object, int | None]] = []

    def evaluate_output_gate(gate: dict[str, object], operations: object) -> dict[str, object]:
        calls.append(("gate", gate, operations, None))
        return {"ok": True, "gate": gate, "operations": operations}

    def evaluate_declarative_output_policy(
        rules: object,
        chunk: dict[str, object],
        *,
        evaluated_at_unix_ms: int,
    ) -> dict[str, object]:
        calls.append(("policy", rules, chunk, evaluated_at_unix_ms))
        return {
            "disposition": "allow",
            "rules": rules,
            "chunk": chunk,
            "evaluatedAtUnixMs": evaluated_at_unix_ms,
        }

    def evaluate_retry_policy(policy: dict[str, object], request: dict[str, object]) -> dict[str, object]:
        calls.append(("retry", policy, request, None))
        return {"decision": "retry", "delayMs": 250, "policy": policy, "request": request}

    def evaluate_provider_limit_policy(
        policy: dict[str, object],
        incident: dict[str, object],
    ) -> dict[str, object]:
        calls.append(("provider_limit", policy, incident, None))
        return {"decision": "fallback", "policy": policy, "incident": incident}

    def evaluate_timeout_deadline(policy: dict[str, object], request: dict[str, object]) -> dict[str, object]:
        calls.append(("timeout", policy, request, None))
        return {"status": "pending", "policy": policy, "request": request}

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            evaluate_declarative_output_policy=evaluate_declarative_output_policy,
            evaluate_output_gate=evaluate_output_gate,
            evaluate_provider_limit_policy=evaluate_provider_limit_policy,
            evaluate_retry_policy=evaluate_retry_policy,
            evaluate_timeout_deadline=evaluate_timeout_deadline,
        ),
    )
    graphblocks_policy = importlib.import_module("graphblocks_policy")

    gate_result = graphblocks_policy.evaluate_native_output_gate(
        {"streamId": "stream-1", "responseId": "response-1"},
        [{"op": "chunk", "sequence": 1}],
    )
    policy_result = graphblocks_policy.evaluate_native_declarative_output_policy(
        [{"ruleId": "allow"}],
        {"streamId": "stream-1", "responseId": "response-1", "sequence": 1},
        evaluated_at_unix_ms=1_782_300_001_000,
    )
    retry_result = graphblocks_policy.evaluate_native_retry_policy(
        {"maxAttempts": 3, "retryOn": ["timeout"]},
        {"attempt": 1, "error": {"category": "timeout", "retryable": True}},
    )
    provider_limit_result = graphblocks_policy.evaluate_native_provider_limit_policy(
        {"fallbackEnabled": True},
        {"kind": "provider_quota_exceeded", "compatibleFallbacks": ["models.fallback"]},
    )
    timeout_result = graphblocks_policy.evaluate_native_timeout_deadline(
        {"durationMs": 1_000},
        {"nodeId": "model", "startedAtMs": 1_000, "nowMs": 1_250},
    )

    assert gate_result == {
        "ok": True,
        "gate": {"streamId": "stream-1", "responseId": "response-1"},
        "operations": [{"op": "chunk", "sequence": 1}],
    }
    assert policy_result == {
        "disposition": "allow",
        "rules": [{"ruleId": "allow"}],
        "chunk": {"streamId": "stream-1", "responseId": "response-1", "sequence": 1},
        "evaluatedAtUnixMs": 1_782_300_001_000,
    }
    assert retry_result == {
        "decision": "retry",
        "delayMs": 250,
        "policy": {"maxAttempts": 3, "retryOn": ["timeout"]},
        "request": {"attempt": 1, "error": {"category": "timeout", "retryable": True}},
    }
    assert provider_limit_result == {
        "decision": "fallback",
        "policy": {"fallbackEnabled": True},
        "incident": {"kind": "provider_quota_exceeded", "compatibleFallbacks": ["models.fallback"]},
    }
    assert timeout_result == {
        "status": "pending",
        "policy": {"durationMs": 1_000},
        "request": {"nodeId": "model", "startedAtMs": 1_000, "nowMs": 1_250},
    }
    assert calls == [
        (
            "gate",
            {"streamId": "stream-1", "responseId": "response-1"},
            [{"op": "chunk", "sequence": 1}],
            None,
        ),
        (
            "policy",
            [{"ruleId": "allow"}],
            {"streamId": "stream-1", "responseId": "response-1", "sequence": 1},
            1_782_300_001_000,
        ),
        (
            "retry",
            {"maxAttempts": 3, "retryOn": ["timeout"]},
            {"attempt": 1, "error": {"category": "timeout", "retryable": True}},
            None,
        ),
        (
            "provider_limit",
            {"fallbackEnabled": True},
            {"kind": "provider_quota_exceeded", "compatibleFallbacks": ["models.fallback"]},
            None,
        ),
        (
            "timeout",
            {"durationMs": 1_000},
            {"nodeId": "model", "startedAtMs": 1_000, "nowMs": 1_250},
            None,
        ),
    ]
    assert "evaluate_native_output_gate" in graphblocks_policy.__all__
    assert "evaluate_native_declarative_output_policy" in graphblocks_policy.__all__
    assert "evaluate_native_provider_limit_policy" in graphblocks_policy.__all__
    assert "evaluate_native_retry_policy" in graphblocks_policy.__all__
    assert "evaluate_native_timeout_deadline" in graphblocks_policy.__all__

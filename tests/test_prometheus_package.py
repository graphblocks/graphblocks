from __future__ import annotations

import importlib
import math

import pytest


def _import_prometheus(monkeypatch):
    return importlib.import_module("graphblocks.integrations.prometheus")


def test_prometheus_projection_builds_generation_samples(monkeypatch) -> None:
    graphblocks_prometheus = _import_prometheus(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")
    observation = graphblocks_telemetry.GenerationTelemetryRecord(
        record_id="gen-1",
        run_id="run-1",
        span_id="span-1",
        node_id="agent",
        provider="openai-compatible",
        model="gpt-test",
        release_id="release-1",
        usage={"input_tokens": 20, "output_tokens": 8},
        timing_ms={"queue_wait": 4, "execution": 128},
    )

    samples = graphblocks_prometheus.prometheus_samples_from_generation(observation)

    assert [sample.sample_contract() for sample in samples] == [
        {
            "name": "graphblocks_generation_usage_tokens_total",
            "labels": {
                "model": "gpt-test",
                "node_id": "agent",
                "provider": "openai-compatible",
                "release_id": "release-1",
                "token_type": "input_tokens",
            },
            "value": 20.0,
        },
        {
            "name": "graphblocks_generation_usage_tokens_total",
            "labels": {
                "model": "gpt-test",
                "node_id": "agent",
                "provider": "openai-compatible",
                "release_id": "release-1",
                "token_type": "output_tokens",
            },
            "value": 8.0,
        },
        {
            "name": "graphblocks_generation_timing_milliseconds",
            "labels": {
                "model": "gpt-test",
                "node_id": "agent",
                "phase": "execution",
                "provider": "openai-compatible",
                "release_id": "release-1",
            },
            "value": 128.0,
        },
        {
            "name": "graphblocks_generation_timing_milliseconds",
            "labels": {
                "model": "gpt-test",
                "node_id": "agent",
                "phase": "queue_wait",
                "provider": "openai-compatible",
                "release_id": "release-1",
            },
            "value": 4.0,
        },
    ]


def test_prometheus_projection_builds_policy_and_tool_samples_without_runtime_ids(monkeypatch) -> None:
    graphblocks_prometheus = _import_prometheus(monkeypatch)
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
    )

    output_samples = graphblocks_prometheus.prometheus_samples_from_output_policy(output_record)
    tool_samples = graphblocks_prometheus.prometheus_samples_from_tool_execution(tool_record)

    assert [sample.sample_contract() for sample in output_samples] == [
        {
            "name": "graphblocks_output_policy_decisions_total",
            "labels": {
                "disposition": "abort_response",
                "enforcement_point": "before_client_delivery",
                "release_id": "release-1",
            },
            "value": 1.0,
        },
        {
            "name": "graphblocks_output_policy_cutoffs_total",
            "labels": {
                "disposition": "abort_response",
                "draft_disposition": "retract",
                "durable_result": "none",
                "enforcement_point": "before_client_delivery",
                "release_id": "release-1",
                "terminal_reason": "policy_denied",
            },
            "value": 1.0,
        },
        {
            "name": "graphblocks_output_policy_accepted_sequence",
            "labels": {
                "disposition": "abort_response",
                "enforcement_point": "before_client_delivery",
                "release_id": "release-1",
            },
            "value": 7.0,
        },
        {
            "name": "graphblocks_output_policy_client_delivered_sequence",
            "labels": {
                "disposition": "abort_response",
                "enforcement_point": "before_client_delivery",
                "release_id": "release-1",
            },
            "value": 5.0,
        },
    ]
    assert [sample.sample_contract() for sample in tool_samples] == [
        {
            "name": "graphblocks_tool_executions_total",
            "labels": {
                "effect_outcome": "committed",
                "release_id": "release-1",
                "result_mode": "value",
                "status": "completed",
                "tool_name": "ticket.create",
            },
            "value": 1.0,
        },
        {
            "name": "graphblocks_tool_effects_total",
            "labels": {
                "effect": "external_write",
                "effect_outcome": "committed",
                "release_id": "release-1",
                "result_mode": "value",
                "status": "completed",
                "tool_name": "ticket.create",
            },
            "value": 1.0,
        },
        {
            "name": "graphblocks_tool_effects_total",
            "labels": {
                "effect": "network",
                "effect_outcome": "committed",
                "release_id": "release-1",
                "result_mode": "value",
                "status": "completed",
                "tool_name": "ticket.create",
            },
            "value": 1.0,
        },
        {
            "name": "graphblocks_tool_execution_duration_milliseconds",
            "labels": {
                "effect_outcome": "committed",
                "release_id": "release-1",
                "result_mode": "value",
                "status": "completed",
                "tool_name": "ticket.create",
            },
            "value": 128.0,
        },
    ]
    high_cardinality_labels = {
        "policy_snapshot_id",
        "record_id",
        "response_id",
        "run_id",
        "stream_id",
        "tool_call_id",
    }
    all_samples = (*output_samples, *tool_samples)
    assert all(not high_cardinality_labels & set(sample.sample_contract()["labels"]) for sample in all_samples)
    assert "prometheus_samples_from_output_policy" in graphblocks_prometheus.__all__
    assert "prometheus_samples_from_tool_execution" in graphblocks_prometheus.__all__


def test_prometheus_package_lints_sample_cardinality(monkeypatch) -> None:
    graphblocks_prometheus = _import_prometheus(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks.telemetry")

    samples = (
        graphblocks_prometheus.PrometheusSample(
            "graphblocks_generation_usage_tokens_total",
            {"provider": "openai-compatible", "model": "small", "run_id": "run-1"},
            1,
        ),
        graphblocks_prometheus.PrometheusSample(
            "graphblocks_generation_usage_tokens_total",
            {"provider": "openai-compatible", "model": "medium"},
            1,
        ),
        graphblocks_prometheus.PrometheusSample(
            "graphblocks_generation_usage_tokens_total",
            {"provider": "openai-compatible", "model": "large"},
            1,
        ),
    )

    result = graphblocks_prometheus.lint_prometheus_samples(
        samples,
        linter=graphblocks_telemetry.MetricCardinalityLinter(max_distinct_values_per_label=2),
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
    assert "lint_prometheus_samples" in graphblocks_prometheus.__all__


@pytest.mark.parametrize("value", (math.nan, math.inf, -math.inf))
def test_prometheus_sample_rejects_non_finite_values(monkeypatch, value: float) -> None:
    graphblocks_prometheus = _import_prometheus(monkeypatch)

    with pytest.raises(graphblocks_prometheus.PrometheusProjectionError, match="must be finite"):
        graphblocks_prometheus.PrometheusSample("graphblocks_test_total", {}, value)


def test_prometheus_sample_rejects_boolean_value(monkeypatch) -> None:
    graphblocks_prometheus = _import_prometheus(monkeypatch)

    with pytest.raises(graphblocks_prometheus.PrometheusProjectionError, match="must be numeric"):
        graphblocks_prometheus.PrometheusSample("graphblocks_test_total", {}, True)  # type: ignore[arg-type]


def test_prometheus_rule_group_builds_rule_file_contract(monkeypatch) -> None:
    graphblocks_prometheus = _import_prometheus(monkeypatch)
    group = graphblocks_prometheus.PrometheusRuleGroup(
        name="graphblocks-runtime",
        rules=(
            graphblocks_prometheus.PrometheusRule.recording(
                record="graphblocks:generation_tokens:rate5m",
                expr='sum(rate(graphblocks_generation_usage_tokens_total[5m])) by (provider, model)',
                labels={"team": "runtime"},
            ),
            graphblocks_prometheus.PrometheusRule.alerting(
                alert="GraphBlocksGenerationLatencyHigh",
                expr="histogram_quantile(0.95, graphblocks_generation_timing_milliseconds) > 30000",
                for_duration="10m",
                labels={"severity": "warning"},
                annotations={"summary": "GraphBlocks generation latency is high"},
            ),
        ),
    )

    assert group.rule_file_contract() == {
        "groups": [
            {
                "name": "graphblocks-runtime",
                "rules": [
                    {
                        "record": "graphblocks:generation_tokens:rate5m",
                        "expr": 'sum(rate(graphblocks_generation_usage_tokens_total[5m])) by (provider, model)',
                        "labels": {"team": "runtime"},
                    },
                    {
                        "alert": "GraphBlocksGenerationLatencyHigh",
                        "expr": "histogram_quantile(0.95, graphblocks_generation_timing_milliseconds) > 30000",
                        "for": "10m",
                        "labels": {"severity": "warning"},
                        "annotations": {"summary": "GraphBlocks generation latency is high"},
                    },
                ],
            }
        ]
    }
    assert group.content_digest().startswith("sha256:")

    without_labels = graphblocks_prometheus.PrometheusRuleGroup(
        name="graphblocks-runtime",
        rules=(
            graphblocks_prometheus.PrometheusRule.recording(
                record="graphblocks:generation_tokens:rate5m",
                expr='sum(rate(graphblocks_generation_usage_tokens_total[5m])) by (provider, model)',
            ),
        ),
    )
    with_labels = graphblocks_prometheus.PrometheusRuleGroup(
        name="graphblocks-runtime",
        rules=(group.rules[0],),
    )
    assert without_labels.content_digest() != with_labels.content_digest()

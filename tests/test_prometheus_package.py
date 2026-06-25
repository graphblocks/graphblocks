from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _import_prometheus(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-telemetry" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-prometheus" / "src"))
    return importlib.import_module("graphblocks_prometheus")


def test_prometheus_projection_builds_generation_samples(monkeypatch) -> None:
    graphblocks_prometheus = _import_prometheus(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks_telemetry")
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


def test_prometheus_package_lints_sample_cardinality(monkeypatch) -> None:
    graphblocks_prometheus = _import_prometheus(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks_telemetry")

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


def test_prometheus_rule_group_builds_rule_file_contract(monkeypatch) -> None:
    graphblocks_prometheus = _import_prometheus(monkeypatch)
    group = graphblocks_prometheus.PrometheusRuleGroup(
        name="graphblocks-runtime",
        rules=(
            graphblocks_prometheus.PrometheusRule.recording(
                record="graphblocks:generation_tokens:rate5m",
                expr='sum(rate(graphblocks_generation_usage_tokens_total[5m])) by (provider, model)',
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

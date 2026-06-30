from __future__ import annotations

from decimal import Decimal
import importlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def _add_observability_package_paths(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-telemetry" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-otel" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-langfuse" / "src"))


def test_telemetry_observation_contract_detaches_mutable_inputs(monkeypatch) -> None:
    _add_observability_package_paths(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks_telemetry")
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


def test_telemetry_capture_policy_redacts_sensitive_observation_fields(monkeypatch) -> None:
    _add_observability_package_paths(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks_telemetry")
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


def test_telemetry_capture_policy_linter_flags_unprotected_secret_and_content_keys(monkeypatch) -> None:
    _add_observability_package_paths(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks_telemetry")
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
    _add_observability_package_paths(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks_telemetry")
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
    _add_observability_package_paths(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks_telemetry")

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


def test_metric_cardinality_linter_flags_unbounded_labels(monkeypatch) -> None:
    _add_observability_package_paths(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks_telemetry")
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


def test_otel_projection_uses_versioned_schema_without_importing_sdk(monkeypatch) -> None:
    _add_observability_package_paths(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks_telemetry")
    graphblocks_otel = importlib.import_module("graphblocks_otel")
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


def test_otel_projection_applies_capture_policy_before_export(monkeypatch) -> None:
    _add_observability_package_paths(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks_telemetry")
    graphblocks_otel = importlib.import_module("graphblocks_otel")
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


def test_otel_collector_template_renders_otlp_pipeline_without_sdk_import(monkeypatch) -> None:
    _add_observability_package_paths(monkeypatch)
    graphblocks_otel = importlib.import_module("graphblocks_otel")

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
                "tls": {"insecure": True},
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
                    "grpc": {"endpoint": "0.0.0.0:4317"},
                    "http": {"endpoint": "0.0.0.0:4318"},
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
    _add_observability_package_paths(monkeypatch)
    graphblocks_otel = importlib.import_module("graphblocks_otel")

    with pytest.raises(graphblocks_otel.OtelCollectorTemplateError, match="unknown collector pipeline"):
        graphblocks_otel.otlp_collector_template("collector.example:4317", pipelines=("profiles",))


def test_langfuse_projection_uses_trace_generation_contract(monkeypatch) -> None:
    _add_observability_package_paths(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks_telemetry")
    graphblocks_langfuse = importlib.import_module("graphblocks_langfuse")
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


def test_langfuse_projection_applies_capture_policy_before_export(monkeypatch) -> None:
    _add_observability_package_paths(monkeypatch)
    graphblocks_telemetry = importlib.import_module("graphblocks_telemetry")
    graphblocks_langfuse = importlib.import_module("graphblocks_langfuse")
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


def test_langfuse_prompt_score_and_dataset_projections_are_body_free(monkeypatch) -> None:
    _add_observability_package_paths(monkeypatch)
    graphblocks_langfuse = importlib.import_module("graphblocks_langfuse")
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

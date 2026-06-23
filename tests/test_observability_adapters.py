from __future__ import annotations

import importlib
from pathlib import Path


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

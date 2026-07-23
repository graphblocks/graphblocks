from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from graphblocks import canonical_dumps, canonical_loads
from graphblocks.telemetry import (
    DEFAULT_CONTENT_TELEMETRY_ATTRIBUTE_KEYS,
    DEFAULT_SENSITIVE_TELEMETRY_ATTRIBUTE_KEYS,
    GenerationTelemetryRecord,
    OutputPolicyTelemetryRecord,
    TelemetryCapturePolicy,
    ToolExecutionTelemetryRecord,
)


VALID_COLLECTOR_PIPELINES = frozenset({"traces", "metrics", "logs"})

DEFAULT_OTLP_CAPTURE_POLICY = TelemetryCapturePolicy(
    redacted_attribute_keys=DEFAULT_SENSITIVE_TELEMETRY_ATTRIBUTE_KEYS,
    dropped_attribute_keys=DEFAULT_CONTENT_TELEMETRY_ATTRIBUTE_KEYS,
)


class OtelCollectorTemplateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OtlpSpanProjection:
    span_json: str

    def span_contract(self) -> dict[str, object]:
        return _strict_json_contract("OTLP span projection", self.span_json)


@dataclass(frozen=True, slots=True)
class OtelCollectorTemplate:
    name: str
    config_json: str

    @classmethod
    def from_config(cls, *, name: str, config: Mapping[str, object]) -> OtelCollectorTemplate:
        _require_non_empty("collector template name", name)
        return cls(name=name, config_json=canonical_dumps(dict(config)))

    def config_contract(self) -> dict[str, object]:
        return _strict_json_contract("OTel collector template", self.config_json)

    def template_contract(self) -> dict[str, object]:
        return {
            "name": self.name,
            "config": self.config_contract(),
        }

    def render_json(self) -> str:
        return self.config_json


def otlp_collector_template(
    exporter_endpoint: str,
    *,
    name: str = "graphblocks-otel-collector",
    listen_grpc_endpoint: str = "127.0.0.1:4317",
    listen_http_endpoint: str = "127.0.0.1:4318",
    exporter_name: str = "otlp/graphblocks",
    insecure: bool = False,
    pipelines: Sequence[str] = ("traces", "metrics"),
    resource_attributes: Mapping[str, object] | None = None,
    memory_limit_mib: int = 512,
    batch_timeout: str = "1s",
) -> OtelCollectorTemplate:
    _require_non_empty("exporter endpoint", exporter_endpoint)
    _require_non_empty("listen grpc endpoint", listen_grpc_endpoint)
    _require_non_empty("listen http endpoint", listen_http_endpoint)
    _require_non_empty("exporter name", exporter_name)
    _require_non_empty("batch timeout", batch_timeout)
    if not isinstance(insecure, bool):
        raise OtelCollectorTemplateError("insecure must be a boolean")
    if not isinstance(memory_limit_mib, int) or isinstance(memory_limit_mib, bool) or memory_limit_mib <= 0:
        raise OtelCollectorTemplateError("memory_limit_mib must be a positive integer")
    if isinstance(pipelines, (str, bytes)) or not isinstance(pipelines, Sequence) or not pipelines:
        raise OtelCollectorTemplateError("collector pipelines must be a non-empty sequence")

    normalized_pipelines: list[str] = []
    for pipeline in pipelines:
        if pipeline not in VALID_COLLECTOR_PIPELINES:
            raise OtelCollectorTemplateError(f"unknown collector pipeline {pipeline!r}")
        if pipeline not in normalized_pipelines:
            normalized_pipelines.append(pipeline)

    processors: dict[str, object] = {
        "memory_limiter": {
            "check_interval": "1s",
            "limit_mib": memory_limit_mib,
        },
        "batch": {
            "timeout": batch_timeout,
        },
    }
    processor_names = ["memory_limiter", "batch"]
    if resource_attributes is not None and not isinstance(resource_attributes, Mapping):
        raise OtelCollectorTemplateError("resource_attributes must be a mapping")
    if resource_attributes:
        if any(
            not isinstance(key, str) or not key.strip() or key != key.strip()
            for key in resource_attributes
        ):
            raise OtelCollectorTemplateError(
                "resource attribute keys must be stable non-empty strings"
            )
        processors["resource/graphblocks"] = {
            "attributes": [
                {"action": "upsert", "key": key, "value": value}
                for key, value in sorted(resource_attributes.items())
            ]
        }
        processor_names = ["memory_limiter", "resource/graphblocks", "batch"]

    config = {
        "receivers": {
            "otlp": {
                "protocols": {
                    "grpc": {"endpoint": listen_grpc_endpoint},
                    "http": {"endpoint": listen_http_endpoint},
                }
            }
        },
        "processors": processors,
        "exporters": {
            exporter_name: {
                "endpoint": exporter_endpoint,
                "tls": {"insecure": insecure},
            }
        },
        "service": {
            "pipelines": {
                pipeline: {
                    "receivers": ["otlp"],
                    "processors": processor_names,
                    "exporters": [exporter_name],
                }
                for pipeline in normalized_pipelines
            }
        },
    }
    return OtelCollectorTemplate.from_config(name=name, config=config)


def otlp_span_from_generation(
    observation: GenerationTelemetryRecord,
    *,
    schema_url: str,
    capture_policy: TelemetryCapturePolicy | None = None,
) -> OtlpSpanProjection:
    schema_url = _require_non_empty("schema_url", schema_url)
    observation = (capture_policy or DEFAULT_OTLP_CAPTURE_POLICY).apply_generation(observation)
    attributes = {
        "gen_ai.request.model": observation.model,
        "gen_ai.system": observation.provider,
        "graphblocks.node_id": observation.node_id,
        "graphblocks.record_id": observation.record_id,
        "graphblocks.run_id": observation.run_id,
    }
    if observation.release_id is not None:
        attributes["graphblocks.release_id"] = observation.release_id
    for key, value in observation.attributes.items():
        attributes[f"graphblocks.attribute.{key}"] = value
    span = {
        "schema_url": schema_url,
        "name": "graphblocks.generation",
        "span_id": observation.span_id,
        "attributes": dict(sorted(attributes.items())),
        "metrics": {f"usage.{key}": value for key, value in sorted(observation.usage.items())},
    }
    return OtlpSpanProjection(span_json=canonical_dumps(span))


def otlp_span_from_output_policy(
    observation: OutputPolicyTelemetryRecord,
    *,
    schema_url: str,
    capture_policy: TelemetryCapturePolicy | None = None,
) -> OtlpSpanProjection:
    schema_url = _require_non_empty("schema_url", schema_url)
    observation = (capture_policy or DEFAULT_OTLP_CAPTURE_POLICY).apply_output_policy(observation)
    attributes: dict[str, object] = {
        "graphblocks.disposition": observation.disposition,
        "graphblocks.enforcement_point": observation.enforcement_point,
        "graphblocks.record_id": observation.record_id,
        "graphblocks.response_id": observation.response_id,
        "graphblocks.run_id": observation.run_id,
        "graphblocks.stream_id": observation.stream_id,
    }
    for key, value in (
        ("graphblocks.release_id", observation.release_id),
        ("graphblocks.policy_snapshot_id", observation.policy_snapshot_id),
        ("graphblocks.terminal_reason", observation.terminal_reason),
        ("graphblocks.draft_disposition", observation.draft_disposition),
        ("graphblocks.pending_tool_calls", observation.pending_tool_calls),
        ("graphblocks.durable_result", observation.durable_result),
    ):
        if value is not None:
            attributes[key] = value
    for key, value in observation.attributes.items():
        attributes[f"graphblocks.attribute.{key}"] = value
    metrics = {}
    if observation.accepted_through_sequence is not None:
        metrics["accepted_through_sequence"] = observation.accepted_through_sequence
    if observation.last_client_delivered_sequence is not None:
        metrics["last_client_delivered_sequence"] = observation.last_client_delivered_sequence
    span = {
        "schema_url": schema_url,
        "name": "graphblocks.output_policy",
        "span_id": observation.record_id,
        "attributes": dict(sorted(attributes.items())),
        "metrics": dict(sorted(metrics.items())),
    }
    return OtlpSpanProjection(span_json=canonical_dumps(span))


def otlp_span_from_tool_execution(
    observation: ToolExecutionTelemetryRecord,
    *,
    schema_url: str,
    capture_policy: TelemetryCapturePolicy | None = None,
) -> OtlpSpanProjection:
    schema_url = _require_non_empty("schema_url", schema_url)
    observation = (capture_policy or DEFAULT_OTLP_CAPTURE_POLICY).apply_tool_execution(observation)
    attributes: dict[str, object] = {
        "graphblocks.record_id": observation.record_id,
        "graphblocks.run_id": observation.run_id,
        "graphblocks.tool_call_id": observation.tool_call_id,
        "graphblocks.tool_name": observation.tool_name,
        "graphblocks.tool_status": observation.status,
    }
    for key, value in (
        ("graphblocks.release_id", observation.release_id),
        ("graphblocks.result_mode", observation.result_mode),
        ("graphblocks.effect_outcome", observation.effect_outcome),
    ):
        if value is not None:
            attributes[key] = value
    if observation.effects:
        attributes["graphblocks.effects"] = list(observation.effects)
    for key, value in observation.attributes.items():
        attributes[f"graphblocks.attribute.{key}"] = value
    metrics = {}
    if observation.duration_ms is not None:
        metrics["duration_ms"] = observation.duration_ms
    span = {
        "schema_url": schema_url,
        "name": "graphblocks.tool_execution",
        "span_id": observation.record_id,
        "attributes": dict(sorted(attributes.items())),
        "metrics": dict(sorted(metrics.items())),
    }
    return OtlpSpanProjection(span_json=canonical_dumps(span))


def _require_non_empty(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OtelCollectorTemplateError(f"{field_name} must be a non-empty string")
    return value


def _strict_json_contract(contract_name: str, payload: str) -> dict[str, object]:
    try:
        parsed = canonical_loads(payload)
    except (TypeError, ValueError) as error:
        raise OtelCollectorTemplateError(f"{contract_name} must be valid strict JSON") from error
    if not isinstance(parsed, Mapping):
        raise OtelCollectorTemplateError(f"{contract_name} must be a JSON object")
    return dict(parsed)


__all__ = [
    "DEFAULT_OTLP_CAPTURE_POLICY",
    "OtelCollectorTemplate",
    "OtelCollectorTemplateError",
    "OtlpSpanProjection",
    "VALID_COLLECTOR_PIPELINES",
    "otlp_collector_template",
    "otlp_span_from_generation",
    "otlp_span_from_output_policy",
    "otlp_span_from_tool_execution",
]

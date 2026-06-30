from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json

from graphblocks import canonical_dumps
from graphblocks_telemetry import (
    DEFAULT_CONTENT_TELEMETRY_ATTRIBUTE_KEYS,
    DEFAULT_SENSITIVE_TELEMETRY_ATTRIBUTE_KEYS,
    GenerationTelemetryRecord,
    TelemetryCapturePolicy,
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
        return json.loads(self.span_json)


@dataclass(frozen=True, slots=True)
class OtelCollectorTemplate:
    name: str
    config_json: str

    @classmethod
    def from_config(cls, *, name: str, config: Mapping[str, object]) -> OtelCollectorTemplate:
        _require_non_empty("collector template name", name)
        return cls(name=name, config_json=canonical_dumps(dict(config)))

    def config_contract(self) -> dict[str, object]:
        return json.loads(self.config_json)

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
    listen_grpc_endpoint: str = "0.0.0.0:4317",
    listen_http_endpoint: str = "0.0.0.0:4318",
    exporter_name: str = "otlp/graphblocks",
    insecure: bool = True,
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
    if not isinstance(memory_limit_mib, int) or isinstance(memory_limit_mib, bool) or memory_limit_mib <= 0:
        raise OtelCollectorTemplateError("memory_limit_mib must be a positive integer")
    if isinstance(pipelines, str) or not pipelines:
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
    if resource_attributes:
        processors["resource/graphblocks"] = {
            "attributes": [
                {"action": "upsert", "key": str(key), "value": value}
                for key, value in sorted(resource_attributes.items(), key=lambda item: str(item[0]))
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
                "tls": {"insecure": bool(insecure)},
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


def _require_non_empty(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OtelCollectorTemplateError(f"{field_name} must be a non-empty string")
    return value


__all__ = [
    "DEFAULT_OTLP_CAPTURE_POLICY",
    "OtelCollectorTemplate",
    "OtelCollectorTemplateError",
    "OtlpSpanProjection",
    "VALID_COLLECTOR_PIPELINES",
    "otlp_collector_template",
    "otlp_span_from_generation",
]

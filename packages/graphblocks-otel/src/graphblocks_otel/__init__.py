from __future__ import annotations

from dataclasses import dataclass
import json

from graphblocks import canonical_dumps
from graphblocks_telemetry import GenerationTelemetryRecord


@dataclass(frozen=True, slots=True)
class OtlpSpanProjection:
    span_json: str

    def span_contract(self) -> dict[str, object]:
        return json.loads(self.span_json)


def otlp_span_from_generation(
    observation: GenerationTelemetryRecord,
    *,
    schema_url: str,
) -> OtlpSpanProjection:
    attributes = {
        "gen_ai.request.model": observation.model,
        "gen_ai.system": observation.provider,
        "graphblocks.node_id": observation.node_id,
        "graphblocks.record_id": observation.record_id,
        "graphblocks.run_id": observation.run_id,
    }
    if observation.release_id is not None:
        attributes["graphblocks.release_id"] = observation.release_id
    span = {
        "schema_url": schema_url,
        "name": "graphblocks.generation",
        "span_id": observation.span_id,
        "attributes": dict(sorted(attributes.items())),
        "metrics": {f"usage.{key}": value for key, value in sorted(observation.usage.items())},
    }
    return OtlpSpanProjection(span_json=canonical_dumps(span))


__all__ = [
    "OtlpSpanProjection",
    "otlp_span_from_generation",
]

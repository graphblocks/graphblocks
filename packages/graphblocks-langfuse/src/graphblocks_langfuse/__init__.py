from __future__ import annotations

from dataclasses import dataclass
import json

from graphblocks import canonical_dumps
from graphblocks_telemetry import GenerationTelemetryRecord


@dataclass(frozen=True, slots=True)
class LangfuseGenerationProjection:
    generation_json: str

    def generation_contract(self) -> dict[str, object]:
        return json.loads(self.generation_json)


def langfuse_generation_from_observation(
    observation: GenerationTelemetryRecord,
    *,
    trace_id: str | None = None,
) -> LangfuseGenerationProjection:
    metadata = {
        "node_id": observation.node_id,
        "record_id": observation.record_id,
        "run_id": observation.run_id,
    }
    if observation.release_id is not None:
        metadata["release_id"] = observation.release_id
    if observation.input_digest is not None:
        metadata["input_digest"] = observation.input_digest
    if observation.output_digest is not None:
        metadata["output_digest"] = observation.output_digest
    generation = {
        "trace_id": trace_id or observation.run_id,
        "generation_id": observation.span_id,
        "name": observation.node_id,
        "model": observation.model,
        "provider": observation.provider,
        "metadata": dict(sorted(metadata.items())),
        "usage": dict(sorted(observation.usage.items())),
    }
    return LangfuseGenerationProjection(generation_json=canonical_dumps(generation))


__all__ = [
    "LangfuseGenerationProjection",
    "langfuse_generation_from_observation",
]

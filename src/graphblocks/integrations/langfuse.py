from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
import math

from graphblocks import canonical_dumps, canonical_loads
from graphblocks.telemetry import (
    DEFAULT_CONTENT_TELEMETRY_ATTRIBUTE_KEYS,
    DEFAULT_SENSITIVE_TELEMETRY_ATTRIBUTE_KEYS,
    GenerationTelemetryRecord,
    OutputPolicyTelemetryRecord,
    TelemetryCapturePolicy,
    ToolExecutionTelemetryRecord,
)


DEFAULT_LANGFUSE_CAPTURE_POLICY = TelemetryCapturePolicy(
    redacted_attribute_keys=DEFAULT_SENSITIVE_TELEMETRY_ATTRIBUTE_KEYS,
    dropped_attribute_keys=DEFAULT_CONTENT_TELEMETRY_ATTRIBUTE_KEYS,
)


@dataclass(frozen=True, slots=True)
class LangfuseGenerationProjection:
    generation_json: str

    def generation_contract(self) -> dict[str, object]:
        return _strict_json_contract("Langfuse generation projection", self.generation_json)


@dataclass(frozen=True, slots=True)
class LangfusePromptProjection:
    prompt_json: str

    def prompt_contract(self) -> dict[str, object]:
        return _strict_json_contract("Langfuse prompt projection", self.prompt_json)


@dataclass(frozen=True, slots=True)
class LangfuseScoreProjection:
    score_json: str

    def score_contract(self) -> dict[str, object]:
        return _strict_json_contract("Langfuse score projection", self.score_json)


@dataclass(frozen=True, slots=True)
class LangfuseDatasetItemProjection:
    dataset_item_json: str

    def dataset_item_contract(self) -> dict[str, object]:
        return _strict_json_contract("Langfuse dataset item projection", self.dataset_item_json)


@dataclass(frozen=True, slots=True)
class LangfuseEventProjection:
    event_json: str

    def event_contract(self) -> dict[str, object]:
        return _strict_json_contract("Langfuse event projection", self.event_json)


def langfuse_generation_from_observation(
    observation: GenerationTelemetryRecord,
    *,
    trace_id: str | None = None,
    capture_policy: TelemetryCapturePolicy | None = None,
) -> LangfuseGenerationProjection:
    if not isinstance(observation, GenerationTelemetryRecord):
        raise ValueError(
            "Langfuse generation observation must be GenerationTelemetryRecord"
        )
    trace_id = _trace_id(trace_id, observation.run_id)
    observation = _capture_policy(capture_policy).apply_generation(observation)
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
    if observation.attributes:
        metadata["attributes"] = dict(sorted(observation.attributes.items()))
    generation = {
        "trace_id": trace_id,
        "generation_id": observation.span_id,
        "name": observation.node_id,
        "model": observation.model,
        "provider": observation.provider,
        "metadata": dict(sorted(metadata.items())),
        "usage": dict(sorted(observation.usage.items())),
    }
    return LangfuseGenerationProjection(generation_json=canonical_dumps(generation))


def langfuse_event_from_output_policy(
    observation: OutputPolicyTelemetryRecord,
    *,
    trace_id: str | None = None,
    capture_policy: TelemetryCapturePolicy | None = None,
) -> LangfuseEventProjection:
    if not isinstance(observation, OutputPolicyTelemetryRecord):
        raise ValueError(
            "Langfuse output-policy observation must be OutputPolicyTelemetryRecord"
        )
    trace_id = _trace_id(trace_id, observation.run_id)
    observation = _capture_policy(capture_policy).apply_output_policy(observation)
    metadata = {
        "disposition": observation.disposition,
        "enforcement_point": observation.enforcement_point,
        "record_id": observation.record_id,
        "response_id": observation.response_id,
        "run_id": observation.run_id,
        "stream_id": observation.stream_id,
    }
    for key, value in (
        ("accepted_through_sequence", observation.accepted_through_sequence),
        ("draft_disposition", observation.draft_disposition),
        ("durable_result", observation.durable_result),
        ("last_client_delivered_sequence", observation.last_client_delivered_sequence),
        ("pending_tool_calls", observation.pending_tool_calls),
        ("policy_snapshot_id", observation.policy_snapshot_id),
        ("release_id", observation.release_id),
        ("terminal_reason", observation.terminal_reason),
    ):
        if value is not None:
            metadata[key] = value
    if observation.attributes:
        metadata["attributes"] = dict(sorted(observation.attributes.items()))
    event = {
        "trace_id": trace_id,
        "event_id": observation.record_id,
        "name": "graphblocks.output_policy",
        "metadata": dict(sorted(metadata.items())),
    }
    return LangfuseEventProjection(event_json=canonical_dumps(event))


def langfuse_event_from_tool_execution(
    observation: ToolExecutionTelemetryRecord,
    *,
    trace_id: str | None = None,
    capture_policy: TelemetryCapturePolicy | None = None,
) -> LangfuseEventProjection:
    if not isinstance(observation, ToolExecutionTelemetryRecord):
        raise ValueError(
            "Langfuse tool-execution observation must be ToolExecutionTelemetryRecord"
        )
    trace_id = _trace_id(trace_id, observation.run_id)
    observation = _capture_policy(capture_policy).apply_tool_execution(observation)
    metadata = {
        "record_id": observation.record_id,
        "run_id": observation.run_id,
        "status": observation.status,
        "tool_call_id": observation.tool_call_id,
        "tool_name": observation.tool_name,
    }
    for key, value in (
        ("duration_ms", observation.duration_ms),
        ("effect_outcome", observation.effect_outcome),
        ("release_id", observation.release_id),
        ("result_mode", observation.result_mode),
    ):
        if value is not None:
            metadata[key] = value
    if observation.effects:
        metadata["effects"] = list(observation.effects)
    if observation.attributes:
        metadata["attributes"] = dict(sorted(observation.attributes.items()))
    event = {
        "trace_id": trace_id,
        "event_id": observation.record_id,
        "name": "graphblocks.tool_execution",
        "metadata": dict(sorted(metadata.items())),
    }
    return LangfuseEventProjection(event_json=canonical_dumps(event))


def langfuse_prompt_from_reference(
    name: str,
    *,
    version: str | None = None,
    label: str | None = None,
    prompt_digest: str | None = None,
    variables_schema_ref: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> LangfusePromptProjection:
    _require_non_empty("prompt name", name)
    prompt: dict[str, object] = {
        "name": name,
        "metadata": _metadata_contract(metadata),
    }
    for key, value in (
        ("version", version),
        ("label", label),
        ("prompt_digest", prompt_digest),
        ("variables_schema_ref", variables_schema_ref),
    ):
        if value is not None:
            prompt[key] = _require_non_empty(key, value)
    return LangfusePromptProjection(prompt_json=canonical_dumps(prompt))


def langfuse_score_from_metric(
    metric: object,
    *,
    trace_id: str,
    observation_id: str | None = None,
    score_id: str | None = None,
    comment: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> LangfuseScoreProjection:
    _require_non_empty("trace_id", trace_id)
    metric_name = _require_non_empty("metric name", getattr(metric, "name", None))
    score_metadata: dict[str, object] = _metadata_contract(metadata)
    for key in ("unit", "direction", "baseline_value", "evaluator"):
        value = getattr(metric, key, None)
        if value is not None:
            score_metadata[key] = _json_value(value)
    subject = getattr(metric, "subject", None)
    if subject is not None:
        score_metadata["subject"] = _snapshot_contract(subject)

    score: dict[str, object] = {
        "trace_id": trace_id,
        "name": metric_name,
        "value": _json_value(getattr(metric, "value", None)),
        "metadata": dict(sorted(score_metadata.items())),
    }
    if observation_id is not None:
        score["observation_id"] = _require_non_empty("observation_id", observation_id)
    if score_id is not None:
        score["score_id"] = _require_non_empty("score_id", score_id)
    if comment is not None:
        score["comment"] = _require_non_empty("comment", comment)
    return LangfuseScoreProjection(score_json=canonical_dumps(score))


def langfuse_dataset_item_from_snapshots(
    dataset_name: str,
    item_id: str,
    *,
    input_snapshot: object,
    expected_output: object | None = None,
    metadata: Mapping[str, object] | None = None,
) -> LangfuseDatasetItemProjection:
    item = {
        "dataset_name": _require_non_empty("dataset_name", dataset_name),
        "item_id": _require_non_empty("item_id", item_id),
        "input": _snapshot_contract(input_snapshot),
        "expected_output": _snapshot_contract(expected_output) if expected_output is not None else None,
        "metadata": _metadata_contract(metadata),
    }
    return LangfuseDatasetItemProjection(dataset_item_json=canonical_dumps(item))


def _snapshot_contract(snapshot: object) -> dict[str, object]:
    if isinstance(snapshot, Mapping):
        for snake_case, camel_case in (
            ("resource_id", "resourceId"),
            ("resource_kind", "resourceKind"),
        ):
            if (
                snake_case in snapshot
                and camel_case in snapshot
                and snapshot[snake_case] != snapshot[camel_case]
            ):
                raise ValueError(
                    f"Langfuse snapshot has conflicting {snake_case} aliases"
                )
        resource_id = snapshot.get("resource_id", snapshot.get("resourceId"))
        digest = snapshot.get("digest")
        resource_kind = snapshot.get("resource_kind", snapshot.get("resourceKind"))
        uri = snapshot.get("uri")
        metadata = snapshot.get("metadata")
    else:
        resource_id = getattr(snapshot, "resource_id", None)
        digest = getattr(snapshot, "digest", None)
        resource_kind = getattr(snapshot, "resource_kind", None)
        uri = getattr(snapshot, "uri", None)
        metadata = getattr(snapshot, "metadata", None)

    if metadata is not None and not isinstance(metadata, Mapping):
        raise ValueError("Langfuse snapshot metadata must be a mapping")
    return {
        "resource_id": _require_non_empty("resource_id", resource_id),
        "digest": _require_non_empty("digest", digest),
        "resource_kind": (
            _require_non_empty("resource_kind", resource_kind)
            if resource_kind is not None
            else None
        ),
        "uri": _require_non_empty("uri", uri) if uri is not None else None,
        "metadata": _metadata_contract(metadata),
    }


def _metadata_contract(metadata: Mapping[str, object] | None) -> dict[str, object]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise ValueError("Langfuse metadata must be a mapping")
    if any(not isinstance(key, str) for key in metadata):
        raise ValueError("Langfuse metadata keys must be strings")
    return {key: _json_value(value) for key, value in sorted(metadata.items())}


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Langfuse decimal values must be finite")
        return str(value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Langfuse JSON object keys must be strings")
        return {key: _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError("Langfuse floating-point values must be finite")
    raise ValueError(f"Langfuse value has unsupported type {type(value).__name__}")


def _require_non_empty(field_name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
    ):
        raise ValueError(f"Langfuse {field_name} must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(
            f"Langfuse {field_name} must not contain control characters"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(
            f"Langfuse {field_name} must contain valid Unicode scalar values"
        ) from error
    return value


def _trace_id(trace_id: object | None, default: str) -> str:
    if trace_id is None:
        return default
    return _require_non_empty("trace_id", trace_id)


def _capture_policy(
    capture_policy: TelemetryCapturePolicy | None,
) -> TelemetryCapturePolicy:
    if capture_policy is None:
        return DEFAULT_LANGFUSE_CAPTURE_POLICY
    if not isinstance(capture_policy, TelemetryCapturePolicy):
        raise ValueError(
            "Langfuse capture_policy must be TelemetryCapturePolicy"
        )
    return capture_policy


def _strict_json_contract(contract_name: str, payload: str) -> dict[str, object]:
    try:
        parsed = canonical_loads(payload)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{contract_name} must be valid strict JSON") from error
    if not isinstance(parsed, Mapping):
        raise ValueError(f"{contract_name} must be a JSON object")
    return dict(parsed)


__all__ = [
    "DEFAULT_LANGFUSE_CAPTURE_POLICY",
    "LangfuseDatasetItemProjection",
    "LangfuseEventProjection",
    "LangfuseGenerationProjection",
    "LangfusePromptProjection",
    "LangfuseScoreProjection",
    "langfuse_dataset_item_from_snapshots",
    "langfuse_event_from_output_policy",
    "langfuse_event_from_tool_execution",
    "langfuse_generation_from_observation",
    "langfuse_prompt_from_reference",
    "langfuse_score_from_metric",
]

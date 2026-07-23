from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math

from graphblocks.telemetry import (
    GenerationTelemetryRecord,
    MetricCardinalityLinter,
    MetricCardinalityLintResult,
    OutputPolicyTelemetryRecord,
    ToolExecutionTelemetryRecord,
)


class PrometheusProjectionError(ValueError):
    """Raised when a Prometheus projection contract is invalid."""


def _canonical_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sorted_str_mapping(values: Mapping[str, str]) -> dict[str, str]:
    return {str(key): str(value) for key, value in sorted(dict(values).items())}


@dataclass(frozen=True, slots=True)
class PrometheusSample:
    name: str
    labels: Mapping[str, str]
    value: float

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise PrometheusProjectionError("sample name must not be empty")
        if isinstance(self.value, bool):
            raise PrometheusProjectionError("sample value must be numeric")
        try:
            value = float(self.value)
        except (TypeError, ValueError, OverflowError) as error:
            raise PrometheusProjectionError("sample value must be numeric") from error
        if not math.isfinite(value):
            raise PrometheusProjectionError("sample value must be finite")
        object.__setattr__(self, "labels", _sorted_str_mapping(self.labels))
        object.__setattr__(self, "value", value)

    def sample_contract(self) -> dict[str, object]:
        return {
            "name": self.name,
            "labels": deepcopy(dict(self.labels)),
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class PrometheusRule:
    expr: str
    record: str | None = None
    alert: str | None = None
    for_duration: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    annotations: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.expr.strip():
            raise PrometheusProjectionError("rule expression must not be empty")
        if (self.record is None) == (self.alert is None):
            raise PrometheusProjectionError("exactly one of record or alert must be provided")
        if self.record is not None and not self.record.strip():
            raise PrometheusProjectionError("record name must not be empty")
        if self.alert is not None and not self.alert.strip():
            raise PrometheusProjectionError("alert name must not be empty")
        if self.for_duration is not None and not self.for_duration.strip():
            raise PrometheusProjectionError("for_duration must not be empty")
        object.__setattr__(self, "labels", _sorted_str_mapping(self.labels))
        object.__setattr__(self, "annotations", _sorted_str_mapping(self.annotations))

    @classmethod
    def recording(
        cls,
        *,
        record: str,
        expr: str,
        labels: Mapping[str, str] | None = None,
    ) -> PrometheusRule:
        return cls(record=record, expr=expr, labels=labels or {})

    @classmethod
    def alerting(
        cls,
        *,
        alert: str,
        expr: str,
        for_duration: str | None = None,
        labels: Mapping[str, str] | None = None,
        annotations: Mapping[str, str] | None = None,
    ) -> PrometheusRule:
        return cls(
            alert=alert,
            expr=expr,
            for_duration=for_duration,
            labels=labels or {},
            annotations=annotations or {},
        )

    def rule_contract(self) -> dict[str, object]:
        if self.record is not None:
            recording_contract: dict[str, object] = {
                "record": self.record,
                "expr": self.expr,
            }
            if self.labels:
                recording_contract["labels"] = deepcopy(dict(self.labels))
            return recording_contract
        contract: dict[str, object] = {
            "alert": self.alert,
            "expr": self.expr,
        }
        if self.for_duration is not None:
            contract["for"] = self.for_duration
        if self.labels:
            contract["labels"] = deepcopy(dict(self.labels))
        if self.annotations:
            contract["annotations"] = deepcopy(dict(self.annotations))
        return contract


@dataclass(frozen=True, slots=True)
class PrometheusRuleGroup:
    name: str
    rules: tuple[PrometheusRule, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise PrometheusProjectionError("rule group name must not be empty")
        object.__setattr__(self, "rules", tuple(self.rules))

    def rule_file_contract(self) -> dict[str, object]:
        return {
            "groups": [
                {
                    "name": self.name,
                    "rules": [rule.rule_contract() for rule in self.rules],
                }
            ]
        }

    def content_digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            _canonical_dumps(self.rule_file_contract()).encode("utf-8")
        ).hexdigest()


def prometheus_samples_from_generation(record: GenerationTelemetryRecord) -> tuple[PrometheusSample, ...]:
    base_labels = {
        "model": record.model,
        "node_id": record.node_id,
        "provider": record.provider,
        "release_id": record.release_id or "",
    }
    samples: list[PrometheusSample] = []
    for token_type, value in sorted(record.usage.items()):
        samples.append(
            PrometheusSample(
                "graphblocks_generation_usage_tokens_total",
                {**base_labels, "token_type": token_type},
                float(value),
            )
        )
    for phase, value in sorted(record.timing_ms.items()):
        samples.append(
            PrometheusSample(
                "graphblocks_generation_timing_milliseconds",
                {**base_labels, "phase": phase},
                float(value),
            )
        )
    return tuple(samples)


def prometheus_samples_from_output_policy(record: OutputPolicyTelemetryRecord) -> tuple[PrometheusSample, ...]:
    base_labels = {
        "disposition": record.disposition,
        "enforcement_point": record.enforcement_point,
        "release_id": record.release_id or "",
    }
    samples = [
        PrometheusSample(
            "graphblocks_output_policy_decisions_total",
            base_labels,
            1.0,
        )
    ]
    if record.terminal_reason is not None:
        samples.append(
            PrometheusSample(
                "graphblocks_output_policy_cutoffs_total",
                {
                    **base_labels,
                    "draft_disposition": record.draft_disposition or "",
                    "durable_result": record.durable_result or "",
                    "terminal_reason": record.terminal_reason,
                },
                1.0,
            )
        )
    if record.accepted_through_sequence is not None:
        samples.append(
            PrometheusSample(
                "graphblocks_output_policy_accepted_sequence",
                base_labels,
                float(record.accepted_through_sequence),
            )
        )
    if record.last_client_delivered_sequence is not None:
        samples.append(
            PrometheusSample(
                "graphblocks_output_policy_client_delivered_sequence",
                base_labels,
                float(record.last_client_delivered_sequence),
            )
        )
    return tuple(samples)


def prometheus_samples_from_tool_execution(record: ToolExecutionTelemetryRecord) -> tuple[PrometheusSample, ...]:
    base_labels = {
        "effect_outcome": record.effect_outcome or "",
        "release_id": record.release_id or "",
        "result_mode": record.result_mode or "",
        "status": record.status,
        "tool_name": record.tool_name,
    }
    samples = [
        PrometheusSample(
            "graphblocks_tool_executions_total",
            base_labels,
            1.0,
        )
    ]
    for effect in record.effects:
        samples.append(
            PrometheusSample(
                "graphblocks_tool_effects_total",
                {**base_labels, "effect": effect},
                1.0,
            )
        )
    if record.duration_ms is not None:
        samples.append(
            PrometheusSample(
                "graphblocks_tool_execution_duration_milliseconds",
                base_labels,
                float(record.duration_ms),
            )
        )
    return tuple(samples)


def lint_prometheus_samples(
    samples: Iterable[PrometheusSample],
    *,
    linter: MetricCardinalityLinter | None = None,
) -> MetricCardinalityLintResult:
    cardinality_linter = linter or MetricCardinalityLinter()
    return cardinality_linter.lint_samples(sample.sample_contract() for sample in samples)


__all__ = [
    "PrometheusProjectionError",
    "PrometheusRule",
    "PrometheusRuleGroup",
    "PrometheusSample",
    "lint_prometheus_samples",
    "prometheus_samples_from_generation",
    "prometheus_samples_from_output_policy",
    "prometheus_samples_from_tool_execution",
]

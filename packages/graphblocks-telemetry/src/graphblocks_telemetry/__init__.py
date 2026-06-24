from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType


DEFAULT_BLOCKED_METRIC_LABELS = (
    "attempt_id",
    "conversation_id",
    "record_id",
    "run_id",
    "span_id",
    "trace_id",
    "turn_id",
    "user_id",
)


class TelemetryProjectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GenerationTelemetryRecord:
    record_id: str
    run_id: str
    span_id: str
    node_id: str
    provider: str
    model: str
    release_id: str | None = None
    input_digest: str | None = None
    output_digest: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    timing_ms: Mapping[str, int] = field(default_factory=dict)
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))
        object.__setattr__(self, "timing_ms", MappingProxyType(dict(self.timing_ms)))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def observation_contract(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "run_id": self.run_id,
            "span_id": self.span_id,
            "node_id": self.node_id,
            "provider": self.provider,
            "model": self.model,
            "release_id": self.release_id,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "usage": dict(sorted(self.usage.items())),
            "timing_ms": dict(sorted(self.timing_ms.items())),
            "attributes": dict(sorted(self.attributes.items())),
        }


@dataclass(frozen=True, slots=True)
class TelemetryCapturePolicy:
    redacted_attribute_keys: tuple[str, ...] = field(default_factory=tuple)
    dropped_attribute_keys: tuple[str, ...] = field(default_factory=tuple)
    replacement: str = "[redacted]"
    capture_input_digest: bool = True
    capture_output_digest: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "redacted_attribute_keys", tuple(sorted(set(self.redacted_attribute_keys))))
        object.__setattr__(self, "dropped_attribute_keys", tuple(sorted(set(self.dropped_attribute_keys))))

    def apply_generation(self, record: GenerationTelemetryRecord) -> GenerationTelemetryRecord:
        dropped = set(self.dropped_attribute_keys)
        redacted = set(self.redacted_attribute_keys)
        attributes = {
            key: self.replacement if key in redacted else value
            for key, value in record.attributes.items()
            if key not in dropped
        }
        return replace(
            record,
            input_digest=record.input_digest if self.capture_input_digest else None,
            output_digest=record.output_digest if self.capture_output_digest else None,
            attributes=attributes,
        )


@dataclass(frozen=True, slots=True)
class TelemetryExportResult:
    exporter: str
    status: str
    record_ids: tuple[str, ...]
    error_type: str | None = None
    retryable: bool = False
    run_impact: str = "none"

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_ids", tuple(self.record_ids))
        if self.run_impact != "none":
            raise TelemetryProjectionError("telemetry export result must not affect run correctness")

    @classmethod
    def failed(
        cls,
        *,
        exporter: str,
        record_ids: tuple[str, ...],
        error_type: str,
        retryable: bool,
    ) -> TelemetryExportResult:
        return cls(
            exporter=exporter,
            status="failed",
            record_ids=record_ids,
            error_type=error_type,
            retryable=retryable,
            run_impact="none",
        )

    def result_contract(self) -> dict[str, object]:
        return {
            "exporter": self.exporter,
            "status": self.status,
            "record_ids": list(self.record_ids),
            "error_type": self.error_type,
            "retryable": self.retryable,
            "run_impact": self.run_impact,
        }


@dataclass(frozen=True, slots=True)
class MetricCardinalityIssue:
    metric_name: str
    label: str
    distinct_values: int
    limit: int
    reason: str

    def issue_contract(self) -> dict[str, object]:
        return {
            "metric_name": self.metric_name,
            "label": self.label,
            "distinct_values": self.distinct_values,
            "limit": self.limit,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class MetricCardinalityLintResult:
    issues: tuple[MetricCardinalityIssue, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "issues",
            tuple(sorted(self.issues, key=lambda issue: (issue.metric_name, issue.label, issue.reason))),
        )

    @property
    def passed(self) -> bool:
        return not self.issues

    def issue_contracts(self) -> list[dict[str, object]]:
        return [issue.issue_contract() for issue in self.issues]


@dataclass(frozen=True, slots=True)
class MetricCardinalityLinter:
    max_distinct_values_per_label: int = 32
    blocked_labels: tuple[str, ...] = DEFAULT_BLOCKED_METRIC_LABELS

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocked_labels", tuple(sorted(set(self.blocked_labels))))

    def lint_samples(self, samples: Iterable[Mapping[str, object]]) -> MetricCardinalityLintResult:
        label_values: dict[tuple[str, str], set[str]] = {}
        blocked_label_values: dict[tuple[str, str], set[str]] = {}
        blocked_labels = set(self.blocked_labels)
        for sample in samples:
            metric_name = sample.get("name")
            if not isinstance(metric_name, str) or not metric_name.strip():
                raise TelemetryProjectionError("metric sample name must be a non-empty string")
            labels = sample.get("labels", {})
            if not isinstance(labels, Mapping):
                raise TelemetryProjectionError("metric sample labels must be a mapping")
            for raw_label, raw_value in labels.items():
                label = str(raw_label)
                value = str(raw_value)
                key = (metric_name, label)
                if label in blocked_labels:
                    blocked_label_values.setdefault(key, set()).add(value)
                else:
                    label_values.setdefault(key, set()).add(value)

        issues: list[MetricCardinalityIssue] = []
        for (metric_name, label), values in label_values.items():
            if len(values) > self.max_distinct_values_per_label:
                issues.append(
                    MetricCardinalityIssue(
                        metric_name=metric_name,
                        label=label,
                        distinct_values=len(values),
                        limit=self.max_distinct_values_per_label,
                        reason="too_many_values",
                    )
                )
        for (metric_name, label), values in blocked_label_values.items():
            issues.append(
                MetricCardinalityIssue(
                    metric_name=metric_name,
                    label=label,
                    distinct_values=len(values),
                    limit=0,
                    reason="blocked_label",
                )
            )
        return MetricCardinalityLintResult(tuple(issues))


__all__ = [
    "DEFAULT_BLOCKED_METRIC_LABELS",
    "GenerationTelemetryRecord",
    "MetricCardinalityIssue",
    "MetricCardinalityLintResult",
    "MetricCardinalityLinter",
    "TelemetryCapturePolicy",
    "TelemetryExportResult",
    "TelemetryProjectionError",
]

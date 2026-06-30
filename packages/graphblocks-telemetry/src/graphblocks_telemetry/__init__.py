from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from graphblocks.diagnostics import Diagnostic, Severity


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
DEFAULT_SENSITIVE_TELEMETRY_ATTRIBUTE_KEYS = (
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
)
DEFAULT_CONTENT_TELEMETRY_ATTRIBUTE_KEYS = (
    "completion",
    "input",
    "messages",
    "output",
    "prompt",
    "tool_result",
)


class TelemetryProjectionError(RuntimeError):
    pass


def capture_native_telemetry_content(
    decision: Mapping[str, object],
    content: Mapping[str, object],
) -> dict[str, object]:
    from graphblocks_runtime import capture_telemetry_content

    return capture_telemetry_content(dict(decision), dict(content))


def _diagnostic_summary(diagnostics: Iterable[Diagnostic]) -> dict[Severity, int]:
    summary: dict[Severity, int] = {"error": 0, "warning": 0, "info": 0}
    for diagnostic in diagnostics:
        summary[diagnostic.severity] += 1
    return summary


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
class OutputPolicyTelemetryRecord:
    record_id: str
    run_id: str
    stream_id: str
    response_id: str
    enforcement_point: str
    disposition: str
    release_id: str | None = None
    policy_snapshot_id: str | None = None
    terminal_reason: str | None = None
    draft_disposition: str | None = None
    pending_tool_calls: str | None = None
    durable_result: str | None = None
    accepted_through_sequence: int | None = None
    last_client_delivered_sequence: int | None = None
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def observation_contract(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "run_id": self.run_id,
            "stream_id": self.stream_id,
            "response_id": self.response_id,
            "enforcement_point": self.enforcement_point,
            "disposition": self.disposition,
            "release_id": self.release_id,
            "policy_snapshot_id": self.policy_snapshot_id,
            "terminal_reason": self.terminal_reason,
            "draft_disposition": self.draft_disposition,
            "pending_tool_calls": self.pending_tool_calls,
            "durable_result": self.durable_result,
            "accepted_through_sequence": self.accepted_through_sequence,
            "last_client_delivered_sequence": self.last_client_delivered_sequence,
            "attributes": dict(sorted(self.attributes.items())),
        }


@dataclass(frozen=True, slots=True)
class ToolExecutionTelemetryRecord:
    record_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    status: str
    release_id: str | None = None
    result_mode: str | None = None
    effect_outcome: str | None = None
    effects: tuple[str, ...] = field(default_factory=tuple)
    duration_ms: int | None = None
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "effects", tuple(sorted(str(effect) for effect in self.effects)))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def observation_contract(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "run_id": self.run_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "release_id": self.release_id,
            "result_mode": self.result_mode,
            "effect_outcome": self.effect_outcome,
            "effects": list(self.effects),
            "duration_ms": self.duration_ms,
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
        return replace(
            record,
            input_digest=record.input_digest if self.capture_input_digest else None,
            output_digest=record.output_digest if self.capture_output_digest else None,
            attributes=self._protected_attributes(record.attributes),
        )

    def apply_output_policy(self, record: OutputPolicyTelemetryRecord) -> OutputPolicyTelemetryRecord:
        return replace(record, attributes=self._protected_attributes(record.attributes))

    def apply_tool_execution(self, record: ToolExecutionTelemetryRecord) -> ToolExecutionTelemetryRecord:
        return replace(record, attributes=self._protected_attributes(record.attributes))

    def _protected_attributes(self, attributes: Mapping[str, object]) -> dict[str, object]:
        dropped = set(self.dropped_attribute_keys)
        redacted = set(self.redacted_attribute_keys)
        return {
            key: self.replacement if key in redacted else value
            for key, value in attributes.items()
            if key not in dropped
        }


@dataclass(frozen=True, slots=True)
class TelemetryCapturePolicyIssue:
    attribute_key: str
    reason: str
    required_action: str

    def issue_contract(self) -> dict[str, object]:
        return {
            "attribute_key": self.attribute_key,
            "reason": self.reason,
            "required_action": self.required_action,
        }


@dataclass(frozen=True, slots=True)
class TelemetryCapturePolicyLintResult:
    issues: tuple[TelemetryCapturePolicyIssue, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "issues",
            tuple(sorted(self.issues, key=lambda issue: (issue.attribute_key, issue.reason))),
        )

    @property
    def passed(self) -> bool:
        return not self.issues

    def issue_contracts(self) -> list[dict[str, object]]:
        return [issue.issue_contract() for issue in self.issues]


@dataclass(frozen=True, slots=True)
class TelemetryCapturePolicyLinter:
    sensitive_attribute_keys: tuple[str, ...] = DEFAULT_SENSITIVE_TELEMETRY_ATTRIBUTE_KEYS
    content_attribute_keys: tuple[str, ...] = DEFAULT_CONTENT_TELEMETRY_ATTRIBUTE_KEYS

    def __post_init__(self) -> None:
        object.__setattr__(self, "sensitive_attribute_keys", tuple(sorted(set(self.sensitive_attribute_keys))))
        object.__setattr__(self, "content_attribute_keys", tuple(sorted(set(self.content_attribute_keys))))

    def lint_policy(self, policy: TelemetryCapturePolicy) -> TelemetryCapturePolicyLintResult:
        redacted = set(policy.redacted_attribute_keys)
        dropped = set(policy.dropped_attribute_keys)
        protected = redacted | dropped
        issues: list[TelemetryCapturePolicyIssue] = []
        for attribute_key in self.sensitive_attribute_keys:
            if attribute_key not in protected:
                issues.append(
                    TelemetryCapturePolicyIssue(
                        attribute_key=attribute_key,
                        reason="sensitive_attribute_not_protected",
                        required_action="redact_or_drop",
                    )
                )
        for attribute_key in self.content_attribute_keys:
            if attribute_key not in protected:
                issues.append(
                    TelemetryCapturePolicyIssue(
                        attribute_key=attribute_key,
                        reason="content_attribute_not_protected",
                        required_action="redact_or_drop",
                    )
                )
        if redacted and not policy.replacement.strip():
            for attribute_key in redacted:
                issues.append(
                    TelemetryCapturePolicyIssue(
                        attribute_key=attribute_key,
                        reason="redaction_replacement_empty",
                        required_action="set_non_empty_replacement",
                    )
                )
        return TelemetryCapturePolicyLintResult(tuple(issues))


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
    def completed(
        cls,
        *,
        exporter: str,
        record_ids: tuple[str, ...],
    ) -> TelemetryExportResult:
        return cls(
            exporter=exporter,
            status="completed",
            record_ids=record_ids,
            error_type=None,
            retryable=False,
            run_impact="none",
        )

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
class TelemetryDiagnosticBundleSection:
    name: str
    diagnostics: tuple[Diagnostic, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise TelemetryProjectionError("telemetry diagnostic section name must be a non-empty string")
        if isinstance(self.diagnostics, Diagnostic):
            raise TelemetryProjectionError("telemetry diagnostic section diagnostics must be a collection")
        try:
            diagnostics = tuple(self.diagnostics)
        except TypeError as error:
            raise TelemetryProjectionError("telemetry diagnostic section diagnostics must be a collection") from error
        if any(not isinstance(diagnostic, Diagnostic) for diagnostic in diagnostics):
            raise TelemetryProjectionError("telemetry diagnostic section diagnostics must be Diagnostic entries")
        object.__setattr__(
            self,
            "diagnostics",
            tuple(
                sorted(
                    diagnostics,
                    key=lambda diagnostic: (
                        diagnostic.severity,
                        diagnostic.code,
                        diagnostic.path,
                        diagnostic.message,
                    ),
                )
            ),
        )

    @property
    def ok(self) -> bool:
        return not any(diagnostic.severity == "error" for diagnostic in self.diagnostics)

    def summary(self) -> dict[Severity, int]:
        return _diagnostic_summary(self.diagnostics)

    def section_contract(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "summary": self.summary(),
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class TelemetryDiagnosticBundle:
    bundle_id: str
    sections: tuple[TelemetryDiagnosticBundleSection, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.bundle_id, str) or not self.bundle_id.strip():
            raise TelemetryProjectionError("telemetry diagnostic bundle_id must be a non-empty string")
        if isinstance(self.sections, TelemetryDiagnosticBundleSection):
            raise TelemetryProjectionError("telemetry diagnostic bundle sections must be a collection")
        try:
            sections = tuple(self.sections)
        except TypeError as error:
            raise TelemetryProjectionError("telemetry diagnostic bundle sections must be a collection") from error
        if any(not isinstance(section, TelemetryDiagnosticBundleSection) for section in sections):
            raise TelemetryProjectionError("telemetry diagnostic bundle sections must be section entries")
        object.__setattr__(self, "sections", tuple(sorted(sections, key=lambda section: section.name)))

    @property
    def ok(self) -> bool:
        return all(section.ok for section in self.sections)

    def summary(self) -> dict[Severity, int]:
        summary: dict[Severity, int] = {"error": 0, "warning": 0, "info": 0}
        for section in self.sections:
            for severity, count in section.summary().items():
                summary[severity] += count
        return summary

    def bundle_contract(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "ok": self.ok,
            "summary": self.summary(),
            "sections": [section.section_contract() for section in self.sections],
        }


def telemetry_diagnostic_bundle(
    bundle_id: str,
    *,
    capture_policy_result: TelemetryCapturePolicyLintResult | None = None,
    metric_cardinality_result: MetricCardinalityLintResult | None = None,
    export_results: Iterable[TelemetryExportResult] = (),
) -> TelemetryDiagnosticBundle:
    sections: list[TelemetryDiagnosticBundleSection] = []
    if capture_policy_result is not None:
        sections.append(
            TelemetryDiagnosticBundleSection(
                "capture_policy",
                tuple(_capture_policy_diagnostics(capture_policy_result)),
            )
        )
    if metric_cardinality_result is not None:
        sections.append(
            TelemetryDiagnosticBundleSection(
                "metric_cardinality",
                tuple(_metric_cardinality_diagnostics(metric_cardinality_result)),
            )
        )
    export_diagnostics = tuple(_export_result_diagnostics(export_results))
    if export_diagnostics:
        sections.append(TelemetryDiagnosticBundleSection("exporters", export_diagnostics))
    return TelemetryDiagnosticBundle(bundle_id, tuple(sections))


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


def _capture_policy_diagnostics(result: TelemetryCapturePolicyLintResult) -> Iterable[Diagnostic]:
    for issue in result.issues:
        yield Diagnostic(
            code=f"TelemetryCapturePolicy.{issue.reason}",
            severity="error",
            path=f"$.capturePolicy.attributes.{issue.attribute_key}",
            message=(
                f"Telemetry attribute {issue.attribute_key!r} failed capture-policy lint; "
                f"required action: {issue.required_action}"
            ),
        )


def _metric_cardinality_diagnostics(result: MetricCardinalityLintResult) -> Iterable[Diagnostic]:
    for issue in result.issues:
        yield Diagnostic(
            code=f"TelemetryMetricCardinality.{issue.reason}",
            severity="warning",
            path=f"$.metrics.{issue.metric_name}.labels.{issue.label}",
            message=(
                f"Telemetry metric {issue.metric_name!r} label {issue.label!r} observed "
                f"{issue.distinct_values} distinct value(s); limit: {issue.limit}"
            ),
        )


def _export_result_diagnostics(results: Iterable[TelemetryExportResult]) -> Iterable[Diagnostic]:
    for result in results:
        if result.status == "completed":
            continue
        yield Diagnostic(
            code=f"TelemetryExport.{result.status}",
            severity="warning",
            path=f"$.exporters.{result.exporter}",
            message=(
                f"Telemetry exporter {result.exporter!r} reported status {result.status!r} "
                f"for {len(result.record_ids)} record(s); retryable: {result.retryable}; "
                f"error_type: {result.error_type or 'none'}"
            ),
        )


__all__ = [
    "DEFAULT_BLOCKED_METRIC_LABELS",
    "DEFAULT_CONTENT_TELEMETRY_ATTRIBUTE_KEYS",
    "DEFAULT_SENSITIVE_TELEMETRY_ATTRIBUTE_KEYS",
    "GenerationTelemetryRecord",
    "MetricCardinalityIssue",
    "MetricCardinalityLintResult",
    "MetricCardinalityLinter",
    "OutputPolicyTelemetryRecord",
    "TelemetryCapturePolicy",
    "TelemetryCapturePolicyIssue",
    "TelemetryCapturePolicyLintResult",
    "TelemetryCapturePolicyLinter",
    "TelemetryDiagnosticBundle",
    "TelemetryDiagnosticBundleSection",
    "TelemetryExportResult",
    "TelemetryProjectionError",
    "ToolExecutionTelemetryRecord",
    "capture_native_telemetry_content",
    "telemetry_diagnostic_bundle",
]

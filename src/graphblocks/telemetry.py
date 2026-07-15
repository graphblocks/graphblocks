from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
import json
from types import MappingProxyType

from graphblocks.canonical import canonical_dumps, canonical_hash
from graphblocks.diagnostics import Diagnostic, Severity
from graphblocks.output_policy import (
    VALID_DRAFT_DISPOSITIONS,
    VALID_OUTPUT_DISPOSITIONS,
    VALID_OUTPUT_DURABLE_RESULTS,
    VALID_PENDING_TOOL_CALLS_DISPOSITIONS,
    VALID_TERMINAL_REASONS,
)
from graphblocks.policy import VALID_ENFORCEMENT_POINTS
from graphblocks.tools import (
    VALID_TOOL_CALL_STATUSES,
    VALID_TOOL_EFFECT_OUTCOMES,
    VALID_TOOL_EFFECTS,
    VALID_TOOL_RESULT_MODES,
    VALID_TOOL_RESULT_STATUSES,
)


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
    "access_token",
    "api_key",
    "authorization",
    "bearer_token",
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

_TOKEN_USAGE_ATTRIBUTE_KEYS = frozenset(
    {
        "cachedtokencount",
        "cachedtokens",
        "completiontokencount",
        "completiontokens",
        "inputtokencount",
        "inputtokens",
        "outputtokencount",
        "outputtokens",
        "prompttokencount",
        "prompttokens",
        "reasoningtokencount",
        "reasoningtokens",
        "tokencount",
        "totaltokencount",
        "totaltokens",
    }
)


class TelemetryProjectionError(RuntimeError):
    pass


class TelemetryExportConflictError(TelemetryProjectionError):
    pass


class TelemetryCorrectnessViolation(TelemetryProjectionError):
    pass


def _normalized_attribute_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _attribute_key_matches(key: str, protected_keys: Iterable[str]) -> bool:
    normalized_key = _normalized_attribute_key(key)
    for protected_key in protected_keys:
        normalized_protected_key = _normalized_attribute_key(protected_key)
        if not normalized_protected_key:
            continue
        if normalized_key == normalized_protected_key:
            return True
        if normalized_protected_key == "token" and normalized_key in _TOKEN_USAGE_ATTRIBUTE_KEYS:
            continue
        if (
            normalized_protected_key in {"completion", "input", "output", "prompt"}
            and normalized_key in _TOKEN_USAGE_ATTRIBUTE_KEYS
        ):
            continue
        if normalized_protected_key in normalized_key:
            return True
    return False


def _require_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TelemetryProjectionError(f"{owner} {field_name} must be a non-empty string")
    return value


def _require_literal(owner: str, field_name: str, value: object, valid_values: frozenset[str]) -> str:
    value = _require_non_empty_string(owner, field_name, value)
    if value not in valid_values:
        raise TelemetryProjectionError(f"{owner} {field_name} has invalid value {value!r}")
    return value


def _optional_literal(
    owner: str,
    field_name: str,
    value: object | None,
    valid_values: frozenset[str],
) -> str | None:
    if value is None:
        return None
    return _require_literal(owner, field_name, value, valid_values)


def _optional_non_negative_integer(owner: str, field_name: str, value: object | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise TelemetryProjectionError(f"{owner} {field_name} must be an integer")
    if value < 0:
        raise TelemetryProjectionError(f"{owner} {field_name} must be non-negative")
    return value


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
        for field_name in ("record_id", "run_id", "span_id", "node_id", "provider", "model"):
            object.__setattr__(
                self,
                field_name,
                _require_non_empty_string(
                    "generation telemetry record",
                    field_name,
                    getattr(self, field_name),
                ),
            )
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
        for field_name in ("record_id", "run_id", "stream_id", "response_id"):
            object.__setattr__(
                self,
                field_name,
                _require_non_empty_string(
                    "output policy telemetry record",
                    field_name,
                    getattr(self, field_name),
                ),
            )
        object.__setattr__(
            self,
            "enforcement_point",
            _require_literal(
                "output policy telemetry record",
                "enforcement_point",
                self.enforcement_point,
                VALID_ENFORCEMENT_POINTS,
            ),
        )
        object.__setattr__(
            self,
            "disposition",
            _require_literal(
                "output policy telemetry record",
                "disposition",
                self.disposition,
                VALID_OUTPUT_DISPOSITIONS,
            ),
        )
        object.__setattr__(
            self,
            "terminal_reason",
            _optional_literal(
                "output policy telemetry record",
                "terminal_reason",
                self.terminal_reason,
                VALID_TERMINAL_REASONS,
            ),
        )
        object.__setattr__(
            self,
            "draft_disposition",
            _optional_literal(
                "output policy telemetry record",
                "draft_disposition",
                self.draft_disposition,
                VALID_DRAFT_DISPOSITIONS,
            ),
        )
        object.__setattr__(
            self,
            "pending_tool_calls",
            _optional_literal(
                "output policy telemetry record",
                "pending_tool_calls",
                self.pending_tool_calls,
                VALID_PENDING_TOOL_CALLS_DISPOSITIONS,
            ),
        )
        object.__setattr__(
            self,
            "durable_result",
            _optional_literal(
                "output policy telemetry record",
                "durable_result",
                self.durable_result,
                VALID_OUTPUT_DURABLE_RESULTS,
            ),
        )
        object.__setattr__(
            self,
            "accepted_through_sequence",
            _optional_non_negative_integer(
                "output policy telemetry record",
                "accepted_through_sequence",
                self.accepted_through_sequence,
            ),
        )
        object.__setattr__(
            self,
            "last_client_delivered_sequence",
            _optional_non_negative_integer(
                "output policy telemetry record",
                "last_client_delivered_sequence",
                self.last_client_delivered_sequence,
            ),
        )
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
        for field_name in ("record_id", "run_id", "tool_call_id", "tool_name"):
            object.__setattr__(
                self,
                field_name,
                _require_non_empty_string(
                    "tool execution telemetry record",
                    field_name,
                    getattr(self, field_name),
                ),
            )
        object.__setattr__(
            self,
            "status",
            _require_literal(
                "tool execution telemetry record",
                "status",
                self.status,
                VALID_TOOL_CALL_STATUSES | VALID_TOOL_RESULT_STATUSES,
            ),
        )
        object.__setattr__(
            self,
            "result_mode",
            _optional_literal(
                "tool execution telemetry record",
                "result_mode",
                self.result_mode,
                VALID_TOOL_RESULT_MODES,
            ),
        )
        object.__setattr__(
            self,
            "effect_outcome",
            _optional_literal(
                "tool execution telemetry record",
                "effect_outcome",
                self.effect_outcome,
                VALID_TOOL_EFFECT_OUTCOMES,
            ),
        )
        object.__setattr__(
            self,
            "duration_ms",
            _optional_non_negative_integer(
                "tool execution telemetry record",
                "duration_ms",
                self.duration_ms,
            ),
        )
        effects = tuple(sorted(str(effect) for effect in self.effects))
        for effect in effects:
            if effect not in VALID_TOOL_EFFECTS:
                raise TelemetryProjectionError(
                    f"tool execution telemetry record effects has invalid value {effect!r}"
                )
        object.__setattr__(self, "effects", effects)
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


TelemetryRecord = GenerationTelemetryRecord | OutputPolicyTelemetryRecord | ToolExecutionTelemetryRecord


@dataclass(frozen=True, slots=True)
class TelemetryCorrectnessSnapshot:
    state_json: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.state_json, str):
            raise TelemetryProjectionError("telemetry correctness snapshot state_json must be a string")
        try:
            state = json.loads(
                self.state_json,
                parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
            )
        except ValueError as error:
            raise TelemetryProjectionError(
                "telemetry correctness snapshot state_json must be valid strict JSON"
            ) from error
        if not isinstance(state, Mapping):
            raise TelemetryProjectionError("telemetry correctness snapshot must contain an object")
        expected_sections = {
            "audit_log",
            "budget_ledger",
            "execution_journal",
            "usage_ledger",
        }
        if set(state) != expected_sections:
            raise TelemetryProjectionError(
                "telemetry correctness snapshot must contain execution_journal, audit_log, "
                "usage_ledger, and budget_ledger"
            )
        object.__setattr__(self, "state_json", canonical_dumps(state))

    @classmethod
    def capture(
        cls,
        *,
        execution_journal: object,
        audit_log: object,
        usage_ledger: object,
        budget_ledger: object,
    ) -> TelemetryCorrectnessSnapshot:
        try:
            state_json = canonical_dumps(
                {
                    "execution_journal": execution_journal,
                    "audit_log": audit_log,
                    "usage_ledger": usage_ledger,
                    "budget_ledger": budget_ledger,
                }
            )
        except (TypeError, ValueError) as error:
            raise TelemetryProjectionError(
                "telemetry correctness snapshot values must be valid strict JSON"
            ) from error
        return cls(state_json)

    @property
    def digest(self) -> str:
        return canonical_hash(self.snapshot_contract())

    def snapshot_contract(self) -> dict[str, object]:
        state = json.loads(self.state_json)
        if not isinstance(state, dict):
            raise TelemetryProjectionError("telemetry correctness snapshot must contain an object")
        return state


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
        return {
            key: self.replacement
            if _attribute_key_matches(key, self.redacted_attribute_keys)
            else value
            for key, value in attributes.items()
            if not _attribute_key_matches(key, self.dropped_attribute_keys)
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
        issues: list[TelemetryCapturePolicyIssue] = []
        for attribute_key in self.sensitive_attribute_keys:
            if not (
                _attribute_key_matches(attribute_key, policy.redacted_attribute_keys)
                or _attribute_key_matches(attribute_key, policy.dropped_attribute_keys)
            ):
                issues.append(
                    TelemetryCapturePolicyIssue(
                        attribute_key=attribute_key,
                        reason="sensitive_attribute_not_protected",
                        required_action="redact_or_drop",
                    )
                )
        for attribute_key in self.content_attribute_keys:
            if not (
                _attribute_key_matches(attribute_key, policy.redacted_attribute_keys)
                or _attribute_key_matches(attribute_key, policy.dropped_attribute_keys)
            ):
                issues.append(
                    TelemetryCapturePolicyIssue(
                        attribute_key=attribute_key,
                        reason="content_attribute_not_protected",
                        required_action="redact_or_drop",
                    )
                )
        if policy.redacted_attribute_keys and not policy.replacement.strip():
            for attribute_key in policy.redacted_attribute_keys:
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
class TelemetryExportEvaluation:
    exporter: str
    attempt: int
    result: TelemetryExportResult
    correctness_before: TelemetryCorrectnessSnapshot
    correctness_after: TelemetryCorrectnessSnapshot
    accepted_record_ids: tuple[str, ...]
    delivered_record_ids: tuple[str, ...]
    pending_record_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "exporter",
            _require_non_empty_string("telemetry export evaluation", "exporter", self.exporter),
        )
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool) or self.attempt <= 0:
            raise TelemetryProjectionError("telemetry export evaluation attempt must be a positive integer")
        if self.result.exporter != self.exporter:
            raise TelemetryProjectionError("telemetry export evaluation exporter must match its result")
        for field_name in ("accepted_record_ids", "delivered_record_ids", "pending_record_ids"):
            record_ids = tuple(getattr(self, field_name))
            if len(record_ids) != len(set(record_ids)):
                raise TelemetryProjectionError(
                    f"telemetry export evaluation {field_name} must not contain duplicates"
                )
            object.__setattr__(self, field_name, record_ids)

    @property
    def correctness_preserved(self) -> bool:
        return self.correctness_before == self.correctness_after

    def evaluation_contract(self) -> dict[str, object]:
        return {
            "exporter": self.exporter,
            "attempt": self.attempt,
            "result": self.result.result_contract(),
            "correctness_preserved": self.correctness_preserved,
            "correctness_before_digest": self.correctness_before.digest,
            "correctness_after_digest": self.correctness_after.digest,
            "accepted_record_ids": list(self.accepted_record_ids),
            "delivered_record_ids": list(self.delivered_record_ids),
            "pending_record_ids": list(self.pending_record_ids),
        }


@dataclass(slots=True)
class TelemetryExportOutbox:
    _records: dict[str, TelemetryRecord] = field(default_factory=dict, init=False, repr=False)
    _record_contracts: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _delivered_by_exporter: dict[str, set[str]] = field(default_factory=dict, init=False, repr=False)
    _attempts_by_exporter: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    @property
    def accepted_record_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._records))

    def accept(self, records: Iterable[TelemetryRecord]) -> tuple[str, ...]:
        try:
            candidates = tuple(records)
        except TypeError as error:
            raise TelemetryProjectionError("telemetry export outbox records must be iterable") from error
        staged_records: dict[str, TelemetryRecord] = {}
        staged_contracts: dict[str, str] = {}
        for record in candidates:
            if not isinstance(
                record,
                GenerationTelemetryRecord | OutputPolicyTelemetryRecord | ToolExecutionTelemetryRecord,
            ):
                raise TelemetryProjectionError(
                    "telemetry export outbox accepts generation, output-policy, or tool-execution records"
                )
            try:
                contract_json = canonical_dumps(record.observation_contract())
            except (TypeError, ValueError) as error:
                raise TelemetryProjectionError(
                    f"telemetry record {record.record_id!r} must have a strict JSON observation contract"
                ) from error
            existing_contract = self._record_contracts.get(record.record_id)
            if existing_contract is None:
                existing_contract = staged_contracts.get(record.record_id)
            if existing_contract is not None and existing_contract != contract_json:
                raise TelemetryExportConflictError(
                    f"telemetry record {record.record_id!r} conflicts with its accepted observation contract"
                )
            staged_records.setdefault(record.record_id, record)
            staged_contracts.setdefault(record.record_id, contract_json)
        self._records.update(staged_records)
        self._record_contracts.update(staged_contracts)
        return self.accepted_record_ids

    def pending_record_ids(self, exporter: str) -> tuple[str, ...]:
        exporter = _require_non_empty_string("telemetry export outbox", "exporter", exporter)
        delivered = self._delivered_by_exporter.get(exporter, set())
        return tuple(record_id for record_id in self.accepted_record_ids if record_id not in delivered)

    def attempt_export(
        self,
        exporter: str,
        export: Callable[[tuple[TelemetryRecord, ...]], object],
        *,
        correctness_probe: Callable[[], TelemetryCorrectnessSnapshot],
        retryable: bool = False,
    ) -> TelemetryExportEvaluation:
        exporter = _require_non_empty_string("telemetry export outbox", "exporter", exporter)
        if not callable(export):
            raise TelemetryProjectionError("telemetry export outbox export must be callable")
        if not callable(correctness_probe):
            raise TelemetryProjectionError("telemetry export outbox correctness_probe must be callable")
        if not isinstance(retryable, bool):
            raise TelemetryProjectionError("telemetry export outbox retryable must be a boolean")
        record_ids = self.pending_record_ids(exporter)
        records = tuple(self._records[record_id] for record_id in record_ids)
        for record in records:
            try:
                current_contract = canonical_dumps(record.observation_contract())
            except (TypeError, ValueError) as error:
                raise TelemetryExportConflictError(
                    f"telemetry record {record.record_id!r} changed after acceptance"
                ) from error
            if current_contract != self._record_contracts[record.record_id]:
                raise TelemetryExportConflictError(
                    f"telemetry record {record.record_id!r} changed after acceptance"
                )
        before = correctness_probe()
        if not isinstance(before, TelemetryCorrectnessSnapshot):
            raise TelemetryProjectionError(
                "telemetry export outbox correctness_probe must return TelemetryCorrectnessSnapshot"
            )
        self._attempts_by_exporter[exporter] = self._attempts_by_exporter.get(exporter, 0) + 1
        attempt = self._attempts_by_exporter[exporter]
        export_error: Exception | None = None
        if records:
            try:
                export(records)
            except Exception as error:
                export_error = error
        after = correctness_probe()
        if not isinstance(after, TelemetryCorrectnessSnapshot):
            raise TelemetryProjectionError(
                "telemetry export outbox correctness_probe must return TelemetryCorrectnessSnapshot"
            )
        if before != after:
            raise TelemetryCorrectnessViolation(
                f"telemetry exporter {exporter!r} changed authoritative durable state"
            ) from export_error
        if export_error is None:
            self._delivered_by_exporter.setdefault(exporter, set()).update(record_ids)
            result = TelemetryExportResult.completed(exporter=exporter, record_ids=record_ids)
        else:
            result = TelemetryExportResult.failed(
                exporter=exporter,
                record_ids=record_ids,
                error_type=type(export_error).__name__,
                retryable=retryable,
            )
        return TelemetryExportEvaluation(
            exporter=exporter,
            attempt=attempt,
            result=result,
            correctness_before=before,
            correctness_after=after,
            accepted_record_ids=self.accepted_record_ids,
            delivered_record_ids=tuple(sorted(self._delivered_by_exporter.get(exporter, set()))),
            pending_record_ids=self.pending_record_ids(exporter),
        )


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
    "TelemetryCorrectnessSnapshot",
    "TelemetryCorrectnessViolation",
    "TelemetryDiagnosticBundle",
    "TelemetryDiagnosticBundleSection",
    "TelemetryExportConflictError",
    "TelemetryExportEvaluation",
    "TelemetryExportOutbox",
    "TelemetryExportResult",
    "TelemetryProjectionError",
    "TelemetryRecord",
    "ToolExecutionTelemetryRecord",
    "VALID_DRAFT_DISPOSITIONS",
    "VALID_ENFORCEMENT_POINTS",
    "VALID_OUTPUT_DISPOSITIONS",
    "VALID_OUTPUT_DURABLE_RESULTS",
    "VALID_PENDING_TOOL_CALLS_DISPOSITIONS",
    "VALID_TERMINAL_REASONS",
    "VALID_TOOL_CALL_STATUSES",
    "VALID_TOOL_EFFECT_OUTCOMES",
    "VALID_TOOL_EFFECTS",
    "VALID_TOOL_RESULT_MODES",
    "VALID_TOOL_RESULT_STATUSES",
    "capture_native_telemetry_content",
    "telemetry_diagnostic_bundle",
]

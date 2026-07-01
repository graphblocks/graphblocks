from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from .canonical import canonical_hash
from .diagnostics import Diagnostic
from .documents import ArtifactRef
from .policy import PrincipalRef
from .tools import ResolvedTool


CheckStatus = Literal["passed", "failed", "error", "timeout", "inconclusive", "skipped"]
MetricDirection = Literal["minimize", "maximize", "target", "informational"]
GateDecision = Literal["pass", "fail", "inconclusive"]
ReviewDecision = Literal["accept", "accept_with_conditions", "revise", "reject"]
ConstraintOperator = Literal["at_least", "at_most", "equals"]
SloComparison = Literal["at_least", "at_most"]
SloReportStatus = Literal["pass", "fail", "no_data"]


VALID_CHECK_STATUSES = frozenset(("passed", "failed", "error", "timeout", "inconclusive", "skipped"))
VALID_METRIC_DIRECTIONS = frozenset(("minimize", "maximize", "target", "informational"))
VALID_GATE_DECISIONS = frozenset(("pass", "fail", "inconclusive"))
VALID_CONSTRAINT_OPERATORS = frozenset(("at_least", "at_most", "equals"))
VALID_REVIEW_DECISIONS = frozenset(("accept", "accept_with_conditions", "revise", "reject"))


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    return value


def _parse_datetime(owner: str, field_name: str, value: object) -> datetime:
    normalized = _validate_non_empty_string(owner, field_name, value).strip()
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{owner} {field_name} must be an ISO datetime") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_string_list(owner: str, field_name: str, values: object) -> list[str]:
    if isinstance(values, str):
        raise ValueError(f"{owner} {field_name} must be a collection of strings")
    try:
        normalized = list(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{owner} {field_name} must be a collection of strings") from error
    for item in normalized:
        if not isinstance(item, str):
            raise ValueError(f"{owner} {field_name} items must be strings")
        if not item.strip():
            raise ValueError(f"{owner} {field_name} item must not be empty")
    return list(normalized)


def _validate_record_list(owner: str, field_name: str, values: object, item_type: type) -> list[object]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{owner} {field_name} must be a collection")
    try:
        normalized = list(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{owner} {field_name} must be a collection") from error
    for item in normalized:
        if not isinstance(item, item_type):
            raise ValueError(f"{owner} {field_name} items must be {item_type.__name__}")
    return list(normalized)


def _copy_mapping(owner: str, field_name: str, value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{owner} {field_name} must be a mapping")
    mapping = dict(value)
    for key in mapping:
        if not isinstance(key, str):
            raise ValueError(f"{owner} {field_name} keys must be strings")
        if not key.strip():
            raise ValueError(f"{owner} {field_name} key must not be empty")
    return mapping


@dataclass(frozen=True, slots=True)
class ResourceSnapshotRef:
    resource_id: str
    digest: str
    resource_kind: str | None = None
    uri: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("resource_id", "digest"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ValueError(f"resource snapshot {field_name} must be a string")
            if not value.strip():
                raise ValueError(f"resource snapshot {field_name} must not be empty")
        for field_name in ("resource_kind", "uri"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"resource snapshot {field_name} must be a string")
            if value is not None and not value.strip():
                raise ValueError(f"resource snapshot {field_name} must not be empty")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("resource snapshot metadata must be a mapping")
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    source: ResourceSnapshotRef | ArtifactRef
    evidence_kind: str
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("evidence ref", "evidence_id", self.evidence_id)
        if not isinstance(self.source, (ResourceSnapshotRef, ArtifactRef)):
            raise ValueError("evidence ref source must be a ResourceSnapshotRef or ArtifactRef")
        _validate_non_empty_string("evidence ref", "evidence_kind", self.evidence_kind)
        object.__setattr__(self, "metadata", _copy_mapping("evidence ref", "metadata", self.metadata))


@dataclass(frozen=True, slots=True)
class TypedValueRef:
    value_id: str
    schema_id: str
    schema_version: int
    digest: str
    encoding: str = "json"
    artifact: ArtifactRef | None = None

    def __post_init__(self) -> None:
        _validate_non_empty_string("typed value ref", "value_id", self.value_id)
        _validate_non_empty_string("typed value ref", "schema_id", self.schema_id)
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            raise ValueError("typed value ref schema_version must be an integer")
        if self.schema_version <= 0:
            raise ValueError("typed value ref schema_version must be positive")
        _validate_non_empty_string("typed value ref", "digest", self.digest)
        _validate_non_empty_string("typed value ref", "encoding", self.encoding)
        if self.artifact is not None and not isinstance(self.artifact, ArtifactRef):
            raise ValueError("typed value ref artifact must be an ArtifactRef")


@dataclass(frozen=True, slots=True, order=True)
class ModelVisibleToolRef:
    tool_name: str
    resolved_tool_id: str
    definition_digest: str
    binding_digest: str
    effective_policy_snapshot_id: str
    allowed_for_principal: bool
    valid_until: str | None = None


@dataclass(frozen=True, slots=True)
class RunProvenance:
    graph_hash: str
    started_at: str
    completed_at: str | None = None
    release_id: str | None = None
    deployment_revision_id: str | None = None
    physical_plan_hash: str | None = None
    release_signature_digest: str | None = None
    model_visible_tools: tuple[ModelVisibleToolRef, ...] = field(default_factory=tuple)
    runner: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_visible_tools", tuple(sorted(self.model_visible_tools)))

    def with_model_visible_tools(self, tools: Iterable[ResolvedTool]) -> RunProvenance:
        return replace(
            self,
            model_visible_tools=tuple(
                ModelVisibleToolRef(
                    tool_name=tool.definition.name,
                    resolved_tool_id=tool.resolved_tool_id,
                    definition_digest=tool.definition_digest,
                    binding_digest=tool.binding_digest,
                    effective_policy_snapshot_id=tool.effective_policy_snapshot_id,
                    allowed_for_principal=tool.allowed_for_principal,
                    valid_until=tool.valid_until,
                )
                for tool in tools
            ),
        )

    def with_release(self, release_id: str, deployment_revision_id: str) -> RunProvenance:
        return replace(self, release_id=release_id, deployment_revision_id=deployment_revision_id)

    def with_physical_plan_hash(self, physical_plan_hash: str) -> RunProvenance:
        return replace(self, physical_plan_hash=physical_plan_hash)

    def with_release_signature_digest(self, release_signature_digest: str) -> RunProvenance:
        return replace(self, release_signature_digest=release_signature_digest)


@dataclass(frozen=True, slots=True)
class ChangeSet:
    change_set_id: str
    base: ResourceSnapshotRef
    candidate: ResourceSnapshotRef
    operations: tuple[dict[str, object], ...] = field(default_factory=tuple)
    summary: str | None = None

    def __post_init__(self) -> None:
        operations: list[dict[str, object]] = []
        try:
            raw_operations = tuple(self.operations)
        except TypeError as error:
            raise ValueError("change set operations must be mappings") from error
        for operation in raw_operations:
            if not isinstance(operation, Mapping):
                raise ValueError("change set operations must be mappings")
            operations.append(dict(operation))
        object.__setattr__(self, "operations", tuple(operations))


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: str
    subject: ResourceSnapshotRef
    status: CheckStatus
    diagnostics: list[Diagnostic] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    tool: dict[str, object] = field(default_factory=dict)
    environment: ResourceSnapshotRef | None = None

    def __post_init__(self) -> None:
        _validate_non_empty_string("check result", "check_id", self.check_id)
        if not isinstance(self.subject, ResourceSnapshotRef):
            raise ValueError("check result subject must be a ResourceSnapshotRef")
        if self.status not in VALID_CHECK_STATUSES:
            raise ValueError(f"invalid check status {self.status}")
        object.__setattr__(
            self,
            "diagnostics",
            _validate_record_list("check result", "diagnostics", self.diagnostics, Diagnostic),
        )
        object.__setattr__(
            self,
            "evidence",
            _validate_record_list("check result", "evidence", self.evidence, EvidenceRef),
        )
        object.__setattr__(
            self,
            "artifacts",
            _validate_record_list("check result", "artifacts", self.artifacts, ArtifactRef),
        )
        object.__setattr__(self, "tool", _copy_mapping("check result", "tool", self.tool))
        if self.environment is not None and not isinstance(self.environment, ResourceSnapshotRef):
            raise ValueError("check result environment must be a ResourceSnapshotRef")


@dataclass(frozen=True, slots=True)
class MetricObservation:
    name: str
    value: Decimal | bool | str | None
    unit: str | None = None
    direction: MetricDirection = "informational"
    baseline_value: Decimal | None = None
    subject: ResourceSnapshotRef | None = None
    evaluator: dict[str, object] | None = None

    def __post_init__(self) -> None:
        _validate_non_empty_string("metric observation", "name", self.name)
        if self.unit is not None:
            _validate_non_empty_string("metric observation", "unit", self.unit)
        if self.direction not in VALID_METRIC_DIRECTIONS:
            raise ValueError(f"invalid metric direction {self.direction}")
        if isinstance(self.value, float):
            object.__setattr__(self, "value", Decimal(str(self.value)))
        if self.baseline_value is not None and not isinstance(self.baseline_value, Decimal):
            object.__setattr__(self, "baseline_value", Decimal(str(self.baseline_value)))
        if self.subject is not None and not isinstance(self.subject, ResourceSnapshotRef):
            raise ValueError("metric observation subject must be a ResourceSnapshotRef")
        if self.evaluator is not None:
            object.__setattr__(self, "evaluator", _copy_mapping("metric observation", "evaluator", self.evaluator))


@dataclass(frozen=True, slots=True)
class GateConstraint:
    metric_name: str
    operator: ConstraintOperator
    threshold: Decimal | bool | str

    def __post_init__(self) -> None:
        _validate_non_empty_string("gate constraint", "metric_name", self.metric_name)
        if self.operator not in VALID_CONSTRAINT_OPERATORS:
            raise ValueError(f"invalid gate constraint operator {self.operator}")
        if isinstance(self.threshold, (int, float)) and not isinstance(self.threshold, bool):
            object.__setattr__(self, "threshold", Decimal(str(self.threshold)))


@dataclass(frozen=True, slots=True)
class GateResult:
    gate_id: str
    subject: ResourceSnapshotRef
    decision: GateDecision
    check_ids: list[str] = field(default_factory=list)
    violated_constraints: list[str] = field(default_factory=list)
    metrics: list[MetricObservation] = field(default_factory=list)

    def __post_init__(self) -> None:
        _validate_non_empty_string("gate result", "gate_id", self.gate_id)
        if not isinstance(self.subject, ResourceSnapshotRef):
            raise ValueError("gate result subject must be a ResourceSnapshotRef")
        if self.decision not in VALID_GATE_DECISIONS:
            raise ValueError(f"invalid gate decision {self.decision}")
        object.__setattr__(self, "check_ids", _validate_string_list("gate result", "check_ids", self.check_ids))
        object.__setattr__(
            self,
            "violated_constraints",
            _validate_string_list("gate result", "violated_constraints", self.violated_constraints),
        )
        object.__setattr__(
            self,
            "metrics",
            _validate_record_list("gate result", "metrics", self.metrics, MetricObservation),
        )
    policy_ref: str | None = None


@dataclass(frozen=True, slots=True)
class SloObjective:
    slo_id: str
    indicator: str
    comparison: SloComparison
    objective: float
    window: str
    unit: str | None = None

    def __post_init__(self) -> None:
        if self.comparison not in {"at_least", "at_most"}:
            raise ValueError(f"unsupported SLO comparison {self.comparison!r}")
        object.__setattr__(self, "objective", float(self.objective))

    @classmethod
    def at_least(cls, slo_id: str, indicator: str, objective: float, window: str) -> SloObjective:
        return cls(slo_id=slo_id, indicator=indicator, comparison="at_least", objective=objective, window=window)

    @classmethod
    def at_most(cls, slo_id: str, indicator: str, objective: float, window: str) -> SloObjective:
        return cls(slo_id=slo_id, indicator=indicator, comparison="at_most", objective=objective, window=window)

    def with_unit(self, unit: str) -> SloObjective:
        return replace(self, unit=unit)

    def evaluate(self, measurement: SloMeasurement) -> SloReport:
        for reason, mismatched in (
            ("indicator_mismatch", self.indicator != measurement.indicator),
            ("window_mismatch", self.window != measurement.window),
            ("unit_mismatch", self.unit != measurement.unit),
        ):
            if mismatched:
                return SloReport(
                    slo_id=self.slo_id,
                    indicator=self.indicator,
                    window=self.window,
                    status="no_data",
                    objective=self.objective,
                    reason=reason,
                )

        passes = (
            measurement.value >= self.objective
            if self.comparison == "at_least"
            else measurement.value <= self.objective
        )
        violated_by = None
        if not passes:
            violated_by = (
                self.objective - measurement.value
                if self.comparison == "at_least"
                else measurement.value - self.objective
            )
        return SloReport(
            slo_id=self.slo_id,
            indicator=self.indicator,
            window=self.window,
            status="pass" if passes else "fail",
            objective=self.objective,
            observed_value=measurement.value,
            sample_count=measurement.sample_count,
            violated_by=violated_by,
        )


@dataclass(frozen=True, slots=True)
class SloMeasurement:
    indicator: str
    value: float
    window: str
    unit: str | None = None
    sample_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", float(self.value))
        if self.sample_count is not None and self.sample_count < 0:
            raise ValueError("SLO sample_count must be non-negative")

    def with_unit(self, unit: str) -> SloMeasurement:
        return replace(self, unit=unit)

    def with_sample_count(self, sample_count: int) -> SloMeasurement:
        return replace(self, sample_count=sample_count)


@dataclass(frozen=True, slots=True)
class SloReport:
    slo_id: str
    indicator: str
    window: str
    status: SloReportStatus
    objective: float
    observed_value: float | None = None
    sample_count: int | None = None
    violated_by: float | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class TrialResult:
    trial_id: str
    base: ResourceSnapshotRef
    candidate: ResourceSnapshotRef
    change_set: ChangeSet | None = None
    checks: list[CheckResult] = field(default_factory=list)
    metrics: list[MetricObservation] = field(default_factory=list)
    gate: GateResult | None = None
    usage: list[str] = field(default_factory=list)
    outcome: str = ""


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    review_id: str
    subject: ResourceSnapshotRef
    subject_digest: str
    scope: str
    reviewer: PrincipalRef
    decision: ReviewDecision
    comments: list[str] = field(default_factory=list)
    credential_refs: list[str] = field(default_factory=list)
    created_at: str = ""
    invalidated_at: str | None = None

    def __post_init__(self) -> None:
        _validate_non_empty_string("review record", "review_id", self.review_id)
        if not isinstance(self.subject, ResourceSnapshotRef):
            raise ValueError("review record subject must be a ResourceSnapshotRef")
        _validate_non_empty_string("review record", "subject_digest", self.subject_digest)
        _validate_non_empty_string("review record", "scope", self.scope)
        if not isinstance(self.reviewer, PrincipalRef):
            raise ValueError("review record reviewer must be a PrincipalRef")
        if self.decision not in VALID_REVIEW_DECISIONS:
            raise ValueError(f"invalid review decision {self.decision}")
        _parse_datetime("review record", "created_at", self.created_at)
        if self.invalidated_at is not None:
            invalidated_at = _parse_datetime("review record", "invalidated_at", self.invalidated_at)
            if invalidated_at < _parse_datetime("review record", "created_at", self.created_at):
                raise ValueError("review record invalidated_at must not be before created_at")
        object.__setattr__(self, "comments", _validate_string_list("review record", "comments", self.comments))
        object.__setattr__(
            self,
            "credential_refs",
            _validate_string_list("review record", "credential_refs", self.credential_refs),
        )

    def is_valid_for(self, subject: ResourceSnapshotRef) -> bool:
        return self.invalidated_at is None and self.subject.resource_id == subject.resource_id and self.subject_digest == subject.digest

    def invalidate(self, invalidated_at: str) -> ReviewRecord:
        return replace(self, invalidated_at=invalidated_at)


@dataclass(frozen=True, slots=True)
class ResultBundle:
    bundle_id: str
    run_id: str
    release_id: str
    inputs: list[ResourceSnapshotRef]
    outputs: list[TypedValueRef]
    deployment_revision_id: str | None = None
    artifacts: list[ArtifactRef] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    metrics: list[MetricObservation] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    reviews: list[ReviewRecord] = field(default_factory=list)
    usage_records: list[str] = field(default_factory=list)
    policy_decision_refs: list[str] = field(default_factory=list)
    provenance: RunProvenance = field(default_factory=lambda: RunProvenance(graph_hash="", started_at=""))

    def content_digest(self) -> str:
        return canonical_hash(
            _canonical_value(
                {
                    "run_id": self.run_id,
                    "release_id": self.release_id,
                    "deployment_revision_id": self.deployment_revision_id,
                    "inputs": self.inputs,
                    "outputs": self.outputs,
                    "artifacts": self.artifacts,
                    "diagnostics": self.diagnostics,
                    "checks": self.checks,
                    "metrics": self.metrics,
                    "evidence": self.evidence,
                    "reviews": self.reviews,
                    "usage_records": self.usage_records,
                    "policy_decision_refs": self.policy_decision_refs,
                    "provenance": self.provenance,
                }
            )
        )


def evaluate_gate(
    gate_id: str,
    subject: ResourceSnapshotRef,
    *,
    checks: list[CheckResult] | None = None,
    metrics: list[MetricObservation] | None = None,
    required_check_ids: list[str] | None = None,
    constraints: list[GateConstraint] | None = None,
    policy_ref: str | None = None,
) -> GateResult:
    check_list = list(checks or [])
    metric_list = list(metrics or [])
    required = list(required_check_ids or [check.check_id for check in check_list])
    violated: list[str] = []

    checks_by_id = {check.check_id: check for check in check_list}
    for check_id in required:
        check = checks_by_id.get(check_id)
        if check is None or check.status != "passed":
            violated.append(f"check:{check_id}")

    metrics_by_name = {metric.name: metric for metric in metric_list}
    for constraint in constraints or []:
        metric = metrics_by_name.get(constraint.metric_name)
        if metric is None or not _metric_satisfies(metric.value, constraint.operator, constraint.threshold):
            violated.append(f"metric:{constraint.metric_name}")

    inconclusive = any(check.status in {"error", "timeout", "inconclusive"} for check in check_list)
    decision: GateDecision
    if violated:
        decision = "fail"
    elif inconclusive:
        decision = "inconclusive"
    else:
        decision = "pass"
    return GateResult(
        gate_id=gate_id,
        subject=subject,
        decision=decision,
        check_ids=required,
        violated_constraints=violated,
        metrics=metric_list,
        policy_ref=policy_ref,
    )


def _metric_satisfies(value: Decimal | bool | str | None, operator: ConstraintOperator, threshold: Decimal | bool | str) -> bool:
    if value is None:
        return False
    if operator == "equals":
        return value == threshold
    try:
        comparable_value = value if isinstance(value, Decimal) else Decimal(str(value))
        comparable_threshold = threshold if isinstance(threshold, Decimal) else Decimal(str(threshold))
    except Exception:
        return False
    if operator == "at_least":
        return comparable_value >= comparable_threshold
    return comparable_value <= comparable_threshold


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return _canonical_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value

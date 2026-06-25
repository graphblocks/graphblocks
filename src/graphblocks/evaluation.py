from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace, is_dataclass
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


@dataclass(frozen=True, slots=True)
class ResourceSnapshotRef:
    resource_id: str
    digest: str
    resource_kind: str | None = None
    uri: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    source: ResourceSnapshotRef | ArtifactRef
    evidence_kind: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TypedValueRef:
    value_id: str
    schema_id: str
    schema_version: int
    digest: str
    encoding: str = "json"
    artifact: ArtifactRef | None = None


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
    operations: list[dict[str, object]] = field(default_factory=list)
    summary: str | None = None


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
        if isinstance(self.value, float):
            object.__setattr__(self, "value", Decimal(str(self.value)))
        if self.baseline_value is not None and not isinstance(self.baseline_value, Decimal):
            object.__setattr__(self, "baseline_value", Decimal(str(self.baseline_value)))


@dataclass(frozen=True, slots=True)
class GateConstraint:
    metric_name: str
    operator: ConstraintOperator
    threshold: Decimal | bool | str

    def __post_init__(self) -> None:
        if isinstance(self.threshold, (int, float)):
            object.__setattr__(self, "threshold", Decimal(str(self.threshold)))


@dataclass(frozen=True, slots=True)
class GateResult:
    gate_id: str
    subject: ResourceSnapshotRef
    decision: GateDecision
    check_ids: list[str] = field(default_factory=list)
    violated_constraints: list[str] = field(default_factory=list)
    metrics: list[MetricObservation] = field(default_factory=list)
    policy_ref: str | None = None


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

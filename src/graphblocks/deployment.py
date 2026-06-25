from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Literal

from .canonical import canonical_dumps, canonical_hash


ExecutionTargetKind = Literal[
    "service",
    "worker_pool",
    "job_pool",
    "sandbox_pool",
    "stateful_service",
    "external",
]
WorkloadKind = Literal[
    "new_request",
    "existing_request",
    "conversation",
    "durable_job",
    "realtime_session",
]
RolloutStepKind = Literal["validate", "shadow", "canary", "blue_green", "promote"]
RolloutEffectsMode = Literal["normal", "suppress", "sandbox"]
RolloutDecisionKind = Literal["hold", "advance", "promote", "abort"]
RolloutStatus = Literal["running", "promoted", "aborted"]
DeploymentConditionStatus = Literal["true", "false", "unknown"]


@dataclass(frozen=True, slots=True)
class GraphReleaseGraph:
    graph_hash: str
    normalized_plan_hash: str

    def canonical_value(self) -> dict[str, str]:
        return {
            "graph_hash": self.graph_hash,
            "normalized_plan_hash": self.normalized_plan_hash,
        }


@dataclass(frozen=True, slots=True)
class ImageRef:
    image: str

    def canonical_value(self) -> dict[str, str]:
        return {"image": self.image}


@dataclass(frozen=True, slots=True)
class PromptLock:
    kind: Literal["versioned", "label"]
    name: str
    version: str | None = None
    lock_label: str | None = None

    @classmethod
    def versioned(cls, name: str, version: str) -> PromptLock:
        return cls(kind="versioned", name=name, version=version)

    @classmethod
    def label(cls, name: str, label: str) -> PromptLock:
        return cls(kind="label", name=name, lock_label=label)

    def canonical_value(self) -> dict[str, str | None]:
        if self.kind == "versioned":
            return {"kind": "versioned", "name": self.name, "version": self.version}
        return {"kind": "label", "name": self.name, "label": self.lock_label}


@dataclass(frozen=True, slots=True)
class KnowledgeBinding:
    index_id: str
    index_revision: str

    def canonical_value(self) -> dict[str, str]:
        return {
            "index_id": self.index_id,
            "index_revision": self.index_revision,
        }


@dataclass(frozen=True, slots=True)
class SupplyChainLock:
    sbom_ref: str | None = None
    provenance_ref: str | None = None
    signature_policy: str | None = None

    def canonical_value(self) -> dict[str, str | None]:
        return {
            "sbom_ref": self.sbom_ref,
            "provenance_ref": self.provenance_ref,
            "signature_policy": self.signature_policy,
        }


class GraphReleaseError(ValueError):
    """Base error for invalid graph release contracts."""


class GraphReleaseMutableReferencesError(GraphReleaseError):
    def __init__(self, references: list[str] | tuple[str, ...]) -> None:
        self.references = tuple(references)
        super().__init__(f"mutable release references: {self.references!r}")


class GraphDeploymentError(ValueError):
    """Base error for invalid graph deployment contracts."""


class RolloutError(ValueError):
    """Base error for rollout planning and gate decisions."""


@dataclass(frozen=True, slots=True)
class GraphRelease:
    name: str
    version: str
    bundle_digest: str | None = None
    bundle_media_type: str | None = None
    application_hash: str | None = None
    graphs: dict[str, GraphReleaseGraph] = field(default_factory=dict)
    images: dict[str, ImageRef] = field(default_factory=dict)
    prompt_locks: dict[str, PromptLock] = field(default_factory=dict)
    knowledge: dict[str, KnowledgeBinding] = field(default_factory=dict)
    supply_chain: SupplyChainLock | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "graphs", dict(self.graphs))
        object.__setattr__(self, "images", dict(self.images))
        object.__setattr__(self, "prompt_locks", dict(self.prompt_locks))
        object.__setattr__(self, "knowledge", dict(self.knowledge))

    def with_bundle(self, digest: str, media_type: str) -> GraphRelease:
        return replace(self, bundle_digest=digest, bundle_media_type=media_type)

    def with_application_hash(self, application_hash: str) -> GraphRelease:
        return replace(self, application_hash=application_hash)

    def with_graph(self, graph_name: str, graph: GraphReleaseGraph) -> GraphRelease:
        graphs = dict(self.graphs)
        graphs[graph_name] = graph
        return replace(self, graphs=graphs)

    def with_image(self, image_name: str, image: ImageRef) -> GraphRelease:
        images = dict(self.images)
        images[image_name] = image
        return replace(self, images=images)

    def with_prompt_lock(self, prompt_name: str, prompt_lock: PromptLock) -> GraphRelease:
        prompt_locks = dict(self.prompt_locks)
        prompt_locks[prompt_name] = prompt_lock
        return replace(self, prompt_locks=prompt_locks)

    def with_knowledge(self, binding: KnowledgeBinding) -> GraphRelease:
        knowledge = dict(self.knowledge)
        knowledge[binding.index_id] = binding
        return replace(self, knowledge=knowledge)

    def with_supply_chain(self, supply_chain: SupplyChainLock) -> GraphRelease:
        return replace(self, supply_chain=supply_chain)

    def content_digest(self) -> str:
        return canonical_hash(
            {
                "version": self.version,
                "bundle": {
                    "digest": self.bundle_digest,
                    "media_type": self.bundle_media_type,
                },
                "application_hash": self.application_hash,
                "graphs": {
                    name: graph.canonical_value()
                    for name, graph in sorted(self.graphs.items())
                },
                "images": {
                    name: image.canonical_value()
                    for name, image in sorted(self.images.items())
                },
                "prompt_locks": {
                    name: prompt.canonical_value()
                    for name, prompt in sorted(self.prompt_locks.items())
                },
                "knowledge": {
                    name: binding.canonical_value()
                    for name, binding in sorted(self.knowledge.items())
                },
                "supply_chain": (
                    self.supply_chain.canonical_value()
                    if self.supply_chain is not None
                    else None
                ),
            }
        )

    def validate_production_pins(self) -> None:
        references: list[str] = []
        if self.bundle_digest is None or not (
            self.bundle_digest.startswith("sha256:") and len(self.bundle_digest) > len("sha256:")
        ):
            references.append("bundle.digest")
        for name, graph in sorted(self.graphs.items()):
            if not (graph.graph_hash.startswith("sha256:") and len(graph.graph_hash) > len("sha256:")):
                references.append(f"graphs.{name}.graph_hash")
            if not (
                graph.normalized_plan_hash.startswith("sha256:")
                and len(graph.normalized_plan_hash) > len("sha256:")
            ):
                references.append(f"graphs.{name}.normalized_plan_hash")
        for name, image in sorted(self.images.items()):
            if "@sha256:" not in image.image:
                references.append(f"images.{name}")
        for name, binding in sorted(self.knowledge.items()):
            if binding.index_revision.strip() == "" or binding.index_revision in {
                "latest",
                "current",
                "main",
                "master",
                "HEAD",
            }:
                references.append(f"knowledge.{name}.index_revision")
        for name, prompt in sorted(self.prompt_locks.items()):
            if prompt.kind == "label":
                references.append(f"prompts.{name}")
        if self.supply_chain is not None:
            if self.supply_chain.provenance_ref is not None and "@sha256:" not in self.supply_chain.provenance_ref:
                references.append("supply_chain.provenance_ref")
            if self.supply_chain.sbom_ref is not None and "@sha256:" not in self.supply_chain.sbom_ref:
                references.append("supply_chain.sbom_ref")
        if references:
            raise GraphReleaseMutableReferencesError(references)


@dataclass(frozen=True, slots=True)
class ReleaseBundle:
    bundle_id: str
    release: GraphRelease
    artifacts: dict[str, str] = field(default_factory=dict)
    signatures: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", {str(key): str(value) for key, value in self.artifacts.items()})
        object.__setattr__(self, "signatures", {str(key): str(value) for key, value in self.signatures.items()})

    def bundle_manifest(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "release_digest": self.release.content_digest(),
            "release_name": self.release.name,
            "release_version": self.release.version,
            "artifacts": {key: self.artifacts[key] for key in sorted(self.artifacts)},
            "signatures": {key: self.signatures[key] for key in sorted(self.signatures)},
        }

    def content_digest(self) -> str:
        return canonical_hash(
            {
                "release_digest": self.release.content_digest(),
                "artifacts": {key: self.artifacts[key] for key in sorted(self.artifacts)},
                "signatures": {key: self.signatures[key] for key in sorted(self.signatures)},
            }
        )


@dataclass(frozen=True, slots=True)
class DeploymentRevision:
    revision_id: str
    release_digest: str
    deployment_spec_hash: str
    physical_plan_hash: str
    resolved_binding_hash: str
    target_capability_hash: str
    created_at: str

    def content_digest(self) -> str:
        return canonical_hash(
            {
                "release_digest": self.release_digest,
                "deployment_spec_hash": self.deployment_spec_hash,
                "physical_plan_hash": self.physical_plan_hash,
                "resolved_binding_hash": self.resolved_binding_hash,
                "target_capability_hash": self.target_capability_hash,
            }
        )


class DeploymentEventKind(str, Enum):
    DEPLOYMENT_STARTED = "deployment.started"
    RELEASE_VERIFIED = "release.verified"
    REVISION_CREATED = "revision.created"
    ROLLOUT_STEP_STARTED = "rollout.step.started"
    ROLLOUT_GATE_PASSED = "rollout.gate.passed"
    ROLLOUT_GATE_FAILED = "rollout.gate.failed"
    RELEASE_PROMOTED = "release.promoted"
    RELEASE_ABORTED = "release.aborted"
    ROLLBACK_STARTED = "rollback.started"
    ROLLBACK_COMPLETED = "rollback.completed"
    WORKER_DRAINING = "worker.draining"
    MIGRATION_STARTED = "migration.started"
    MIGRATION_COMPLETED = "migration.completed"

    def as_str(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class DeploymentObservabilityContext:
    release_id: str
    deployment_revision_id: str
    release_digest: str | None = None
    rollout_id: str | None = None
    rollout_step: str | None = None
    cohort: str | None = None

    def with_release_digest(self, release_digest: str) -> DeploymentObservabilityContext:
        return replace(self, release_digest=release_digest)

    def with_rollout(self, rollout_id: str, rollout_step: str, cohort: str) -> DeploymentObservabilityContext:
        return replace(self, rollout_id=rollout_id, rollout_step=rollout_step, cohort=cohort)

    def same_rollout_step(self, other: DeploymentObservabilityContext) -> bool:
        return self.rollout_id is not None and self.rollout_id == other.rollout_id and self.rollout_step == other.rollout_step


@dataclass(frozen=True, slots=True)
class DeploymentEvent:
    event_id: str
    kind: DeploymentEventKind | str
    context: DeploymentObservabilityContext
    occurred_at: str
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata))

    def with_metadata(self, key: str, value: object) -> DeploymentEvent:
        metadata = dict(self.metadata)
        metadata[key] = value
        return replace(self, metadata=metadata)

    def telemetry_attributes(self) -> dict[str, str]:
        event_kind = self.kind.value if isinstance(self.kind, DeploymentEventKind) else self.kind
        attributes = {
            "deployment.event": event_kind,
            "graphblocks.release.id": self.context.release_id,
            "graphblocks.deployment.revision": self.context.deployment_revision_id,
        }
        if self.context.release_digest is not None:
            attributes["graphblocks.release.digest"] = self.context.release_digest
        if self.context.rollout_id is not None:
            attributes["graphblocks.rollout.id"] = self.context.rollout_id
        if self.context.rollout_step is not None:
            attributes["graphblocks.rollout.step"] = self.context.rollout_step
        if self.context.cohort is not None:
            attributes["graphblocks.rollout.cohort"] = self.context.cohort
        return attributes


@dataclass(frozen=True, slots=True)
class DeploymentCondition:
    condition_type: str
    status: DeploymentConditionStatus
    reason: str
    message: str = ""

    def __post_init__(self) -> None:
        if not self.condition_type.strip():
            raise GraphDeploymentError("deployment condition type must not be empty")
        if self.status not in {"true", "false", "unknown"}:
            raise GraphDeploymentError(f"invalid deployment condition status {self.status!r}")
        if not self.reason.strip():
            raise GraphDeploymentError("deployment condition reason must not be empty")

    def condition_contract(self) -> dict[str, str]:
        return {
            "type": self.condition_type,
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class DeploymentSloProfile:
    profile_id: str
    slo_objective_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise GraphDeploymentError("deployment SLO profile id must not be empty")
        objective_ids = tuple(sorted({str(item) for item in self.slo_objective_ids if str(item).strip()}))
        if not objective_ids:
            raise GraphDeploymentError("deployment SLO profile requires at least one SLO objective")
        object.__setattr__(self, "slo_objective_ids", objective_ids)

    def evaluate_slo_reports(self, reports: Iterable[object]) -> DeploymentCondition:
        reports_by_id = {
            str(slo_id): report
            for report in reports
            if (slo_id := getattr(report, "slo_id", None)) is not None
        }
        failed: list[str] = []
        missing_or_no_data: list[str] = []
        for objective_id in self.slo_objective_ids:
            report = reports_by_id.get(objective_id)
            status = getattr(report, "status", None) if report is not None else None
            if report is None or status == "no_data":
                missing_or_no_data.append(objective_id)
            elif status != "pass":
                failed.append(objective_id)

        if failed:
            return DeploymentCondition(
                "SLOWithinBudget",
                "false",
                "slo_failed",
                f"failed SLO objectives: {', '.join(failed)}",
            )
        if missing_or_no_data:
            return DeploymentCondition(
                "SLOWithinBudget",
                "unknown",
                "slo_no_data",
                f"missing or no-data SLO objectives: {', '.join(missing_or_no_data)}",
            )
        return DeploymentCondition("SLOWithinBudget", "true", "slo_within_budget")

    def profile_contract(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "slo_objective_ids": list(self.slo_objective_ids),
        }

    def content_digest(self) -> str:
        return canonical_hash(self.profile_contract())


@dataclass(frozen=True, slots=True)
class RecoveryObjective:
    target: str
    rto: str
    rpo: str

    def __post_init__(self) -> None:
        if not self.target.strip():
            raise GraphDeploymentError("recovery objective target must not be empty")
        if not self.rto.strip():
            raise GraphDeploymentError("recovery objective rto must not be empty")
        if not self.rpo.strip():
            raise GraphDeploymentError("recovery objective rpo must not be empty")

    def objective_contract(self) -> dict[str, str]:
        return {"target": self.target, "rto": self.rto, "rpo": self.rpo}


@dataclass(frozen=True, slots=True)
class DeploymentRecoveryProfile:
    profile_id: str
    objectives: tuple[RecoveryObjective, ...] = field(default_factory=tuple)
    knowledge_index_rebuildable_from: tuple[str, ...] = field(default_factory=tuple)
    regional_failover_mode: str | None = None
    max_restore_test_age_seconds: int | None = None

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise GraphDeploymentError("deployment recovery profile id must not be empty")
        if self.max_restore_test_age_seconds is not None and (
            isinstance(self.max_restore_test_age_seconds, bool) or self.max_restore_test_age_seconds <= 0
        ):
            raise GraphDeploymentError("restore test max age must be positive")
        object.__setattr__(self, "objectives", tuple(sorted(self.objectives, key=lambda item: item.target)))
        object.__setattr__(
            self,
            "knowledge_index_rebuildable_from",
            tuple(sorted({str(item) for item in self.knowledge_index_rebuildable_from if str(item).strip()})),
        )

    def with_objective(self, target: str, *, rto: str, rpo: str) -> DeploymentRecoveryProfile:
        objectives = {objective.target: objective for objective in self.objectives}
        objectives[target] = RecoveryObjective(target, rto, rpo)
        return replace(self, objectives=tuple(objectives.values()))

    def with_knowledge_index_sources(self, sources: Iterable[str]) -> DeploymentRecoveryProfile:
        return replace(self, knowledge_index_rebuildable_from=tuple(str(item) for item in sources))

    def with_regional_failover(self, mode: str) -> DeploymentRecoveryProfile:
        if not mode.strip():
            raise GraphDeploymentError("regional failover mode must not be empty")
        return replace(self, regional_failover_mode=mode)

    def with_max_restore_test_age_seconds(self, max_restore_test_age_seconds: int) -> DeploymentRecoveryProfile:
        return replace(self, max_restore_test_age_seconds=max_restore_test_age_seconds)

    def evaluate_restore_test(
        self,
        *,
        tested_at_unix_seconds: int | None,
        now_unix_seconds: int,
        passed: bool,
    ) -> DeploymentCondition:
        if not passed:
            return DeploymentCondition("RecoveryTestCurrent", "false", "restore_test_failed")
        if tested_at_unix_seconds is None:
            return DeploymentCondition("RecoveryTestCurrent", "unknown", "restore_test_missing")
        age_seconds = now_unix_seconds - tested_at_unix_seconds
        if age_seconds < 0:
            return DeploymentCondition("RecoveryTestCurrent", "unknown", "restore_test_in_future")
        if self.max_restore_test_age_seconds is not None and age_seconds > self.max_restore_test_age_seconds:
            return DeploymentCondition(
                "RecoveryTestCurrent",
                "false",
                "restore_test_stale",
                f"last restore test age {age_seconds}s exceeds {self.max_restore_test_age_seconds}s",
            )
        return DeploymentCondition("RecoveryTestCurrent", "true", "restore_test_current")

    def recovery_contract(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "objectives": [objective.objective_contract() for objective in self.objectives],
            "knowledge_index_rebuildable_from": list(self.knowledge_index_rebuildable_from),
            "regional_failover_mode": self.regional_failover_mode,
            "max_restore_test_age_seconds": self.max_restore_test_age_seconds,
        }

    def content_digest(self) -> str:
        return canonical_hash(self.recovery_contract())


@dataclass(frozen=True, slots=True)
class RolloutStep:
    step_id: str
    kind: RolloutStepKind
    traffic_percent: int = 0
    minimum_samples: int | None = None
    minimum_duration_seconds: int | None = None
    effects: RolloutEffectsMode = "normal"

    def __post_init__(self) -> None:
        if not self.step_id.strip():
            raise RolloutError("rollout step_id must not be empty")
        if self.kind not in {"validate", "shadow", "canary", "blue_green", "promote"}:
            raise RolloutError(f"invalid rollout step kind {self.kind!r}")
        if isinstance(self.traffic_percent, bool) or not 0 <= self.traffic_percent <= 100:
            raise RolloutError("rollout traffic_percent must be between 0 and 100")
        if self.minimum_samples is not None and (
            isinstance(self.minimum_samples, bool) or self.minimum_samples < 1
        ):
            raise RolloutError("rollout minimum_samples must be positive")
        if self.minimum_duration_seconds is not None and (
            isinstance(self.minimum_duration_seconds, bool) or self.minimum_duration_seconds < 1
        ):
            raise RolloutError("rollout minimum_duration_seconds must be positive")
        if self.effects not in {"normal", "suppress", "sandbox"}:
            raise RolloutError(f"invalid rollout effects mode {self.effects!r}")

    @classmethod
    def validate(cls, step_id: str = "validate") -> RolloutStep:
        return cls(step_id=step_id, kind="validate", traffic_percent=0)

    @classmethod
    def shadow(cls, step_id: str = "shadow", *, effects: RolloutEffectsMode = "suppress") -> RolloutStep:
        return cls(step_id=step_id, kind="shadow", traffic_percent=0, effects=effects)

    @classmethod
    def canary(
        cls,
        step_id: str,
        *,
        traffic_percent: int,
        minimum_samples: int | None = None,
        minimum_duration_seconds: int | None = None,
        effects: RolloutEffectsMode = "normal",
    ) -> RolloutStep:
        return cls(
            step_id=step_id,
            kind="canary",
            traffic_percent=traffic_percent,
            minimum_samples=minimum_samples,
            minimum_duration_seconds=minimum_duration_seconds,
            effects=effects,
        )

    @classmethod
    def promote(cls, step_id: str = "promote") -> RolloutStep:
        return cls(step_id=step_id, kind="promote", traffic_percent=100)


@dataclass(frozen=True, slots=True)
class RolloutAnalysisResult:
    step_id: str
    passed: bool
    sample_count: int = 0
    duration_seconds: int = 0
    metrics: dict[str, object] = field(default_factory=dict)
    reason: str | None = None
    non_reversible_effect_observed: bool = False

    def __post_init__(self) -> None:
        if not self.step_id.strip():
            raise RolloutError("rollout analysis step_id must not be empty")
        if isinstance(self.sample_count, bool) or self.sample_count < 0:
            raise RolloutError("rollout analysis sample_count must be non-negative")
        if isinstance(self.duration_seconds, bool) or self.duration_seconds < 0:
            raise RolloutError("rollout analysis duration_seconds must be non-negative")
        object.__setattr__(self, "metrics", dict(self.metrics))


@dataclass(frozen=True, slots=True)
class RolloutDecision:
    decision: RolloutDecisionKind
    reason: str
    next_state: RolloutState
    automatic_rollback_allowed: bool = True


@dataclass(frozen=True, slots=True)
class RolloutPlan:
    rollout_id: str
    stable_revision_id: str
    candidate_revision_id: str
    strategy: Literal["canary", "blue_green"] = "canary"
    affinity: str | None = None
    analysis_profile_ref: str | None = None
    steps: tuple[RolloutStep, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.rollout_id.strip():
            raise RolloutError("rollout_id must not be empty")
        if not self.stable_revision_id.strip():
            raise RolloutError("stable_revision_id must not be empty")
        if not self.candidate_revision_id.strip():
            raise RolloutError("candidate_revision_id must not be empty")
        if self.strategy not in {"canary", "blue_green"}:
            raise RolloutError(f"invalid rollout strategy {self.strategy!r}")
        steps = tuple(self.steps)
        if not steps:
            raise RolloutError("rollout plan requires at least one step")
        if steps[0].kind != "validate":
            raise RolloutError("rollout plan must start with validate")
        if steps[-1].kind != "promote":
            raise RolloutError("rollout plan must end with promote")
        object.__setattr__(self, "steps", steps)

    @classmethod
    def canary(
        cls,
        rollout_id: str,
        stable_revision_id: str,
        candidate_revision_id: str,
        *,
        canary_steps: tuple[RolloutStep, ...],
        affinity: str | None = None,
        analysis_profile_ref: str | None = None,
    ) -> RolloutPlan:
        if not canary_steps:
            raise RolloutError("canary rollout requires at least one canary step")
        if any(step.kind != "canary" for step in canary_steps):
            raise RolloutError("canary rollout canary_steps must all have kind 'canary'")
        return cls(
            rollout_id=rollout_id,
            stable_revision_id=stable_revision_id,
            candidate_revision_id=candidate_revision_id,
            strategy="canary",
            affinity=affinity,
            analysis_profile_ref=analysis_profile_ref,
            steps=(RolloutStep.validate(), RolloutStep.shadow(), *canary_steps, RolloutStep.promote()),
        )

    def initial_state(self) -> RolloutState:
        return RolloutState(plan=self)

    def current_step(self, index: int) -> RolloutStep:
        if index < 0 or index >= len(self.steps):
            raise RolloutError("rollout step index out of range")
        return self.steps[index]

    def assign_revision(self, affinity_key: str, step: RolloutStep) -> str:
        if step.traffic_percent <= 0:
            return self.stable_revision_id
        if step.traffic_percent >= 100:
            return self.candidate_revision_id
        bucket_digest = canonical_hash(
            {
                "rollout_id": self.rollout_id,
                "affinity": self.affinity,
                "affinity_key": affinity_key,
            }
        )
        bucket = int(bucket_digest.removeprefix("sha256:")[:8], 16) % 100
        if bucket < step.traffic_percent:
            return self.candidate_revision_id
        return self.stable_revision_id


@dataclass(frozen=True, slots=True)
class RolloutState:
    plan: RolloutPlan
    current_step_index: int = 0
    status: RolloutStatus = "running"

    def __post_init__(self) -> None:
        if self.current_step_index < 0 or self.current_step_index >= len(self.plan.steps):
            raise RolloutError("current_step_index out of range")
        if self.status not in {"running", "promoted", "aborted"}:
            raise RolloutError(f"invalid rollout status {self.status!r}")

    @property
    def current_step(self) -> RolloutStep:
        return self.plan.steps[self.current_step_index]

    def advance_for_test(self, current_step_index: int) -> RolloutState:
        return replace(self, current_step_index=current_step_index, status="running")

    def evaluate_gate(self, result: RolloutAnalysisResult) -> RolloutDecision:
        if self.status != "running":
            return RolloutDecision(
                decision="hold",
                reason=f"rollout_{self.status}",
                next_state=self,
                automatic_rollback_allowed=self.status != "aborted",
            )
        step = self.current_step
        if result.step_id != step.step_id:
            raise RolloutError(
                f"analysis step {result.step_id!r} does not match current rollout step {step.step_id!r}"
            )
        if step.minimum_samples is not None and result.sample_count < step.minimum_samples:
            return RolloutDecision("hold", "minimum_samples_not_met", self)
        if step.minimum_duration_seconds is not None and result.duration_seconds < step.minimum_duration_seconds:
            return RolloutDecision("hold", "minimum_duration_not_met", self)
        if not result.passed:
            reason = result.reason or "analysis_failed"
            return RolloutDecision(
                decision="abort",
                reason=reason,
                next_state=replace(self, status="aborted"),
                automatic_rollback_allowed=not result.non_reversible_effect_observed,
            )
        if step.kind == "promote":
            return RolloutDecision("promote", "promote_gate_passed", replace(self, status="promoted"))
        next_index = min(self.current_step_index + 1, len(self.plan.steps) - 1)
        return RolloutDecision("advance", "gate_passed", replace(self, current_step_index=next_index))


@dataclass(frozen=True, slots=True)
class RevisionDecision:
    kind: Literal[
        "admit_on_new",
        "finish_on_old",
        "keep_affinity",
        "checkpoint_and_migrate",
        "drain_on_old",
    ]
    revision_id: str | None = None
    from_revision_id: str | None = None
    to_revision_id: str | None = None

    @classmethod
    def admit_on_new(cls, revision_id: str) -> RevisionDecision:
        return cls(kind="admit_on_new", revision_id=revision_id)

    @classmethod
    def finish_on_old(cls, revision_id: str) -> RevisionDecision:
        return cls(kind="finish_on_old", revision_id=revision_id)

    @classmethod
    def keep_affinity(cls, revision_id: str) -> RevisionDecision:
        return cls(kind="keep_affinity", revision_id=revision_id)

    @classmethod
    def checkpoint_and_migrate(cls, from_revision_id: str, to_revision_id: str) -> RevisionDecision:
        return cls(
            kind="checkpoint_and_migrate",
            from_revision_id=from_revision_id,
            to_revision_id=to_revision_id,
        )

    @classmethod
    def drain_on_old(cls, revision_id: str) -> RevisionDecision:
        return cls(kind="drain_on_old", revision_id=revision_id)


@dataclass(frozen=True, slots=True)
class UpgradePolicy:
    old_revision_id: str
    new_revision_id: str

    @classmethod
    def workload_aware(cls, old_revision_id: str, new_revision_id: str) -> UpgradePolicy:
        return cls(old_revision_id=old_revision_id, new_revision_id=new_revision_id)

    def decide(
        self,
        workload: WorkloadKind,
        affinity_revision_id: str | None,
        checkpoint_compatible: bool,
    ) -> RevisionDecision:
        if workload == "new_request":
            return RevisionDecision.admit_on_new(self.new_revision_id)
        if workload == "existing_request":
            return RevisionDecision.finish_on_old(affinity_revision_id or self.old_revision_id)
        if workload == "conversation":
            if affinity_revision_id is not None:
                return RevisionDecision.keep_affinity(affinity_revision_id)
            return RevisionDecision.admit_on_new(self.new_revision_id)
        if workload == "durable_job":
            if checkpoint_compatible:
                return RevisionDecision.checkpoint_and_migrate(
                    affinity_revision_id or self.old_revision_id,
                    self.new_revision_id,
                )
            return RevisionDecision.finish_on_old(affinity_revision_id or self.old_revision_id)
        if workload == "realtime_session":
            return RevisionDecision.drain_on_old(affinity_revision_id or self.old_revision_id)
        raise ValueError(f"unknown workload kind: {workload!r}")


@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    target_id: str
    kind: ExecutionTargetKind
    execution_host: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    effects: tuple[str, ...] = field(default_factory=tuple)
    package_lock: str | None = None
    image: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", tuple(sorted(set(self.capabilities))))
        object.__setattr__(self, "effects", tuple(sorted(set(self.effects))))

    def with_capabilities(self, capabilities: list[str] | tuple[str, ...]) -> ExecutionTarget:
        return replace(self, capabilities=tuple(capabilities))

    def with_effects(self, effects: list[str] | tuple[str, ...]) -> ExecutionTarget:
        return replace(self, effects=tuple(effects))

    def with_package_lock(self, package_lock: str) -> ExecutionTarget:
        return replace(self, package_lock=package_lock)

    def with_image(self, image: str) -> ExecutionTarget:
        return replace(self, image=image)

    def canonical_value(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "kind": self.kind,
            "execution_host": self.execution_host,
            "capabilities": list(self.capabilities),
            "effects": list(self.effects),
            "package_lock": self.package_lock,
            "image": self.image,
        }


@dataclass(frozen=True, slots=True)
class DeploymentTargetProfile:
    target_id: str
    image_role: str
    kind: ExecutionTargetKind
    execution_host: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    effects: tuple[str, ...] = field(default_factory=tuple)
    package_lock: str | None = None
    default_replicas: int = 1

    def __post_init__(self) -> None:
        if not self.target_id.strip():
            raise GraphDeploymentError("deployment target profile id must not be empty")
        if not self.image_role.strip():
            raise GraphDeploymentError("deployment target image_role must not be empty")
        if not self.execution_host.strip():
            raise GraphDeploymentError("deployment target execution_host must not be empty")
        if self.default_replicas <= 0:
            raise GraphDeploymentError("deployment target default_replicas must be positive")
        object.__setattr__(self, "capabilities", tuple(sorted({str(item) for item in self.capabilities})))
        object.__setattr__(self, "effects", tuple(sorted({str(item) for item in self.effects})))

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> DeploymentTargetProfile:
        target_id = mapping.get("id", mapping.get("targetId", mapping.get("target_id")))
        image_role = mapping.get("imageRole", mapping.get("image_role"))
        kind = mapping.get("kind")
        execution_host = mapping.get("executionHost", mapping.get("execution_host"))
        for field_name, value in (
            ("id", target_id),
            ("imageRole", image_role),
            ("kind", kind),
            ("executionHost", execution_host),
        ):
            if not isinstance(value, str):
                raise GraphDeploymentError(f"deployment target profile {field_name} must be a string")
        raw_capabilities = mapping.get("capabilities", ())
        if isinstance(raw_capabilities, str):
            capabilities = (raw_capabilities,)
        else:
            capabilities = tuple(str(item) for item in raw_capabilities or ())
        raw_effects = mapping.get("effects", ())
        if isinstance(raw_effects, str):
            effects = (raw_effects,)
        else:
            effects = tuple(str(item) for item in raw_effects or ())
        package_lock = mapping.get("packageLock", mapping.get("package_lock"))
        default_replicas = mapping.get("defaultReplicas", mapping.get("default_replicas", 1))
        if package_lock is not None and not isinstance(package_lock, str):
            raise GraphDeploymentError("deployment target profile packageLock must be a string")
        if not isinstance(default_replicas, int):
            raise GraphDeploymentError("deployment target profile defaultReplicas must be an integer")
        return cls(
            target_id=target_id,
            image_role=image_role,
            kind=kind,
            execution_host=execution_host,
            capabilities=capabilities,
            effects=effects,
            package_lock=package_lock,
            default_replicas=default_replicas,
        )

    def to_execution_target(self, image: str) -> ExecutionTarget:
        if "@sha256:" not in image:
            raise GraphDeploymentError("deployment target image must be digest-pinned")
        target = (
            ExecutionTarget(self.target_id, self.kind, self.execution_host)
            .with_capabilities(self.capabilities)
            .with_effects(self.effects)
            .with_image(image)
        )
        if self.package_lock is not None:
            target = target.with_package_lock(self.package_lock)
        return target

    def profile_contract(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "image_role": self.image_role,
            "kind": self.kind,
            "execution_host": self.execution_host,
            "capabilities": list(self.capabilities),
            "effects": list(self.effects),
            "package_lock": self.package_lock,
            "default_replicas": self.default_replicas,
        }


@dataclass(frozen=True, slots=True)
class DeploymentTargetCoverageIssue:
    code: str
    image_role: str
    target_id: str
    path: str
    message: str

    def issue_contract(self) -> dict[str, str]:
        return {
            "code": self.code,
            "image_role": self.image_role,
            "target_id": self.target_id,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class DeploymentTargetCoverageResult:
    issues: tuple[DeploymentTargetCoverageIssue, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.issues

    def issue_contracts(self) -> list[dict[str, str]]:
        return [issue.issue_contract() for issue in self.issues]


@dataclass(frozen=True, slots=True)
class DeploymentTargetProfileSet:
    targets: tuple[DeploymentTargetProfile, ...]

    def __post_init__(self) -> None:
        seen_ids: set[str] = set()
        seen_roles: set[str] = set()
        for target in self.targets:
            if target.target_id in seen_ids:
                raise GraphDeploymentError(f"duplicate deployment target id {target.target_id!r}")
            if target.image_role in seen_roles:
                raise GraphDeploymentError(f"duplicate deployment target image role {target.image_role!r}")
            seen_ids.add(target.target_id)
            seen_roles.add(target.image_role)

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> DeploymentTargetProfileSet:
        if document.get("kind") != "DeploymentTargetProfileSet":
            raise GraphDeploymentError("deployment target manifest kind must be DeploymentTargetProfileSet")
        spec = document.get("spec")
        if not isinstance(spec, Mapping):
            raise GraphDeploymentError("deployment target manifest spec must be a mapping")
        raw_targets = spec.get("targets", ())
        if not isinstance(raw_targets, list):
            raise GraphDeploymentError("deployment target manifest spec.targets must be a list")
        targets = []
        for index, raw_target in enumerate(raw_targets):
            if not isinstance(raw_target, Mapping):
                raise GraphDeploymentError(f"deployment target manifest target {index} must be a mapping")
            targets.append(DeploymentTargetProfile.from_mapping(raw_target))
        return cls(tuple(targets))

    def by_id(self, target_id: str) -> DeploymentTargetProfile:
        for target in self.targets:
            if target.target_id == target_id:
                return target
        raise KeyError(target_id)

    def target_ids(self) -> tuple[str, ...]:
        return tuple(sorted(target.target_id for target in self.targets))

    def image_roles(self) -> tuple[str, ...]:
        return tuple(target.image_role for target in self.targets)

    def coverage_for_required_image_roles(
        self,
        required_image_roles: tuple[str, ...] | list[str],
    ) -> DeploymentTargetCoverageResult:
        known_roles = {target.image_role for target in self.targets}
        issues = [
            DeploymentTargetCoverageIssue(
                code="DeploymentTargetRoleMissing",
                image_role=image_role,
                target_id="",
                path="$.spec.targets",
                message="required production image role has no deployment target profile",
            )
            for image_role in required_image_roles
            if image_role not in known_roles
        ]
        return DeploymentTargetCoverageResult(tuple(issues))

    def manifest_contract(self) -> dict[str, object]:
        return {
            "targets": [
                target.profile_contract()
                for target in sorted(self.targets, key=lambda target: target.target_id)
            ],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.manifest_contract())


@dataclass(frozen=True, slots=True)
class PlacementSelector:
    kind: Literal["nodes", "execution_groups", "blocks", "capabilities", "effects", "execution_classes"]
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple(sorted(set(self.values))))

    @classmethod
    def nodes(cls, nodes: list[str] | tuple[str, ...]) -> PlacementSelector:
        return cls("nodes", tuple(nodes))

    @classmethod
    def execution_groups(cls, groups: list[str] | tuple[str, ...]) -> PlacementSelector:
        return cls("execution_groups", tuple(groups))

    @classmethod
    def blocks(cls, blocks: list[str] | tuple[str, ...]) -> PlacementSelector:
        return cls("blocks", tuple(blocks))

    @classmethod
    def capabilities(cls, capabilities: list[str] | tuple[str, ...]) -> PlacementSelector:
        return cls("capabilities", tuple(capabilities))

    @classmethod
    def effects(cls, effects: list[str] | tuple[str, ...]) -> PlacementSelector:
        return cls("effects", tuple(effects))

    @classmethod
    def execution_classes(cls, classes: list[str] | tuple[str, ...]) -> PlacementSelector:
        return cls("execution_classes", tuple(classes))

    @property
    def priority(self) -> int:
        if self.kind == "nodes":
            return 5
        if self.kind == "execution_groups":
            return 4
        if self.kind == "blocks":
            return 3
        if self.kind in {"capabilities", "effects"}:
            return 2
        return 1

    @property
    def priority_name(self) -> str:
        if self.kind == "nodes":
            return "node"
        if self.kind == "execution_groups":
            return "execution_group"
        if self.kind == "blocks":
            return "block"
        if self.kind in {"capabilities", "effects"}:
            return "capability"
        return "execution_class"

    def canonical_value(self) -> dict[str, object]:
        return {"kind": self.kind, "values": list(self.values)}


@dataclass(frozen=True, slots=True)
class PlacementRule:
    rule_id: str
    selector: PlacementSelector
    target_id: str

    def canonical_value(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "selector": self.selector.canonical_value(),
            "target_id": self.target_id,
        }


class PlacementError(ValueError):
    """Base error for deployment placement resolution."""


class PlacementNoCompatibleTargetError(PlacementError):
    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        super().__init__(f"no compatible deployment target for node {node_id!r}")


class PlacementUnknownTargetError(PlacementError):
    def __init__(self, target_id: str) -> None:
        self.target_id = target_id
        super().__init__(f"placement target {target_id!r} is not defined")


class PlacementAmbiguousError(PlacementError):
    def __init__(self, node_id: str, priority: str, target_ids: list[str] | tuple[str, ...]) -> None:
        self.node_id = node_id
        self.priority = priority
        self.target_ids = tuple(target_ids)
        super().__init__(f"ambiguous placement for node {node_id!r} at priority {priority}: {self.target_ids!r}")


@dataclass(frozen=True, slots=True)
class ResolvedPlacement:
    node_id: str
    target_id: str
    rule_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PhysicalExecutionPlan:
    release_digest: str
    deployment_revision_id: str
    graph_hash: str
    package_lock_hash: str | None = None
    targets: dict[str, ExecutionTarget] = field(default_factory=dict)
    placements: tuple[PlacementRule, ...] = field(default_factory=tuple)
    default_target: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", dict(self.targets))
        object.__setattr__(self, "placements", tuple(self.placements))

    def with_package_lock_hash(self, package_lock_hash: str) -> PhysicalExecutionPlan:
        return replace(self, package_lock_hash=package_lock_hash)

    def with_target(self, target: ExecutionTarget) -> PhysicalExecutionPlan:
        targets = dict(self.targets)
        targets[target.target_id] = target
        return replace(self, targets=targets)

    def with_placement(self, placement: PlacementRule) -> PhysicalExecutionPlan:
        return replace(self, placements=(*self.placements, placement))

    def with_default_target(self, target_id: str) -> PhysicalExecutionPlan:
        return replace(self, default_target=target_id)

    def plan_hash(self) -> str:
        placements = [placement.canonical_value() for placement in self.placements]
        placements.sort(key=canonical_dumps)
        return canonical_hash(
            {
                "release_digest": self.release_digest,
                "deployment_revision_id": self.deployment_revision_id,
                "graph_hash": self.graph_hash,
                "package_lock_hash": self.package_lock_hash,
                "targets": [
                    target.canonical_value()
                    for _, target in sorted(self.targets.items())
                ],
                "placements": placements,
                "default_target": self.default_target,
            }
        )

    def target_capability_hash(self) -> str:
        return canonical_hash(
            {
                "targets": {
                    target_id: target.canonical_value()
                    for target_id, target in sorted(self.targets.items())
                },
            }
        )

    def resolve_target(
        self,
        node_id: str,
        execution_group: str | None,
        block_id: str,
        capabilities: list[str] | tuple[str, ...],
        effects: list[str] | tuple[str, ...],
        execution_class: str | None,
    ) -> ResolvedPlacement:
        capability_set = set(capabilities)
        effect_set = set(effects)
        matches: list[PlacementRule] = []
        for rule in self.placements:
            selector = rule.selector
            selector_values = set(selector.values)
            if selector.kind == "nodes" and node_id in selector_values:
                matches.append(rule)
            elif selector.kind == "execution_groups" and execution_group in selector_values:
                matches.append(rule)
            elif selector.kind == "blocks" and block_id in selector_values:
                matches.append(rule)
            elif selector.kind == "capabilities" and selector_values.issubset(capability_set):
                matches.append(rule)
            elif selector.kind == "effects" and selector_values.issubset(effect_set):
                matches.append(rule)
            elif selector.kind == "execution_classes" and execution_class in selector_values:
                matches.append(rule)

        if not matches:
            if self.default_target is not None:
                if self.default_target not in self.targets:
                    raise PlacementUnknownTargetError(self.default_target)
                return ResolvedPlacement(node_id=node_id, target_id=self.default_target)
            raise PlacementNoCompatibleTargetError(node_id)

        matches.sort(key=lambda rule: rule.selector.priority, reverse=True)
        best_priority = matches[0].selector.priority
        best = [rule for rule in matches if rule.selector.priority == best_priority]
        target_ids = tuple(sorted({rule.target_id for rule in best}))
        if len(target_ids) > 1:
            raise PlacementAmbiguousError(node_id, best[0].selector.priority_name, target_ids)
        target_id = target_ids[0]
        if target_id not in self.targets:
            raise PlacementUnknownTargetError(target_id)
        return ResolvedPlacement(
            node_id=node_id,
            target_id=target_id,
            rule_ids=tuple(rule.rule_id for rule in best if rule.target_id == target_id),
        )


@dataclass(frozen=True, slots=True)
class GraphDeployment:
    deployment_id: str
    release: GraphRelease
    graph_name: str
    deployment_revision_id: str
    environment: str = "local"
    targets: dict[str, ExecutionTarget] = field(default_factory=dict)
    placements: tuple[PlacementRule, ...] = field(default_factory=tuple)
    default_target: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", dict(self.targets))
        object.__setattr__(self, "placements", tuple(self.placements))

    def with_target(self, target: ExecutionTarget) -> GraphDeployment:
        targets = dict(self.targets)
        targets[target.target_id] = target
        return replace(self, targets=targets)

    def with_placement(self, placement: PlacementRule) -> GraphDeployment:
        return replace(self, placements=(*self.placements, placement))

    def with_default_target(self, target_id: str) -> GraphDeployment:
        return replace(self, default_target=target_id)

    def deployment_spec_hash(self) -> str:
        placements = [placement.canonical_value() for placement in self.placements]
        placements.sort(key=canonical_dumps)
        return canonical_hash(
            {
                "release_digest": self.release.content_digest(),
                "graph_name": self.graph_name,
                "environment": self.environment,
                "targets": [target.canonical_value() for _, target in sorted(self.targets.items())],
                "placements": placements,
                "default_target": self.default_target,
            }
        )

    def to_physical_plan(self, *, package_lock_hash: str | None = None) -> PhysicalExecutionPlan:
        graph = self.release.graphs.get(self.graph_name)
        if graph is None:
            raise GraphDeploymentError(f"release {self.release.name!r} has no graph {self.graph_name!r}")
        plan = PhysicalExecutionPlan(
            release_digest=self.release.content_digest(),
            deployment_revision_id=self.deployment_revision_id,
            graph_hash=graph.graph_hash,
            package_lock_hash=package_lock_hash,
            default_target=self.default_target,
        )
        for _, target in sorted(self.targets.items()):
            plan = plan.with_target(target)
        for placement in self.placements:
            plan = plan.with_placement(placement)
        return plan


__all__ = [
    "DeploymentCondition",
    "DeploymentConditionStatus",
    "DeploymentEvent",
    "DeploymentEventKind",
    "DeploymentObservabilityContext",
    "DeploymentRecoveryProfile",
    "DeploymentRevision",
    "DeploymentSloProfile",
    "DeploymentTargetCoverageIssue",
    "DeploymentTargetCoverageResult",
    "DeploymentTargetProfile",
    "DeploymentTargetProfileSet",
    "ExecutionTarget",
    "ExecutionTargetKind",
    "GraphDeployment",
    "GraphDeploymentError",
    "GraphRelease",
    "GraphReleaseError",
    "GraphReleaseGraph",
    "GraphReleaseMutableReferencesError",
    "ImageRef",
    "KnowledgeBinding",
    "PhysicalExecutionPlan",
    "PlacementAmbiguousError",
    "PlacementError",
    "PlacementNoCompatibleTargetError",
    "PlacementRule",
    "PlacementSelector",
    "PlacementUnknownTargetError",
    "PromptLock",
    "RecoveryObjective",
    "ReleaseBundle",
    "ResolvedPlacement",
    "RevisionDecision",
    "RolloutAnalysisResult",
    "RolloutDecision",
    "RolloutError",
    "RolloutPlan",
    "RolloutState",
    "RolloutStep",
    "RolloutStepKind",
    "SupplyChainLock",
    "UpgradePolicy",
    "WorkloadKind",
]

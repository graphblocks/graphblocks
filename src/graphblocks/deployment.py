from __future__ import annotations

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


class GraphReleaseError(ValueError):
    """Base error for invalid graph release contracts."""


class GraphReleaseMutableReferencesError(GraphReleaseError):
    def __init__(self, references: list[str] | tuple[str, ...]) -> None:
        self.references = tuple(references)
        super().__init__(f"mutable release references: {self.references!r}")


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
        if references:
            raise GraphReleaseMutableReferencesError(references)


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


__all__ = [
    "DeploymentEvent",
    "DeploymentEventKind",
    "DeploymentObservabilityContext",
    "DeploymentRevision",
    "ExecutionTarget",
    "ExecutionTargetKind",
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
    "ResolvedPlacement",
    "RevisionDecision",
    "UpgradePolicy",
    "WorkloadKind",
]

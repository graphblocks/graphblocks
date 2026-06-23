use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphReleaseGraph {
    pub graph_hash: String,
    pub normalized_plan_hash: String,
}

impl GraphReleaseGraph {
    pub fn new(graph_hash: impl Into<String>, normalized_plan_hash: impl Into<String>) -> Self {
        Self {
            graph_hash: graph_hash.into(),
            normalized_plan_hash: normalized_plan_hash.into(),
        }
    }

    fn canonical_value(&self) -> Value {
        json!({
            "graph_hash": self.graph_hash,
            "normalized_plan_hash": self.normalized_plan_hash,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ImageRef {
    pub image: String,
}

impl ImageRef {
    pub fn new(image: impl Into<String>) -> Self {
        Self {
            image: image.into(),
        }
    }

    fn canonical_value(&self) -> Value {
        json!({ "image": self.image })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PromptLock {
    Versioned { name: String, version: String },
    Label { name: String, label: String },
}

impl PromptLock {
    pub fn versioned(name: impl Into<String>, version: impl Into<String>) -> Self {
        Self::Versioned {
            name: name.into(),
            version: version.into(),
        }
    }

    pub fn label(name: impl Into<String>, label: impl Into<String>) -> Self {
        Self::Label {
            name: name.into(),
            label: label.into(),
        }
    }

    fn canonical_value(&self) -> Value {
        match self {
            Self::Versioned { name, version } => {
                json!({"kind": "versioned", "name": name, "version": version})
            }
            Self::Label { name, label } => {
                json!({"kind": "label", "name": name, "label": label})
            }
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KnowledgeBinding {
    pub index_id: String,
    pub index_revision: String,
}

impl KnowledgeBinding {
    pub fn new(index_id: impl Into<String>, index_revision: impl Into<String>) -> Self {
        Self {
            index_id: index_id.into(),
            index_revision: index_revision.into(),
        }
    }

    fn canonical_value(&self) -> Value {
        json!({
            "index_id": self.index_id,
            "index_revision": self.index_revision,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphRelease {
    pub name: String,
    pub version: String,
    pub bundle_digest: Option<String>,
    pub bundle_media_type: Option<String>,
    pub application_hash: Option<String>,
    pub graphs: BTreeMap<String, GraphReleaseGraph>,
    pub images: BTreeMap<String, ImageRef>,
    pub prompt_locks: BTreeMap<String, PromptLock>,
    pub knowledge: BTreeMap<String, KnowledgeBinding>,
}

impl GraphRelease {
    pub fn new(name: impl Into<String>, version: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            version: version.into(),
            bundle_digest: None,
            bundle_media_type: None,
            application_hash: None,
            graphs: BTreeMap::new(),
            images: BTreeMap::new(),
            prompt_locks: BTreeMap::new(),
            knowledge: BTreeMap::new(),
        }
    }

    pub fn with_bundle(mut self, digest: impl Into<String>, media_type: impl Into<String>) -> Self {
        self.bundle_digest = Some(digest.into());
        self.bundle_media_type = Some(media_type.into());
        self
    }

    pub fn with_application_hash(mut self, application_hash: impl Into<String>) -> Self {
        self.application_hash = Some(application_hash.into());
        self
    }

    pub fn with_graph(mut self, graph_name: impl Into<String>, graph: GraphReleaseGraph) -> Self {
        self.graphs.insert(graph_name.into(), graph);
        self
    }

    pub fn with_image(mut self, image_name: impl Into<String>, image: ImageRef) -> Self {
        self.images.insert(image_name.into(), image);
        self
    }

    pub fn with_prompt_lock(
        mut self,
        prompt_name: impl Into<String>,
        prompt_lock: PromptLock,
    ) -> Self {
        self.prompt_locks.insert(prompt_name.into(), prompt_lock);
        self
    }

    pub fn with_knowledge(mut self, binding: KnowledgeBinding) -> Self {
        self.knowledge.insert(binding.index_id.clone(), binding);
        self
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "version": self.version,
            "bundle": {
                "digest": self.bundle_digest,
                "media_type": self.bundle_media_type,
            },
            "application_hash": self.application_hash,
            "graphs": self.graphs.iter().map(|(name, graph)| (name, graph.canonical_value())).collect::<BTreeMap<_, _>>(),
            "images": self.images.iter().map(|(name, image)| (name, image.canonical_value())).collect::<BTreeMap<_, _>>(),
            "prompt_locks": self.prompt_locks.iter().map(|(name, prompt)| (name, prompt.canonical_value())).collect::<BTreeMap<_, _>>(),
            "knowledge": self.knowledge.iter().map(|(name, binding)| (name, binding.canonical_value())).collect::<BTreeMap<_, _>>(),
        }))
    }

    pub fn validate_production_pins(&self) -> Result<(), GraphReleaseError> {
        let mut references = Vec::new();
        if self
            .bundle_digest
            .as_deref()
            .is_none_or(|digest| !is_sha256_digest(digest))
        {
            references.push("bundle.digest".to_owned());
        }
        for (name, graph) in &self.graphs {
            if !is_sha256_digest(&graph.graph_hash) {
                references.push(format!("graphs.{name}.graph_hash"));
            }
            if !is_sha256_digest(&graph.normalized_plan_hash) {
                references.push(format!("graphs.{name}.normalized_plan_hash"));
            }
        }
        for (name, image) in &self.images {
            if !image.image.contains("@sha256:") {
                references.push(format!("images.{name}"));
            }
        }
        for (name, binding) in &self.knowledge {
            if is_mutable_label(&binding.index_revision) {
                references.push(format!("knowledge.{name}.index_revision"));
            }
        }
        for (name, prompt) in &self.prompt_locks {
            if matches!(prompt, PromptLock::Label { .. }) {
                references.push(format!("prompts.{name}"));
            }
        }
        if references.is_empty() {
            Ok(())
        } else {
            Err(GraphReleaseError::MutableReferences { references })
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum GraphReleaseError {
    MutableReferences { references: Vec<String> },
}

impl fmt::Display for GraphReleaseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MutableReferences { references } => {
                write!(formatter, "mutable release references: {references:?}")
            }
        }
    }
}

impl Error for GraphReleaseError {}

fn is_sha256_digest(value: &str) -> bool {
    value.starts_with("sha256:") && value.len() > "sha256:".len()
}

fn is_mutable_label(value: &str) -> bool {
    value.trim().is_empty() || matches!(value, "latest" | "current" | "main" | "master" | "HEAD")
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeploymentRevision {
    pub revision_id: String,
    pub release_digest: String,
    pub deployment_spec_hash: String,
    pub physical_plan_hash: String,
    pub resolved_binding_hash: String,
    pub target_capability_hash: String,
    pub created_at: String,
}

impl DeploymentRevision {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        revision_id: impl Into<String>,
        release_digest: impl Into<String>,
        deployment_spec_hash: impl Into<String>,
        physical_plan_hash: impl Into<String>,
        resolved_binding_hash: impl Into<String>,
        target_capability_hash: impl Into<String>,
        created_at: impl Into<String>,
    ) -> Self {
        Self {
            revision_id: revision_id.into(),
            release_digest: release_digest.into(),
            deployment_spec_hash: deployment_spec_hash.into(),
            physical_plan_hash: physical_plan_hash.into(),
            resolved_binding_hash: resolved_binding_hash.into(),
            target_capability_hash: target_capability_hash.into(),
            created_at: created_at.into(),
        }
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "release_digest": self.release_digest,
            "deployment_spec_hash": self.deployment_spec_hash,
            "physical_plan_hash": self.physical_plan_hash,
            "resolved_binding_hash": self.resolved_binding_hash,
            "target_capability_hash": self.target_capability_hash,
        }))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeploymentEventKind {
    DeploymentStarted,
    ReleaseVerified,
    RevisionCreated,
    RolloutStepStarted,
    RolloutGatePassed,
    RolloutGateFailed,
    ReleasePromoted,
    ReleaseAborted,
    RollbackStarted,
    RollbackCompleted,
    WorkerDraining,
    MigrationStarted,
    MigrationCompleted,
}

impl DeploymentEventKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::DeploymentStarted => "deployment.started",
            Self::ReleaseVerified => "release.verified",
            Self::RevisionCreated => "revision.created",
            Self::RolloutStepStarted => "rollout.step.started",
            Self::RolloutGatePassed => "rollout.gate.passed",
            Self::RolloutGateFailed => "rollout.gate.failed",
            Self::ReleasePromoted => "release.promoted",
            Self::ReleaseAborted => "release.aborted",
            Self::RollbackStarted => "rollback.started",
            Self::RollbackCompleted => "rollback.completed",
            Self::WorkerDraining => "worker.draining",
            Self::MigrationStarted => "migration.started",
            Self::MigrationCompleted => "migration.completed",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeploymentObservabilityContext {
    pub release_id: String,
    pub deployment_revision_id: String,
    pub release_digest: Option<String>,
    pub rollout_id: Option<String>,
    pub rollout_step: Option<String>,
    pub cohort: Option<String>,
}

impl DeploymentObservabilityContext {
    pub fn new(release_id: impl Into<String>, deployment_revision_id: impl Into<String>) -> Self {
        Self {
            release_id: release_id.into(),
            deployment_revision_id: deployment_revision_id.into(),
            release_digest: None,
            rollout_id: None,
            rollout_step: None,
            cohort: None,
        }
    }

    pub fn with_release_digest(mut self, release_digest: impl Into<String>) -> Self {
        self.release_digest = Some(release_digest.into());
        self
    }

    pub fn with_rollout(
        mut self,
        rollout_id: impl Into<String>,
        rollout_step: impl Into<String>,
        cohort: impl Into<String>,
    ) -> Self {
        self.rollout_id = Some(rollout_id.into());
        self.rollout_step = Some(rollout_step.into());
        self.cohort = Some(cohort.into());
        self
    }

    pub fn same_rollout_step(&self, other: &Self) -> bool {
        self.rollout_id.is_some()
            && self.rollout_id == other.rollout_id
            && self.rollout_step == other.rollout_step
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct DeploymentEvent {
    pub event_id: String,
    pub kind: DeploymentEventKind,
    pub context: DeploymentObservabilityContext,
    pub occurred_at: String,
    pub metadata: BTreeMap<String, Value>,
}

impl DeploymentEvent {
    pub fn new(
        event_id: impl Into<String>,
        kind: DeploymentEventKind,
        context: DeploymentObservabilityContext,
        occurred_at: impl Into<String>,
    ) -> Self {
        Self {
            event_id: event_id.into(),
            kind,
            context,
            occurred_at: occurred_at.into(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: Value) -> Self {
        self.metadata.insert(key.into(), value);
        self
    }

    pub fn telemetry_attributes(&self) -> BTreeMap<String, String> {
        let mut attributes = BTreeMap::from([
            ("deployment.event".to_owned(), self.kind.as_str().to_owned()),
            (
                "graphblocks.release.id".to_owned(),
                self.context.release_id.clone(),
            ),
            (
                "graphblocks.deployment.revision".to_owned(),
                self.context.deployment_revision_id.clone(),
            ),
        ]);
        if let Some(release_digest) = &self.context.release_digest {
            attributes.insert(
                "graphblocks.release.digest".to_owned(),
                release_digest.clone(),
            );
        }
        if let Some(rollout_id) = &self.context.rollout_id {
            attributes.insert("graphblocks.rollout.id".to_owned(), rollout_id.clone());
        }
        if let Some(rollout_step) = &self.context.rollout_step {
            attributes.insert("graphblocks.rollout.step".to_owned(), rollout_step.clone());
        }
        if let Some(cohort) = &self.context.cohort {
            attributes.insert("graphblocks.rollout.cohort".to_owned(), cohort.clone());
        }
        attributes
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub enum ExecutionTargetKind {
    Service,
    WorkerPool,
    JobPool,
    SandboxPool,
    StatefulService,
    External,
}

impl ExecutionTargetKind {
    fn as_str(self) -> &'static str {
        match self {
            Self::Service => "service",
            Self::WorkerPool => "worker_pool",
            Self::JobPool => "job_pool",
            Self::SandboxPool => "sandbox_pool",
            Self::StatefulService => "stateful_service",
            Self::External => "external",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExecutionTarget {
    pub target_id: String,
    pub kind: ExecutionTargetKind,
    pub execution_host: String,
    pub capabilities: BTreeSet<String>,
    pub effects: BTreeSet<String>,
    pub package_lock: Option<String>,
    pub image: Option<String>,
}

impl ExecutionTarget {
    pub fn new(
        target_id: impl Into<String>,
        kind: ExecutionTargetKind,
        execution_host: impl Into<String>,
    ) -> Self {
        Self {
            target_id: target_id.into(),
            kind,
            execution_host: execution_host.into(),
            capabilities: BTreeSet::new(),
            effects: BTreeSet::new(),
            package_lock: None,
            image: None,
        }
    }

    pub fn with_capabilities<I, S>(mut self, capabilities: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.capabilities = capabilities.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_effects<I, S>(mut self, effects: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.effects = effects.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_package_lock(mut self, package_lock: impl Into<String>) -> Self {
        self.package_lock = Some(package_lock.into());
        self
    }

    pub fn with_image(mut self, image: impl Into<String>) -> Self {
        self.image = Some(image.into());
        self
    }

    fn canonical_value(&self) -> Value {
        json!({
            "target_id": self.target_id,
            "kind": self.kind.as_str(),
            "execution_host": self.execution_host,
            "capabilities": self.capabilities,
            "effects": self.effects,
            "package_lock": self.package_lock,
            "image": self.image,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
enum PlacementPriority {
    ExecutionClass = 1,
    Capability = 2,
    Block = 3,
    ExecutionGroup = 4,
    Node = 5,
}

impl PlacementPriority {
    fn as_str(self) -> &'static str {
        match self {
            Self::Node => "node",
            Self::ExecutionGroup => "execution_group",
            Self::Block => "block",
            Self::Capability => "capability",
            Self::ExecutionClass => "execution_class",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PlacementSelector {
    Nodes(BTreeSet<String>),
    ExecutionGroups(BTreeSet<String>),
    Blocks(BTreeSet<String>),
    Capabilities(BTreeSet<String>),
    Effects(BTreeSet<String>),
    ExecutionClasses(BTreeSet<String>),
}

impl PlacementSelector {
    pub fn nodes<I, S>(nodes: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Nodes(nodes.into_iter().map(Into::into).collect())
    }

    pub fn execution_groups<I, S>(groups: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::ExecutionGroups(groups.into_iter().map(Into::into).collect())
    }

    pub fn blocks<I, S>(blocks: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Blocks(blocks.into_iter().map(Into::into).collect())
    }

    pub fn capabilities<I, S>(capabilities: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Capabilities(capabilities.into_iter().map(Into::into).collect())
    }

    pub fn effects<I, S>(effects: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Effects(effects.into_iter().map(Into::into).collect())
    }

    pub fn execution_classes<I, S>(classes: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::ExecutionClasses(classes.into_iter().map(Into::into).collect())
    }

    fn priority(&self) -> PlacementPriority {
        match self {
            Self::Nodes(_) => PlacementPriority::Node,
            Self::ExecutionGroups(_) => PlacementPriority::ExecutionGroup,
            Self::Blocks(_) => PlacementPriority::Block,
            Self::Capabilities(_) | Self::Effects(_) => PlacementPriority::Capability,
            Self::ExecutionClasses(_) => PlacementPriority::ExecutionClass,
        }
    }

    fn canonical_value(&self) -> Value {
        match self {
            Self::Nodes(values) => json!({"kind": "nodes", "values": values}),
            Self::ExecutionGroups(values) => {
                json!({"kind": "execution_groups", "values": values})
            }
            Self::Blocks(values) => json!({"kind": "blocks", "values": values}),
            Self::Capabilities(values) => json!({"kind": "capabilities", "values": values}),
            Self::Effects(values) => json!({"kind": "effects", "values": values}),
            Self::ExecutionClasses(values) => {
                json!({"kind": "execution_classes", "values": values})
            }
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PlacementRule {
    pub rule_id: String,
    pub selector: PlacementSelector,
    pub target_id: String,
}

impl PlacementRule {
    pub fn new(
        rule_id: impl Into<String>,
        selector: PlacementSelector,
        target_id: impl Into<String>,
    ) -> Self {
        Self {
            rule_id: rule_id.into(),
            selector,
            target_id: target_id.into(),
        }
    }

    fn canonical_value(&self) -> Value {
        json!({
            "rule_id": self.rule_id,
            "selector": self.selector.canonical_value(),
            "target_id": self.target_id,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PlacementError {
    NoCompatibleTarget {
        node_id: String,
    },
    UnknownTarget {
        target_id: String,
    },
    AmbiguousPlacement {
        node_id: String,
        priority: String,
        target_ids: Vec<String>,
    },
}

impl fmt::Display for PlacementError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoCompatibleTarget { node_id } => {
                write!(
                    formatter,
                    "no compatible deployment target for node {node_id:?}"
                )
            }
            Self::UnknownTarget { target_id } => {
                write!(formatter, "placement target {target_id:?} is not defined")
            }
            Self::AmbiguousPlacement {
                node_id,
                priority,
                target_ids,
            } => write!(
                formatter,
                "ambiguous placement for node {node_id:?} at priority {priority}: {target_ids:?}"
            ),
        }
    }
}

impl Error for PlacementError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ResolvedPlacement {
    pub node_id: String,
    pub target_id: String,
    pub rule_ids: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PhysicalExecutionPlan {
    pub release_digest: String,
    pub deployment_revision_id: String,
    pub graph_hash: String,
    pub package_lock_hash: Option<String>,
    pub targets: BTreeMap<String, ExecutionTarget>,
    pub placements: Vec<PlacementRule>,
    pub default_target: Option<String>,
}

impl PhysicalExecutionPlan {
    pub fn new(
        release_digest: impl Into<String>,
        deployment_revision_id: impl Into<String>,
        graph_hash: impl Into<String>,
    ) -> Self {
        Self {
            release_digest: release_digest.into(),
            deployment_revision_id: deployment_revision_id.into(),
            graph_hash: graph_hash.into(),
            package_lock_hash: None,
            targets: BTreeMap::new(),
            placements: Vec::new(),
            default_target: None,
        }
    }

    pub fn with_package_lock_hash(mut self, package_lock_hash: impl Into<String>) -> Self {
        self.package_lock_hash = Some(package_lock_hash.into());
        self
    }

    pub fn with_target(mut self, target: ExecutionTarget) -> Self {
        self.targets.insert(target.target_id.clone(), target);
        self
    }

    pub fn with_placement(mut self, placement: PlacementRule) -> Self {
        self.placements.push(placement);
        self
    }

    pub fn with_default_target(mut self, target_id: impl Into<String>) -> Self {
        self.default_target = Some(target_id.into());
        self
    }

    pub fn plan_hash(&self) -> String {
        let mut placements = self
            .placements
            .iter()
            .map(PlacementRule::canonical_value)
            .collect::<Vec<_>>();
        placements.sort_by(|left, right| left.to_string().cmp(&right.to_string()));
        canonical_hash(&json!({
            "release_digest": self.release_digest,
            "deployment_revision_id": self.deployment_revision_id,
            "graph_hash": self.graph_hash,
            "package_lock_hash": self.package_lock_hash,
            "targets": self.targets.values().map(ExecutionTarget::canonical_value).collect::<Vec<_>>(),
            "placements": placements,
            "default_target": self.default_target,
        }))
    }

    pub fn resolve_target<I, S, J, E>(
        &self,
        node_id: impl AsRef<str>,
        execution_group: Option<&str>,
        block_id: impl AsRef<str>,
        capabilities: I,
        effects: J,
        execution_class: Option<&str>,
    ) -> Result<ResolvedPlacement, PlacementError>
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
        J: IntoIterator<Item = E>,
        E: Into<String>,
    {
        let node_id = node_id.as_ref();
        let block_id = block_id.as_ref();
        let capabilities = capabilities
            .into_iter()
            .map(Into::into)
            .collect::<BTreeSet<_>>();
        let effects = effects.into_iter().map(Into::into).collect::<BTreeSet<_>>();

        let mut matches = self
            .placements
            .iter()
            .filter(|rule| {
                selector_matches(
                    &rule.selector,
                    node_id,
                    execution_group,
                    block_id,
                    &capabilities,
                    &effects,
                    execution_class,
                )
            })
            .map(|rule| (rule.selector.priority(), rule))
            .collect::<Vec<_>>();

        if matches.is_empty()
            && let Some(default_target) = &self.default_target
        {
            if !self.targets.contains_key(default_target) {
                return Err(PlacementError::UnknownTarget {
                    target_id: default_target.clone(),
                });
            }
            return Ok(ResolvedPlacement {
                node_id: node_id.to_owned(),
                target_id: default_target.clone(),
                rule_ids: Vec::new(),
            });
        }
        if matches.is_empty() {
            return Err(PlacementError::NoCompatibleTarget {
                node_id: node_id.to_owned(),
            });
        }

        matches.sort_by(|left, right| right.0.cmp(&left.0));
        let best_priority = matches[0].0;
        let best = matches
            .into_iter()
            .filter(|(priority, _)| *priority == best_priority)
            .map(|(_, rule)| rule)
            .collect::<Vec<_>>();
        let target_ids = best
            .iter()
            .map(|rule| rule.target_id.clone())
            .collect::<BTreeSet<_>>();
        if target_ids.len() > 1 {
            return Err(PlacementError::AmbiguousPlacement {
                node_id: node_id.to_owned(),
                priority: best_priority.as_str().to_owned(),
                target_ids: target_ids.into_iter().collect(),
            });
        }

        let target_id = best[0].target_id.clone();
        if !self.targets.contains_key(&target_id) {
            return Err(PlacementError::UnknownTarget { target_id });
        }
        Ok(ResolvedPlacement {
            node_id: node_id.to_owned(),
            target_id,
            rule_ids: best.iter().map(|rule| rule.rule_id.clone()).collect(),
        })
    }
}

fn selector_matches(
    selector: &PlacementSelector,
    node_id: &str,
    execution_group: Option<&str>,
    block_id: &str,
    capabilities: &BTreeSet<String>,
    effects: &BTreeSet<String>,
    execution_class: Option<&str>,
) -> bool {
    match selector {
        PlacementSelector::Nodes(nodes) => nodes.contains(node_id),
        PlacementSelector::ExecutionGroups(groups) => {
            execution_group.is_some_and(|group| groups.contains(group))
        }
        PlacementSelector::Blocks(blocks) => blocks.contains(block_id),
        PlacementSelector::Capabilities(required) => !required.is_disjoint(capabilities),
        PlacementSelector::Effects(required) => !required.is_disjoint(effects),
        PlacementSelector::ExecutionClasses(classes) => {
            execution_class.is_some_and(|class| classes.contains(class))
        }
    }
}

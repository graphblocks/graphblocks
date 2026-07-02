use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::typed_value::{
    RemoteBoundaryValuePolicy, RemoteBoundaryValuePolicyError, TypedValue, ValueEncoding,
};

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
pub struct ReleaseLockRef {
    pub reference: String,
    pub digest: Option<String>,
    pub lock_type: Option<String>,
}

impl ReleaseLockRef {
    pub fn new(reference: impl Into<String>) -> Self {
        Self {
            reference: reference.into(),
            digest: None,
            lock_type: None,
        }
    }

    pub fn with_digest(mut self, digest: impl Into<String>) -> Self {
        self.digest = Some(digest.into());
        self
    }

    pub fn with_lock_type(mut self, lock_type: impl Into<String>) -> Self {
        self.lock_type = Some(lock_type.into());
        self
    }

    pub fn canonical_value(&self) -> Value {
        json!({
            "ref": self.reference,
            "digest": self.digest,
            "lock_type": self.lock_type,
        })
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct SupplyChainLock {
    pub sbom_ref: Option<String>,
    pub provenance_ref: Option<String>,
    pub signature_policy: Option<String>,
}

impl SupplyChainLock {
    pub fn new<S, P, I>(
        sbom_ref: Option<S>,
        provenance_ref: Option<P>,
        signature_policy: Option<I>,
    ) -> Self
    where
        S: Into<String>,
        P: Into<String>,
        I: Into<String>,
    {
        Self {
            sbom_ref: sbom_ref.map(Into::into),
            provenance_ref: provenance_ref.map(Into::into),
            signature_policy: signature_policy.map(Into::into),
        }
    }

    pub fn canonical_value(&self) -> Value {
        json!({
            "sbom_ref": self.sbom_ref,
            "provenance_ref": self.provenance_ref,
            "signature_policy": self.signature_policy,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CallbackIngressRoute {
    pub path: String,
    pub command: String,
}

impl CallbackIngressRoute {
    pub fn new(path: impl Into<String>, command: impl Into<String>) -> Self {
        Self {
            path: path.into(),
            command: command.into(),
        }
    }

    fn from_value(value: &Value) -> Result<Self, DeploymentTargetProfileError> {
        let Some(object) = value.as_object() else {
            return Err(DeploymentTargetProfileError::new(
                "callback ingress route must be a mapping",
            ));
        };
        let route = Self {
            path: required_manifest_string(object, &["path"], "callbackIngress.routes[].path")?,
            command: required_manifest_string(
                object,
                &["command"],
                "callbackIngress.routes[].command",
            )?,
        };
        route.validate()?;
        Ok(route)
    }

    fn validate(&self) -> Result<(), DeploymentTargetProfileError> {
        if self.path.trim().is_empty() {
            return Err(DeploymentTargetProfileError::new(
                "callback ingress route path must not be empty",
            ));
        }
        if self.command.trim().is_empty() {
            return Err(DeploymentTargetProfileError::new(
                "callback ingress route command must not be empty",
            ));
        }
        Ok(())
    }

    fn canonical_value(&self) -> Value {
        json!({
            "path": self.path,
            "command": self.command,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CallbackIngressSecurity {
    pub require_signature: bool,
    pub anti_enumeration: bool,
}

impl Default for CallbackIngressSecurity {
    fn default() -> Self {
        Self {
            require_signature: true,
            anti_enumeration: true,
        }
    }
}

impl CallbackIngressSecurity {
    fn from_value(value: Option<&Value>) -> Result<Self, DeploymentTargetProfileError> {
        let Some(value) = value else {
            return Ok(Self::default());
        };
        let Some(object) = value.as_object() else {
            return Err(DeploymentTargetProfileError::new(
                "callback ingress security must be a mapping",
            ));
        };
        Ok(Self {
            require_signature: optional_manifest_bool(
                object,
                &["requireSignature", "require_signature"],
            )?
            .unwrap_or(true),
            anti_enumeration: optional_manifest_bool(
                object,
                &["antiEnumeration", "anti_enumeration"],
            )?
            .unwrap_or(true),
        })
    }

    fn canonical_value(&self) -> Value {
        json!({
            "require_signature": self.require_signature,
            "anti_enumeration": self.anti_enumeration,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CallbackIngressLimits {
    pub max_payload_bytes: usize,
    pub max_requests_per_second: u32,
}

impl Default for CallbackIngressLimits {
    fn default() -> Self {
        Self {
            max_payload_bytes: 262_144,
            max_requests_per_second: 100,
        }
    }
}

impl CallbackIngressLimits {
    fn from_value(value: Option<&Value>) -> Result<Self, DeploymentTargetProfileError> {
        let Some(value) = value else {
            return Ok(Self::default());
        };
        let Some(object) = value.as_object() else {
            return Err(DeploymentTargetProfileError::new(
                "callback ingress limits must be a mapping",
            ));
        };
        Ok(Self {
            max_payload_bytes: optional_manifest_usize(
                object,
                &["maxPayloadBytes", "max_payload_bytes"],
                "callbackIngress.limits.maxPayloadBytes",
            )?
            .unwrap_or(262_144),
            max_requests_per_second: optional_manifest_u32(
                object,
                &["maxRequestsPerSecond", "max_requests_per_second"],
                "callbackIngress.limits.maxRequestsPerSecond",
            )?
            .unwrap_or(100),
        })
    }

    fn canonical_value(&self) -> Value {
        json!({
            "max_payload_bytes": self.max_payload_bytes,
            "max_requests_per_second": self.max_requests_per_second,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CallbackIngressDiagnostic {
    pub code: String,
    pub field: String,
    pub message: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CallbackIngressConfig {
    pub enabled: bool,
    pub routes: Vec<CallbackIngressRoute>,
    pub security: CallbackIngressSecurity,
    pub limits: CallbackIngressLimits,
}

impl CallbackIngressConfig {
    pub fn from_document(document: &Value) -> Result<Self, DeploymentTargetProfileError> {
        let Some(object) = document.as_object() else {
            return Err(DeploymentTargetProfileError::new(
                "callback ingress manifest must be a mapping",
            ));
        };
        let enabled = optional_manifest_bool(object, &["enabled"])?.unwrap_or(false);
        let routes = match object.get("routes") {
            Some(Value::Array(routes)) => routes
                .iter()
                .map(CallbackIngressRoute::from_value)
                .collect::<Result<Vec<_>, _>>()?,
            Some(_) => {
                return Err(DeploymentTargetProfileError::new(
                    "callback ingress routes must be a list",
                ));
            }
            None => Vec::new(),
        };
        let config = Self {
            enabled,
            routes,
            security: CallbackIngressSecurity::from_value(object.get("security"))?,
            limits: CallbackIngressLimits::from_value(object.get("limits"))?,
        };
        config.validate()?;
        Ok(config)
    }

    pub fn manifest_contract(&self) -> Value {
        json!({
            "enabled": self.enabled,
            "routes": self.routes.iter().map(CallbackIngressRoute::canonical_value).collect::<Vec<_>>(),
            "security": self.security.canonical_value(),
            "limits": self.limits.canonical_value(),
        })
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&self.manifest_contract())
    }

    pub fn diagnostics(&self) -> Vec<CallbackIngressDiagnostic> {
        if self.enabled && !self.security.require_signature {
            vec![CallbackIngressDiagnostic {
                code: "GB6002".to_owned(),
                field: "callbackIngress.security.requireSignature".to_owned(),
                message: "enabled callback ingress must require authenticated callback signatures"
                    .to_owned(),
            }]
        } else {
            Vec::new()
        }
    }

    fn validate(&self) -> Result<(), DeploymentTargetProfileError> {
        if self.enabled
            && !self
                .routes
                .iter()
                .any(|route| route.command == "SubmitAsyncCallback")
        {
            return Err(DeploymentTargetProfileError::new(
                "enabled callback ingress requires a SubmitAsyncCallback route",
            ));
        }
        if self.limits.max_payload_bytes == 0 {
            return Err(DeploymentTargetProfileError::new(
                "callback ingress maxPayloadBytes must be positive",
            ));
        }
        if self.limits.max_requests_per_second == 0 {
            return Err(DeploymentTargetProfileError::new(
                "callback ingress maxRequestsPerSecond must be positive",
            ));
        }
        Ok(())
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
    pub locks: BTreeMap<String, ReleaseLockRef>,
    pub prompt_locks: BTreeMap<String, PromptLock>,
    pub knowledge: BTreeMap<String, KnowledgeBinding>,
    pub supply_chain: Option<SupplyChainLock>,
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
            locks: BTreeMap::new(),
            prompt_locks: BTreeMap::new(),
            knowledge: BTreeMap::new(),
            supply_chain: None,
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

    pub fn with_lock(mut self, lock_name: impl Into<String>, lock: ReleaseLockRef) -> Self {
        self.locks.insert(lock_name.into(), lock);
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

    pub fn with_supply_chain(mut self, supply_chain: SupplyChainLock) -> Self {
        self.supply_chain = Some(supply_chain);
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
            "locks": self.locks.iter().map(|(name, lock)| (name, lock.canonical_value())).collect::<BTreeMap<_, _>>(),
            "prompt_locks": self.prompt_locks.iter().map(|(name, prompt)| (name, prompt.canonical_value())).collect::<BTreeMap<_, _>>(),
            "knowledge": self.knowledge.iter().map(|(name, binding)| (name, binding.canonical_value())).collect::<BTreeMap<_, _>>(),
            "supply_chain": self.supply_chain.as_ref().map(SupplyChainLock::canonical_value),
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
        for (name, lock) in &self.locks {
            if lock
                .digest
                .as_deref()
                .is_none_or(|digest| !is_sha256_digest(digest))
                && !lock.reference.contains("@sha256:")
            {
                references.push(format!("locks.{name}.digest"));
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
        if let Some(supply_chain) = &self.supply_chain {
            if supply_chain
                .provenance_ref
                .as_deref()
                .is_some_and(|reference| !reference.contains("@sha256:"))
            {
                references.push("supply_chain.provenance_ref".to_owned());
            }
            if supply_chain
                .sbom_ref
                .as_deref()
                .is_some_and(|reference| !reference.contains("@sha256:"))
            {
                references.push("supply_chain.sbom_ref".to_owned());
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeploymentConditionError {
    pub message: String,
}

impl DeploymentConditionError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for DeploymentConditionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl Error for DeploymentConditionError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeploymentCondition {
    pub condition_type: String,
    pub status: String,
    pub reason: String,
    pub message: String,
}

impl DeploymentCondition {
    pub fn new(
        condition_type: impl Into<String>,
        status: impl Into<String>,
        reason: impl Into<String>,
        message: impl Into<String>,
    ) -> Result<Self, DeploymentConditionError> {
        let condition = Self {
            condition_type: condition_type.into(),
            status: status.into(),
            reason: reason.into(),
            message: message.into(),
        };
        if condition.condition_type.trim().is_empty() {
            return Err(DeploymentConditionError::new(
                "deployment condition type must not be empty",
            ));
        }
        if !matches!(condition.status.as_str(), "true" | "false" | "unknown") {
            return Err(DeploymentConditionError::new(format!(
                "invalid deployment condition status {:?}",
                condition.status
            )));
        }
        if condition.reason.trim().is_empty() {
            return Err(DeploymentConditionError::new(
                "deployment condition reason must not be empty",
            ));
        }
        Ok(condition)
    }

    pub fn condition_contract(&self) -> Value {
        json!({
            "type": self.condition_type,
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeploymentSloReport {
    pub slo_id: String,
    pub status: String,
}

impl DeploymentSloReport {
    pub fn passed(slo_id: impl Into<String>) -> Self {
        Self {
            slo_id: slo_id.into(),
            status: "pass".to_owned(),
        }
    }

    pub fn failed(slo_id: impl Into<String>) -> Self {
        Self {
            slo_id: slo_id.into(),
            status: "fail".to_owned(),
        }
    }

    pub fn no_data(slo_id: impl Into<String>) -> Self {
        Self {
            slo_id: slo_id.into(),
            status: "no_data".to_owned(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeploymentSloProfile {
    pub profile_id: String,
    pub slo_objective_ids: BTreeSet<String>,
}

impl DeploymentSloProfile {
    pub fn new<I, S>(profile_id: impl Into<String>, slo_objective_ids: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        let profile_id = profile_id.into();
        let slo_objective_ids = slo_objective_ids
            .into_iter()
            .map(Into::into)
            .filter(|item: &String| !item.trim().is_empty())
            .collect::<BTreeSet<_>>();
        assert!(
            !profile_id.trim().is_empty(),
            "deployment SLO profile id must not be empty"
        );
        assert!(
            !slo_objective_ids.is_empty(),
            "deployment SLO profile requires at least one SLO objective"
        );
        Self {
            profile_id,
            slo_objective_ids,
        }
    }

    pub fn evaluate_slo_reports<I>(&self, reports: I) -> DeploymentCondition
    where
        I: IntoIterator<Item = DeploymentSloReport>,
    {
        let reports_by_id = reports
            .into_iter()
            .map(|report| (report.slo_id.clone(), report))
            .collect::<BTreeMap<_, _>>();
        let mut failed = Vec::new();
        let mut missing_or_no_data = Vec::new();
        for objective_id in &self.slo_objective_ids {
            match reports_by_id
                .get(objective_id)
                .map(|report| report.status.as_str())
            {
                Some("pass") => {}
                Some("no_data") | None => missing_or_no_data.push(objective_id.clone()),
                Some(_) => failed.push(objective_id.clone()),
            }
        }
        if !failed.is_empty() {
            return DeploymentCondition::new(
                "SLOWithinBudget",
                "false",
                "slo_failed",
                format!("failed SLO objectives: {}", failed.join(", ")),
            )
            .expect("static SLO condition must be valid");
        }
        if !missing_or_no_data.is_empty() {
            return DeploymentCondition::new(
                "SLOWithinBudget",
                "unknown",
                "slo_no_data",
                format!(
                    "missing or no-data SLO objectives: {}",
                    missing_or_no_data.join(", ")
                ),
            )
            .expect("static SLO condition must be valid");
        }
        DeploymentCondition::new("SLOWithinBudget", "true", "slo_within_budget", "")
            .expect("static SLO condition must be valid")
    }

    pub fn profile_contract(&self) -> Value {
        json!({
            "profile_id": self.profile_id,
            "slo_objective_ids": self.slo_objective_ids,
        })
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&self.profile_contract())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RecoveryObjective {
    pub target: String,
    pub rto: String,
    pub rpo: String,
}

impl RecoveryObjective {
    pub fn new(target: impl Into<String>, rto: impl Into<String>, rpo: impl Into<String>) -> Self {
        let objective = Self {
            target: target.into(),
            rto: rto.into(),
            rpo: rpo.into(),
        };
        assert!(
            !objective.target.trim().is_empty(),
            "recovery objective target must not be empty"
        );
        assert!(
            !objective.rto.trim().is_empty(),
            "recovery objective rto must not be empty"
        );
        assert!(
            !objective.rpo.trim().is_empty(),
            "recovery objective rpo must not be empty"
        );
        objective
    }

    pub fn objective_contract(&self) -> Value {
        json!({
            "target": self.target,
            "rto": self.rto,
            "rpo": self.rpo,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeploymentRecoveryProfile {
    pub profile_id: String,
    pub objectives: BTreeMap<String, RecoveryObjective>,
    pub knowledge_index_rebuildable_from: BTreeSet<String>,
    pub regional_failover_mode: Option<String>,
    pub max_restore_test_age_seconds: Option<u64>,
}

impl DeploymentRecoveryProfile {
    pub fn new(profile_id: impl Into<String>) -> Self {
        let profile_id = profile_id.into();
        assert!(
            !profile_id.trim().is_empty(),
            "deployment recovery profile id must not be empty"
        );
        Self {
            profile_id,
            objectives: BTreeMap::new(),
            knowledge_index_rebuildable_from: BTreeSet::new(),
            regional_failover_mode: None,
            max_restore_test_age_seconds: None,
        }
    }

    pub fn with_objective(
        mut self,
        target: impl Into<String>,
        rto: impl Into<String>,
        rpo: impl Into<String>,
    ) -> Self {
        let objective = RecoveryObjective::new(target, rto, rpo);
        self.objectives.insert(objective.target.clone(), objective);
        self
    }

    pub fn with_knowledge_index_sources<I, S>(mut self, sources: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.knowledge_index_rebuildable_from = sources
            .into_iter()
            .map(Into::into)
            .filter(|item: &String| !item.trim().is_empty())
            .collect();
        self
    }

    pub fn with_regional_failover(mut self, mode: impl Into<String>) -> Self {
        let mode = mode.into();
        assert!(
            !mode.trim().is_empty(),
            "regional failover mode must not be empty"
        );
        self.regional_failover_mode = Some(mode);
        self
    }

    pub fn with_max_restore_test_age_seconds(mut self, max_age_seconds: u64) -> Self {
        assert!(max_age_seconds > 0, "restore test max age must be positive");
        self.max_restore_test_age_seconds = Some(max_age_seconds);
        self
    }

    pub fn evaluate_restore_test(
        &self,
        tested_at_unix_seconds: Option<u64>,
        now_unix_seconds: u64,
        passed: bool,
    ) -> DeploymentCondition {
        if !passed {
            return DeploymentCondition::new(
                "RecoveryTestCurrent",
                "false",
                "restore_test_failed",
                "",
            )
            .expect("static recovery condition must be valid");
        }
        let Some(tested_at_unix_seconds) = tested_at_unix_seconds else {
            return DeploymentCondition::new(
                "RecoveryTestCurrent",
                "unknown",
                "restore_test_missing",
                "",
            )
            .expect("static recovery condition must be valid");
        };
        let Some(age_seconds) = now_unix_seconds.checked_sub(tested_at_unix_seconds) else {
            return DeploymentCondition::new(
                "RecoveryTestCurrent",
                "unknown",
                "restore_test_in_future",
                "",
            )
            .expect("static recovery condition must be valid");
        };
        if self
            .max_restore_test_age_seconds
            .is_some_and(|max_age| age_seconds > max_age)
        {
            let max_age = self.max_restore_test_age_seconds.unwrap_or_default();
            return DeploymentCondition::new(
                "RecoveryTestCurrent",
                "false",
                "restore_test_stale",
                format!("last restore test age {age_seconds}s exceeds {max_age}s"),
            )
            .expect("static recovery condition must be valid");
        }
        DeploymentCondition::new("RecoveryTestCurrent", "true", "restore_test_current", "")
            .expect("static recovery condition must be valid")
    }

    pub fn recovery_contract(&self) -> Value {
        json!({
            "profile_id": self.profile_id,
            "objectives": self.objectives.values().map(RecoveryObjective::objective_contract).collect::<Vec<_>>(),
            "knowledge_index_rebuildable_from": self.knowledge_index_rebuildable_from,
            "regional_failover_mode": self.regional_failover_mode,
            "max_restore_test_age_seconds": self.max_restore_test_age_seconds,
        })
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&self.recovery_contract())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RolloutError {
    pub message: String,
}

impl RolloutError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for RolloutError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl Error for RolloutError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RolloutStep {
    pub step_id: String,
    pub kind: String,
    pub traffic_percent: u8,
    pub minimum_samples: Option<u64>,
    pub minimum_duration_seconds: Option<u64>,
    pub effects: String,
}

impl RolloutStep {
    pub fn validate(step_id: impl Into<String>) -> Self {
        Self::new(step_id, "validate", 0, None, None, "normal")
    }

    pub fn shadow(step_id: impl Into<String>) -> Self {
        Self::new(step_id, "shadow", 0, None, None, "suppress")
    }

    pub fn canary(step_id: impl Into<String>, traffic_percent: u8) -> Self {
        assert!(
            traffic_percent <= 100,
            "rollout traffic_percent must be between 0 and 100"
        );
        Self::new(step_id, "canary", traffic_percent, None, None, "normal")
    }

    pub fn promote(step_id: impl Into<String>) -> Self {
        Self::new(step_id, "promote", 100, None, None, "normal")
    }

    pub fn with_minimum_samples(mut self, minimum_samples: u64) -> Self {
        assert!(
            minimum_samples > 0,
            "rollout minimum_samples must be positive"
        );
        self.minimum_samples = Some(minimum_samples);
        self
    }

    pub fn with_minimum_duration_seconds(mut self, minimum_duration_seconds: u64) -> Self {
        assert!(
            minimum_duration_seconds > 0,
            "rollout minimum_duration_seconds must be positive"
        );
        self.minimum_duration_seconds = Some(minimum_duration_seconds);
        self
    }

    pub fn with_effects(mut self, effects: impl Into<String>) -> Self {
        let effects = effects.into();
        assert!(
            matches!(effects.as_str(), "normal" | "suppress" | "sandbox"),
            "invalid rollout effects mode {effects:?}"
        );
        self.effects = effects;
        self
    }

    fn new(
        step_id: impl Into<String>,
        kind: impl Into<String>,
        traffic_percent: u8,
        minimum_samples: Option<u64>,
        minimum_duration_seconds: Option<u64>,
        effects: impl Into<String>,
    ) -> Self {
        let step_id = step_id.into();
        let kind = kind.into();
        let effects = effects.into();
        assert!(
            !step_id.trim().is_empty(),
            "rollout step_id must not be empty"
        );
        assert!(
            matches!(
                kind.as_str(),
                "validate" | "shadow" | "canary" | "blue_green" | "promote"
            ),
            "invalid rollout step kind {kind:?}"
        );
        assert!(
            matches!(effects.as_str(), "normal" | "suppress" | "sandbox"),
            "invalid rollout effects mode {effects:?}"
        );
        Self {
            step_id,
            kind,
            traffic_percent,
            minimum_samples,
            minimum_duration_seconds,
            effects,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RolloutAnalysisResult {
    pub step_id: String,
    pub passed: bool,
    pub sample_count: u64,
    pub duration_seconds: u64,
    pub metrics: BTreeMap<String, Value>,
    pub reason: Option<String>,
    pub non_reversible_effect_observed: bool,
}

impl RolloutAnalysisResult {
    pub fn passed(step_id: impl Into<String>) -> Self {
        Self {
            step_id: step_id.into(),
            passed: true,
            sample_count: 0,
            duration_seconds: 0,
            metrics: BTreeMap::new(),
            reason: None,
            non_reversible_effect_observed: false,
        }
    }

    pub fn failed(step_id: impl Into<String>, reason: impl Into<String>) -> Self {
        Self {
            step_id: step_id.into(),
            passed: false,
            sample_count: 0,
            duration_seconds: 0,
            metrics: BTreeMap::new(),
            reason: Some(reason.into()),
            non_reversible_effect_observed: false,
        }
    }

    pub fn with_sample_count(mut self, sample_count: u64) -> Self {
        self.sample_count = sample_count;
        self
    }

    pub fn with_duration_seconds(mut self, duration_seconds: u64) -> Self {
        self.duration_seconds = duration_seconds;
        self
    }

    pub fn with_metric(mut self, key: impl Into<String>, value: Value) -> Self {
        self.metrics.insert(key.into(), value);
        self
    }

    pub fn with_non_reversible_effect_observed(
        mut self,
        non_reversible_effect_observed: bool,
    ) -> Self {
        self.non_reversible_effect_observed = non_reversible_effect_observed;
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RolloutDecision {
    pub decision: String,
    pub reason: String,
    pub next_state: RolloutState,
    pub automatic_rollback_allowed: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RolloutPlan {
    pub rollout_id: String,
    pub stable_revision_id: String,
    pub candidate_revision_id: String,
    pub strategy: String,
    pub affinity: Option<String>,
    pub analysis_profile_ref: Option<String>,
    pub steps: Vec<RolloutStep>,
}

impl RolloutPlan {
    pub fn canary<I>(
        rollout_id: impl Into<String>,
        stable_revision_id: impl Into<String>,
        candidate_revision_id: impl Into<String>,
        canary_steps: I,
    ) -> Self
    where
        I: IntoIterator<Item = RolloutStep>,
    {
        let canary_steps = canary_steps.into_iter().collect::<Vec<_>>();
        assert!(
            !canary_steps.is_empty(),
            "canary rollout requires at least one canary step"
        );
        assert!(
            canary_steps.iter().all(|step| step.kind == "canary"),
            "canary rollout canary_steps must all have kind 'canary'"
        );

        let mut steps = vec![
            RolloutStep::validate("validate"),
            RolloutStep::shadow("shadow"),
        ];
        steps.extend(canary_steps);
        steps.push(RolloutStep::promote("promote"));

        let rollout_id = rollout_id.into();
        let stable_revision_id = stable_revision_id.into();
        let candidate_revision_id = candidate_revision_id.into();
        assert!(
            !rollout_id.trim().is_empty(),
            "rollout_id must not be empty"
        );
        assert!(
            !stable_revision_id.trim().is_empty(),
            "stable_revision_id must not be empty"
        );
        assert!(
            !candidate_revision_id.trim().is_empty(),
            "candidate_revision_id must not be empty"
        );

        Self {
            rollout_id,
            stable_revision_id,
            candidate_revision_id,
            strategy: "canary".to_owned(),
            affinity: None,
            analysis_profile_ref: None,
            steps,
        }
    }

    pub fn with_affinity(mut self, affinity: impl Into<String>) -> Self {
        self.affinity = Some(affinity.into());
        self
    }

    pub fn with_analysis_profile(mut self, analysis_profile_ref: impl Into<String>) -> Self {
        self.analysis_profile_ref = Some(analysis_profile_ref.into());
        self
    }

    pub fn initial_state(&self) -> RolloutState {
        RolloutState {
            plan: self.clone(),
            current_step_index: 0,
            status: "running".to_owned(),
        }
    }

    pub fn current_step(&self, index: usize) -> Result<&RolloutStep, RolloutError> {
        self.steps
            .get(index)
            .ok_or_else(|| RolloutError::new("rollout step index out of range"))
    }

    pub fn assign_revision(&self, affinity_key: &str, step: &RolloutStep) -> String {
        if step.traffic_percent == 0 {
            return self.stable_revision_id.clone();
        }
        if step.traffic_percent >= 100 {
            return self.candidate_revision_id.clone();
        }

        let bucket_digest = canonical_hash(&json!({
            "rollout_id": self.rollout_id,
            "affinity": self.affinity,
            "affinity_key": affinity_key,
        }));
        let bucket_hex = bucket_digest
            .strip_prefix("sha256:")
            .unwrap_or(bucket_digest.as_str());
        let bucket = bucket_hex
            .get(..8)
            .and_then(|prefix| u32::from_str_radix(prefix, 16).ok())
            .unwrap_or(0)
            % 100;
        if bucket < u32::from(step.traffic_percent) {
            self.candidate_revision_id.clone()
        } else {
            self.stable_revision_id.clone()
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RolloutState {
    pub plan: RolloutPlan,
    pub current_step_index: usize,
    pub status: String,
}

impl RolloutState {
    pub fn current_step(&self) -> Result<&RolloutStep, RolloutError> {
        self.plan.current_step(self.current_step_index)
    }

    pub fn advance_for_test(&self, current_step_index: usize) -> Result<Self, RolloutError> {
        self.plan.current_step(current_step_index)?;
        Ok(Self {
            plan: self.plan.clone(),
            current_step_index,
            status: "running".to_owned(),
        })
    }

    pub fn evaluate_gate(
        &self,
        result: RolloutAnalysisResult,
    ) -> Result<RolloutDecision, RolloutError> {
        if self.status != "running" {
            return Ok(RolloutDecision {
                decision: "hold".to_owned(),
                reason: format!("rollout_{}", self.status),
                next_state: self.clone(),
                automatic_rollback_allowed: self.status != "aborted",
            });
        }

        let step = self.current_step()?;
        if result.step_id != step.step_id {
            return Err(RolloutError::new(format!(
                "analysis step {:?} does not match current rollout step {:?}",
                result.step_id, step.step_id
            )));
        }
        if step
            .minimum_samples
            .is_some_and(|minimum_samples| result.sample_count < minimum_samples)
        {
            return Ok(RolloutDecision {
                decision: "hold".to_owned(),
                reason: "minimum_samples_not_met".to_owned(),
                next_state: self.clone(),
                automatic_rollback_allowed: true,
            });
        }
        if step
            .minimum_duration_seconds
            .is_some_and(|minimum_duration_seconds| {
                result.duration_seconds < minimum_duration_seconds
            })
        {
            return Ok(RolloutDecision {
                decision: "hold".to_owned(),
                reason: "minimum_duration_not_met".to_owned(),
                next_state: self.clone(),
                automatic_rollback_allowed: true,
            });
        }
        if !result.passed {
            return Ok(RolloutDecision {
                decision: "abort".to_owned(),
                reason: result
                    .reason
                    .unwrap_or_else(|| "analysis_failed".to_owned()),
                next_state: Self {
                    plan: self.plan.clone(),
                    current_step_index: self.current_step_index,
                    status: "aborted".to_owned(),
                },
                automatic_rollback_allowed: !result.non_reversible_effect_observed,
            });
        }
        if step.kind == "promote" {
            return Ok(RolloutDecision {
                decision: "promote".to_owned(),
                reason: "promote_gate_passed".to_owned(),
                next_state: Self {
                    plan: self.plan.clone(),
                    current_step_index: self.current_step_index,
                    status: "promoted".to_owned(),
                },
                automatic_rollback_allowed: true,
            });
        }

        let next_step_index = (self.current_step_index + 1).min(self.plan.steps.len() - 1);
        Ok(RolloutDecision {
            decision: "advance".to_owned(),
            reason: "gate_passed".to_owned(),
            next_state: Self {
                plan: self.plan.clone(),
                current_step_index: next_step_index,
                status: "running".to_owned(),
            },
            automatic_rollback_allowed: true,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WorkloadKind {
    NewRequest,
    ExistingRequest,
    Conversation,
    DurableJob,
    RealtimeSession,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RevisionDecision {
    AdmitOnNew {
        revision_id: String,
    },
    FinishOnOld {
        revision_id: String,
    },
    KeepAffinity {
        revision_id: String,
    },
    CheckpointAndMigrate {
        from_revision_id: String,
        to_revision_id: String,
    },
    DrainOnOld {
        revision_id: String,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UpgradePolicy {
    pub old_revision_id: String,
    pub new_revision_id: String,
}

impl UpgradePolicy {
    pub fn workload_aware(
        old_revision_id: impl Into<String>,
        new_revision_id: impl Into<String>,
    ) -> Self {
        Self {
            old_revision_id: old_revision_id.into(),
            new_revision_id: new_revision_id.into(),
        }
    }

    pub fn decide(
        &self,
        workload: WorkloadKind,
        affinity_revision_id: Option<&str>,
        checkpoint_compatible: bool,
    ) -> RevisionDecision {
        match workload {
            WorkloadKind::NewRequest => RevisionDecision::AdmitOnNew {
                revision_id: self.new_revision_id.clone(),
            },
            WorkloadKind::ExistingRequest => RevisionDecision::FinishOnOld {
                revision_id: affinity_revision_id
                    .unwrap_or(&self.old_revision_id)
                    .to_owned(),
            },
            WorkloadKind::Conversation => {
                if let Some(revision_id) = affinity_revision_id {
                    RevisionDecision::KeepAffinity {
                        revision_id: revision_id.to_owned(),
                    }
                } else {
                    RevisionDecision::AdmitOnNew {
                        revision_id: self.new_revision_id.clone(),
                    }
                }
            }
            WorkloadKind::DurableJob => {
                if checkpoint_compatible {
                    RevisionDecision::CheckpointAndMigrate {
                        from_revision_id: affinity_revision_id
                            .unwrap_or(&self.old_revision_id)
                            .to_owned(),
                        to_revision_id: self.new_revision_id.clone(),
                    }
                } else {
                    RevisionDecision::FinishOnOld {
                        revision_id: affinity_revision_id
                            .unwrap_or(&self.old_revision_id)
                            .to_owned(),
                    }
                }
            }
            WorkloadKind::RealtimeSession => RevisionDecision::DrainOnOld {
                revision_id: affinity_revision_id
                    .unwrap_or(&self.old_revision_id)
                    .to_owned(),
            },
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkerDrainRoutingDecision {
    AdmitOnReplacement { worker_id: String },
    KeepAffinity { worker_id: String },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkerDrainPlan {
    pub draining_worker_id: String,
    pub replacement_worker_id: String,
    pub affinities: BTreeMap<String, String>,
}

impl WorkerDrainPlan {
    pub fn new(
        draining_worker_id: impl Into<String>,
        replacement_worker_id: impl Into<String>,
    ) -> Self {
        Self {
            draining_worker_id: draining_worker_id.into(),
            replacement_worker_id: replacement_worker_id.into(),
            affinities: BTreeMap::new(),
        }
    }

    pub fn with_affinity(
        mut self,
        affinity_key: impl Into<String>,
        worker_id: impl Into<String>,
    ) -> Self {
        self.affinities.insert(affinity_key.into(), worker_id.into());
        self
    }

    pub fn route(
        &self,
        affinity_key: &str,
        workload_kind: WorkloadKind,
    ) -> WorkerDrainRoutingDecision {
        if !matches!(workload_kind, WorkloadKind::NewRequest)
            && let Some(worker_id) = self.affinities.get(affinity_key)
        {
            return WorkerDrainRoutingDecision::KeepAffinity {
                worker_id: worker_id.clone(),
            };
        }
        WorkerDrainRoutingDecision::AdmitOnReplacement {
            worker_id: self.replacement_worker_id.clone(),
        }
    }

    pub fn can_complete_drain<I, S>(&self, completed_affinities: I) -> bool
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let completed = completed_affinities
            .into_iter()
            .map(|affinity| affinity.as_ref().to_owned())
            .collect::<BTreeSet<_>>();
        self.affinities
            .keys()
            .all(|affinity_key| completed.contains(affinity_key))
    }

    pub fn plan_contract(&self) -> Value {
        json!({
            "draining_worker_id": self.draining_worker_id,
            "replacement_worker_id": self.replacement_worker_id,
            "affinities": self.affinities.iter().map(|(affinity_key, worker_id)| {
                json!({
                    "affinity_key": affinity_key,
                    "worker_id": worker_id,
                })
            }).collect::<Vec<_>>(),
        })
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&self.plan_contract())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RemoteTraceContext {
    pub trace_id: String,
    pub span_id: String,
    pub parent_span_id: Option<String>,
    pub baggage: BTreeMap<String, String>,
}

impl RemoteTraceContext {
    pub fn new(trace_id: impl Into<String>, span_id: impl Into<String>) -> Self {
        Self {
            trace_id: trace_id.into(),
            span_id: span_id.into(),
            parent_span_id: None,
            baggage: BTreeMap::new(),
        }
    }

    pub fn with_parent_span_id(mut self, parent_span_id: impl Into<String>) -> Self {
        self.parent_span_id = Some(parent_span_id.into());
        self
    }

    pub fn with_baggage(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.baggage.insert(key.into(), value.into());
        self
    }

    fn canonical_value(&self) -> Value {
        json!({
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "baggage": self.baggage,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RemoteExecutionContext {
    pub run_id: String,
    pub node_id: String,
    pub attempt_id: String,
    pub release_id: String,
    pub trace: RemoteTraceContext,
    pub policy_snapshot_id: String,
    pub budget_permit_id: Option<String>,
}

impl RemoteExecutionContext {
    pub fn new(
        run_id: impl Into<String>,
        node_id: impl Into<String>,
        attempt_id: impl Into<String>,
        release_id: impl Into<String>,
        trace: RemoteTraceContext,
        policy_snapshot_id: impl Into<String>,
    ) -> Self {
        Self {
            run_id: run_id.into(),
            node_id: node_id.into(),
            attempt_id: attempt_id.into(),
            release_id: release_id.into(),
            trace,
            policy_snapshot_id: policy_snapshot_id.into(),
            budget_permit_id: None,
        }
    }

    pub fn with_budget_permit_id(mut self, budget_permit_id: impl Into<String>) -> Self {
        self.budget_permit_id = Some(budget_permit_id.into());
        self
    }

    fn canonical_value(&self) -> Value {
        json!({
            "run_id": self.run_id,
            "node_id": self.node_id,
            "attempt_id": self.attempt_id,
            "release_id": self.release_id,
            "trace": self.trace.canonical_value(),
            "policy_snapshot_id": self.policy_snapshot_id,
            "budget_permit_id": self.budget_permit_id,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RemoteExecutionEnvelope {
    pub envelope_id: String,
    pub target_id: String,
    pub worker_id: String,
    pub context: RemoteExecutionContext,
    pub inputs: BTreeMap<String, TypedValue>,
}

impl RemoteExecutionEnvelope {
    pub fn new(
        envelope_id: impl Into<String>,
        target_id: impl Into<String>,
        worker_id: impl Into<String>,
        context: RemoteExecutionContext,
    ) -> Self {
        Self {
            envelope_id: envelope_id.into(),
            target_id: target_id.into(),
            worker_id: worker_id.into(),
            context,
            inputs: BTreeMap::new(),
        }
    }

    pub fn with_input(mut self, port: impl Into<String>, value: TypedValue) -> Self {
        self.inputs.insert(port.into(), value);
        self
    }

    pub fn validate(
        &self,
        policy: &RemoteBoundaryValuePolicy,
    ) -> Result<(), RemoteBoundaryValuePolicyError> {
        for (port, value) in &self.inputs {
            policy.validate(&self.context.node_id, port, value)?;
        }
        Ok(())
    }

    pub fn context_contract(&self) -> Value {
        json!({
            "target_id": self.target_id,
            "worker_id": self.worker_id,
            "context": self.context.canonical_value(),
            "inputs": self.inputs.iter().map(|(port, value)| {
                json!({
                    "port": port,
                    "schema_id": value.schema_id(),
                    "schema_version": value.schema_version(),
                    "encoding": value_encoding_name(value.encoding()),
                    "payload_digest": payload_digest(value.payload()),
                })
            }).collect::<Vec<_>>(),
        })
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&self.context_contract())
    }
}

fn value_encoding_name(encoding: ValueEncoding) -> &'static str {
    match encoding {
        ValueEncoding::Json => "json",
        ValueEncoding::MessagePack => "messagepack",
        ValueEncoding::ArrowIpc => "arrow_ipc",
        ValueEncoding::RawBytes => "raw_bytes",
        ValueEncoding::ArtifactRef => "artifact_ref",
    }
}

fn payload_digest(payload: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(payload))
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

    fn from_manifest(value: &str) -> Result<Self, DeploymentTargetProfileError> {
        match value {
            "service" => Ok(Self::Service),
            "worker_pool" => Ok(Self::WorkerPool),
            "job_pool" => Ok(Self::JobPool),
            "sandbox_pool" => Ok(Self::SandboxPool),
            "stateful_service" => Ok(Self::StatefulService),
            "external" => Ok(Self::External),
            _ => Err(DeploymentTargetProfileError::new(format!(
                "invalid deployment target kind {value:?}"
            ))),
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkerAdvertisement {
    pub worker_id: String,
    pub target_id: String,
    pub protocol_version: String,
    pub package_lock_hash: Option<String>,
    pub capabilities: BTreeSet<String>,
}

impl WorkerAdvertisement {
    pub fn new(
        worker_id: impl Into<String>,
        target_id: impl Into<String>,
        protocol_version: impl Into<String>,
    ) -> Self {
        Self {
            worker_id: worker_id.into(),
            target_id: target_id.into(),
            protocol_version: protocol_version.into(),
            package_lock_hash: None,
            capabilities: BTreeSet::new(),
        }
    }

    pub fn with_package_lock_hash(mut self, package_lock_hash: impl Into<String>) -> Self {
        self.package_lock_hash = Some(package_lock_hash.into());
        self
    }

    pub fn with_capabilities<I, S>(mut self, capabilities: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.capabilities = capabilities.into_iter().map(Into::into).collect();
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkerAdmissionRequirement {
    pub target_id: String,
    pub protocol_version: String,
    pub package_lock_hash: Option<String>,
    pub required_capabilities: BTreeSet<String>,
}

impl WorkerAdmissionRequirement {
    pub fn new(target_id: impl Into<String>, protocol_version: impl Into<String>) -> Self {
        Self {
            target_id: target_id.into(),
            protocol_version: protocol_version.into(),
            package_lock_hash: None,
            required_capabilities: BTreeSet::new(),
        }
    }

    pub fn with_package_lock_hash(mut self, package_lock_hash: impl Into<String>) -> Self {
        self.package_lock_hash = Some(package_lock_hash.into());
        self
    }

    pub fn with_required_capabilities<I, S>(mut self, capabilities: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.required_capabilities = capabilities.into_iter().map(Into::into).collect();
        self
    }

    pub fn admit(&self, worker: &WorkerAdvertisement) -> Result<(), WorkerAdmissionError> {
        if worker.target_id != self.target_id {
            return Err(WorkerAdmissionError::TargetMismatch {
                worker_id: worker.worker_id.clone(),
                expected: self.target_id.clone(),
                actual: worker.target_id.clone(),
            });
        }
        if worker.protocol_version != self.protocol_version {
            return Err(WorkerAdmissionError::ProtocolMismatch {
                worker_id: worker.worker_id.clone(),
                expected: self.protocol_version.clone(),
                actual: worker.protocol_version.clone(),
            });
        }
        if let Some(expected_package_lock_hash) = &self.package_lock_hash
            && worker.package_lock_hash.as_ref() != Some(expected_package_lock_hash)
        {
            return Err(WorkerAdmissionError::PackageLockMismatch {
                worker_id: worker.worker_id.clone(),
                expected: expected_package_lock_hash.clone(),
                actual: worker.package_lock_hash.clone(),
            });
        }
        let missing = self
            .required_capabilities
            .difference(&worker.capabilities)
            .cloned()
            .collect::<Vec<_>>();
        if !missing.is_empty() {
            return Err(WorkerAdmissionError::MissingCapabilities {
                worker_id: worker.worker_id.clone(),
                missing,
            });
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkerAdmissionError {
    TargetMismatch {
        worker_id: String,
        expected: String,
        actual: String,
    },
    ProtocolMismatch {
        worker_id: String,
        expected: String,
        actual: String,
    },
    PackageLockMismatch {
        worker_id: String,
        expected: String,
        actual: Option<String>,
    },
    MissingCapabilities {
        worker_id: String,
        missing: Vec<String>,
    },
}

impl fmt::Display for WorkerAdmissionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TargetMismatch {
                worker_id,
                expected,
                actual,
            } => write!(
                formatter,
                "worker {worker_id:?} advertises target {actual:?}, expected {expected:?}"
            ),
            Self::ProtocolMismatch {
                worker_id,
                expected,
                actual,
            } => write!(
                formatter,
                "worker {worker_id:?} advertises protocol {actual:?}, expected {expected:?}"
            ),
            Self::PackageLockMismatch {
                worker_id,
                expected,
                actual,
            } => write!(
                formatter,
                "worker {worker_id:?} advertises package lock {actual:?}, expected {expected:?}"
            ),
            Self::MissingCapabilities { worker_id, missing } => {
                write!(
                    formatter,
                    "worker {worker_id:?} is missing required capabilities {missing:?}"
                )
            }
        }
    }
}

impl Error for WorkerAdmissionError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeploymentTargetProfileError {
    pub message: String,
}

impl DeploymentTargetProfileError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for DeploymentTargetProfileError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl Error for DeploymentTargetProfileError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeploymentTargetProfile {
    pub target_id: String,
    pub image_role: String,
    pub kind: ExecutionTargetKind,
    pub execution_host: String,
    pub capabilities: BTreeSet<String>,
    pub effects: BTreeSet<String>,
    pub package_lock: Option<String>,
    pub default_replicas: u32,
}

impl DeploymentTargetProfile {
    pub fn new(
        target_id: impl Into<String>,
        image_role: impl Into<String>,
        kind: ExecutionTargetKind,
        execution_host: impl Into<String>,
    ) -> Result<Self, DeploymentTargetProfileError> {
        Self {
            target_id: target_id.into(),
            image_role: image_role.into(),
            kind,
            execution_host: execution_host.into(),
            capabilities: BTreeSet::new(),
            effects: BTreeSet::new(),
            package_lock: None,
            default_replicas: 1,
        }
        .validate()
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

    pub fn with_default_replicas(mut self, default_replicas: u32) -> Self {
        self.default_replicas = default_replicas;
        self
    }

    pub fn from_value(value: &Value) -> Result<Self, DeploymentTargetProfileError> {
        let object = value.as_object().ok_or_else(|| {
            DeploymentTargetProfileError::new("deployment target manifest target must be a mapping")
        })?;
        let target_id = required_string_field(object, &["id", "targetId", "target_id"], "id")?;
        let image_role = required_string_field(object, &["imageRole", "image_role"], "imageRole")?;
        let kind = required_string_field(object, &["kind"], "kind")?;
        let execution_host = required_string_field(
            object,
            &["executionHost", "execution_host"],
            "executionHost",
        )?;
        let package_lock = optional_string_field(object, &["packageLock", "package_lock"])?;
        let default_replicas =
            optional_positive_u32_field(object, &["defaultReplicas", "default_replicas"])?
                .unwrap_or(1);

        Self {
            target_id,
            image_role,
            kind: ExecutionTargetKind::from_manifest(&kind)?,
            execution_host,
            capabilities: string_set_field(object, "capabilities")?,
            effects: string_set_field(object, "effects")?,
            package_lock,
            default_replicas,
        }
        .validate()
    }

    pub fn to_execution_target(
        &self,
        image: impl Into<String>,
    ) -> Result<ExecutionTarget, DeploymentTargetProfileError> {
        let image = image.into();
        if !image.contains("@sha256:") {
            return Err(DeploymentTargetProfileError::new(
                "deployment target image must be digest-pinned",
            ));
        }
        let mut target = ExecutionTarget::new(&self.target_id, self.kind, &self.execution_host)
            .with_capabilities(self.capabilities.iter().cloned())
            .with_effects(self.effects.iter().cloned())
            .with_image(image);
        if let Some(package_lock) = &self.package_lock {
            target = target.with_package_lock(package_lock);
        }
        Ok(target)
    }

    pub fn profile_contract(&self) -> Value {
        json!({
            "target_id": self.target_id,
            "image_role": self.image_role,
            "kind": self.kind.as_str(),
            "execution_host": self.execution_host,
            "capabilities": self.capabilities,
            "effects": self.effects,
            "package_lock": self.package_lock,
            "default_replicas": self.default_replicas,
        })
    }

    fn validate(self) -> Result<Self, DeploymentTargetProfileError> {
        if self.target_id.trim().is_empty() {
            return Err(DeploymentTargetProfileError::new(
                "deployment target profile id must not be empty",
            ));
        }
        if self.image_role.trim().is_empty() {
            return Err(DeploymentTargetProfileError::new(
                "deployment target image_role must not be empty",
            ));
        }
        if self.execution_host.trim().is_empty() {
            return Err(DeploymentTargetProfileError::new(
                "deployment target execution_host must not be empty",
            ));
        }
        if self.default_replicas == 0 {
            return Err(DeploymentTargetProfileError::new(
                "deployment target default_replicas must be positive",
            ));
        }
        Ok(self)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KubernetesTargetRenderer {
    pub namespace: String,
}

impl KubernetesTargetRenderer {
    pub fn new(namespace: impl Into<String>) -> Self {
        Self {
            namespace: namespace.into(),
        }
    }

    pub fn render_target_profile(
        &self,
        profile: &DeploymentTargetProfile,
        image: impl Into<String>,
    ) -> Result<Vec<Value>, DeploymentTargetProfileError> {
        if self.namespace.trim().is_empty() {
            return Err(DeploymentTargetProfileError::new(
                "kubernetes namespace must not be empty",
            ));
        }
        let target = profile.to_execution_target(image)?;
        let deployment = self.deployment_manifest(profile, &target);
        let mut manifests = vec![deployment];
        if matches!(
            profile.kind,
            ExecutionTargetKind::Service | ExecutionTargetKind::StatefulService
        ) {
            manifests.push(self.service_manifest(profile));
        }
        Ok(manifests)
    }

    fn deployment_manifest(
        &self,
        profile: &DeploymentTargetProfile,
        target: &ExecutionTarget,
    ) -> Value {
        let mut env = vec![
            json!({"name": "GRAPHBLOCKS_TARGET_ID", "value": profile.target_id}),
            json!({"name": "GRAPHBLOCKS_IMAGE_ROLE", "value": profile.image_role}),
            json!({"name": "GRAPHBLOCKS_EXECUTION_HOST", "value": profile.execution_host}),
        ];
        if let Some(package_lock) = &profile.package_lock {
            env.push(json!({"name": "GRAPHBLOCKS_PACKAGE_LOCK", "value": package_lock}));
        }
        json!({
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": profile.target_id,
                "namespace": self.namespace,
                "labels": self.labels(profile),
            },
            "spec": {
                "replicas": profile.default_replicas,
                "selector": {
                    "matchLabels": {
                        "graphblocks.ai/target-id": profile.target_id,
                    },
                },
                "template": {
                    "metadata": {
                        "labels": self.labels(profile),
                    },
                    "spec": {
                        "containers": [{
                            "name": profile.target_id,
                            "image": target.image,
                            "env": env,
                        }],
                    },
                },
            },
        })
    }

    fn service_manifest(&self, profile: &DeploymentTargetProfile) -> Value {
        json!({
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": profile.target_id,
                "namespace": self.namespace,
                "labels": self.labels(profile),
            },
            "spec": {
                "selector": {
                    "graphblocks.ai/target-id": profile.target_id,
                },
                "ports": [{
                    "name": "http",
                    "port": 8080,
                    "targetPort": 8080,
                }],
            },
        })
    }

    fn labels(&self, profile: &DeploymentTargetProfile) -> Value {
        json!({
            "app.kubernetes.io/name": "graphblocks",
            "graphblocks.ai/target-id": profile.target_id,
            "graphblocks.ai/image-role": profile.image_role,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct HelmRenderedValues {
    pub values: Value,
}

impl HelmRenderedValues {
    pub fn content_digest(&self) -> String {
        canonical_hash(&self.values)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct HelmTargetRenderer {
    pub release_name: String,
    pub namespace: String,
}

impl HelmTargetRenderer {
    pub fn new(release_name: impl Into<String>, namespace: impl Into<String>) -> Self {
        Self {
            release_name: release_name.into(),
            namespace: namespace.into(),
        }
    }

    pub fn render_target_set(
        &self,
        target_set: &DeploymentTargetProfileSet,
        images_by_target_id: &BTreeMap<String, String>,
    ) -> Result<HelmRenderedValues, DeploymentTargetProfileError> {
        if self.release_name.trim().is_empty() {
            return Err(DeploymentTargetProfileError::new(
                "helm release name must not be empty",
            ));
        }
        if self.namespace.trim().is_empty() {
            return Err(DeploymentTargetProfileError::new(
                "helm namespace must not be empty",
            ));
        }
        let mut targets = target_set.targets.iter().collect::<Vec<_>>();
        targets.sort_by(|left, right| left.target_id.cmp(&right.target_id));
        let target_values = targets
            .into_iter()
            .map(|profile| {
                let image = images_by_target_id
                    .get(&profile.target_id)
                    .ok_or_else(|| {
                        DeploymentTargetProfileError::new(format!(
                            "missing digest-pinned image for deployment target {:?}",
                            profile.target_id
                        ))
                    })?
                    .clone();
                let target = profile.to_execution_target(image)?;
                Ok(json!({
                    "id": profile.target_id,
                    "image_role": profile.image_role,
                    "kind": profile.kind.as_str(),
                    "execution_host": profile.execution_host,
                    "image": target.image,
                    "replicas": profile.default_replicas,
                    "capabilities": profile.capabilities,
                    "effects": profile.effects,
                    "package_lock": profile.package_lock,
                }))
            })
            .collect::<Result<Vec<_>, DeploymentTargetProfileError>>()?;
        Ok(HelmRenderedValues {
            values: json!({
                "graphblocks": {
                    "release_name": self.release_name,
                    "namespace": self.namespace,
                    "targets": target_values,
                }
            }),
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub enum TerraformOutputValueKind {
    String,
    Number,
    Bool,
    Object,
    Array,
}

impl TerraformOutputValueKind {
    fn as_str(self) -> &'static str {
        match self {
            Self::String => "string",
            Self::Number => "number",
            Self::Bool => "bool",
            Self::Object => "object",
            Self::Array => "array",
        }
    }

    fn matches_value(self, value: &Value) -> bool {
        match self {
            Self::String => value.is_string(),
            Self::Number => value.is_number(),
            Self::Bool => value.is_boolean(),
            Self::Object => value.is_object(),
            Self::Array => value.is_array(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TerraformOutputRequirement {
    pub output_name: String,
    pub value_kind: TerraformOutputValueKind,
    pub binds_to: String,
    pub required: bool,
}

impl TerraformOutputRequirement {
    pub fn new(
        output_name: impl Into<String>,
        value_kind: TerraformOutputValueKind,
        binds_to: impl Into<String>,
    ) -> Self {
        Self {
            output_name: output_name.into(),
            value_kind,
            binds_to: binds_to.into(),
            required: true,
        }
    }

    pub fn optional(
        output_name: impl Into<String>,
        value_kind: TerraformOutputValueKind,
        binds_to: impl Into<String>,
    ) -> Self {
        Self {
            output_name: output_name.into(),
            value_kind,
            binds_to: binds_to.into(),
            required: false,
        }
    }

    fn requirement_contract(&self) -> Value {
        json!({
            "output_name": self.output_name,
            "value_kind": self.value_kind.as_str(),
            "binds_to": self.binds_to,
            "required": self.required,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TerraformOutputIssue {
    pub code: String,
    pub output_name: String,
    pub binds_to: String,
    pub message: String,
}

impl TerraformOutputIssue {
    pub fn issue_contract(&self) -> Value {
        json!({
            "code": self.code,
            "output_name": self.output_name,
            "binds_to": self.binds_to,
            "message": self.message,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TerraformOutputValidationResult {
    pub issues: Vec<TerraformOutputIssue>,
}

impl TerraformOutputValidationResult {
    pub fn ok(&self) -> bool {
        self.issues.is_empty()
    }

    pub fn issue_contracts(&self) -> Vec<Value> {
        self.issues
            .iter()
            .map(TerraformOutputIssue::issue_contract)
            .collect()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TerraformOutputRequirementSet {
    pub requirements: Vec<TerraformOutputRequirement>,
}

impl TerraformOutputRequirementSet {
    pub fn new<I>(requirements: I) -> Self
    where
        I: IntoIterator<Item = TerraformOutputRequirement>,
    {
        let mut requirements = requirements.into_iter().collect::<Vec<_>>();
        requirements.sort_by(|left, right| left.output_name.cmp(&right.output_name));
        Self { requirements }
    }

    pub fn requirement_contracts(&self) -> Vec<Value> {
        self.requirements
            .iter()
            .map(TerraformOutputRequirement::requirement_contract)
            .collect()
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "requirements": self.requirement_contracts(),
        }))
    }

    pub fn validate_outputs(&self, outputs: &Value) -> TerraformOutputValidationResult {
        let output_object = outputs.as_object();
        let issues = self
            .requirements
            .iter()
            .filter_map(|requirement| {
                let value = output_object.and_then(|object| object.get(&requirement.output_name));
                match value {
                    None if requirement.required => Some(TerraformOutputIssue {
                        code: "TerraformOutputMissing".to_owned(),
                        output_name: requirement.output_name.clone(),
                        binds_to: requirement.binds_to.clone(),
                        message: "required Terraform output is missing".to_owned(),
                    }),
                    Some(value) if !requirement.value_kind.matches_value(value) => {
                        Some(TerraformOutputIssue {
                            code: "TerraformOutputTypeMismatch".to_owned(),
                            output_name: requirement.output_name.clone(),
                            binds_to: requirement.binds_to.clone(),
                            message: "Terraform output has the wrong value type".to_owned(),
                        })
                    }
                    _ => None,
                }
            })
            .collect();
        TerraformOutputValidationResult { issues }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeploymentTargetCoverageIssue {
    pub code: String,
    pub image_role: String,
    pub target_id: String,
    pub path: String,
    pub message: String,
}

impl DeploymentTargetCoverageIssue {
    pub fn issue_contract(&self) -> Value {
        json!({
            "code": self.code,
            "image_role": self.image_role,
            "target_id": self.target_id,
            "path": self.path,
            "message": self.message,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeploymentTargetCoverageResult {
    pub issues: Vec<DeploymentTargetCoverageIssue>,
}

impl DeploymentTargetCoverageResult {
    pub fn ok(&self) -> bool {
        self.issues.is_empty()
    }

    pub fn issue_contracts(&self) -> Vec<Value> {
        self.issues
            .iter()
            .map(DeploymentTargetCoverageIssue::issue_contract)
            .collect()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeploymentTargetProfileSet {
    pub targets: Vec<DeploymentTargetProfile>,
}

impl DeploymentTargetProfileSet {
    pub fn new<I>(targets: I) -> Self
    where
        I: IntoIterator<Item = DeploymentTargetProfile>,
    {
        let targets = targets.into_iter().collect::<Vec<_>>();
        assert_unique_deployment_targets(&targets).expect("deployment target set must be valid");
        Self { targets }
    }

    pub fn from_document(document: &Value) -> Result<Self, DeploymentTargetProfileError> {
        let object = document.as_object().ok_or_else(|| {
            DeploymentTargetProfileError::new("deployment target manifest must be a mapping")
        })?;
        if object.get("kind").and_then(Value::as_str) != Some("DeploymentTargetProfileSet") {
            return Err(DeploymentTargetProfileError::new(
                "deployment target manifest kind must be DeploymentTargetProfileSet",
            ));
        }
        let spec = object
            .get("spec")
            .and_then(Value::as_object)
            .ok_or_else(|| {
                DeploymentTargetProfileError::new(
                    "deployment target manifest spec must be a mapping",
                )
            })?;
        let raw_targets = spec
            .get("targets")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                DeploymentTargetProfileError::new(
                    "deployment target manifest spec.targets must be a list",
                )
            })?;
        let mut targets = Vec::with_capacity(raw_targets.len());
        for raw_target in raw_targets {
            targets.push(DeploymentTargetProfile::from_value(raw_target)?);
        }
        assert_unique_deployment_targets(&targets)?;
        Ok(Self { targets })
    }

    pub fn by_id(&self, target_id: &str) -> Option<&DeploymentTargetProfile> {
        self.targets
            .iter()
            .find(|target| target.target_id == target_id)
    }

    pub fn target_ids(&self) -> Vec<String> {
        let mut target_ids = self
            .targets
            .iter()
            .map(|target| target.target_id.clone())
            .collect::<Vec<_>>();
        target_ids.sort();
        target_ids
    }

    pub fn image_roles(&self) -> Vec<String> {
        self.targets
            .iter()
            .map(|target| target.image_role.clone())
            .collect()
    }

    pub fn coverage_for_required_image_roles<I, S>(
        &self,
        required_image_roles: I,
    ) -> DeploymentTargetCoverageResult
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        let known_roles = self
            .targets
            .iter()
            .map(|target| target.image_role.as_str())
            .collect::<BTreeSet<_>>();
        let issues = required_image_roles
            .into_iter()
            .map(Into::into)
            .filter(|image_role| !known_roles.contains(image_role.as_str()))
            .map(|image_role| DeploymentTargetCoverageIssue {
                code: "DeploymentTargetRoleMissing".to_owned(),
                image_role,
                target_id: String::new(),
                path: "$.spec.targets".to_owned(),
                message: "required production image role has no deployment target profile"
                    .to_owned(),
            })
            .collect();
        DeploymentTargetCoverageResult { issues }
    }

    pub fn manifest_contract(&self) -> Value {
        let mut targets = self.targets.iter().collect::<Vec<_>>();
        targets.sort_by(|left, right| left.target_id.cmp(&right.target_id));
        json!({
            "targets": targets
                .into_iter()
                .map(DeploymentTargetProfile::profile_contract)
                .collect::<Vec<_>>(),
        })
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&self.manifest_contract())
    }
}

fn assert_unique_deployment_targets(
    targets: &[DeploymentTargetProfile],
) -> Result<(), DeploymentTargetProfileError> {
    let mut seen_ids = BTreeSet::new();
    let mut seen_roles = BTreeSet::new();
    for target in targets {
        if !seen_ids.insert(target.target_id.as_str()) {
            return Err(DeploymentTargetProfileError::new(format!(
                "duplicate deployment target id {:?}",
                target.target_id
            )));
        }
        if !seen_roles.insert(target.image_role.as_str()) {
            return Err(DeploymentTargetProfileError::new(format!(
                "duplicate deployment target image role {:?}",
                target.image_role
            )));
        }
    }
    Ok(())
}

fn required_manifest_string(
    object: &serde_json::Map<String, Value>,
    keys: &[&str],
    label: &str,
) -> Result<String, DeploymentTargetProfileError> {
    let value = keys.iter().find_map(|key| object.get(*key));
    match value {
        Some(Value::String(value)) if !value.trim().is_empty() => Ok(value.clone()),
        _ => Err(DeploymentTargetProfileError::new(format!(
            "{label} must be a non-empty string"
        ))),
    }
}

fn optional_manifest_bool(
    object: &serde_json::Map<String, Value>,
    keys: &[&str],
) -> Result<Option<bool>, DeploymentTargetProfileError> {
    match keys.iter().find_map(|key| object.get(*key)) {
        Some(Value::Bool(value)) => Ok(Some(*value)),
        Some(_) => Err(DeploymentTargetProfileError::new(
            "manifest boolean field must be true or false",
        )),
        None => Ok(None),
    }
}

fn optional_manifest_usize(
    object: &serde_json::Map<String, Value>,
    keys: &[&str],
    label: &str,
) -> Result<Option<usize>, DeploymentTargetProfileError> {
    match keys.iter().find_map(|key| object.get(*key)) {
        Some(Value::Number(value)) => {
            let Some(value) = value.as_u64() else {
                return Err(DeploymentTargetProfileError::new(format!(
                    "{label} must be a positive integer"
                )));
            };
            if value == 0 || value > usize::MAX as u64 {
                return Err(DeploymentTargetProfileError::new(format!(
                    "{label} must be a positive integer"
                )));
            }
            Ok(Some(value as usize))
        }
        Some(_) => Err(DeploymentTargetProfileError::new(format!(
            "{label} must be an integer"
        ))),
        None => Ok(None),
    }
}

fn optional_manifest_u32(
    object: &serde_json::Map<String, Value>,
    keys: &[&str],
    label: &str,
) -> Result<Option<u32>, DeploymentTargetProfileError> {
    match keys.iter().find_map(|key| object.get(*key)) {
        Some(Value::Number(value)) => {
            let Some(value) = value.as_u64() else {
                return Err(DeploymentTargetProfileError::new(format!(
                    "{label} must be a positive integer"
                )));
            };
            if value == 0 || value > u64::from(u32::MAX) {
                return Err(DeploymentTargetProfileError::new(format!(
                    "{label} must be a positive integer"
                )));
            }
            Ok(Some(value as u32))
        }
        Some(_) => Err(DeploymentTargetProfileError::new(format!(
            "{label} must be an integer"
        ))),
        None => Ok(None),
    }
}

fn required_string_field(
    object: &serde_json::Map<String, Value>,
    keys: &[&str],
    label: &str,
) -> Result<String, DeploymentTargetProfileError> {
    keys.iter()
        .find_map(|key| object.get(*key))
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .ok_or_else(|| {
            DeploymentTargetProfileError::new(format!(
                "deployment target profile {label} must be a string"
            ))
        })
}

fn optional_string_field(
    object: &serde_json::Map<String, Value>,
    keys: &[&str],
) -> Result<Option<String>, DeploymentTargetProfileError> {
    match keys.iter().find_map(|key| object.get(*key)) {
        Some(Value::String(value)) => Ok(Some(value.clone())),
        Some(_) => Err(DeploymentTargetProfileError::new(
            "deployment target profile packageLock must be a string",
        )),
        None => Ok(None),
    }
}

fn optional_positive_u32_field(
    object: &serde_json::Map<String, Value>,
    keys: &[&str],
) -> Result<Option<u32>, DeploymentTargetProfileError> {
    match keys.iter().find_map(|key| object.get(*key)) {
        Some(Value::Number(value)) => {
            let Some(value) = value.as_u64() else {
                return Err(DeploymentTargetProfileError::new(
                    "deployment target profile defaultReplicas must be a positive integer",
                ));
            };
            if value == 0 || value > u64::from(u32::MAX) {
                return Err(DeploymentTargetProfileError::new(
                    "deployment target profile defaultReplicas must be a positive integer",
                ));
            }
            Ok(Some(value as u32))
        }
        Some(_) => Err(DeploymentTargetProfileError::new(
            "deployment target profile defaultReplicas must be an integer",
        )),
        None => Ok(None),
    }
}

fn string_set_field(
    object: &serde_json::Map<String, Value>,
    key: &str,
) -> Result<BTreeSet<String>, DeploymentTargetProfileError> {
    match object.get(key) {
        Some(Value::String(value)) => Ok(BTreeSet::from([value.clone()])),
        Some(Value::Array(values)) => values
            .iter()
            .map(|value| {
                value.as_str().map(ToOwned::to_owned).ok_or_else(|| {
                    DeploymentTargetProfileError::new(format!(
                        "deployment target profile {key} must contain only strings"
                    ))
                })
            })
            .collect(),
        Some(_) => Err(DeploymentTargetProfileError::new(format!(
            "deployment target profile {key} must be a string or list"
        ))),
        None => Ok(BTreeSet::new()),
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
        placements.sort_by_key(|left| left.to_string());
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

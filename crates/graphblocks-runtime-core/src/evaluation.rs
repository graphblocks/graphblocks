use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

use crate::orchestration::LeaseGrant;
use crate::policy::PrincipalRef;
use crate::tool::ResolvedTool;
use crate::tool_result::ArtifactRef;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ResourceSnapshotRef {
    pub resource_id: String,
    pub digest: String,
    pub resource_kind: Option<String>,
    pub uri: Option<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl ResourceSnapshotRef {
    pub fn new(resource_id: impl Into<String>, digest: impl Into<String>) -> Self {
        Self {
            resource_id: resource_id.into(),
            digest: digest.into(),
            resource_kind: None,
            uri: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_resource_kind(mut self, resource_kind: impl Into<String>) -> Self {
        self.resource_kind = Some(resource_kind.into());
        self
    }

    pub fn with_uri(mut self, uri: impl Into<String>) -> Self {
        self.uri = Some(uri.into());
        self
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: Value) -> Self {
        self.metadata.insert(key.into(), value);
        self
    }

    fn canonical_value(&self) -> Value {
        json!({
            "resource_id": self.resource_id,
            "digest": self.digest,
            "resource_kind": self.resource_kind,
            "uri": self.uri,
            "metadata": self.metadata,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EvidenceRef {
    pub evidence_id: String,
    pub source: Value,
    pub evidence_kind: String,
    pub metadata: BTreeMap<String, Value>,
}

impl EvidenceRef {
    fn canonical_value(&self) -> Value {
        json!({
            "evidence_id": self.evidence_id,
            "source": self.source,
            "evidence_kind": self.evidence_kind,
            "metadata": self.metadata,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TypedValueRef {
    pub value_id: String,
    pub schema_id: String,
    pub schema_version: u64,
    pub digest: String,
    pub encoding: String,
    pub artifact: Option<ArtifactRef>,
}

impl TypedValueRef {
    fn canonical_value(&self) -> Value {
        json!({
            "value_id": self.value_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "digest": self.digest,
            "encoding": self.encoding,
            "artifact": self.artifact.as_ref().map(artifact_value),
        })
    }
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct ModelVisibleToolRef {
    pub tool_name: String,
    pub resolved_tool_id: String,
    pub definition_digest: String,
    pub binding_digest: String,
    pub effective_policy_snapshot_id: String,
    pub allowed_for_principal: bool,
    pub valid_until_unix_ms: Option<u64>,
}

impl ModelVisibleToolRef {
    fn canonical_value(&self) -> Value {
        json!({
            "tool_name": self.tool_name,
            "resolved_tool_id": self.resolved_tool_id,
            "definition_digest": self.definition_digest,
            "binding_digest": self.binding_digest,
            "effective_policy_snapshot_id": self.effective_policy_snapshot_id,
            "allowed_for_principal": self.allowed_for_principal,
            "valid_until_unix_ms": self.valid_until_unix_ms,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RunProvenance {
    pub graph_hash: String,
    pub release_id: Option<String>,
    pub deployment_revision_id: Option<String>,
    pub physical_plan_hash: Option<String>,
    pub release_signature_digest: Option<String>,
    pub model_visible_tools: Vec<ModelVisibleToolRef>,
    pub started_at: String,
    pub completed_at: Option<String>,
    pub runner: BTreeMap<String, Value>,
    pub metadata: BTreeMap<String, Value>,
}

impl RunProvenance {
    pub fn new(graph_hash: impl Into<String>, started_at: impl Into<String>) -> Self {
        Self {
            graph_hash: graph_hash.into(),
            release_id: None,
            deployment_revision_id: None,
            physical_plan_hash: None,
            release_signature_digest: None,
            model_visible_tools: Vec::new(),
            started_at: started_at.into(),
            completed_at: None,
            runner: BTreeMap::new(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_release(
        mut self,
        release_id: impl Into<String>,
        deployment_revision_id: impl Into<String>,
    ) -> Self {
        self.release_id = Some(release_id.into());
        self.deployment_revision_id = Some(deployment_revision_id.into());
        self
    }

    pub fn with_physical_plan_hash(mut self, physical_plan_hash: impl Into<String>) -> Self {
        self.physical_plan_hash = Some(physical_plan_hash.into());
        self
    }

    pub fn with_release_signature_digest(
        mut self,
        release_signature_digest: impl Into<String>,
    ) -> Self {
        self.release_signature_digest = Some(release_signature_digest.into());
        self
    }

    pub fn with_model_visible_tools<'a, I>(mut self, tools: I) -> Self
    where
        I: IntoIterator<Item = &'a ResolvedTool>,
    {
        self.model_visible_tools = tools
            .into_iter()
            .map(|tool| ModelVisibleToolRef {
                tool_name: tool.definition.name.clone(),
                resolved_tool_id: tool.resolved_tool_id.clone(),
                definition_digest: tool.definition_digest.clone(),
                binding_digest: tool.binding_digest.clone(),
                effective_policy_snapshot_id: tool.effective_policy_snapshot_id.clone(),
                allowed_for_principal: tool.allowed_for_principal,
                valid_until_unix_ms: tool.valid_until_unix_ms,
            })
            .collect();
        self.model_visible_tools.sort();
        self
    }

    fn canonical_value(&self) -> Value {
        json!({
            "graph_hash": self.graph_hash,
            "release_id": self.release_id,
            "deployment_revision_id": self.deployment_revision_id,
            "physical_plan_hash": self.physical_plan_hash,
            "release_signature_digest": self.release_signature_digest,
            "model_visible_tools": self.model_visible_tools.iter().map(ModelVisibleToolRef::canonical_value).collect::<Vec<_>>(),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "runner": self.runner,
            "metadata": self.metadata,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ChangeSet {
    pub change_set_id: String,
    pub base: ResourceSnapshotRef,
    pub candidate: ResourceSnapshotRef,
    pub operations: Vec<Value>,
    pub summary: Option<String>,
}

impl ChangeSet {
    fn canonical_value(&self) -> Value {
        json!({
            "change_set_id": self.change_set_id,
            "base": self.base.canonical_value(),
            "candidate": self.candidate.canonical_value(),
            "operations": self.operations,
            "summary": self.summary,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkspaceHead {
    pub workspace_id: String,
    pub current: ResourceSnapshotRef,
    pub revision: u64,
}

impl WorkspaceHead {
    pub fn new(
        workspace_id: impl Into<String>,
        current: ResourceSnapshotRef,
        revision: u64,
    ) -> Self {
        Self {
            workspace_id: workspace_id.into(),
            current,
            revision,
        }
    }

    pub fn commit(
        &self,
        request: WorkspaceCommitRequest,
    ) -> Result<(Self, WorkspaceCommitRecord), WorkspaceCommitError> {
        if request.expected_base_revision != self.revision
            || request.change_set.base.digest != self.current.digest
        {
            return Err(WorkspaceCommitError::StaleHead {
                expected_revision: request.expected_base_revision,
                actual_revision: self.revision,
                expected_digest: request.change_set.base.digest,
                actual_digest: self.current.digest.clone(),
            });
        }
        if !request
            .mutation_decision
            .as_ref()
            .map(|decision| decision.allowed)
            .unwrap_or(true)
        {
            return Err(WorkspaceCommitError::MutationDenied {
                reason_codes: request
                    .mutation_decision
                    .map(|decision| decision.reason_codes)
                    .unwrap_or_default(),
            });
        }
        if let Some(gate) = &request.gate
            && gate.decision != GateDecision::Pass
        {
            return Err(WorkspaceCommitError::GateNotPassed {
                gate_id: gate.gate_id.clone(),
                decision: gate.decision,
            });
        }
        for review in &request.reviews {
            if !matches!(
                review.decision,
                ReviewDecision::Accept | ReviewDecision::AcceptWithConditions
            ) || !review.is_valid_for(&request.change_set.candidate)
            {
                return Err(WorkspaceCommitError::ReviewInvalid {
                    review_id: review.review_id.clone(),
                });
            }
        }

        let new_revision = self.revision + 1;
        let record = WorkspaceCommitRecord {
            commit_id: request.commit_id,
            workspace_id: self.workspace_id.clone(),
            change_set_id: request.change_set.change_set_id.clone(),
            previous: self.current.clone(),
            candidate: request.change_set.candidate.clone(),
            previous_revision: self.revision,
            new_revision,
            change_set: request.change_set,
            gate: request.gate,
            reviews: request.reviews,
            metadata: request.metadata,
        };
        Ok((
            Self {
                workspace_id: self.workspace_id.clone(),
                current: record.candidate.clone(),
                revision: new_revision,
            },
            record,
        ))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct WorkspaceCommitRequest {
    pub commit_id: String,
    pub change_set: ChangeSet,
    pub expected_base_revision: u64,
    pub mutation_decision: Option<WorkspaceMutationDecision>,
    pub gate: Option<GateResult>,
    pub reviews: Vec<ReviewRecord>,
    pub metadata: BTreeMap<String, Value>,
}

impl WorkspaceCommitRequest {
    pub fn new(
        commit_id: impl Into<String>,
        change_set: ChangeSet,
        expected_base_revision: u64,
    ) -> Self {
        Self {
            commit_id: commit_id.into(),
            change_set,
            expected_base_revision,
            mutation_decision: None,
            gate: None,
            reviews: Vec::new(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_mutation_decision(mut self, mutation_decision: WorkspaceMutationDecision) -> Self {
        self.mutation_decision = Some(mutation_decision);
        self
    }

    pub fn with_gate(mut self, gate: GateResult) -> Self {
        self.gate = Some(gate);
        self
    }

    pub fn with_review(mut self, review: ReviewRecord) -> Self {
        self.reviews.push(review);
        self
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: Value) -> Self {
        self.metadata.insert(key.into(), value);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct WorkspaceCommitRecord {
    pub commit_id: String,
    pub workspace_id: String,
    pub change_set_id: String,
    pub previous: ResourceSnapshotRef,
    pub candidate: ResourceSnapshotRef,
    pub previous_revision: u64,
    pub new_revision: u64,
    pub change_set: ChangeSet,
    pub gate: Option<GateResult>,
    pub reviews: Vec<ReviewRecord>,
    pub metadata: BTreeMap<String, Value>,
}

impl WorkspaceCommitRecord {
    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "workspace_id": self.workspace_id,
            "change_set_id": self.change_set_id,
            "previous": self.previous.canonical_value(),
            "candidate": self.candidate.canonical_value(),
            "previous_revision": self.previous_revision,
            "new_revision": self.new_revision,
            "change_set": self.change_set.canonical_value(),
            "gate": self.gate.as_ref().map(GateResult::canonical_value),
            "reviews": self.reviews.iter().map(ReviewRecord::canonical_value).collect::<Vec<_>>(),
            "metadata": self.metadata,
        }))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum WorkspaceCommitError {
    StaleHead {
        expected_revision: u64,
        actual_revision: u64,
        expected_digest: String,
        actual_digest: String,
    },
    MutationDenied {
        reason_codes: Vec<String>,
    },
    GateNotPassed {
        gate_id: String,
        decision: GateDecision,
    },
    ReviewInvalid {
        review_id: String,
    },
}

impl fmt::Display for WorkspaceCommitError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::StaleHead {
                expected_revision,
                actual_revision,
                expected_digest,
                actual_digest,
            } => write!(
                formatter,
                "workspace head is stale: expected {expected_digest}@{expected_revision}, actual {actual_digest}@{actual_revision}"
            ),
            Self::MutationDenied { reason_codes } => {
                write!(formatter, "workspace mutation denied: {reason_codes:?}")
            }
            Self::GateNotPassed { gate_id, decision } => {
                write!(
                    formatter,
                    "workspace commit gate {gate_id:?} did not pass: {decision:?}"
                )
            }
            Self::ReviewInvalid { review_id } => {
                write!(
                    formatter,
                    "workspace review {review_id:?} is not valid for commit"
                )
            }
        }
    }
}

impl Error for WorkspaceCommitError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkspaceMutationDecision {
    pub allowed: bool,
    pub reason_codes: Vec<String>,
}

impl WorkspaceMutationDecision {
    fn from_reasons(mut reason_codes: Vec<String>) -> Self {
        reason_codes.sort();
        reason_codes.dedup();
        Self {
            allowed: reason_codes.is_empty(),
            reason_codes,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkspaceMutationPolicy {
    pub policy_id: String,
    pub allowed_resource_kinds: Vec<String>,
    pub denied_operations: Vec<String>,
    pub required_review_scopes: Vec<String>,
    pub read_only_resource_ids: Vec<String>,
    pub read_only_resource_kinds: Vec<String>,
}

impl WorkspaceMutationPolicy {
    pub fn new<I, S>(policy_id: impl Into<String>, allowed_resource_kinds: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        let mut allowed_resource_kinds = allowed_resource_kinds
            .into_iter()
            .map(Into::into)
            .collect::<Vec<_>>();
        allowed_resource_kinds.sort();
        allowed_resource_kinds.dedup();
        Self {
            policy_id: policy_id.into(),
            allowed_resource_kinds,
            denied_operations: Vec::new(),
            required_review_scopes: Vec::new(),
            read_only_resource_ids: Vec::new(),
            read_only_resource_kinds: Vec::new(),
        }
    }

    pub fn with_denied_operation(mut self, operation: impl Into<String>) -> Self {
        self.denied_operations.push(operation.into());
        self.denied_operations.sort();
        self.denied_operations.dedup();
        self
    }

    pub fn with_required_review_scope(mut self, scope: impl Into<String>) -> Self {
        self.required_review_scopes.push(scope.into());
        self.required_review_scopes.sort();
        self.required_review_scopes.dedup();
        self
    }

    pub fn with_read_only_resource_id(mut self, resource_id: impl Into<String>) -> Self {
        self.read_only_resource_ids.push(resource_id.into());
        self.read_only_resource_ids.sort();
        self.read_only_resource_ids.dedup();
        self
    }

    pub fn with_read_only_resource_kind(mut self, resource_kind: impl Into<String>) -> Self {
        self.read_only_resource_kinds.push(resource_kind.into());
        self.read_only_resource_kinds.sort();
        self.read_only_resource_kinds.dedup();
        self
    }

    pub fn evaluate(
        &self,
        change_set: &ChangeSet,
        _principal: &PrincipalRef,
        review_scopes: &[&str],
        base_resources: &[ResourceSnapshotRef],
        candidate_resources: &[ResourceSnapshotRef],
    ) -> WorkspaceMutationDecision {
        let mut reasons = Vec::new();
        if !self
            .required_review_scopes
            .iter()
            .all(|required| review_scopes.contains(&required.as_str()))
        {
            reasons.push("workspace.review_required".to_string());
        }
        for operation in &change_set.operations {
            let operation_name = operation.get("op").and_then(Value::as_str);
            let resource_id = operation.get("resource_id").and_then(Value::as_str);
            let resource_kind = operation.get("resource_kind").and_then(Value::as_str);
            if let Some(operation_name) = operation_name
                && self
                    .denied_operations
                    .iter()
                    .any(|denied| denied == operation_name)
            {
                reasons.push("workspace.operation_denied".to_string());
            }
            if let Some(resource_kind) = resource_kind
                && !self
                    .allowed_resource_kinds
                    .iter()
                    .any(|allowed| allowed == resource_kind)
            {
                reasons.push("workspace.resource_kind_denied".to_string());
            }

            let operation_action = operation_name
                .and_then(|name| name.rsplit('.').next())
                .unwrap_or("")
                .to_ascii_lowercase();
            let is_read_only_operation = matches!(
                operation_action.as_str(),
                "check" | "diff" | "inspect" | "list" | "read" | "stat" | "validate"
            );
            if !is_read_only_operation {
                if resource_id
                    .map(|id| {
                        self.read_only_resource_ids
                            .iter()
                            .any(|read_only| read_only == id)
                    })
                    .unwrap_or(false)
                {
                    reasons.push("workspace.read_only_resource_changed".to_string());
                }
                if resource_kind
                    .map(|kind| {
                        self.read_only_resource_kinds
                            .iter()
                            .any(|read_only| read_only == kind)
                    })
                    .unwrap_or(false)
                {
                    reasons.push("workspace.read_only_resource_kind_changed".to_string());
                }
            }
        }

        if !base_resources.is_empty() || !candidate_resources.is_empty() {
            let candidate_by_resource_id = candidate_resources
                .iter()
                .map(|resource| (resource.resource_id.as_str(), resource))
                .collect::<BTreeMap<_, _>>();
            for resource in base_resources {
                let is_read_only_resource = self
                    .read_only_resource_ids
                    .iter()
                    .any(|resource_id| resource_id == &resource.resource_id)
                    || resource
                        .resource_kind
                        .as_ref()
                        .map(|resource_kind| {
                            self.read_only_resource_kinds
                                .iter()
                                .any(|read_only| read_only == resource_kind)
                        })
                        .unwrap_or(false);
                if !is_read_only_resource {
                    continue;
                }
                if candidate_by_resource_id
                    .get(resource.resource_id.as_str())
                    .map(|candidate| *candidate != resource)
                    .unwrap_or(true)
                {
                    reasons.push("workspace.read_only_resource_changed".to_string());
                }
            }
        }

        WorkspaceMutationDecision::from_reasons(reasons)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CheckStatus {
    Passed,
    Failed,
    Error,
    Timeout,
    Inconclusive,
    Skipped,
}

impl CheckStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::Passed => "passed",
            Self::Failed => "failed",
            Self::Error => "error",
            Self::Timeout => "timeout",
            Self::Inconclusive => "inconclusive",
            Self::Skipped => "skipped",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct CheckResult {
    pub check_id: String,
    pub subject: ResourceSnapshotRef,
    pub status: CheckStatus,
    pub diagnostics: Vec<Value>,
    pub evidence: Vec<EvidenceRef>,
    pub artifacts: Vec<ArtifactRef>,
    pub tool: BTreeMap<String, Value>,
    pub environment: Option<ResourceSnapshotRef>,
}

impl CheckResult {
    pub fn new(
        check_id: impl Into<String>,
        subject: ResourceSnapshotRef,
        status: CheckStatus,
    ) -> Self {
        Self {
            check_id: check_id.into(),
            subject,
            status,
            diagnostics: Vec::new(),
            evidence: Vec::new(),
            artifacts: Vec::new(),
            tool: BTreeMap::new(),
            environment: None,
        }
    }

    pub fn with_diagnostic(mut self, diagnostic: Value) -> Self {
        self.diagnostics.push(diagnostic);
        self
    }

    pub fn with_tool(mut self, key: impl Into<String>, value: Value) -> Self {
        self.tool.insert(key.into(), value);
        self
    }

    fn canonical_value(&self) -> Value {
        json!({
            "check_id": self.check_id,
            "subject": self.subject.canonical_value(),
            "status": self.status.as_str(),
            "diagnostics": self.diagnostics,
            "evidence": self.evidence.iter().map(EvidenceRef::canonical_value).collect::<Vec<_>>(),
            "artifacts": self.artifacts.iter().map(artifact_value).collect::<Vec<_>>(),
            "tool": self.tool,
            "environment": self.environment.as_ref().map(ResourceSnapshotRef::canonical_value),
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MetricDirection {
    Minimize,
    Maximize,
    Target,
    Informational,
}

impl MetricDirection {
    fn as_str(self) -> &'static str {
        match self {
            Self::Minimize => "minimize",
            Self::Maximize => "maximize",
            Self::Target => "target",
            Self::Informational => "informational",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct MetricObservation {
    pub name: String,
    pub value: Value,
    pub unit: Option<String>,
    pub direction: MetricDirection,
    pub baseline_value: Option<Value>,
    pub subject: Option<ResourceSnapshotRef>,
    pub evaluator: Option<Value>,
}

impl MetricObservation {
    pub fn new(name: impl Into<String>, value: Value) -> Self {
        Self {
            name: name.into(),
            value,
            unit: None,
            direction: MetricDirection::Informational,
            baseline_value: None,
            subject: None,
            evaluator: None,
        }
    }

    pub fn with_unit(mut self, unit: impl Into<String>) -> Self {
        self.unit = Some(unit.into());
        self
    }

    pub fn with_direction(mut self, direction: MetricDirection) -> Self {
        self.direction = direction;
        self
    }

    pub fn with_baseline_value(mut self, baseline_value: Value) -> Self {
        self.baseline_value = Some(baseline_value);
        self
    }

    fn canonical_value(&self) -> Value {
        json!({
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "direction": self.direction.as_str(),
            "baseline_value": self.baseline_value,
            "subject": self.subject.as_ref().map(ResourceSnapshotRef::canonical_value),
            "evaluator": self.evaluator,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConstraintOperator {
    AtLeast,
    AtMost,
    Equals,
    MaxRegression,
}

#[derive(Clone, Debug, PartialEq)]
pub struct GateConstraint {
    pub metric_name: String,
    pub operator: ConstraintOperator,
    pub threshold: Value,
}

impl GateConstraint {
    pub fn new(
        metric_name: impl Into<String>,
        operator: ConstraintOperator,
        threshold: Value,
    ) -> Self {
        Self {
            metric_name: metric_name.into(),
            operator,
            threshold,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GateDecision {
    Pass,
    Fail,
    Inconclusive,
}

impl GateDecision {
    fn as_str(self) -> &'static str {
        match self {
            Self::Pass => "pass",
            Self::Fail => "fail",
            Self::Inconclusive => "inconclusive",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct GateResult {
    pub gate_id: String,
    pub subject: ResourceSnapshotRef,
    pub decision: GateDecision,
    pub check_ids: Vec<String>,
    pub violated_constraints: Vec<String>,
    pub metrics: Vec<MetricObservation>,
    pub policy_ref: Option<String>,
}

impl GateResult {
    fn canonical_value(&self) -> Value {
        json!({
            "gate_id": self.gate_id,
            "subject": self.subject.canonical_value(),
            "decision": self.decision.as_str(),
            "check_ids": self.check_ids,
            "violated_constraints": self.violated_constraints,
            "metrics": self.metrics.iter().map(MetricObservation::canonical_value).collect::<Vec<_>>(),
            "policy_ref": self.policy_ref,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SloComparison {
    AtLeast,
    AtMost,
}

#[derive(Clone, Debug, PartialEq)]
pub struct SloObjective {
    pub slo_id: String,
    pub indicator: String,
    pub comparison: SloComparison,
    pub objective: f64,
    pub window: String,
    pub unit: Option<String>,
}

impl SloObjective {
    pub fn at_least(
        slo_id: impl Into<String>,
        indicator: impl Into<String>,
        objective: f64,
        window: impl Into<String>,
    ) -> Self {
        Self {
            slo_id: slo_id.into(),
            indicator: indicator.into(),
            comparison: SloComparison::AtLeast,
            objective,
            window: window.into(),
            unit: None,
        }
    }

    pub fn at_most(
        slo_id: impl Into<String>,
        indicator: impl Into<String>,
        objective: f64,
        window: impl Into<String>,
    ) -> Self {
        Self {
            slo_id: slo_id.into(),
            indicator: indicator.into(),
            comparison: SloComparison::AtMost,
            objective,
            window: window.into(),
            unit: None,
        }
    }

    pub fn with_unit(mut self, unit: impl Into<String>) -> Self {
        self.unit = Some(unit.into());
        self
    }

    pub fn evaluate(&self, measurement: &SloMeasurement) -> SloReport {
        if self.indicator != measurement.indicator {
            return SloReport::no_data(self, "indicator_mismatch");
        }
        if self.window != measurement.window {
            return SloReport::no_data(self, "window_mismatch");
        }
        if self.unit != measurement.unit {
            return SloReport::no_data(self, "unit_mismatch");
        }

        let passes = match self.comparison {
            SloComparison::AtLeast => measurement.value >= self.objective,
            SloComparison::AtMost => measurement.value <= self.objective,
        };
        let violated_by = if passes {
            None
        } else {
            Some(match self.comparison {
                SloComparison::AtLeast => self.objective - measurement.value,
                SloComparison::AtMost => measurement.value - self.objective,
            })
        };

        SloReport {
            slo_id: self.slo_id.clone(),
            indicator: self.indicator.clone(),
            window: self.window.clone(),
            status: if passes {
                SloReportStatus::Pass
            } else {
                SloReportStatus::Fail
            },
            objective: self.objective,
            observed_value: Some(measurement.value),
            sample_count: measurement.sample_count,
            violated_by,
            reason: None,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SloMeasurement {
    pub indicator: String,
    pub value: f64,
    pub window: String,
    pub unit: Option<String>,
    pub sample_count: Option<u64>,
}

impl SloMeasurement {
    pub fn new(indicator: impl Into<String>, value: f64, window: impl Into<String>) -> Self {
        Self {
            indicator: indicator.into(),
            value,
            window: window.into(),
            unit: None,
            sample_count: None,
        }
    }

    pub fn with_unit(mut self, unit: impl Into<String>) -> Self {
        self.unit = Some(unit.into());
        self
    }

    pub fn with_sample_count(mut self, sample_count: u64) -> Self {
        self.sample_count = Some(sample_count);
        self
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SloReportStatus {
    Pass,
    Fail,
    NoData,
}

#[derive(Clone, Debug, PartialEq)]
pub struct SloReport {
    pub slo_id: String,
    pub indicator: String,
    pub window: String,
    pub status: SloReportStatus,
    pub objective: f64,
    pub observed_value: Option<f64>,
    pub sample_count: Option<u64>,
    pub violated_by: Option<f64>,
    pub reason: Option<String>,
}

impl SloReport {
    fn no_data(objective: &SloObjective, reason: impl Into<String>) -> Self {
        Self {
            slo_id: objective.slo_id.clone(),
            indicator: objective.indicator.clone(),
            window: objective.window.clone(),
            status: SloReportStatus::NoData,
            objective: objective.objective,
            observed_value: None,
            sample_count: None,
            violated_by: None,
            reason: Some(reason.into()),
        }
    }
}

pub fn evaluate_gate<I, S>(
    gate_id: impl Into<String>,
    subject: ResourceSnapshotRef,
    checks: &[CheckResult],
    metrics: &[MetricObservation],
    required_check_ids: Option<I>,
    constraints: &[GateConstraint],
    policy_ref: Option<String>,
) -> GateResult
where
    I: IntoIterator<Item = S>,
    S: Into<String>,
{
    let required = required_check_ids
        .map(|ids| ids.into_iter().map(Into::into).collect::<Vec<_>>())
        .unwrap_or_else(|| checks.iter().map(|check| check.check_id.clone()).collect());
    let mut violated = Vec::new();

    for check_id in &required {
        let check = checks.iter().find(|check| &check.check_id == check_id);
        if !matches!(check.map(|check| check.status), Some(CheckStatus::Passed)) {
            violated.push(format!("check:{check_id}"));
        }
    }

    for constraint in constraints {
        let metric = metrics
            .iter()
            .find(|metric| metric.name == constraint.metric_name);
        if !metric
            .map(|metric| metric_satisfies(metric, constraint))
            .unwrap_or(false)
        {
            violated.push(format!("metric:{}", constraint.metric_name));
        }
    }

    let inconclusive = checks.iter().any(|check| {
        matches!(
            check.status,
            CheckStatus::Error | CheckStatus::Timeout | CheckStatus::Inconclusive
        )
    });
    let decision = if !violated.is_empty() {
        GateDecision::Fail
    } else if inconclusive {
        GateDecision::Inconclusive
    } else {
        GateDecision::Pass
    };

    GateResult {
        gate_id: gate_id.into(),
        subject,
        decision,
        check_ids: required,
        violated_constraints: violated,
        metrics: metrics.to_vec(),
        policy_ref,
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct TrialResult {
    pub trial_id: String,
    pub base: ResourceSnapshotRef,
    pub candidate: ResourceSnapshotRef,
    pub change_set: Option<ChangeSet>,
    pub checks: Vec<CheckResult>,
    pub metrics: Vec<MetricObservation>,
    pub gate: Option<GateResult>,
    pub usage: Vec<String>,
    pub outcome: String,
}

impl TrialResult {
    pub fn new(
        trial_id: impl Into<String>,
        base: ResourceSnapshotRef,
        candidate: ResourceSnapshotRef,
    ) -> Self {
        Self {
            trial_id: trial_id.into(),
            base,
            candidate,
            change_set: None,
            checks: Vec::new(),
            metrics: Vec::new(),
            gate: None,
            usage: Vec::new(),
            outcome: String::new(),
        }
    }

    pub fn with_gate(mut self, gate: GateResult) -> Self {
        self.gate = Some(gate);
        self
    }

    pub fn with_outcome(mut self, outcome: impl Into<String>) -> Self {
        self.outcome = outcome.into();
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct WorkspaceTrialPlan {
    pub trial_id: String,
    pub change_set: ChangeSet,
    pub expected_base_revision: u64,
    pub required_check_ids: Vec<String>,
    pub required_lease_kinds: Vec<String>,
    pub required_review_scopes: Vec<String>,
    pub checks: Vec<CheckResult>,
    pub gate: Option<GateResult>,
    pub mutation_decision: Option<WorkspaceMutationDecision>,
    pub leases: Vec<LeaseGrant>,
    pub reviews: Vec<ReviewRecord>,
}

impl WorkspaceTrialPlan {
    pub fn new(
        trial_id: impl Into<String>,
        change_set: ChangeSet,
        expected_base_revision: u64,
    ) -> Self {
        Self {
            trial_id: trial_id.into(),
            change_set,
            expected_base_revision,
            required_check_ids: Vec::new(),
            required_lease_kinds: Vec::new(),
            required_review_scopes: Vec::new(),
            checks: Vec::new(),
            gate: None,
            mutation_decision: None,
            leases: Vec::new(),
            reviews: Vec::new(),
        }
    }

    pub fn require_checks<I, S>(mut self, check_ids: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.required_check_ids = sorted_unique(check_ids);
        self
    }

    pub fn require_lease_kinds<I, S>(mut self, resource_kinds: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.required_lease_kinds = sorted_unique(resource_kinds);
        self
    }

    pub fn require_review_scopes<I, S>(mut self, scopes: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.required_review_scopes = sorted_unique(scopes);
        self
    }

    pub fn with_check(mut self, check: CheckResult) -> Self {
        self.checks.push(check);
        self
    }

    pub fn with_gate(mut self, gate: GateResult) -> Self {
        self.gate = Some(gate);
        self
    }

    pub fn with_mutation_decision(mut self, mutation_decision: WorkspaceMutationDecision) -> Self {
        self.mutation_decision = Some(mutation_decision);
        self
    }

    pub fn with_lease(mut self, lease: LeaseGrant) -> Self {
        self.leases.push(lease);
        self
    }

    pub fn with_review(mut self, review: ReviewRecord) -> Self {
        self.reviews.push(review);
        self
    }

    pub fn to_commit_request(
        &self,
        commit_id: impl Into<String>,
        now: &str,
    ) -> Result<WorkspaceCommitRequest, WorkspaceTrialError> {
        self.validate_for_commit(now)?;
        let mut request = WorkspaceCommitRequest::new(
            commit_id,
            self.change_set.clone(),
            self.expected_base_revision,
        )
        .with_metadata("trial_id", json!(self.trial_id))
        .with_metadata("lease_ids", json!(self.active_lease_ids(now)));

        if let Some(mutation_decision) = &self.mutation_decision {
            request = request.with_mutation_decision(mutation_decision.clone());
        }
        if let Some(gate) = &self.gate {
            request = request.with_gate(gate.clone());
        }
        for review in self
            .reviews
            .iter()
            .filter(|review| self.review_satisfies_commit(review))
        {
            request = request.with_review(review.clone());
        }
        Ok(request)
    }

    fn validate_for_commit(&self, now: &str) -> Result<(), WorkspaceTrialError> {
        if self.trial_id.trim().is_empty() {
            return Err(WorkspaceTrialError::EmptyField { field: "trial_id" });
        }
        for check_id in &self.required_check_ids {
            let check = self
                .checks
                .iter()
                .find(|check| check.check_id == *check_id)
                .ok_or_else(|| WorkspaceTrialError::MissingCheck {
                    check_id: check_id.clone(),
                })?;
            if check.subject != self.change_set.candidate {
                return Err(WorkspaceTrialError::SubjectMismatch {
                    field: format!("check:{check_id}"),
                    expected_digest: self.change_set.candidate.digest.clone(),
                    actual_digest: check.subject.digest.clone(),
                });
            }
            if check.status != CheckStatus::Passed {
                return Err(WorkspaceTrialError::CheckNotPassed {
                    check_id: check_id.clone(),
                    status: check.status,
                });
            }
        }

        let gate = self.gate.as_ref().ok_or(WorkspaceTrialError::MissingGate)?;
        if gate.subject != self.change_set.candidate {
            return Err(WorkspaceTrialError::SubjectMismatch {
                field: "gate".to_owned(),
                expected_digest: self.change_set.candidate.digest.clone(),
                actual_digest: gate.subject.digest.clone(),
            });
        }
        if gate.decision != GateDecision::Pass {
            return Err(WorkspaceTrialError::GateNotPassed {
                gate_id: gate.gate_id.clone(),
                decision: gate.decision,
            });
        }

        if let Some(mutation_decision) = &self.mutation_decision
            && !mutation_decision.allowed
        {
            return Err(WorkspaceTrialError::MutationDenied {
                reason_codes: mutation_decision.reason_codes.clone(),
            });
        }

        for resource_kind in &self.required_lease_kinds {
            if !self.leases.iter().any(|lease| {
                lease.resource_kind == *resource_kind
                    && lease.holder == format!("trial:{}", self.trial_id)
                    && lease.is_active_at(now)
            }) {
                return Err(WorkspaceTrialError::MissingLeaseKind {
                    resource_kind: resource_kind.clone(),
                });
            }
        }

        for scope in &self.required_review_scopes {
            if !self
                .reviews
                .iter()
                .any(|review| review.scope == *scope && self.review_satisfies_commit(review))
            {
                return Err(WorkspaceTrialError::MissingReviewScope {
                    scope: scope.clone(),
                });
            }
        }

        Ok(())
    }

    fn review_satisfies_commit(&self, review: &ReviewRecord) -> bool {
        matches!(
            review.decision,
            ReviewDecision::Accept | ReviewDecision::AcceptWithConditions
        ) && review.is_valid_for(&self.change_set.candidate)
    }

    fn active_lease_ids(&self, now: &str) -> Vec<String> {
        let mut lease_ids = self
            .leases
            .iter()
            .filter(|lease| lease.is_active_at(now))
            .map(|lease| lease.lease_id.clone())
            .collect::<Vec<_>>();
        lease_ids.sort();
        lease_ids.dedup();
        lease_ids
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum WorkspaceTrialError {
    EmptyField {
        field: &'static str,
    },
    MissingCheck {
        check_id: String,
    },
    CheckNotPassed {
        check_id: String,
        status: CheckStatus,
    },
    MissingGate,
    GateNotPassed {
        gate_id: String,
        decision: GateDecision,
    },
    MissingLeaseKind {
        resource_kind: String,
    },
    MissingReviewScope {
        scope: String,
    },
    MutationDenied {
        reason_codes: Vec<String>,
    },
    SubjectMismatch {
        field: String,
        expected_digest: String,
        actual_digest: String,
    },
}

impl fmt::Display for WorkspaceTrialError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyField { field } => write!(formatter, "{field} must not be empty"),
            Self::MissingCheck { check_id } => {
                write!(
                    formatter,
                    "workspace trial is missing required check {check_id:?}"
                )
            }
            Self::CheckNotPassed { check_id, status } => {
                write!(
                    formatter,
                    "workspace trial check {check_id:?} did not pass: {status:?}"
                )
            }
            Self::MissingGate => write!(formatter, "workspace trial is missing a gate result"),
            Self::GateNotPassed { gate_id, decision } => {
                write!(
                    formatter,
                    "workspace trial gate {gate_id:?} did not pass: {decision:?}"
                )
            }
            Self::MissingLeaseKind { resource_kind } => {
                write!(
                    formatter,
                    "workspace trial is missing active lease kind {resource_kind:?}"
                )
            }
            Self::MissingReviewScope { scope } => {
                write!(
                    formatter,
                    "workspace trial is missing valid review scope {scope:?}"
                )
            }
            Self::MutationDenied { reason_codes } => {
                write!(
                    formatter,
                    "workspace trial mutation denied: {reason_codes:?}"
                )
            }
            Self::SubjectMismatch {
                field,
                expected_digest,
                actual_digest,
            } => write!(
                formatter,
                "workspace trial {field} subject mismatch: expected {expected_digest}, got {actual_digest}"
            ),
        }
    }
}

impl Error for WorkspaceTrialError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReviewDecision {
    Accept,
    AcceptWithConditions,
    Revise,
    Reject,
}

impl ReviewDecision {
    fn as_str(self) -> &'static str {
        match self {
            Self::Accept => "accept",
            Self::AcceptWithConditions => "accept_with_conditions",
            Self::Revise => "revise",
            Self::Reject => "reject",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ReviewRecord {
    pub review_id: String,
    pub subject: ResourceSnapshotRef,
    pub subject_digest: String,
    pub scope: String,
    pub reviewer: PrincipalRef,
    pub decision: ReviewDecision,
    pub comments: Vec<String>,
    pub credential_refs: Vec<String>,
    pub created_at: String,
    pub invalidated_at: Option<String>,
}

impl ReviewRecord {
    pub fn new(
        review_id: impl Into<String>,
        subject: ResourceSnapshotRef,
        subject_digest: impl Into<String>,
        scope: impl Into<String>,
        reviewer: PrincipalRef,
        decision: ReviewDecision,
    ) -> Self {
        Self {
            review_id: review_id.into(),
            subject,
            subject_digest: subject_digest.into(),
            scope: scope.into(),
            reviewer,
            decision,
            comments: Vec::new(),
            credential_refs: Vec::new(),
            created_at: String::new(),
            invalidated_at: None,
        }
    }

    pub fn with_created_at(mut self, created_at: impl Into<String>) -> Self {
        self.created_at = created_at.into();
        self
    }

    pub fn is_valid_for(&self, subject: &ResourceSnapshotRef) -> bool {
        self.invalidated_at.is_none()
            && self.subject.resource_id == subject.resource_id
            && self.subject_digest == subject.digest
    }

    pub fn invalidate(mut self, invalidated_at: impl Into<String>) -> Self {
        self.invalidated_at = Some(invalidated_at.into());
        self
    }

    fn canonical_value(&self) -> Value {
        json!({
            "review_id": self.review_id,
            "subject": self.subject.canonical_value(),
            "subject_digest": self.subject_digest,
            "scope": self.scope,
            "reviewer": {
                "principal_id": self.reviewer.principal_id,
                "tenant_id": self.reviewer.tenant_id,
                "groups": self.reviewer.groups,
                "roles": self.reviewer.roles,
                "attributes": self.reviewer.attributes,
            },
            "decision": self.decision.as_str(),
            "comments": self.comments,
            "credential_refs": self.credential_refs,
            "created_at": self.created_at,
            "invalidated_at": self.invalidated_at,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ResultBundle {
    pub bundle_id: String,
    pub run_id: String,
    pub release_id: String,
    pub inputs: Vec<ResourceSnapshotRef>,
    pub outputs: Vec<TypedValueRef>,
    pub deployment_revision_id: Option<String>,
    pub artifacts: Vec<ArtifactRef>,
    pub diagnostics: Vec<Value>,
    pub checks: Vec<CheckResult>,
    pub metrics: Vec<MetricObservation>,
    pub evidence: Vec<EvidenceRef>,
    pub reviews: Vec<ReviewRecord>,
    pub usage_records: Vec<String>,
    pub policy_decision_refs: Vec<String>,
    pub provenance: RunProvenance,
}

impl ResultBundle {
    pub fn new(
        bundle_id: impl Into<String>,
        run_id: impl Into<String>,
        release_id: impl Into<String>,
    ) -> Self {
        Self {
            bundle_id: bundle_id.into(),
            run_id: run_id.into(),
            release_id: release_id.into(),
            inputs: Vec::new(),
            outputs: Vec::new(),
            deployment_revision_id: None,
            artifacts: Vec::new(),
            diagnostics: Vec::new(),
            checks: Vec::new(),
            metrics: Vec::new(),
            evidence: Vec::new(),
            reviews: Vec::new(),
            usage_records: Vec::new(),
            policy_decision_refs: Vec::new(),
            provenance: RunProvenance::new("", ""),
        }
    }

    pub fn with_input(mut self, input: ResourceSnapshotRef) -> Self {
        self.inputs.push(input);
        self
    }

    pub fn with_artifact(mut self, artifact: ArtifactRef) -> Self {
        self.artifacts.push(artifact);
        self
    }

    pub fn with_policy_decision_ref(mut self, policy_decision_ref: impl Into<String>) -> Self {
        self.policy_decision_refs.push(policy_decision_ref.into());
        self
    }

    pub fn with_provenance(mut self, provenance: RunProvenance) -> Self {
        self.provenance = provenance;
        self
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "run_id": self.run_id,
            "release_id": self.release_id,
            "deployment_revision_id": self.deployment_revision_id,
            "inputs": self.inputs.iter().map(ResourceSnapshotRef::canonical_value).collect::<Vec<_>>(),
            "outputs": self.outputs.iter().map(TypedValueRef::canonical_value).collect::<Vec<_>>(),
            "artifacts": self.artifacts.iter().map(artifact_value).collect::<Vec<_>>(),
            "diagnostics": self.diagnostics,
            "checks": self.checks.iter().map(CheckResult::canonical_value).collect::<Vec<_>>(),
            "metrics": self.metrics.iter().map(MetricObservation::canonical_value).collect::<Vec<_>>(),
            "evidence": self.evidence.iter().map(EvidenceRef::canonical_value).collect::<Vec<_>>(),
            "reviews": self.reviews.iter().map(ReviewRecord::canonical_value).collect::<Vec<_>>(),
            "usage_records": self.usage_records,
            "policy_decision_refs": self.policy_decision_refs,
            "provenance": self.provenance.canonical_value(),
        }))
    }
}

fn metric_satisfies(metric: &MetricObservation, constraint: &GateConstraint) -> bool {
    if constraint.operator == ConstraintOperator::Equals {
        return metric.value == constraint.threshold;
    }
    if constraint.operator == ConstraintOperator::MaxRegression {
        let Some(value) = numeric_value(&metric.value) else {
            return false;
        };
        let Some(baseline) = metric.baseline_value.as_ref().and_then(numeric_value) else {
            return false;
        };
        let Some(max_regression) = numeric_value(&constraint.threshold) else {
            return false;
        };
        if max_regression < 0.0 {
            return false;
        }
        return match metric.direction {
            MetricDirection::Minimize => {
                value <= baseline
                    || (baseline.abs() > f64::EPSILON
                        && (value - baseline) / baseline.abs() <= max_regression)
            }
            MetricDirection::Maximize => {
                value >= baseline
                    || (baseline.abs() > f64::EPSILON
                        && (baseline - value) / baseline.abs() <= max_regression)
            }
            MetricDirection::Target | MetricDirection::Informational => false,
        };
    }
    let Some(value) = numeric_value(&metric.value) else {
        return false;
    };
    let Some(threshold) = numeric_value(&constraint.threshold) else {
        return false;
    };
    match constraint.operator {
        ConstraintOperator::AtLeast => value >= threshold,
        ConstraintOperator::AtMost => value <= threshold,
        ConstraintOperator::Equals => true,
        ConstraintOperator::MaxRegression => true,
    }
}

fn numeric_value(value: &Value) -> Option<f64> {
    if let Some(value) = value.as_f64() {
        return Some(value);
    }
    value.as_str().and_then(|value| value.parse::<f64>().ok())
}

fn sorted_unique<I, S>(items: I) -> Vec<String>
where
    I: IntoIterator<Item = S>,
    S: Into<String>,
{
    let mut items = items.into_iter().map(Into::into).collect::<Vec<_>>();
    items.sort();
    items.dedup();
    items
}

fn artifact_value(artifact: &ArtifactRef) -> Value {
    json!({
        "artifact_id": artifact.artifact_id,
        "uri": artifact.uri,
        "checksum": artifact.checksum,
        "media_type": artifact.media_type,
    })
}

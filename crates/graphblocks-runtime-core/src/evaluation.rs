use std::collections::BTreeMap;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

use crate::policy::PrincipalRef;
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RunProvenance {
    pub graph_hash: String,
    pub started_at: String,
    pub completed_at: Option<String>,
    pub runner: BTreeMap<String, Value>,
    pub metadata: BTreeMap<String, Value>,
}

impl RunProvenance {
    pub fn new(graph_hash: impl Into<String>, started_at: impl Into<String>) -> Self {
        Self {
            graph_hash: graph_hash.into(),
            started_at: started_at.into(),
            completed_at: None,
            runner: BTreeMap::new(),
            metadata: BTreeMap::new(),
        }
    }

    fn canonical_value(&self) -> Value {
        json!({
            "graph_hash": self.graph_hash,
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
            .map(|metric| metric_satisfies(&metric.value, constraint))
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

fn metric_satisfies(value: &Value, constraint: &GateConstraint) -> bool {
    if constraint.operator == ConstraintOperator::Equals {
        return value == &constraint.threshold;
    }
    let Some(value) = numeric_value(value) else {
        return false;
    };
    let Some(threshold) = numeric_value(&constraint.threshold) else {
        return false;
    };
    match constraint.operator {
        ConstraintOperator::AtLeast => value >= threshold,
        ConstraintOperator::AtMost => value <= threshold,
        ConstraintOperator::Equals => true,
    }
}

fn numeric_value(value: &Value) -> Option<f64> {
    if let Some(value) = value.as_f64() {
        return Some(value);
    }
    value.as_str().and_then(|value| value.parse::<f64>().ok())
}

fn artifact_value(artifact: &ArtifactRef) -> Value {
    json!({
        "artifact_id": artifact.artifact_id,
        "uri": artifact.uri,
        "checksum": artifact.checksum,
        "media_type": artifact.media_type,
    })
}

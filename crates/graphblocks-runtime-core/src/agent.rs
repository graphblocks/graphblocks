use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use serde_json::Value;

use crate::documents::ArtifactRef;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolFailurePolicy {
    ReturnToModel,
    Fail,
    Fallback,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AgentSpec {
    pub model_pool: String,
    pub tools: Vec<String>,
    pub state_schema: Option<String>,
    pub max_steps: usize,
    pub exit_conditions: Vec<String>,
    pub tool_failure: ToolFailurePolicy,
    pub parallel_tool_calls: bool,
    pub budget_policy_ref: Option<String>,
    pub completion_reserve_ref: Option<String>,
    pub completion_reserve_units: Option<i64>,
    pub output_policy_profile_ref: Option<String>,
}

impl AgentSpec {
    pub fn new(model_pool: impl Into<String>) -> Self {
        Self {
            model_pool: model_pool.into(),
            tools: Vec::new(),
            state_schema: None,
            max_steps: 12,
            exit_conditions: vec!["final_message".to_owned()],
            tool_failure: ToolFailurePolicy::ReturnToModel,
            parallel_tool_calls: true,
            budget_policy_ref: None,
            completion_reserve_ref: None,
            completion_reserve_units: None,
            output_policy_profile_ref: None,
        }
    }

    pub fn with_tools<I, S>(mut self, tools: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.tools = tools.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_max_steps(mut self, max_steps: usize) -> Self {
        self.max_steps = max_steps;
        self
    }

    pub fn with_completion_reserve_units(mut self, completion_reserve_units: i64) -> Self {
        self.completion_reserve_units = Some(completion_reserve_units);
        self
    }

    pub fn with_output_policy_profile_ref(
        mut self,
        output_policy_profile_ref: impl Into<String>,
    ) -> Self {
        self.output_policy_profile_ref = Some(output_policy_profile_ref.into());
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelProfile {
    pub profile_id: String,
    pub connection: String,
    pub capabilities: BTreeSet<String>,
    pub quality_tier: String,
    pub cost_class: String,
    pub latency_class: String,
    pub allowed_sensitivity: BTreeSet<String>,
    pub regions: BTreeSet<String>,
    pub supports_cancellation: bool,
    pub supports_usage_report: bool,
}

impl ModelProfile {
    pub fn new(profile_id: impl Into<String>, connection: impl Into<String>) -> Self {
        Self {
            profile_id: profile_id.into(),
            connection: connection.into(),
            capabilities: BTreeSet::new(),
            quality_tier: "standard".to_owned(),
            cost_class: "standard".to_owned(),
            latency_class: "standard".to_owned(),
            allowed_sensitivity: BTreeSet::new(),
            regions: BTreeSet::new(),
            supports_cancellation: false,
            supports_usage_report: false,
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

    pub fn with_allowed_sensitivity<I, S>(mut self, allowed_sensitivity: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.allowed_sensitivity = allowed_sensitivity.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_regions<I, S>(mut self, regions: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.regions = regions.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_quality_tier(mut self, quality_tier: impl Into<String>) -> Self {
        self.quality_tier = quality_tier.into();
        self
    }

    pub fn with_cost_class(mut self, cost_class: impl Into<String>) -> Self {
        self.cost_class = cost_class.into();
        self
    }

    pub fn with_latency_class(mut self, latency_class: impl Into<String>) -> Self {
        self.latency_class = latency_class.into();
        self
    }

    pub fn with_cancellation(mut self, supports_cancellation: bool) -> Self {
        self.supports_cancellation = supports_cancellation;
        self
    }

    pub fn with_usage_report(mut self, supports_usage_report: bool) -> Self {
        self.supports_usage_report = supports_usage_report;
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelPool {
    pub pool_id: String,
    pub models: Vec<ModelProfile>,
    pub selection_policy_ref: String,
}

impl ModelPool {
    pub fn new(pool_id: impl Into<String>, selection_policy_ref: impl Into<String>) -> Self {
        Self {
            pool_id: pool_id.into(),
            models: Vec::new(),
            selection_policy_ref: selection_policy_ref.into(),
        }
    }

    pub fn with_models<I>(mut self, models: I) -> Self
    where
        I: IntoIterator<Item = ModelProfile>,
    {
        self.models = models.into_iter().collect();
        self
    }

    pub fn select_model(
        &self,
        request: &ModelSelectionRequest,
    ) -> Result<ModelProfile, ModelSelectionError> {
        if request
            .worker
            .model_pool_ref
            .as_ref()
            .is_some_and(|pool_ref| pool_ref != &self.pool_id)
        {
            return Err(ModelSelectionError::PoolMismatch {
                expected: request.worker.model_pool_ref.clone().unwrap_or_default(),
                actual: self.pool_id.clone(),
            });
        }
        for tool_name in &request.required_tools {
            if !request.worker.allowed_tools.contains(tool_name) {
                return Err(ModelSelectionError::ToolNotAllowed {
                    tool_name: tool_name.clone(),
                });
            }
        }
        if let (Some(requested), Some(ceiling)) =
            (&request.sensitivity, &request.worker.sensitivity_ceiling)
        {
            let requested_rank = match requested.as_str() {
                "public" => 0,
                "internal" => 1,
                "confidential" => 2,
                "restricted" => 3,
                _ => 4,
            };
            let ceiling_rank = match ceiling.as_str() {
                "public" => 0,
                "internal" => 1,
                "confidential" => 2,
                "restricted" => 3,
                _ => 4,
            };
            if requested_rank > ceiling_rank {
                return Err(ModelSelectionError::SensitivityAboveCeiling {
                    requested: requested.clone(),
                    ceiling: ceiling.clone(),
                });
            }
        }

        let mut required_capabilities = request.worker.required_capabilities.clone();
        required_capabilities.extend(request.required_capabilities.iter().cloned());
        let mut rejection_reasons = Vec::new();
        for model in &self.models {
            if !required_capabilities.is_subset(&model.capabilities) {
                rejection_reasons.push(format!("{}:missing_capability", model.profile_id));
                continue;
            }
            if request.sensitivity.as_ref().is_some_and(|sensitivity| {
                !model.allowed_sensitivity.is_empty()
                    && !model.allowed_sensitivity.contains(sensitivity)
            }) {
                rejection_reasons.push(format!("{}:sensitivity_not_allowed", model.profile_id));
                continue;
            }
            if request
                .region
                .as_ref()
                .is_some_and(|region| !model.regions.is_empty() && !model.regions.contains(region))
            {
                rejection_reasons.push(format!("{}:region_not_allowed", model.profile_id));
                continue;
            }
            return Ok(model.clone());
        }
        Err(ModelSelectionError::NoEligibleModel {
            pool_id: self.pool_id.clone(),
            reasons: rejection_reasons,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkerProfile {
    pub profile_id: String,
    pub required_capabilities: BTreeSet<String>,
    pub allowed_tools: BTreeSet<String>,
    pub model_pool_ref: Option<String>,
    pub sensitivity_ceiling: Option<String>,
    pub default_budget_ref: Option<String>,
}

impl WorkerProfile {
    pub fn new(profile_id: impl Into<String>) -> Self {
        Self {
            profile_id: profile_id.into(),
            required_capabilities: BTreeSet::new(),
            allowed_tools: BTreeSet::new(),
            model_pool_ref: None,
            sensitivity_ceiling: None,
            default_budget_ref: None,
        }
    }

    pub fn with_required_capabilities<I, S>(mut self, required_capabilities: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.required_capabilities = required_capabilities.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_allowed_tools<I, S>(mut self, allowed_tools: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.allowed_tools = allowed_tools.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_model_pool_ref(mut self, model_pool_ref: impl Into<String>) -> Self {
        self.model_pool_ref = Some(model_pool_ref.into());
        self
    }

    pub fn with_sensitivity_ceiling(mut self, sensitivity_ceiling: impl Into<String>) -> Self {
        self.sensitivity_ceiling = Some(sensitivity_ceiling.into());
        self
    }

    pub fn with_default_budget_ref(mut self, default_budget_ref: impl Into<String>) -> Self {
        self.default_budget_ref = Some(default_budget_ref.into());
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelSelectionRequest {
    pub worker: WorkerProfile,
    pub required_tools: BTreeSet<String>,
    pub required_capabilities: BTreeSet<String>,
    pub sensitivity: Option<String>,
    pub region: Option<String>,
}

impl ModelSelectionRequest {
    pub fn new(worker: WorkerProfile) -> Self {
        Self {
            worker,
            required_tools: BTreeSet::new(),
            required_capabilities: BTreeSet::new(),
            sensitivity: None,
            region: None,
        }
    }

    pub fn with_required_tools<I, S>(mut self, required_tools: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.required_tools = required_tools.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_required_capabilities<I, S>(mut self, required_capabilities: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.required_capabilities = required_capabilities.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_sensitivity(mut self, sensitivity: impl Into<String>) -> Self {
        self.sensitivity = Some(sensitivity.into());
        self
    }

    pub fn with_region(mut self, region: impl Into<String>) -> Self {
        self.region = Some(region.into());
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ModelSelectionError {
    PoolMismatch {
        expected: String,
        actual: String,
    },
    ToolNotAllowed {
        tool_name: String,
    },
    SensitivityAboveCeiling {
        requested: String,
        ceiling: String,
    },
    NoEligibleModel {
        pool_id: String,
        reasons: Vec<String>,
    },
}

impl fmt::Display for ModelSelectionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::PoolMismatch { expected, actual } => {
                write!(
                    formatter,
                    "worker requires model pool {expected:?}, not {actual:?}"
                )
            }
            Self::ToolNotAllowed { tool_name } => {
                write!(
                    formatter,
                    "tool {tool_name:?} is not allowed by worker profile"
                )
            }
            Self::SensitivityAboveCeiling { requested, ceiling } => write!(
                formatter,
                "sensitivity {requested:?} exceeds worker ceiling {ceiling:?}"
            ),
            Self::NoEligibleModel { pool_id, reasons } => write!(
                formatter,
                "no eligible model in pool {pool_id:?}: {}",
                reasons.join(", ")
            ),
        }
    }
}

impl Error for ModelSelectionError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AgentStateSchema {
    pub allowed_keys: BTreeSet<String>,
}

impl AgentStateSchema {
    pub fn new<I, S>(allowed_keys: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self {
            allowed_keys: allowed_keys.into_iter().map(Into::into).collect(),
        }
    }

    pub fn allows(&self, key: &str) -> bool {
        self.allowed_keys.contains(key)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum AgentStatePatchOp {
    Set { key: String, value: Value },
    Delete { key: String },
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct AgentStatePatch {
    pub ops: Vec<AgentStatePatchOp>,
}

impl AgentStatePatch {
    pub fn new() -> Self {
        Self { ops: Vec::new() }
    }

    pub fn set(mut self, key: impl Into<String>, value: Value) -> Self {
        self.ops.push(AgentStatePatchOp::Set {
            key: key.into(),
            value,
        });
        self
    }

    pub fn delete(mut self, key: impl Into<String>) -> Self {
        self.ops.push(AgentStatePatchOp::Delete { key: key.into() });
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct AgentState {
    pub revision: u64,
    pub values: BTreeMap<String, Value>,
    pub artifacts: Vec<ArtifactRef>,
    pub pending_approvals: Vec<String>,
    pub pending_reviews: Vec<String>,
    pub budget_id: Option<String>,
    pub active_task_plan_id: Option<String>,
}

impl AgentState {
    pub fn new() -> Self {
        Self {
            revision: 0,
            values: BTreeMap::new(),
            artifacts: Vec::new(),
            pending_approvals: Vec::new(),
            pending_reviews: Vec::new(),
            budget_id: None,
            active_task_plan_id: None,
        }
    }

    pub fn apply_patch(
        &mut self,
        expected_revision: u64,
        patch: AgentStatePatch,
        schema: Option<&AgentStateSchema>,
    ) -> Result<u64, AgentStateError> {
        if self.revision != expected_revision {
            return Err(AgentStateError::RevisionConflict {
                expected: expected_revision,
                actual: self.revision,
            });
        }
        for op in &patch.ops {
            let key = match op {
                AgentStatePatchOp::Set { key, .. } | AgentStatePatchOp::Delete { key } => key,
            };
            if schema.is_some_and(|schema| !schema.allows(key)) {
                return Err(AgentStateError::UnknownStateKey { key: key.clone() });
            }
        }
        for op in patch.ops {
            match op {
                AgentStatePatchOp::Set { key, value } => {
                    self.values.insert(key, value);
                }
                AgentStatePatchOp::Delete { key } => {
                    self.values.remove(&key);
                }
            }
        }
        self.revision += 1;
        Ok(self.revision)
    }
}

impl Default for AgentState {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AgentStateError {
    RevisionConflict { expected: u64, actual: u64 },
    UnknownStateKey { key: String },
}

impl fmt::Display for AgentStateError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::RevisionConflict { expected, actual } => write!(
                formatter,
                "agent state is at revision {actual}, not expected revision {expected}"
            ),
            Self::UnknownStateKey { key } => {
                write!(formatter, "agent state key {key:?} is not allowed")
            }
        }
    }
}

impl Error for AgentStateError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AgentLoopDecision {
    Continue { reason: String },
    Finalize { reason: String },
    Stop { reason: String },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AgentLoopController {
    pub spec: AgentSpec,
}

impl AgentLoopController {
    pub fn new(spec: AgentSpec) -> Self {
        Self { spec }
    }

    pub fn decide_next_step(
        &self,
        completed_steps: usize,
        remaining_budget_units: i64,
    ) -> AgentLoopDecision {
        if completed_steps >= self.spec.max_steps {
            return AgentLoopDecision::Stop {
                reason: "max_steps_reached".to_owned(),
            };
        }
        if self
            .spec
            .completion_reserve_units
            .is_some_and(|reserve| remaining_budget_units <= reserve)
        {
            return AgentLoopDecision::Finalize {
                reason: "completion_reserve_reached".to_owned(),
            };
        }
        AgentLoopDecision::Continue {
            reason: "admitted".to_owned(),
        }
    }
}

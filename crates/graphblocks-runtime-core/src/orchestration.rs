use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

use crate::budget::{BudgetPermit, UsageAmount};
use crate::policy::parse_policy_datetime_millis;

fn validate_identity(
    entity: &'static str,
    field: &'static str,
    value: &str,
) -> Result<(), TaskPlanError> {
    if value.trim().is_empty() {
        return Err(TaskPlanError::Identity { entity, field });
    }
    Ok(())
}

fn sorted_unique(items: impl IntoIterator<Item = String>) -> Vec<String> {
    items
        .into_iter()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

#[derive(Clone, Debug, PartialEq)]
pub struct TaskStep {
    pub step_id: String,
    pub description: String,
    pub depends_on: Vec<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl TaskStep {
    pub fn new(step_id: impl Into<String>, description: impl Into<String>) -> Self {
        Self {
            step_id: step_id.into(),
            description: description.into(),
            depends_on: Vec::new(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_depends_on<I, S>(mut self, depends_on: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.depends_on = depends_on.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: Value) -> Self {
        self.metadata.insert(key.into(), value);
        self
    }

    fn canonical_value(&self) -> Value {
        json!({
            "step_id": self.step_id,
            "description": self.description,
            "depends_on": self.depends_on,
            "metadata": self.metadata,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct TaskPlanPatch {
    pub patch_id: String,
    pub base_plan_id: String,
    pub base_revision: u64,
    pub upsert_steps: Vec<TaskStep>,
    pub remove_step_ids: Vec<String>,
    pub created_at: String,
    pub metadata: BTreeMap<String, Value>,
}

impl TaskPlanPatch {
    pub fn new(
        patch_id: impl Into<String>,
        base_plan_id: impl Into<String>,
        base_revision: u64,
    ) -> Self {
        Self {
            patch_id: patch_id.into(),
            base_plan_id: base_plan_id.into(),
            base_revision,
            upsert_steps: Vec::new(),
            remove_step_ids: Vec::new(),
            created_at: String::new(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_upsert_steps<I>(mut self, steps: I) -> Self
    where
        I: IntoIterator<Item = TaskStep>,
    {
        self.upsert_steps = steps.into_iter().collect();
        self
    }

    pub fn with_remove_step_ids<I, S>(mut self, step_ids: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.remove_step_ids = step_ids.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_created_at(mut self, created_at: impl Into<String>) -> Self {
        self.created_at = created_at.into();
        self
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: Value) -> Self {
        self.metadata.insert(key.into(), value);
        self
    }

    fn validated(mut self) -> Result<Self, TaskPlanError> {
        validate_identity("patch", "patch_id", &self.patch_id)?;
        validate_identity("patch", "base_plan_id", &self.base_plan_id)?;
        for step_id in &self.remove_step_ids {
            validate_identity("patch", "remove_step_ids", step_id)?;
        }
        self.remove_step_ids = sorted_unique(self.remove_step_ids);
        Ok(self)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TaskPlanLimits {
    pub max_steps: usize,
    pub max_dependencies_per_step: usize,
    pub max_description_chars: usize,
}

impl Default for TaskPlanLimits {
    fn default() -> Self {
        Self {
            max_steps: 128,
            max_dependencies_per_step: 16,
            max_description_chars: 4096,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TaskContextAccess {
    pub step_id: String,
    pub resource_id: String,
    pub mode: String,
    pub reason: Option<String>,
}

impl TaskContextAccess {
    pub fn new(
        step_id: impl Into<String>,
        resource_id: impl Into<String>,
        mode: impl Into<String>,
    ) -> Self {
        Self {
            step_id: step_id.into(),
            resource_id: resource_id.into(),
            mode: mode.into(),
            reason: None,
        }
    }

    pub fn with_reason(mut self, reason: impl Into<String>) -> Self {
        self.reason = Some(reason.into());
        self
    }

    fn canonical_value(&self) -> Value {
        json!({
            "step_id": self.step_id,
            "resource_id": self.resource_id,
            "mode": self.mode,
            "reason": self.reason,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TaskContextConflictKind {
    ReadWrite,
    WriteRead,
    WriteWrite,
}

impl TaskContextConflictKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::ReadWrite => "read_write",
            Self::WriteRead => "write_read",
            Self::WriteWrite => "write_write",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TaskContextAccessEdge {
    pub from_step_id: String,
    pub to_step_id: String,
    pub resource_id: String,
    pub conflict: TaskContextConflictKind,
}

impl TaskContextAccessEdge {
    fn canonical_value(&self) -> Value {
        json!({
            "from_step_id": self.from_step_id,
            "to_step_id": self.to_step_id,
            "resource_id": self.resource_id,
            "conflict": self.conflict.as_str(),
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TaskContextAccessGraph {
    pub edges: Vec<TaskContextAccessEdge>,
}

impl TaskContextAccessGraph {
    pub fn edge_contracts(&self) -> Vec<Value> {
        self.edges
            .iter()
            .map(TaskContextAccessEdge::canonical_value)
            .collect()
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "edges": self.edge_contracts(),
        }))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TaskContextAccessErrorReason {
    InvalidMode,
    UnknownStep,
    UnknownResource,
}

impl TaskContextAccessErrorReason {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::InvalidMode => "invalid_mode",
            Self::UnknownStep => "unknown_step",
            Self::UnknownResource => "unknown_resource",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TaskPlanError {
    Identity {
        entity: &'static str,
        field: &'static str,
    },
    Limit {
        limit_name: &'static str,
        limit: usize,
        actual: usize,
    },
    PatchMismatch {
        expected_plan_id: String,
        actual_plan_id: String,
        expected_revision: u64,
        actual_revision: u64,
    },
    StepNotFound {
        step_id: String,
    },
    DuplicateStep {
        step_id: String,
    },
    DependencyMissing {
        step_id: String,
        dependency_id: String,
    },
    Cycle {
        cycle: Vec<String>,
    },
    ContextAccess {
        step_id: String,
        resource_id: String,
        mode: String,
        reason: TaskContextAccessErrorReason,
    },
}

impl fmt::Display for TaskPlanError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Identity { entity, field } => {
                write!(formatter, "task {entity} {field} must not be empty")
            }
            Self::Limit {
                limit_name,
                limit,
                actual,
            } => write!(
                formatter,
                "task plan exceeds {limit_name}: limit {limit}, actual {actual}"
            ),
            Self::PatchMismatch {
                expected_plan_id,
                actual_plan_id,
                expected_revision,
                actual_revision,
            } => write!(
                formatter,
                "task plan patch mismatch: expected {expected_plan_id}@{expected_revision}, got {actual_plan_id}@{actual_revision}"
            ),
            Self::StepNotFound { step_id } => {
                write!(formatter, "task step {step_id:?} does not exist")
            }
            Self::DuplicateStep { step_id } => {
                write!(formatter, "task step {step_id:?} appears more than once")
            }
            Self::DependencyMissing {
                step_id,
                dependency_id,
            } => write!(
                formatter,
                "task step {step_id:?} depends on missing step {dependency_id:?}"
            ),
            Self::Cycle { cycle } => {
                write!(
                    formatter,
                    "task plan dependency cycle: {}",
                    cycle.join(" -> ")
                )
            }
            Self::ContextAccess {
                step_id,
                resource_id,
                mode,
                reason,
            } => write!(
                formatter,
                "task context access {step_id:?}:{resource_id:?}:{mode:?} is invalid: {}",
                reason.as_str()
            ),
        }
    }
}

impl Error for TaskPlanError {}

#[derive(Clone, Debug, PartialEq)]
pub struct TaskPlan {
    pub plan_id: String,
    pub objective: String,
    pub steps: Vec<TaskStep>,
    pub revision: u64,
    pub metadata: BTreeMap<String, Value>,
    pub limits: TaskPlanLimits,
    pub context_resources: Vec<String>,
    pub context_access: Vec<TaskContextAccess>,
}

impl TaskPlan {
    pub fn new(
        plan_id: impl Into<String>,
        objective: impl Into<String>,
    ) -> Result<Self, TaskPlanError> {
        Self::from_parts(
            plan_id,
            objective,
            1,
            Vec::new(),
            BTreeMap::new(),
            TaskPlanLimits::default(),
            Vec::new(),
            Vec::new(),
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn from_parts(
        plan_id: impl Into<String>,
        objective: impl Into<String>,
        revision: u64,
        steps: Vec<TaskStep>,
        metadata: BTreeMap<String, Value>,
        limits: TaskPlanLimits,
        context_resources: Vec<String>,
        context_access: Vec<TaskContextAccess>,
    ) -> Result<Self, TaskPlanError> {
        Self {
            plan_id: plan_id.into(),
            objective: objective.into(),
            steps,
            revision,
            metadata,
            limits,
            context_resources,
            context_access,
        }
        .normalized()
    }

    pub fn with_steps<I>(mut self, steps: I) -> Result<Self, TaskPlanError>
    where
        I: IntoIterator<Item = TaskStep>,
    {
        self.steps = steps.into_iter().collect();
        self.normalized()
    }

    pub fn with_limits(mut self, limits: TaskPlanLimits) -> Result<Self, TaskPlanError> {
        self.limits = limits;
        self.normalized()
    }

    pub fn with_context_resources<I, S>(mut self, resources: I) -> Result<Self, TaskPlanError>
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.context_resources = resources.into_iter().map(Into::into).collect();
        self.normalized()
    }

    pub fn with_context_access<I>(mut self, access: I) -> Result<Self, TaskPlanError>
    where
        I: IntoIterator<Item = TaskContextAccess>,
    {
        self.context_access = access.into_iter().collect();
        self.normalized()
    }

    pub fn with_metadata(
        mut self,
        key: impl Into<String>,
        value: Value,
    ) -> Result<Self, TaskPlanError> {
        self.metadata.insert(key.into(), value);
        self.normalized()
    }

    fn normalized(mut self) -> Result<Self, TaskPlanError> {
        validate_identity("plan", "plan_id", &self.plan_id)?;
        validate_identity("plan", "objective", &self.objective)?;
        for resource_id in &self.context_resources {
            validate_identity("plan", "context_resources", resource_id)?;
        }

        self.steps
            .sort_by(|left, right| left.step_id.cmp(&right.step_id));
        self.context_resources = sorted_unique(self.context_resources);
        self.context_access.sort_by(|left, right| {
            (
                left.step_id.as_str(),
                left.resource_id.as_str(),
                left.mode.as_str(),
            )
                .cmp(&(
                    right.step_id.as_str(),
                    right.resource_id.as_str(),
                    right.mode.as_str(),
                ))
        });

        if self.steps.len() > self.limits.max_steps {
            return Err(TaskPlanError::Limit {
                limit_name: "max_steps",
                limit: self.limits.max_steps,
                actual: self.steps.len(),
            });
        }

        let mut steps_by_id = BTreeMap::new();
        for step in &self.steps {
            validate_identity("step", "step_id", &step.step_id)?;
            validate_identity("step", "description", &step.description)?;
            if steps_by_id.contains_key(&step.step_id) {
                return Err(TaskPlanError::DuplicateStep {
                    step_id: step.step_id.clone(),
                });
            }
            if step.depends_on.len() > self.limits.max_dependencies_per_step {
                return Err(TaskPlanError::Limit {
                    limit_name: "max_dependencies_per_step",
                    limit: self.limits.max_dependencies_per_step,
                    actual: step.depends_on.len(),
                });
            }
            if step.description.chars().count() > self.limits.max_description_chars {
                return Err(TaskPlanError::Limit {
                    limit_name: "max_description_chars",
                    limit: self.limits.max_description_chars,
                    actual: step.description.chars().count(),
                });
            }
            for dependency_id in &step.depends_on {
                validate_identity("step", "depends_on", dependency_id)?;
            }
            steps_by_id.insert(step.step_id.clone(), step.clone());
        }

        for step in &self.steps {
            for dependency_id in &step.depends_on {
                if !steps_by_id.contains_key(dependency_id) {
                    return Err(TaskPlanError::DependencyMissing {
                        step_id: step.step_id.clone(),
                        dependency_id: dependency_id.clone(),
                    });
                }
            }
        }

        let mut visiting = BTreeSet::new();
        let mut visited = BTreeSet::new();
        let mut stack = Vec::new();
        for step_id in steps_by_id.keys() {
            visit_step(
                step_id,
                &steps_by_id,
                &mut visiting,
                &mut visited,
                &mut stack,
            )?;
        }

        let context_resource_ids = self
            .context_resources
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        for access in &self.context_access {
            validate_identity("context_access", "step_id", &access.step_id)?;
            validate_identity("context_access", "resource_id", &access.resource_id)?;
            let reason = if !matches!(access.mode.as_str(), "read" | "write" | "read_write") {
                Some(TaskContextAccessErrorReason::InvalidMode)
            } else if !steps_by_id.contains_key(&access.step_id) {
                Some(TaskContextAccessErrorReason::UnknownStep)
            } else if !context_resource_ids.contains(&access.resource_id) {
                Some(TaskContextAccessErrorReason::UnknownResource)
            } else {
                None
            };
            if let Some(reason) = reason {
                return Err(TaskPlanError::ContextAccess {
                    step_id: access.step_id.clone(),
                    resource_id: access.resource_id.clone(),
                    mode: access.mode.clone(),
                    reason,
                });
            }
        }

        Ok(self)
    }

    pub fn step(&self, step_id: impl AsRef<str>) -> Result<&TaskStep, TaskPlanError> {
        let step_id = step_id.as_ref();
        self.steps
            .iter()
            .find(|step| step.step_id == step_id)
            .ok_or_else(|| TaskPlanError::StepNotFound {
                step_id: step_id.to_string(),
            })
    }

    pub fn apply_patch(&self, patch: TaskPlanPatch) -> Result<Self, TaskPlanError> {
        let patch = patch.validated()?;
        if patch.base_plan_id != self.plan_id || patch.base_revision != self.revision {
            return Err(TaskPlanError::PatchMismatch {
                expected_plan_id: self.plan_id.clone(),
                actual_plan_id: patch.base_plan_id,
                expected_revision: self.revision,
                actual_revision: patch.base_revision,
            });
        }

        let remove_step_ids = patch.remove_step_ids.into_iter().collect::<BTreeSet<_>>();
        let mut steps_by_id = self
            .steps
            .iter()
            .filter(|step| !remove_step_ids.contains(&step.step_id))
            .map(|step| (step.step_id.clone(), step.clone()))
            .collect::<BTreeMap<_, _>>();
        for step in patch.upsert_steps {
            steps_by_id.insert(step.step_id.clone(), step);
        }

        let mut metadata = self.metadata.clone();
        metadata.extend(patch.metadata);
        Self::from_parts(
            self.plan_id.clone(),
            self.objective.clone(),
            self.revision + 1,
            steps_by_id.into_values().collect(),
            metadata,
            self.limits.clone(),
            self.context_resources.clone(),
            self.context_access.clone(),
        )
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "objective": self.objective,
            "steps": self.steps.iter().map(TaskStep::canonical_value).collect::<Vec<_>>(),
            "metadata": self.metadata,
            "limits": {
                "max_steps": self.limits.max_steps,
                "max_dependencies_per_step": self.limits.max_dependencies_per_step,
                "max_description_chars": self.limits.max_description_chars,
            },
            "context_resources": self.context_resources,
            "context_access": self.context_access.iter().map(TaskContextAccess::canonical_value).collect::<Vec<_>>(),
        }))
    }

    pub fn context_access_graph(&self) -> TaskContextAccessGraph {
        let step_order = self.dependency_ordered_step_ids();
        let positions = step_order
            .iter()
            .enumerate()
            .map(|(index, step_id)| (step_id.clone(), index))
            .collect::<BTreeMap<_, _>>();
        let mut access_by_resource = BTreeMap::<String, BTreeMap<String, TaskContextMode>>::new();
        for access in &self.context_access {
            access_by_resource
                .entry(access.resource_id.clone())
                .or_default()
                .entry(access.step_id.clone())
                .and_modify(|mode| mode.merge(access.mode.as_str()))
                .or_insert_with(|| TaskContextMode::from_mode(access.mode.as_str()));
        }

        let mut edges = Vec::new();
        for (resource_id, access_by_step) in access_by_resource {
            let mut step_access = access_by_step.into_iter().collect::<Vec<_>>();
            step_access.sort_by(|(left_step_id, _), (right_step_id, _)| {
                positions[left_step_id]
                    .cmp(&positions[right_step_id])
                    .then_with(|| left_step_id.cmp(right_step_id))
            });
            for left_index in 0..step_access.len() {
                for right_index in (left_index + 1)..step_access.len() {
                    let (left_step_id, left_mode) = &step_access[left_index];
                    let (right_step_id, right_mode) = &step_access[right_index];
                    let Some(conflict) = context_conflict_kind(*left_mode, *right_mode) else {
                        continue;
                    };
                    edges.push(TaskContextAccessEdge {
                        from_step_id: left_step_id.clone(),
                        to_step_id: right_step_id.clone(),
                        resource_id: resource_id.clone(),
                        conflict,
                    });
                }
            }
        }
        edges.sort_by(|left, right| {
            (
                left.from_step_id.as_str(),
                left.to_step_id.as_str(),
                left.resource_id.as_str(),
                left.conflict.as_str(),
            )
                .cmp(&(
                    right.from_step_id.as_str(),
                    right.to_step_id.as_str(),
                    right.resource_id.as_str(),
                    right.conflict.as_str(),
                ))
        });
        TaskContextAccessGraph { edges }
    }

    fn dependency_ordered_step_ids(&self) -> Vec<String> {
        let steps_by_id = self
            .steps
            .iter()
            .map(|step| (step.step_id.clone(), step))
            .collect::<BTreeMap<_, _>>();
        let mut ordered = Vec::new();
        let mut visited = BTreeSet::new();
        for step_id in steps_by_id.keys() {
            push_step_after_dependencies(step_id, &steps_by_id, &mut visited, &mut ordered);
        }
        ordered
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct TaskContextMode {
    reads: bool,
    writes: bool,
}

impl TaskContextMode {
    fn from_mode(mode: &str) -> Self {
        match mode {
            "write" => Self {
                reads: false,
                writes: true,
            },
            "read_write" => Self {
                reads: true,
                writes: true,
            },
            _ => Self {
                reads: true,
                writes: false,
            },
        }
    }

    fn merge(&mut self, mode: &str) {
        let mode = Self::from_mode(mode);
        self.reads |= mode.reads;
        self.writes |= mode.writes;
    }
}

fn context_conflict_kind(
    left: TaskContextMode,
    right: TaskContextMode,
) -> Option<TaskContextConflictKind> {
    if !left.writes && !right.writes {
        None
    } else if left.writes && right.writes {
        Some(TaskContextConflictKind::WriteWrite)
    } else if left.writes && right.reads {
        Some(TaskContextConflictKind::WriteRead)
    } else {
        Some(TaskContextConflictKind::ReadWrite)
    }
}

fn push_step_after_dependencies(
    step_id: &str,
    steps_by_id: &BTreeMap<String, &TaskStep>,
    visited: &mut BTreeSet<String>,
    ordered: &mut Vec<String>,
) {
    if !visited.insert(step_id.to_owned()) {
        return;
    }
    if let Some(step) = steps_by_id.get(step_id) {
        for dependency_id in &step.depends_on {
            push_step_after_dependencies(dependency_id, steps_by_id, visited, ordered);
        }
    }
    ordered.push(step_id.to_owned());
}

fn visit_step(
    step_id: &str,
    steps_by_id: &BTreeMap<String, TaskStep>,
    visiting: &mut BTreeSet<String>,
    visited: &mut BTreeSet<String>,
    stack: &mut Vec<String>,
) -> Result<(), TaskPlanError> {
    if visited.contains(step_id) {
        return Ok(());
    }
    if visiting.contains(step_id) {
        let start = stack
            .iter()
            .position(|candidate| candidate == step_id)
            .unwrap_or(0);
        let mut cycle = stack[start..].to_vec();
        cycle.push(step_id.to_string());
        return Err(TaskPlanError::Cycle { cycle });
    }

    visiting.insert(step_id.to_string());
    stack.push(step_id.to_string());
    for dependency_id in &steps_by_id[step_id].depends_on {
        visit_step(dependency_id, steps_by_id, visiting, visited, stack)?;
    }
    stack.pop();
    visiting.remove(step_id);
    visited.insert(step_id.to_string());
    Ok(())
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelProfile {
    pub profile_id: String,
    pub connection: String,
    pub capabilities: Vec<String>,
    pub quality_tier: String,
    pub cost_class: String,
    pub latency_class: String,
    pub allowed_sensitivity: Vec<String>,
    pub regions: Vec<String>,
    pub supports_cancellation: bool,
    pub supports_usage_report: bool,
}

impl ModelProfile {
    pub fn new(profile_id: impl Into<String>, connection: impl Into<String>) -> Self {
        Self {
            profile_id: profile_id.into(),
            connection: connection.into(),
            capabilities: Vec::new(),
            quality_tier: "standard".to_string(),
            cost_class: "standard".to_string(),
            latency_class: "standard".to_string(),
            allowed_sensitivity: Vec::new(),
            regions: Vec::new(),
            supports_cancellation: false,
            supports_usage_report: false,
        }
    }

    pub fn with_capabilities<I, S>(mut self, capabilities: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.capabilities = sorted_unique(capabilities.into_iter().map(Into::into));
        self
    }

    pub fn with_allowed_sensitivity<I, S>(mut self, allowed_sensitivity: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.allowed_sensitivity = sorted_unique(allowed_sensitivity.into_iter().map(Into::into));
        self
    }

    pub fn with_regions<I, S>(mut self, regions: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.regions = sorted_unique(regions.into_iter().map(Into::into));
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
pub struct WorkerProfile {
    pub profile_id: String,
    pub required_capabilities: Vec<String>,
    pub allowed_tools: Vec<String>,
    pub model_pool_ref: Option<String>,
    pub sensitivity_ceiling: Option<String>,
    pub default_budget_ref: Option<String>,
}

impl WorkerProfile {
    pub fn new(profile_id: impl Into<String>) -> Self {
        Self {
            profile_id: profile_id.into(),
            required_capabilities: Vec::new(),
            allowed_tools: Vec::new(),
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
        self.required_capabilities =
            sorted_unique(required_capabilities.into_iter().map(Into::into));
        self
    }

    pub fn with_allowed_tools<I, S>(mut self, allowed_tools: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.allowed_tools = sorted_unique(allowed_tools.into_iter().map(Into::into));
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
    pub required_tools: Vec<String>,
    pub required_capabilities: Vec<String>,
    pub sensitivity: Option<String>,
    pub region: Option<String>,
}

impl ModelSelectionRequest {
    pub fn new(worker: WorkerProfile) -> Self {
        Self {
            worker,
            required_tools: Vec::new(),
            required_capabilities: Vec::new(),
            sensitivity: None,
            region: None,
        }
    }

    pub fn with_required_tools<I, S>(mut self, required_tools: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.required_tools = sorted_unique(required_tools.into_iter().map(Into::into));
        self
    }

    pub fn with_required_capabilities<I, S>(mut self, required_capabilities: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.required_capabilities =
            sorted_unique(required_capabilities.into_iter().map(Into::into));
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
            Self::SensitivityAboveCeiling { requested, ceiling } => {
                write!(
                    formatter,
                    "sensitivity {requested:?} exceeds worker ceiling {ceiling:?}"
                )
            }
            Self::NoEligibleModel { pool_id, reasons } => {
                write!(
                    formatter,
                    "no eligible model in pool {pool_id:?}: {}",
                    reasons.join(", ")
                )
            }
        }
    }
}

impl Error for ModelSelectionError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelPool {
    pub pool_id: String,
    pub selection_policy_ref: String,
    pub models: Vec<ModelProfile>,
}

impl ModelPool {
    pub fn new(pool_id: impl Into<String>, selection_policy_ref: impl Into<String>) -> Self {
        Self {
            pool_id: pool_id.into(),
            selection_policy_ref: selection_policy_ref.into(),
            models: Vec::new(),
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
        if let Some(model_pool_ref) = &request.worker.model_pool_ref
            && model_pool_ref != &self.pool_id
        {
            return Err(ModelSelectionError::PoolMismatch {
                expected: model_pool_ref.clone(),
                actual: self.pool_id.clone(),
            });
        }
        for tool_name in &request.required_tools {
            if !request
                .worker
                .allowed_tools
                .iter()
                .any(|item| item == tool_name)
            {
                return Err(ModelSelectionError::ToolNotAllowed {
                    tool_name: tool_name.clone(),
                });
            }
        }
        if let (Some(requested), Some(ceiling)) =
            (&request.sensitivity, &request.worker.sensitivity_ceiling)
            && sensitivity_rank(requested) > sensitivity_rank(ceiling)
        {
            return Err(ModelSelectionError::SensitivityAboveCeiling {
                requested: requested.clone(),
                ceiling: ceiling.clone(),
            });
        }

        let mut required_capabilities = request
            .worker
            .required_capabilities
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        required_capabilities.extend(request.required_capabilities.iter().cloned());

        let mut rejection_reasons = Vec::new();
        for model in &self.models {
            let model_capabilities = model.capabilities.iter().cloned().collect::<BTreeSet<_>>();
            if !required_capabilities.is_subset(&model_capabilities) {
                rejection_reasons.push(format!("{}:missing_capability", model.profile_id));
                continue;
            }
            if let Some(sensitivity) = &request.sensitivity
                && !model.allowed_sensitivity.is_empty()
                && !model
                    .allowed_sensitivity
                    .iter()
                    .any(|candidate| candidate == sensitivity)
            {
                rejection_reasons.push(format!("{}:sensitivity_not_allowed", model.profile_id));
                continue;
            }
            if let Some(region) = &request.region
                && !model.regions.is_empty()
                && !model.regions.iter().any(|candidate| candidate == region)
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

fn sensitivity_rank(sensitivity: &str) -> u8 {
    match sensitivity {
        "public" => 0,
        "internal" => 1,
        "confidential" => 2,
        "restricted" => 3,
        _ => 4,
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LeasePoolError {
    Capacity {
        field_name: &'static str,
        value: u64,
    },
    Exhausted {
        pool_id: String,
        requested_units: u64,
        available_units: u64,
    },
    ResourceKindMismatch {
        expected: String,
        actual: String,
    },
    LeaseAlreadyExists {
        lease_id: String,
    },
    LeaseNotFound {
        lease_id: String,
    },
    EpochMismatch {
        lease_id: String,
        expected_epoch: u64,
        actual_epoch: u64,
    },
}

impl fmt::Display for LeasePoolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Capacity { field_name, value } => {
                write!(formatter, "{field_name} must be positive, got {value}")
            }
            Self::Exhausted {
                pool_id,
                requested_units,
                available_units,
            } => write!(
                formatter,
                "lease pool {pool_id:?} has {available_units} units available, requested {requested_units}"
            ),
            Self::ResourceKindMismatch { expected, actual } => {
                write!(
                    formatter,
                    "lease resource kind mismatch: expected {expected:?}, got {actual:?}"
                )
            }
            Self::LeaseAlreadyExists { lease_id } => {
                write!(formatter, "lease {lease_id:?} already exists")
            }
            Self::LeaseNotFound { lease_id } => {
                write!(formatter, "lease {lease_id:?} does not exist")
            }
            Self::EpochMismatch {
                lease_id,
                expected_epoch,
                actual_epoch,
            } => write!(
                formatter,
                "lease {lease_id:?} fencing epoch mismatch: expected {expected_epoch}, got {actual_epoch}"
            ),
        }
    }
}

impl Error for LeasePoolError {}

#[derive(Clone, Debug, PartialEq)]
pub struct LeaseRequest {
    pub request_id: String,
    pub holder: String,
    pub resource_kind: String,
    pub units: u64,
    pub metadata: BTreeMap<String, Value>,
}

impl LeaseRequest {
    pub fn new(
        request_id: impl Into<String>,
        holder: impl Into<String>,
        resource_kind: impl Into<String>,
    ) -> Self {
        Self {
            request_id: request_id.into(),
            holder: holder.into(),
            resource_kind: resource_kind.into(),
            units: 1,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_units(mut self, units: u64) -> Self {
        self.units = units;
        self
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: Value) -> Self {
        self.metadata.insert(key.into(), value);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct LeaseGrant {
    pub lease_id: String,
    pub request_id: String,
    pub pool_id: String,
    pub holder: String,
    pub resource_kind: String,
    pub units: u64,
    pub fencing_epoch: u64,
    pub acquired_at: String,
    pub expires_at: String,
    pub metadata: BTreeMap<String, Value>,
}

impl LeaseGrant {
    pub fn is_active_at(&self, now: &str) -> bool {
        match (
            parse_policy_datetime_millis(&self.expires_at),
            parse_policy_datetime_millis(now),
        ) {
            (Some(expires_at), Some(now)) => expires_at > now,
            _ => false,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct LeasePool {
    pub pool_id: String,
    pub resource_kind: String,
    pub capacity_units: u64,
    pub active_leases: Vec<LeaseGrant>,
    pub next_fencing_epoch: u64,
    pub policy_ref: Option<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl LeasePool {
    pub fn new(
        pool_id: impl Into<String>,
        resource_kind: impl Into<String>,
        capacity_units: u64,
    ) -> Result<Self, LeasePoolError> {
        Self {
            pool_id: pool_id.into(),
            resource_kind: resource_kind.into(),
            capacity_units,
            active_leases: Vec::new(),
            next_fencing_epoch: 1,
            policy_ref: None,
            metadata: BTreeMap::new(),
        }
        .normalized()
    }

    fn normalized(mut self) -> Result<Self, LeasePoolError> {
        if self.capacity_units == 0 {
            return Err(LeasePoolError::Capacity {
                field_name: "capacity_units",
                value: self.capacity_units,
            });
        }
        self.active_leases.sort_by(|left, right| {
            (left.expires_at.as_str(), left.lease_id.as_str())
                .cmp(&(right.expires_at.as_str(), right.lease_id.as_str()))
        });
        let mut seen = BTreeSet::new();
        let mut used_units = 0;
        let mut highest_epoch = 0;
        for lease in &self.active_leases {
            if lease.pool_id != self.pool_id {
                return Err(LeasePoolError::ResourceKindMismatch {
                    expected: self.pool_id.clone(),
                    actual: lease.pool_id.clone(),
                });
            }
            if lease.resource_kind != self.resource_kind {
                return Err(LeasePoolError::ResourceKindMismatch {
                    expected: self.resource_kind.clone(),
                    actual: lease.resource_kind.clone(),
                });
            }
            if lease.units == 0 {
                return Err(LeasePoolError::Capacity {
                    field_name: "units",
                    value: lease.units,
                });
            }
            if !seen.insert(lease.lease_id.clone()) {
                return Err(LeasePoolError::LeaseAlreadyExists {
                    lease_id: lease.lease_id.clone(),
                });
            }
            used_units += lease.units;
            highest_epoch = highest_epoch.max(lease.fencing_epoch);
        }
        if used_units > self.capacity_units {
            return Err(LeasePoolError::Exhausted {
                pool_id: self.pool_id.clone(),
                requested_units: used_units,
                available_units: self.capacity_units,
            });
        }
        self.next_fencing_epoch = self.next_fencing_epoch.max(highest_epoch + 1);
        Ok(self)
    }

    pub fn used_units(&self) -> u64 {
        self.active_leases.iter().map(|lease| lease.units).sum()
    }

    pub fn available_units(&self) -> u64 {
        self.capacity_units - self.used_units()
    }

    pub fn reap_expired(&self, now: impl AsRef<str>) -> Result<Self, LeasePoolError> {
        let now = now.as_ref();
        let mut reaped = self.clone();
        reaped.active_leases.retain(|lease| lease.is_active_at(now));
        reaped.normalized()
    }

    pub fn acquire(
        &self,
        request: &LeaseRequest,
        lease_id: impl Into<String>,
        acquired_at: impl Into<String>,
        expires_at: impl Into<String>,
    ) -> Result<(Self, LeaseGrant), LeasePoolError> {
        if request.units == 0 {
            return Err(LeasePoolError::Capacity {
                field_name: "units",
                value: request.units,
            });
        }
        if request.resource_kind != self.resource_kind {
            return Err(LeasePoolError::ResourceKindMismatch {
                expected: self.resource_kind.clone(),
                actual: request.resource_kind.clone(),
            });
        }

        let acquired_at = acquired_at.into();
        let lease_id = lease_id.into();
        let mut current = self.reap_expired(&acquired_at)?;
        if current
            .active_leases
            .iter()
            .any(|lease| lease.lease_id == lease_id)
        {
            return Err(LeasePoolError::LeaseAlreadyExists { lease_id });
        }
        if request.units > current.available_units() {
            return Err(LeasePoolError::Exhausted {
                pool_id: self.pool_id.clone(),
                requested_units: request.units,
                available_units: current.available_units(),
            });
        }

        let grant = LeaseGrant {
            lease_id,
            request_id: request.request_id.clone(),
            pool_id: self.pool_id.clone(),
            holder: request.holder.clone(),
            resource_kind: request.resource_kind.clone(),
            units: request.units,
            fencing_epoch: current.next_fencing_epoch,
            acquired_at,
            expires_at: expires_at.into(),
            metadata: request.metadata.clone(),
        };
        current.next_fencing_epoch += 1;
        current.active_leases.push(grant.clone());
        Ok((current.normalized()?, grant))
    }

    pub fn release(
        &self,
        lease_id: impl AsRef<str>,
        fencing_epoch: u64,
    ) -> Result<Self, LeasePoolError> {
        let lease_id = lease_id.as_ref();
        let mut active_leases = self.active_leases.clone();
        let index = active_leases
            .iter()
            .position(|lease| lease.lease_id == lease_id)
            .ok_or_else(|| LeasePoolError::LeaseNotFound {
                lease_id: lease_id.to_string(),
            })?;
        let lease = &active_leases[index];
        if lease.fencing_epoch != fencing_epoch {
            return Err(LeasePoolError::EpochMismatch {
                lease_id: lease_id.to_string(),
                expected_epoch: lease.fencing_epoch,
                actual_epoch: fencing_epoch,
            });
        }
        active_leases.remove(index);
        let mut released = self.clone();
        released.active_leases = active_leases;
        released.normalized()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ChildBudgetDelegationError {
    ParentPermitDoesNotCoverDelegation { permit_id: String },
}

impl fmt::Display for ChildBudgetDelegationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ParentPermitDoesNotCoverDelegation { permit_id } => {
                write!(
                    formatter,
                    "parent permit {permit_id:?} does not cover delegation"
                )
            }
        }
    }
}

impl Error for ChildBudgetDelegationError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ChildBudgetDelegation {
    pub delegation_id: String,
    pub parent_permit: BudgetPermit,
    pub child_owner: String,
    pub amounts: Vec<UsageAmount>,
    pub expires_at: String,
    pub continuation_profile: Option<String>,
}

impl ChildBudgetDelegation {
    pub fn new(
        delegation_id: impl Into<String>,
        parent_permit: BudgetPermit,
        child_owner: impl Into<String>,
        amounts: impl IntoIterator<Item = UsageAmount>,
        expires_at: impl Into<String>,
    ) -> Self {
        Self {
            delegation_id: delegation_id.into(),
            parent_permit,
            child_owner: child_owner.into(),
            amounts: amounts.into_iter().collect(),
            expires_at: expires_at.into(),
            continuation_profile: None,
        }
    }

    pub fn with_continuation_profile(mut self, continuation_profile: impl Into<String>) -> Self {
        self.continuation_profile = Some(continuation_profile.into());
        self
    }

    pub fn create_child_permit(
        &self,
        permit_id: impl Into<String>,
    ) -> Result<BudgetPermit, ChildBudgetDelegationError> {
        if !self.parent_permit.allows(self.amounts.clone()) {
            return Err(
                ChildBudgetDelegationError::ParentPermitDoesNotCoverDelegation {
                    permit_id: self.parent_permit.permit_id.clone(),
                },
            );
        }
        Ok(BudgetPermit {
            permit_id: permit_id.into(),
            reservation_refs: self.parent_permit.reservation_refs.clone(),
            owner: self.child_owner.clone(),
            atomic_unit: self.parent_permit.atomic_unit.clone(),
            admission_epoch: self.parent_permit.admission_epoch,
            authorized_amounts: self.amounts.clone(),
            continuation_profile: self
                .continuation_profile
                .clone()
                .unwrap_or_else(|| self.parent_permit.continuation_profile.clone()),
            policy_snapshot_digest: self.parent_permit.policy_snapshot_digest.clone(),
            expires_at: self.expires_at.clone(),
            low_watermark: Vec::new(),
            fencing_tokens: self.parent_permit.fencing_tokens.clone(),
        })
    }
}

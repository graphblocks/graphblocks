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
}

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

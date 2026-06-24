use std::collections::VecDeque;
use std::collections::{BTreeMap, BTreeSet};

use crate::output_policy::PendingToolCallsDisposition;
use crate::tool_call::ToolCall;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolExecutionFailurePolicy {
    FailFast,
    Collect,
    ReturnFailuresToModel,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolExecutionCancellationPolicy {
    CancelDependents,
    CancelAll,
    AllowIndependentCalls,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolExecutionState {
    Pending,
    Running,
    Completed,
    Failed,
    Denied,
    Cancelled,
    Skipped,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolExecutionPlanError {
    InvalidMaximumParallelism,
    DuplicateToolCall {
        tool_call_id: String,
    },
    ResponseMismatch {
        tool_call_id: String,
        expected_response_id: String,
        actual_response_id: String,
    },
    UnknownDependency {
        tool_call_id: String,
        dependency_id: String,
    },
    DependencyCycle {
        tool_call_id: String,
    },
    UnknownToolCall {
        tool_call_id: String,
    },
    ToolCallNotPending {
        tool_call_id: String,
        current: ToolExecutionState,
    },
    ToolCallNotRunning {
        tool_call_id: String,
        current: ToolExecutionState,
    },
    DependenciesNotReady {
        tool_call_id: String,
    },
    ParallelismExhausted,
    EffectConflict {
        effect_key: String,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolPlanCall {
    pub call: ToolCall,
    pub effect_key: Option<String>,
}

impl ToolPlanCall {
    pub fn new(call: ToolCall) -> Self {
        Self {
            call,
            effect_key: None,
        }
    }

    pub fn with_effect_key(mut self, effect_key: impl Into<String>) -> Self {
        self.effect_key = Some(effect_key.into());
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolExecutionPlan {
    pub plan_id: String,
    pub response_id: String,
    pub maximum_parallelism: usize,
    pub failure_policy: ToolExecutionFailurePolicy,
    pub cancellation_policy: ToolExecutionCancellationPolicy,
    calls: BTreeMap<String, ToolPlanCall>,
    states: BTreeMap<String, ToolExecutionState>,
}

impl ToolExecutionPlan {
    pub fn new<I>(
        plan_id: impl Into<String>,
        response_id: impl Into<String>,
        calls: I,
        maximum_parallelism: usize,
    ) -> Result<Self, ToolExecutionPlanError>
    where
        I: IntoIterator<Item = ToolPlanCall>,
    {
        if maximum_parallelism == 0 {
            return Err(ToolExecutionPlanError::InvalidMaximumParallelism);
        }

        let response_id = response_id.into();
        let mut indexed_calls = BTreeMap::new();
        let mut states = BTreeMap::new();
        for planned_call in calls {
            let tool_call_id = planned_call.call.tool_call_id.clone();
            if planned_call.call.response_id != response_id {
                return Err(ToolExecutionPlanError::ResponseMismatch {
                    tool_call_id,
                    expected_response_id: response_id,
                    actual_response_id: planned_call.call.response_id,
                });
            }
            if indexed_calls
                .insert(tool_call_id.clone(), planned_call)
                .is_some()
            {
                return Err(ToolExecutionPlanError::DuplicateToolCall { tool_call_id });
            }
            states.insert(tool_call_id, ToolExecutionState::Pending);
        }
        for (tool_call_id, planned_call) in &indexed_calls {
            for dependency_id in &planned_call.call.depends_on {
                if !indexed_calls.contains_key(dependency_id) {
                    return Err(ToolExecutionPlanError::UnknownDependency {
                        tool_call_id: tool_call_id.clone(),
                        dependency_id: dependency_id.clone(),
                    });
                }
            }
        }

        let mut remaining_dependencies = indexed_calls
            .iter()
            .map(|(tool_call_id, planned_call)| {
                (
                    tool_call_id.clone(),
                    planned_call
                        .call
                        .depends_on
                        .iter()
                        .cloned()
                        .collect::<BTreeSet<_>>(),
                )
            })
            .collect::<BTreeMap<_, _>>();
        let mut ready = remaining_dependencies
            .iter()
            .filter_map(|(tool_call_id, dependencies)| {
                dependencies.is_empty().then(|| tool_call_id.clone())
            })
            .collect::<VecDeque<_>>();
        while let Some(completed_id) = ready.pop_front() {
            if remaining_dependencies.remove(&completed_id).is_none() {
                continue;
            }
            for (candidate_id, dependencies) in &mut remaining_dependencies {
                if dependencies.remove(&completed_id) && dependencies.is_empty() {
                    ready.push_back(candidate_id.clone());
                }
            }
        }
        if let Some(tool_call_id) = remaining_dependencies.keys().next() {
            return Err(ToolExecutionPlanError::DependencyCycle {
                tool_call_id: tool_call_id.clone(),
            });
        }

        Ok(Self {
            plan_id: plan_id.into(),
            response_id,
            maximum_parallelism,
            failure_policy: ToolExecutionFailurePolicy::ReturnFailuresToModel,
            cancellation_policy: ToolExecutionCancellationPolicy::CancelDependents,
            calls: indexed_calls,
            states,
        })
    }

    pub fn with_failure_policy(mut self, policy: ToolExecutionFailurePolicy) -> Self {
        self.failure_policy = policy;
        self
    }

    pub fn with_cancellation_policy(mut self, policy: ToolExecutionCancellationPolicy) -> Self {
        self.cancellation_policy = policy;
        self
    }

    pub fn state(&self, tool_call_id: impl AsRef<str>) -> Option<ToolExecutionState> {
        self.states.get(tool_call_id.as_ref()).copied()
    }

    pub fn ready_call_ids(&self) -> Vec<String> {
        let running_count = self.running_count();
        if running_count >= self.maximum_parallelism {
            return Vec::new();
        }

        let mut remaining_slots = self.maximum_parallelism - running_count;
        let mut reserved_effect_keys = self.running_effect_keys();
        let mut ready = Vec::new();
        for (tool_call_id, planned_call) in &self.calls {
            if remaining_slots == 0 {
                break;
            }
            if self.states.get(tool_call_id) != Some(&ToolExecutionState::Pending) {
                continue;
            }
            if !self.dependencies_completed(&planned_call.call) {
                continue;
            }
            if let Some(effect_key) = &planned_call.effect_key {
                if reserved_effect_keys.contains(effect_key) {
                    continue;
                }
                reserved_effect_keys.insert(effect_key.clone());
            }

            ready.push(tool_call_id.clone());
            remaining_slots -= 1;
        }
        ready
    }

    pub fn record_started(
        &mut self,
        tool_call_id: impl AsRef<str>,
    ) -> Result<(), ToolExecutionPlanError> {
        let tool_call_id = tool_call_id.as_ref();
        let current = self.states.get(tool_call_id).ok_or_else(|| {
            ToolExecutionPlanError::UnknownToolCall {
                tool_call_id: tool_call_id.to_owned(),
            }
        })?;
        if *current != ToolExecutionState::Pending {
            return Err(ToolExecutionPlanError::ToolCallNotPending {
                tool_call_id: tool_call_id.to_owned(),
                current: *current,
            });
        }

        let planned_call = self.calls.get(tool_call_id).ok_or_else(|| {
            ToolExecutionPlanError::UnknownToolCall {
                tool_call_id: tool_call_id.to_owned(),
            }
        })?;
        if !self.dependencies_completed(&planned_call.call) {
            return Err(ToolExecutionPlanError::DependenciesNotReady {
                tool_call_id: tool_call_id.to_owned(),
            });
        }
        if self.running_count() >= self.maximum_parallelism {
            return Err(ToolExecutionPlanError::ParallelismExhausted);
        }
        if let Some(effect_key) = &planned_call.effect_key {
            if self.running_effect_keys().contains(effect_key) {
                return Err(ToolExecutionPlanError::EffectConflict {
                    effect_key: effect_key.clone(),
                });
            }
        }

        self.states
            .insert(tool_call_id.to_owned(), ToolExecutionState::Running);
        Ok(())
    }

    pub fn record_completed(
        &mut self,
        tool_call_id: impl AsRef<str>,
    ) -> Result<(), ToolExecutionPlanError> {
        self.enter_terminal(tool_call_id.as_ref(), ToolExecutionState::Completed)
    }

    pub fn record_failed(
        &mut self,
        tool_call_id: impl AsRef<str>,
    ) -> Result<(), ToolExecutionPlanError> {
        self.enter_terminal(tool_call_id.as_ref(), ToolExecutionState::Failed)?;
        self.mark_blocked_dependents(ToolExecutionState::Skipped);

        if self.failure_policy == ToolExecutionFailurePolicy::FailFast {
            for state in self.states.values_mut() {
                if *state == ToolExecutionState::Pending {
                    *state = ToolExecutionState::Cancelled;
                }
            }
        }

        Ok(())
    }

    pub fn record_cancelled(
        &mut self,
        tool_call_id: impl AsRef<str>,
    ) -> Result<(), ToolExecutionPlanError> {
        self.enter_terminal(tool_call_id.as_ref(), ToolExecutionState::Cancelled)?;

        match self.cancellation_policy {
            ToolExecutionCancellationPolicy::CancelDependents => {
                self.mark_blocked_dependents(ToolExecutionState::Cancelled);
            }
            ToolExecutionCancellationPolicy::CancelAll => {
                for state in self.states.values_mut() {
                    if matches!(
                        state,
                        ToolExecutionState::Pending | ToolExecutionState::Running
                    ) {
                        *state = ToolExecutionState::Cancelled;
                    }
                }
            }
            ToolExecutionCancellationPolicy::AllowIndependentCalls => {
                self.mark_blocked_dependents(ToolExecutionState::Skipped);
            }
        }

        Ok(())
    }

    pub fn apply_policy_stop(
        &mut self,
        pending_tool_calls: PendingToolCallsDisposition,
    ) -> Vec<String> {
        let mut affected = Vec::new();
        match pending_tool_calls {
            PendingToolCallsDisposition::Keep => {}
            PendingToolCallsDisposition::Deny => {
                for (tool_call_id, state) in &mut self.states {
                    if *state == ToolExecutionState::Pending {
                        *state = ToolExecutionState::Denied;
                        affected.push(tool_call_id.clone());
                    }
                }
            }
            PendingToolCallsDisposition::CancelAdmitted => {
                for (tool_call_id, state) in &mut self.states {
                    if *state == ToolExecutionState::Running {
                        *state = ToolExecutionState::Cancelled;
                        affected.push(tool_call_id.clone());
                    } else if *state == ToolExecutionState::Pending {
                        *state = ToolExecutionState::Denied;
                        affected.push(tool_call_id.clone());
                    }
                }
            }
        }
        affected
    }

    fn enter_terminal(
        &mut self,
        tool_call_id: &str,
        terminal_state: ToolExecutionState,
    ) -> Result<(), ToolExecutionPlanError> {
        let current = self.states.get(tool_call_id).ok_or_else(|| {
            ToolExecutionPlanError::UnknownToolCall {
                tool_call_id: tool_call_id.to_owned(),
            }
        })?;
        if *current != ToolExecutionState::Running {
            return Err(ToolExecutionPlanError::ToolCallNotRunning {
                tool_call_id: tool_call_id.to_owned(),
                current: *current,
            });
        }
        self.states.insert(tool_call_id.to_owned(), terminal_state);
        Ok(())
    }

    fn mark_blocked_dependents(&mut self, blocked_state: ToolExecutionState) {
        loop {
            let blocked = self
                .calls
                .iter()
                .filter_map(|(candidate_id, planned_call)| {
                    if self.states.get(candidate_id) != Some(&ToolExecutionState::Pending) {
                        return None;
                    }
                    let blocked = planned_call.call.depends_on.iter().any(|dependency| {
                        matches!(
                            self.states.get(dependency),
                            Some(
                                ToolExecutionState::Failed
                                    | ToolExecutionState::Denied
                                    | ToolExecutionState::Cancelled
                                    | ToolExecutionState::Skipped
                            )
                        )
                    });
                    blocked.then(|| candidate_id.clone())
                })
                .collect::<Vec<_>>();
            if blocked.is_empty() {
                break;
            }
            for blocked_id in blocked {
                self.states.insert(blocked_id, blocked_state);
            }
        }
    }

    fn running_count(&self) -> usize {
        self.states
            .values()
            .filter(|state| **state == ToolExecutionState::Running)
            .count()
    }

    fn running_effect_keys(&self) -> BTreeSet<String> {
        let mut effect_keys = BTreeSet::new();
        for (tool_call_id, state) in &self.states {
            if *state != ToolExecutionState::Running {
                continue;
            }
            if let Some(effect_key) = self
                .calls
                .get(tool_call_id)
                .and_then(|planned_call| planned_call.effect_key.as_ref())
            {
                effect_keys.insert(effect_key.clone());
            }
        }
        effect_keys
    }

    fn dependencies_completed(&self, call: &ToolCall) -> bool {
        call.depends_on
            .iter()
            .all(|dependency| self.states.get(dependency) == Some(&ToolExecutionState::Completed))
    }
}

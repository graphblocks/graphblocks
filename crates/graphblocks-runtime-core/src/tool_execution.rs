use std::collections::VecDeque;
use std::collections::{BTreeMap, BTreeSet};

use crate::output_policy::PendingToolCallsDisposition;
use crate::tool::{ToolCancellation, ToolEffect, has_conflicting_tool_effects};
use crate::tool_call::{ToolCall, ToolCallError};
use serde_json::Value;

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
    PolicyStopped,
    Expired,
    Skipped,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolExecutionPlanError {
    EmptyField {
        field: &'static str,
    },
    InvalidMaximumParallelism,
    InvalidToolCall {
        source: ToolCallError,
    },
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
    EmptyEffectKey {
        tool_call_id: String,
    },
    ConflictingToolEffects {
        tool_call_id: String,
    },
    UnsafeParallelEffects {
        tool_call_id: String,
    },
    InvalidEffectKeyTemplate {
        template: String,
    },
    EffectKeyTemplateUnsupportedPlaceholder {
        placeholder: String,
    },
    EffectKeyTemplateMissingValue {
        placeholder: String,
    },
    EffectKeyTemplateNonScalarValue {
        placeholder: String,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolPlanCall {
    pub call: ToolCall,
    pub effect_key: Option<String>,
    pub effects: BTreeSet<ToolEffect>,
    pub cancellation: ToolCancellation,
}

impl ToolPlanCall {
    pub fn new(call: ToolCall) -> Self {
        Self {
            call,
            effect_key: None,
            effects: BTreeSet::new(),
            cancellation: ToolCancellation::Cooperative,
        }
    }

    pub fn with_effect_key(mut self, effect_key: impl Into<String>) -> Self {
        self.effect_key = Some(effect_key.into());
        self
    }

    pub fn with_effect_key_template(
        mut self,
        template: &str,
    ) -> Result<Self, ToolExecutionPlanError> {
        let mut effect_key = String::new();
        let mut rest = template;
        while let Some(open_index) = rest.find('{') {
            effect_key.push_str(&rest[..open_index]);
            let after_open = &rest[open_index + 1..];
            let Some(close_index) = after_open.find('}') else {
                return Err(ToolExecutionPlanError::InvalidEffectKeyTemplate {
                    template: template.to_owned(),
                });
            };
            let placeholder = &after_open[..close_index];
            if placeholder.is_empty() {
                return Err(ToolExecutionPlanError::InvalidEffectKeyTemplate {
                    template: template.to_owned(),
                });
            }
            if placeholder == "tool.name" {
                effect_key.push_str(&self.call.name);
            } else if let Some(arguments_path) = placeholder.strip_prefix("arguments.") {
                if arguments_path.is_empty() {
                    return Err(
                        ToolExecutionPlanError::EffectKeyTemplateUnsupportedPlaceholder {
                            placeholder: placeholder.to_owned(),
                        },
                    );
                }
                let mut value = &self.call.arguments;
                for segment in arguments_path.split('.') {
                    if segment.is_empty() {
                        return Err(
                            ToolExecutionPlanError::EffectKeyTemplateUnsupportedPlaceholder {
                                placeholder: placeholder.to_owned(),
                            },
                        );
                    }
                    let Some(next_value) = value.get(segment) else {
                        return Err(ToolExecutionPlanError::EffectKeyTemplateMissingValue {
                            placeholder: placeholder.to_owned(),
                        });
                    };
                    value = next_value;
                }
                match value {
                    Value::String(value) => effect_key.push_str(value),
                    Value::Number(value) => effect_key.push_str(&value.to_string()),
                    Value::Bool(value) => effect_key.push_str(&value.to_string()),
                    Value::Null => {
                        return Err(ToolExecutionPlanError::EffectKeyTemplateMissingValue {
                            placeholder: placeholder.to_owned(),
                        });
                    }
                    Value::Array(_) | Value::Object(_) => {
                        return Err(ToolExecutionPlanError::EffectKeyTemplateNonScalarValue {
                            placeholder: placeholder.to_owned(),
                        });
                    }
                }
            } else {
                return Err(
                    ToolExecutionPlanError::EffectKeyTemplateUnsupportedPlaceholder {
                        placeholder: placeholder.to_owned(),
                    },
                );
            }
            rest = &after_open[close_index + 1..];
        }
        if rest.contains('}') {
            return Err(ToolExecutionPlanError::InvalidEffectKeyTemplate {
                template: template.to_owned(),
            });
        }
        effect_key.push_str(rest);
        self.effect_key = Some(effect_key);
        Ok(self)
    }

    pub fn with_effects<I>(mut self, effects: I) -> Self
    where
        I: IntoIterator<Item = ToolEffect>,
    {
        self.effects = effects.into_iter().collect();
        self
    }

    pub fn with_cancellation(mut self, cancellation: ToolCancellation) -> Self {
        self.cancellation = cancellation;
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
        let plan_id = plan_id.into();
        let response_id = response_id.into();
        for (field, value) in [
            ("plan_id", plan_id.as_str()),
            ("response_id", response_id.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(ToolExecutionPlanError::EmptyField { field });
            }
        }
        if maximum_parallelism == 0 {
            return Err(ToolExecutionPlanError::InvalidMaximumParallelism);
        }

        let mut indexed_calls = BTreeMap::new();
        let mut states = BTreeMap::new();
        for planned_call in calls {
            planned_call
                .call
                .validate()
                .map_err(|source| ToolExecutionPlanError::InvalidToolCall { source })?;
            let tool_call_id = planned_call.call.tool_call_id.clone();
            if planned_call.call.response_id != response_id {
                return Err(ToolExecutionPlanError::ResponseMismatch {
                    tool_call_id,
                    expected_response_id: response_id,
                    actual_response_id: planned_call.call.response_id,
                });
            }
            if planned_call
                .effect_key
                .as_ref()
                .is_some_and(|effect_key| effect_key.trim().is_empty())
            {
                return Err(ToolExecutionPlanError::EmptyEffectKey { tool_call_id });
            }
            if has_conflicting_tool_effects(&planned_call.effects) {
                return Err(ToolExecutionPlanError::ConflictingToolEffects { tool_call_id });
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
            .filter(|(_, dependencies)| dependencies.is_empty())
            .map(|(tool_call_id, _)| tool_call_id.clone())
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
        if maximum_parallelism > 1 {
            let call_ids = indexed_calls.keys().cloned().collect::<Vec<_>>();
            for (left_index, left_id) in call_ids.iter().enumerate() {
                let left_call = &indexed_calls[left_id];
                if !has_state_changing_tool_effects(&left_call.effects) {
                    continue;
                }
                for right_id in call_ids.iter().skip(left_index + 1) {
                    let right_call = &indexed_calls[right_id];
                    if !has_state_changing_tool_effects(&right_call.effects) {
                        continue;
                    }
                    if depends_on(&indexed_calls, left_id, right_id)
                        || depends_on(&indexed_calls, right_id, left_id)
                    {
                        continue;
                    }
                    if left_call.effect_key.is_none() {
                        return Err(ToolExecutionPlanError::UnsafeParallelEffects {
                            tool_call_id: left_id.clone(),
                        });
                    }
                    if right_call.effect_key.is_none() {
                        return Err(ToolExecutionPlanError::UnsafeParallelEffects {
                            tool_call_id: right_id.clone(),
                        });
                    }
                }
            }
        }

        Ok(Self {
            plan_id,
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
        if let Some(effect_key) = &planned_call.effect_key
            && self.running_effect_keys().contains(effect_key)
        {
            return Err(ToolExecutionPlanError::EffectConflict {
                effect_key: effect_key.clone(),
            });
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

    pub fn record_denied(
        &mut self,
        tool_call_id: impl AsRef<str>,
    ) -> Result<(), ToolExecutionPlanError> {
        self.enter_pending_terminal(tool_call_id.as_ref(), ToolExecutionState::Denied)?;
        self.mark_blocked_dependents(ToolExecutionState::Skipped);
        Ok(())
    }

    pub fn record_expired(
        &mut self,
        tool_call_id: impl AsRef<str>,
    ) -> Result<(), ToolExecutionPlanError> {
        self.enter_pending_terminal(tool_call_id.as_ref(), ToolExecutionState::Expired)?;
        self.mark_blocked_dependents(ToolExecutionState::Skipped);
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

    pub fn record_policy_stopped(
        &mut self,
        tool_call_id: impl AsRef<str>,
    ) -> Result<(), ToolExecutionPlanError> {
        self.enter_terminal(tool_call_id.as_ref(), ToolExecutionState::PolicyStopped)?;
        self.mark_blocked_dependents(ToolExecutionState::Skipped);
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
                        let can_cancel_running =
                            self.calls.get(tool_call_id).is_some_and(|planned_call| {
                                planned_call.cancellation == ToolCancellation::ForceTerminable
                                    || (planned_call.cancellation == ToolCancellation::Cooperative
                                        && planned_call.effects.iter().all(|effect| {
                                            matches!(
                                                effect,
                                                ToolEffect::None
                                                    | ToolEffect::ExternalRead
                                                    | ToolEffect::FilesystemRead
                                                    | ToolEffect::Network
                                            )
                                        }))
                            });
                        if can_cancel_running {
                            *state = ToolExecutionState::Cancelled;
                            affected.push(tool_call_id.clone());
                        }
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

    fn enter_pending_terminal(
        &mut self,
        tool_call_id: &str,
        terminal_state: ToolExecutionState,
    ) -> Result<(), ToolExecutionPlanError> {
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
                                    | ToolExecutionState::PolicyStopped
                                    | ToolExecutionState::Expired
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

fn has_state_changing_tool_effects(effects: &BTreeSet<ToolEffect>) -> bool {
    effects.iter().any(|effect| {
        matches!(
            effect,
            ToolEffect::ExternalWrite
                | ToolEffect::FilesystemWrite
                | ToolEffect::Process
                | ToolEffect::Destructive
        )
    })
}

fn depends_on(
    calls: &BTreeMap<String, ToolPlanCall>,
    tool_call_id: &str,
    dependency_id: &str,
) -> bool {
    let Some(call) = calls.get(tool_call_id) else {
        return false;
    };
    call.call
        .depends_on
        .iter()
        .any(|candidate| candidate == dependency_id || depends_on(calls, candidate, dependency_id))
}

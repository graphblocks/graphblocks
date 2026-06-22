use std::collections::BTreeMap;

use crate::outcome::{BlockError, CancelReason};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TaskGroupFailurePolicy {
    Collect,
    FailFast,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SiblingCancellationPolicy {
    KeepRunning,
    CancelSiblingsOnFatal,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TaskGroupPolicy {
    pub minimum_successes: usize,
    pub failure: TaskGroupFailurePolicy,
    pub cancellation: SiblingCancellationPolicy,
    pub deadline_ms: Option<u64>,
}

impl TaskGroupPolicy {
    pub fn new(minimum_successes: usize) -> Self {
        Self {
            minimum_successes,
            failure: TaskGroupFailurePolicy::Collect,
            cancellation: SiblingCancellationPolicy::KeepRunning,
            deadline_ms: None,
        }
    }

    pub fn with_failure(mut self, failure: TaskGroupFailurePolicy) -> Self {
        self.failure = failure;
        self
    }

    pub fn with_cancellation(mut self, cancellation: SiblingCancellationPolicy) -> Self {
        self.cancellation = cancellation;
        self
    }

    pub fn with_deadline_ms(mut self, deadline_ms: u64) -> Self {
        self.deadline_ms = Some(deadline_ms);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum ChildTaskState {
    Pending,
    Running,
    Succeeded,
    Failed(BlockError),
    Cancelled(CancelReason),
}

impl ChildTaskState {
    pub fn is_terminal(&self) -> bool {
        matches!(self, Self::Succeeded | Self::Failed(_) | Self::Cancelled(_))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum TaskGroupFailure {
    ChildFailed { child_id: String, error: BlockError },
    InsufficientSuccesses { successes: usize, required: usize },
    DeadlineExceeded { deadline_ms: u64, now_ms: u64 },
}

#[derive(Clone, Debug, PartialEq)]
pub enum TaskGroupDecision {
    Pending,
    Succeeded {
        successes: usize,
        failures: usize,
    },
    Failed {
        failure: TaskGroupFailure,
        cancel_siblings: Vec<String>,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TaskGroupError {
    InvalidMinimumSuccesses {
        minimum_successes: usize,
        child_count: usize,
    },
    DuplicateChild {
        child_id: String,
    },
    UnknownChild {
        child_id: String,
    },
    ChildAlreadyTerminal {
        child_id: String,
    },
    AlreadyTerminal,
}

#[derive(Clone, Debug, PartialEq)]
pub struct TaskGroupState {
    policy: TaskGroupPolicy,
    children: BTreeMap<String, ChildTaskState>,
    terminal: bool,
}

impl TaskGroupState {
    pub fn new<I, S>(children: I, policy: TaskGroupPolicy) -> Result<Self, TaskGroupError>
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        let mut child_states = BTreeMap::new();
        for child in children {
            let child_id = child.into();
            if child_states
                .insert(child_id.clone(), ChildTaskState::Pending)
                .is_some()
            {
                return Err(TaskGroupError::DuplicateChild { child_id });
            }
        }
        if policy.minimum_successes == 0 || policy.minimum_successes > child_states.len() {
            return Err(TaskGroupError::InvalidMinimumSuccesses {
                minimum_successes: policy.minimum_successes,
                child_count: child_states.len(),
            });
        }
        Ok(Self {
            policy,
            children: child_states,
            terminal: false,
        })
    }

    pub fn policy(&self) -> &TaskGroupPolicy {
        &self.policy
    }

    pub fn child_state(&self, child_id: impl AsRef<str>) -> Option<&ChildTaskState> {
        self.children.get(child_id.as_ref())
    }

    pub fn record_started(
        &mut self,
        child_id: impl AsRef<str>,
    ) -> Result<TaskGroupDecision, TaskGroupError> {
        self.set_child_state(child_id.as_ref(), ChildTaskState::Running)?;
        Ok(TaskGroupDecision::Pending)
    }

    pub fn record_success(
        &mut self,
        child_id: impl AsRef<str>,
    ) -> Result<TaskGroupDecision, TaskGroupError> {
        self.set_child_state(child_id.as_ref(), ChildTaskState::Succeeded)?;
        Ok(self.evaluate_progress())
    }

    pub fn record_failure(
        &mut self,
        child_id: impl AsRef<str>,
        error: BlockError,
    ) -> Result<TaskGroupDecision, TaskGroupError> {
        let child_id = child_id.as_ref();
        self.set_child_state(child_id, ChildTaskState::Failed(error.clone()))?;
        if self.policy.failure == TaskGroupFailurePolicy::FailFast {
            self.terminal = true;
            return Ok(TaskGroupDecision::Failed {
                failure: TaskGroupFailure::ChildFailed {
                    child_id: child_id.to_owned(),
                    error,
                },
                cancel_siblings: self.cancel_siblings(),
            });
        }
        Ok(self.evaluate_progress())
    }

    pub fn record_cancelled(
        &mut self,
        child_id: impl AsRef<str>,
        reason: CancelReason,
    ) -> Result<TaskGroupDecision, TaskGroupError> {
        self.set_child_state(child_id.as_ref(), ChildTaskState::Cancelled(reason))?;
        Ok(self.evaluate_progress())
    }

    pub fn check_deadline(&mut self, now_ms: u64) -> Result<TaskGroupDecision, TaskGroupError> {
        if self.terminal {
            return Err(TaskGroupError::AlreadyTerminal);
        }
        let Some(deadline_ms) = self.policy.deadline_ms else {
            return Ok(TaskGroupDecision::Pending);
        };
        if now_ms < deadline_ms {
            return Ok(TaskGroupDecision::Pending);
        }

        self.terminal = true;
        Ok(TaskGroupDecision::Failed {
            failure: TaskGroupFailure::DeadlineExceeded {
                deadline_ms,
                now_ms,
            },
            cancel_siblings: self.cancel_siblings(),
        })
    }

    fn set_child_state(
        &mut self,
        child_id: &str,
        state: ChildTaskState,
    ) -> Result<(), TaskGroupError> {
        if self.terminal {
            return Err(TaskGroupError::AlreadyTerminal);
        }
        let Some(current) = self.children.get_mut(child_id) else {
            return Err(TaskGroupError::UnknownChild {
                child_id: child_id.to_owned(),
            });
        };
        if current.is_terminal() {
            return Err(TaskGroupError::ChildAlreadyTerminal {
                child_id: child_id.to_owned(),
            });
        }
        *current = state;
        Ok(())
    }

    fn evaluate_progress(&mut self) -> TaskGroupDecision {
        let mut successes = 0;
        let mut failures = 0;
        let mut unfinished = 0;
        for state in self.children.values() {
            match state {
                ChildTaskState::Succeeded => successes += 1,
                ChildTaskState::Failed(_) | ChildTaskState::Cancelled(_) => failures += 1,
                ChildTaskState::Pending | ChildTaskState::Running => unfinished += 1,
            }
        }

        if successes >= self.policy.minimum_successes {
            self.terminal = true;
            return TaskGroupDecision::Succeeded {
                successes,
                failures,
            };
        }
        if successes + unfinished < self.policy.minimum_successes {
            self.terminal = true;
            return TaskGroupDecision::Failed {
                failure: TaskGroupFailure::InsufficientSuccesses {
                    successes,
                    required: self.policy.minimum_successes,
                },
                cancel_siblings: self.cancel_siblings(),
            };
        }
        TaskGroupDecision::Pending
    }

    fn cancel_siblings(&self) -> Vec<String> {
        if self.policy.cancellation == SiblingCancellationPolicy::KeepRunning {
            return Vec::new();
        }
        self.children
            .iter()
            .filter_map(|(child_id, state)| {
                if state.is_terminal() {
                    None
                } else {
                    Some(child_id.clone())
                }
            })
            .collect()
    }
}

use serde_json::Value;

use crate::outcome::{BlockError, CancelReason};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NodeStatus {
    Pending,
    Ready,
    WaitingBudget,
    WaitingLease,
    WaitingApproval,
    Running,
    Completed,
    Failed,
    Cancelled,
    Skipped,
    Paused,
    PolicyStopped,
}

impl NodeStatus {
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Completed
                | Self::Failed
                | Self::Cancelled
                | Self::Skipped
                | Self::Paused
                | Self::PolicyStopped
        )
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LifecycleError {
    AlreadyTerminal { current: NodeStatus },
    OutputAfterTerminal { current: NodeStatus },
    PatchAfterTerminal { current: NodeStatus },
    TerminalTransitionRequiresOutcome { requested: NodeStatus },
}

#[derive(Clone, Debug, PartialEq)]
pub struct NodeOutput {
    pub port: String,
    pub value: Value,
}

#[derive(Clone, Debug, PartialEq)]
pub struct NodeLifecycle {
    status: NodeStatus,
    outputs: Vec<NodeOutput>,
    state_patches: Vec<Value>,
    terminal_error: Option<BlockError>,
    cancel_reason: Option<CancelReason>,
}

impl Default for NodeLifecycle {
    fn default() -> Self {
        Self::new()
    }
}

impl NodeLifecycle {
    pub fn new() -> Self {
        Self {
            status: NodeStatus::Pending,
            outputs: Vec::new(),
            state_patches: Vec::new(),
            terminal_error: None,
            cancel_reason: None,
        }
    }

    pub fn status(&self) -> NodeStatus {
        self.status
    }

    pub fn outputs(&self) -> &[NodeOutput] {
        &self.outputs
    }

    pub fn state_patches(&self) -> &[Value] {
        &self.state_patches
    }

    pub fn terminal_error(&self) -> Option<&BlockError> {
        self.terminal_error.as_ref()
    }

    pub fn cancel_reason(&self) -> Option<&CancelReason> {
        self.cancel_reason.as_ref()
    }

    pub fn transition(&mut self, status: NodeStatus) -> Result<(), LifecycleError> {
        if self.status.is_terminal() {
            return Err(LifecycleError::AlreadyTerminal {
                current: self.status,
            });
        }
        if status.is_terminal() {
            return Err(LifecycleError::TerminalTransitionRequiresOutcome { requested: status });
        }
        self.status = status;
        Ok(())
    }

    pub fn record_output(
        &mut self,
        port: impl Into<String>,
        value: Value,
    ) -> Result<(), LifecycleError> {
        if self.status.is_terminal() {
            return Err(LifecycleError::OutputAfterTerminal {
                current: self.status,
            });
        }
        self.outputs.push(NodeOutput {
            port: port.into(),
            value,
        });
        Ok(())
    }

    pub fn apply_state_patch(&mut self, patch: Value) -> Result<(), LifecycleError> {
        if self.status.is_terminal() {
            return Err(LifecycleError::PatchAfterTerminal {
                current: self.status,
            });
        }
        self.state_patches.push(patch);
        Ok(())
    }

    pub fn complete(&mut self) -> Result<bool, LifecycleError> {
        self.enter_terminal(NodeStatus::Completed)
    }

    pub fn skip(&mut self) -> Result<bool, LifecycleError> {
        self.enter_terminal(NodeStatus::Skipped)
    }

    pub fn pause(&mut self) -> Result<bool, LifecycleError> {
        self.enter_terminal(NodeStatus::Paused)
    }

    pub fn policy_stop(&mut self) -> Result<bool, LifecycleError> {
        self.enter_terminal(NodeStatus::PolicyStopped)
    }

    pub fn fail(&mut self, error: BlockError) -> Result<bool, LifecycleError> {
        if self.status.is_terminal() {
            return Err(LifecycleError::AlreadyTerminal {
                current: self.status,
            });
        }
        self.status = NodeStatus::Failed;
        self.terminal_error = Some(error);
        Ok(true)
    }

    pub fn cancel(&mut self, reason: CancelReason) -> Result<bool, LifecycleError> {
        if self.status == NodeStatus::Cancelled {
            return Ok(false);
        }
        if self.status.is_terminal() {
            return Err(LifecycleError::AlreadyTerminal {
                current: self.status,
            });
        }
        self.status = NodeStatus::Cancelled;
        self.cancel_reason = Some(reason);
        Ok(true)
    }

    fn enter_terminal(&mut self, status: NodeStatus) -> Result<bool, LifecycleError> {
        if self.status.is_terminal() {
            return Err(LifecycleError::AlreadyTerminal {
                current: self.status,
            });
        }
        self.status = status;
        Ok(true)
    }
}

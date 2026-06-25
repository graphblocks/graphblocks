use graphblocks_runtime_core::output_policy::PendingToolCallsDisposition;
use graphblocks_runtime_core::tool_call::ToolCallStatus;
use graphblocks_runtime_core::tool_execution::{
    ToolExecutionCancellationPolicy, ToolExecutionFailurePolicy, ToolExecutionPlan,
    ToolExecutionPlanError, ToolExecutionState, ToolPlanCall,
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SequentialToolQueueError {
    ToolCallNotAdmitted {
        tool_call_id: String,
        status: ToolCallStatus,
    },
    RunningCallMismatch {
        expected: String,
        actual: String,
    },
    Plan(ToolExecutionPlanError),
}

impl From<ToolExecutionPlanError> for SequentialToolQueueError {
    fn from(error: ToolExecutionPlanError) -> Self {
        Self::Plan(error)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SequentialToolQueue {
    plan: ToolExecutionPlan,
    running_call_id: Option<String>,
}

impl SequentialToolQueue {
    pub fn new<I>(
        plan_id: impl Into<String>,
        response_id: impl Into<String>,
        calls: I,
    ) -> Result<Self, SequentialToolQueueError>
    where
        I: IntoIterator<Item = ToolPlanCall>,
    {
        let calls = calls.into_iter().collect::<Vec<_>>();
        for planned_call in &calls {
            if planned_call.call.status != ToolCallStatus::Admitted {
                return Err(SequentialToolQueueError::ToolCallNotAdmitted {
                    tool_call_id: planned_call.call.tool_call_id.clone(),
                    status: planned_call.call.status,
                });
            }
        }

        Ok(Self {
            plan: ToolExecutionPlan::new(plan_id, response_id, calls, 1)?,
            running_call_id: None,
        })
    }

    pub fn with_failure_policy(mut self, policy: ToolExecutionFailurePolicy) -> Self {
        self.plan = self.plan.with_failure_policy(policy);
        self
    }

    pub fn with_cancellation_policy(mut self, policy: ToolExecutionCancellationPolicy) -> Self {
        self.plan = self.plan.with_cancellation_policy(policy);
        self
    }

    pub fn state(&self, tool_call_id: impl AsRef<str>) -> Option<ToolExecutionState> {
        self.plan.state(tool_call_id)
    }

    pub fn running_call_id(&self) -> Option<&str> {
        self.running_call_id.as_deref()
    }

    pub fn start_next_ready(&mut self) -> Result<Option<String>, SequentialToolQueueError> {
        if self.running_call_id.is_some() {
            return Ok(None);
        }

        let Some(tool_call_id) = self.plan.ready_call_ids().into_iter().next() else {
            return Ok(None);
        };
        self.plan.record_started(&tool_call_id)?;
        self.running_call_id = Some(tool_call_id.clone());
        Ok(Some(tool_call_id))
    }

    pub fn record_completed(
        &mut self,
        tool_call_id: impl AsRef<str>,
    ) -> Result<(), SequentialToolQueueError> {
        let tool_call_id = tool_call_id.as_ref();
        self.ensure_running_call(tool_call_id)?;
        self.plan.record_completed(tool_call_id)?;
        self.running_call_id = None;
        Ok(())
    }

    pub fn record_failed(
        &mut self,
        tool_call_id: impl AsRef<str>,
    ) -> Result<(), SequentialToolQueueError> {
        let tool_call_id = tool_call_id.as_ref();
        self.ensure_running_call(tool_call_id)?;
        self.plan.record_failed(tool_call_id)?;
        self.running_call_id = None;
        Ok(())
    }

    pub fn record_denied(
        &mut self,
        tool_call_id: impl AsRef<str>,
    ) -> Result<(), SequentialToolQueueError> {
        self.plan.record_denied(tool_call_id)?;
        Ok(())
    }

    pub fn record_expired(
        &mut self,
        tool_call_id: impl AsRef<str>,
    ) -> Result<(), SequentialToolQueueError> {
        self.plan.record_expired(tool_call_id)?;
        Ok(())
    }

    pub fn record_cancelled(
        &mut self,
        tool_call_id: impl AsRef<str>,
    ) -> Result<(), SequentialToolQueueError> {
        let tool_call_id = tool_call_id.as_ref();
        self.ensure_running_call(tool_call_id)?;
        self.plan.record_cancelled(tool_call_id)?;
        self.running_call_id = None;
        Ok(())
    }

    pub fn apply_policy_stop(
        &mut self,
        pending_tool_calls: PendingToolCallsDisposition,
    ) -> Vec<String> {
        let affected = self.plan.apply_policy_stop(pending_tool_calls);
        if self
            .running_call_id
            .as_ref()
            .is_some_and(|running_call_id| affected.contains(running_call_id))
        {
            self.running_call_id = None;
        }
        affected
    }

    fn ensure_running_call(&self, tool_call_id: &str) -> Result<(), SequentialToolQueueError> {
        if let Some(running_call_id) = &self.running_call_id
            && running_call_id != tool_call_id
        {
            return Err(SequentialToolQueueError::RunningCallMismatch {
                expected: running_call_id.clone(),
                actual: tool_call_id.to_owned(),
            });
        }
        Ok(())
    }
}

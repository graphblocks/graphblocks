use std::collections::{BTreeMap, VecDeque};

use serde_json::{Value, json};

use crate::cancellation::CancellationToken;
use crate::journal::{ExecutionJournal, JournalError, JournalMetadata};
use crate::outcome::{BlockError, Outcome};
use crate::readiness::PortRef;
use crate::retry::{EffectKind, RetryDecision, RetryPolicy, RetryRequest};
use crate::run_store::{InMemoryRunStore, RunStatus, RunStoreError};
use crate::scheduler::{
    LocalScheduler, NodeExecutionState, ScheduledNode, SchedulerError, StartedNode,
};
use crate::timeout::{Deadline, TimeoutPolicy};

pub trait NodeExecutor {
    fn execute(&mut self, node: StartedNode) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError>;
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NodeRetryBoundary {
    policy: RetryPolicy,
    effect: Option<EffectKind>,
    idempotency_key: Option<String>,
}

impl NodeRetryBoundary {
    pub fn new(policy: RetryPolicy) -> Self {
        Self {
            policy,
            effect: None,
            idempotency_key: None,
        }
    }

    pub fn with_effect(mut self, effect: EffectKind) -> Self {
        self.effect = Some(effect);
        self
    }

    pub fn with_idempotency_key(mut self, key: impl Into<String>) -> Self {
        self.idempotency_key = Some(key.into());
        self
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TestRunStatus {
    Succeeded,
    Failed,
    Cancelled,
}

#[derive(Clone, Debug, PartialEq)]
pub struct TestRunResult {
    pub run_id: String,
    pub status: TestRunStatus,
    pub journal: ExecutionJournal,
}

#[derive(Clone, Debug, PartialEq)]
pub enum TestRuntimeError {
    Scheduler(SchedulerError),
    Journal(JournalError),
    RunStore(RunStoreError),
}

impl From<SchedulerError> for TestRuntimeError {
    fn from(error: SchedulerError) -> Self {
        Self::Scheduler(error)
    }
}

impl From<JournalError> for TestRuntimeError {
    fn from(error: JournalError) -> Self {
        Self::Journal(error)
    }
}

impl From<RunStoreError> for TestRuntimeError {
    fn from(error: RunStoreError) -> Self {
        Self::RunStore(error)
    }
}

#[derive(Clone, Debug)]
pub struct InProcessTestRuntime {
    scheduler: LocalScheduler,
    journal: ExecutionJournal,
    retry_boundaries: BTreeMap<String, NodeRetryBoundary>,
    timeout_policies: BTreeMap<String, TimeoutPolicy>,
    node_durations_ms: BTreeMap<String, u64>,
    node_attempt_durations_ms: BTreeMap<String, Vec<u64>>,
    virtual_now_ms: u64,
}

impl InProcessTestRuntime {
    pub fn new<I>(run_id: impl Into<String>, nodes: I) -> Result<Self, SchedulerError>
    where
        I: IntoIterator<Item = ScheduledNode>,
    {
        Ok(Self {
            scheduler: LocalScheduler::new(nodes)?,
            journal: ExecutionJournal::new(run_id),
            retry_boundaries: BTreeMap::new(),
            timeout_policies: BTreeMap::new(),
            node_durations_ms: BTreeMap::new(),
            node_attempt_durations_ms: BTreeMap::new(),
            virtual_now_ms: 0,
        })
    }

    pub fn journal(&self) -> &ExecutionJournal {
        &self.journal
    }

    pub fn with_retry_policy(mut self, node_id: impl Into<String>, policy: RetryPolicy) -> Self {
        self.retry_boundaries
            .insert(node_id.into(), NodeRetryBoundary::new(policy));
        self
    }

    pub fn with_retry_boundary(
        mut self,
        node_id: impl Into<String>,
        boundary: NodeRetryBoundary,
    ) -> Self {
        self.retry_boundaries.insert(node_id.into(), boundary);
        self
    }

    pub fn with_timeout_policy(
        mut self,
        node_id: impl Into<String>,
        policy: TimeoutPolicy,
    ) -> Self {
        self.timeout_policies.insert(node_id.into(), policy);
        self
    }

    pub fn with_node_duration_ms(mut self, node_id: impl Into<String>, duration_ms: u64) -> Self {
        self.node_durations_ms.insert(node_id.into(), duration_ms);
        self
    }

    pub fn with_node_attempt_durations_ms<I>(
        mut self,
        node_id: impl Into<String>,
        durations_ms: I,
    ) -> Self
    where
        I: IntoIterator<Item = u64>,
    {
        self.node_attempt_durations_ms
            .insert(node_id.into(), durations_ms.into_iter().collect());
        self
    }

    pub fn run<E>(&mut self, executor: &mut E) -> Result<TestRunResult, TestRuntimeError>
    where
        E: NodeExecutor,
    {
        self.run_with_cancellation_state(None, executor)
    }

    pub fn run_with_cancellation<E>(
        &mut self,
        cancellation_token: &CancellationToken,
        executor: &mut E,
    ) -> Result<TestRunResult, TestRuntimeError>
    where
        E: NodeExecutor,
    {
        self.run_with_cancellation_state(Some(cancellation_token), executor)
    }

    fn run_with_cancellation_state<E>(
        &mut self,
        cancellation_token: Option<&CancellationToken>,
        executor: &mut E,
    ) -> Result<TestRunResult, TestRuntimeError>
    where
        E: NodeExecutor,
    {
        self.journal
            .append_with_metadata("run_started", JournalMetadata::new(), None)?;
        if let Some(token) = cancellation_token
            && let Some(reason) = token.reason()
        {
            self.journal.append_terminal_with_metadata(
                "run_cancelled",
                JournalMetadata::new(),
                Some(json!({
                    "code": format!("{:?}", reason.code),
                    "message": reason.message,
                    "requestedBy": reason.requested_by,
                    "policyDecisionRef": reason.policy_decision_ref,
                })),
            )?;
            return Ok(TestRunResult {
                run_id: self.journal.run_id().to_owned(),
                status: TestRunStatus::Cancelled,
                journal: self.journal.clone(),
            });
        }
        let mut ready = VecDeque::from(self.scheduler.admit_run()?);

        while let Some(node_id) = ready.pop_front() {
            if let Some(token) = cancellation_token
                && let Some(reason) = token.reason()
            {
                self.journal.append_terminal_with_metadata(
                    "run_cancelled",
                    JournalMetadata::new(),
                    Some(json!({
                        "code": format!("{:?}", reason.code),
                        "message": reason.message,
                        "requestedBy": reason.requested_by,
                        "policyDecisionRef": reason.policy_decision_ref,
                    })),
                )?;
                return Ok(TestRunResult {
                    run_id: self.journal.run_id().to_owned(),
                    status: TestRunStatus::Cancelled,
                    journal: self.journal.clone(),
                });
            }
            let started = self.scheduler.start_node(&node_id)?;
            let mut attempt = 1_u32;
            loop {
                let metadata = JournalMetadata::new()
                    .with_node_id(node_id.clone())
                    .with_attempt_id(format!("attempt-{attempt}"));
                self.journal
                    .append_with_metadata("node_started", metadata.clone(), None)?;

                let started_at_ms = self.virtual_now_ms;
                let execution_result = executor.execute(started.clone());
                let duration_ms = self
                    .node_attempt_durations_ms
                    .get(&node_id)
                    .and_then(|durations| durations.get(attempt.saturating_sub(1) as usize))
                    .copied()
                    .or_else(|| self.node_durations_ms.get(&node_id).copied())
                    .unwrap_or(0);
                self.virtual_now_ms = self.virtual_now_ms.saturating_add(duration_ms);

                if let Some(policy) = self.timeout_policies.get(&node_id)
                    && let Ok(deadline) = Deadline::new(node_id.clone(), started_at_ms, *policy)
                {
                    let decision = deadline.check(self.virtual_now_ms);
                    if self.virtual_now_ms >= deadline.deadline_ms() {
                        let mut error = decision.block_error();
                        error.retryable = true;
                        if let Some(boundary) = self.retry_boundaries.get(&node_id) {
                            let mut request = RetryRequest::new(attempt, error.clone());
                            if let Some(effect) = boundary.effect {
                                request = request.with_effect(effect);
                            }
                            if let Some(idempotency_key) = &boundary.idempotency_key {
                                request = request.with_idempotency_key(idempotency_key.clone());
                            }

                            match boundary.policy.decide(&request) {
                                RetryDecision::Retry { delay_ms } => {
                                    self.journal.append_with_metadata(
                                        "node_retry",
                                        metadata,
                                        Some(json!({
                                            "attempt": attempt,
                                            "code": error.code,
                                            "category": format!("{:?}", error.category),
                                            "message": error.message,
                                            "details": error.details,
                                            "delayMs": delay_ms,
                                        })),
                                    )?;
                                    attempt += 1;
                                    continue;
                                }
                                RetryDecision::Stop { reason } => {
                                    let payload = json!({
                                        "code": error.code,
                                        "category": format!("{:?}", error.category),
                                        "message": error.message,
                                        "details": error.details,
                                        "retryStopReason": reason,
                                    });
                                    self.journal.append_with_metadata(
                                        "node_failed",
                                        metadata.clone(),
                                        Some(payload.clone()),
                                    )?;
                                    self.journal.append_terminal_with_metadata(
                                        "run_failed",
                                        metadata,
                                        Some(payload),
                                    )?;
                                    return Ok(TestRunResult {
                                        run_id: self.journal.run_id().to_owned(),
                                        status: TestRunStatus::Failed,
                                        journal: self.journal.clone(),
                                    });
                                }
                            }
                        }

                        let payload = json!({
                            "code": error.code,
                            "category": format!("{:?}", error.category),
                            "message": error.message,
                            "details": error.details,
                        });
                        self.journal.append_with_metadata(
                            "node_failed",
                            metadata.clone(),
                            Some(payload.clone()),
                        )?;
                        self.journal.append_terminal_with_metadata(
                            "run_failed",
                            metadata,
                            Some(payload),
                        )?;
                        return Ok(TestRunResult {
                            run_id: self.journal.run_id().to_owned(),
                            status: TestRunStatus::Failed,
                            journal: self.journal.clone(),
                        });
                    }
                }

                match execution_result {
                    Ok(outputs) => {
                        let newly_ready = self.scheduler.complete_node(&node_id, outputs)?;
                        self.journal
                            .append_with_metadata("node_completed", metadata, None)?;
                        for node_id in newly_ready {
                            ready.push_back(node_id);
                        }
                        break;
                    }
                    Err(error) => {
                        if let Some(token) = cancellation_token
                            && let Some(reason) = token.reason()
                        {
                            self.journal.append_terminal_with_metadata(
                                "run_cancelled",
                                JournalMetadata::new(),
                                Some(json!({
                                    "code": format!("{:?}", reason.code),
                                    "message": reason.message,
                                    "requestedBy": reason.requested_by,
                                    "policyDecisionRef": reason.policy_decision_ref,
                                })),
                            )?;
                            return Ok(TestRunResult {
                                run_id: self.journal.run_id().to_owned(),
                                status: TestRunStatus::Cancelled,
                                journal: self.journal.clone(),
                            });
                        }
                        if let Some(boundary) = self.retry_boundaries.get(&node_id) {
                            let mut request = RetryRequest::new(attempt, error.clone());
                            if let Some(effect) = boundary.effect {
                                request = request.with_effect(effect);
                            }
                            if let Some(idempotency_key) = &boundary.idempotency_key {
                                request = request.with_idempotency_key(idempotency_key.clone());
                            }

                            match boundary.policy.decide(&request) {
                                RetryDecision::Retry { delay_ms } => {
                                    self.journal.append_with_metadata(
                                        "node_retry",
                                        metadata,
                                        Some(json!({
                                            "attempt": attempt,
                                            "code": error.code,
                                            "category": format!("{:?}", error.category),
                                            "message": error.message,
                                            "delayMs": delay_ms,
                                        })),
                                    )?;
                                    attempt += 1;
                                    continue;
                                }
                                RetryDecision::Stop { reason } => {
                                    let payload = json!({
                                        "code": error.code,
                                        "category": format!("{:?}", error.category),
                                        "message": error.message,
                                        "retryStopReason": reason,
                                    });
                                    self.journal.append_with_metadata(
                                        "node_failed",
                                        metadata.clone(),
                                        Some(payload.clone()),
                                    )?;
                                    self.journal.append_terminal_with_metadata(
                                        "run_failed",
                                        metadata,
                                        Some(payload),
                                    )?;
                                    return Ok(TestRunResult {
                                        run_id: self.journal.run_id().to_owned(),
                                        status: TestRunStatus::Failed,
                                        journal: self.journal.clone(),
                                    });
                                }
                            }
                        }

                        let payload = json!({
                            "code": error.code,
                            "category": format!("{:?}", error.category),
                            "message": error.message,
                        });
                        self.journal.append_with_metadata(
                            "node_failed",
                            metadata.clone(),
                            Some(payload.clone()),
                        )?;
                        self.journal.append_terminal_with_metadata(
                            "run_failed",
                            metadata,
                            Some(payload),
                        )?;
                        return Ok(TestRunResult {
                            run_id: self.journal.run_id().to_owned(),
                            status: TestRunStatus::Failed,
                            journal: self.journal.clone(),
                        });
                    }
                }
            }
        }

        let unfinished = self
            .scheduler
            .node_states()
            .into_iter()
            .filter(|(_, state)| *state != NodeExecutionState::Completed)
            .collect::<Vec<_>>();
        if unfinished.is_empty() {
            self.journal.append_terminal_with_metadata(
                "run_succeeded",
                JournalMetadata::new(),
                None,
            )?;
            return Ok(TestRunResult {
                run_id: self.journal.run_id().to_owned(),
                status: TestRunStatus::Succeeded,
                journal: self.journal.clone(),
            });
        }

        self.journal.append_terminal_with_metadata(
            "run_failed",
            JournalMetadata::new(),
            Some(json!({
                "unfinished": unfinished
                    .into_iter()
                    .map(|(node_id, state)| json!({
                        "node": node_id,
                        "state": format!("{:?}", state),
                    }))
                    .collect::<Vec<_>>(),
            })),
        )?;
        Ok(TestRunResult {
            run_id: self.journal.run_id().to_owned(),
            status: TestRunStatus::Failed,
            journal: self.journal.clone(),
        })
    }

    pub fn run_with_store<E>(
        &mut self,
        store: &mut InMemoryRunStore,
        graph_hash: impl Into<String>,
        inputs: Value,
        executor: &mut E,
    ) -> Result<TestRunResult, TestRuntimeError>
    where
        E: NodeExecutor,
    {
        self.run_with_store_state(store, graph_hash, inputs, None, executor)
    }

    pub fn run_with_store_and_cancellation<E>(
        &mut self,
        store: &mut InMemoryRunStore,
        graph_hash: impl Into<String>,
        inputs: Value,
        cancellation_token: &CancellationToken,
        executor: &mut E,
    ) -> Result<TestRunResult, TestRuntimeError>
    where
        E: NodeExecutor,
    {
        self.run_with_store_state(
            store,
            graph_hash,
            inputs,
            Some(cancellation_token),
            executor,
        )
    }

    fn run_with_store_state<E>(
        &mut self,
        store: &mut InMemoryRunStore,
        graph_hash: impl Into<String>,
        inputs: Value,
        cancellation_token: Option<&CancellationToken>,
        executor: &mut E,
    ) -> Result<TestRunResult, TestRuntimeError>
    where
        E: NodeExecutor,
    {
        let run = store.create_run(graph_hash, inputs);
        store.set_status(&run.run_id, RunStatus::Running)?;
        self.journal = ExecutionJournal::new(run.run_id);
        let result = self.run_with_cancellation_state(cancellation_token, executor)?;
        let status = match result.status {
            TestRunStatus::Succeeded => RunStatus::Completed,
            TestRunStatus::Failed => RunStatus::Failed,
            TestRunStatus::Cancelled => RunStatus::Cancelled,
        };
        store.set_status(&result.run_id, status)?;
        Ok(result)
    }
}

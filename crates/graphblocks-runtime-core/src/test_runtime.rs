use std::collections::{BTreeMap, VecDeque};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::cancellation::CancellationToken;
use crate::journal::{ExecutionJournal, JournalError, JournalMetadata};
use crate::outcome::{BlockError, CancelCode, CancelReason, ErrorCategory, Outcome, SkipReason};
use crate::readiness::PortRef;
use crate::retry::{EffectKind, RetryDecision, RetryPolicy, RetryPolicyError, RetryRequest};
use crate::run_store::{InMemoryRunStore, RunStatus, RunStoreError};
use crate::scheduler::{
    LocalScheduler, NodeExecutionState, ScheduledNode, SchedulerError, StartedNode,
};
use crate::timeout::{Deadline, TimeoutPolicy};

pub trait NodeExecutor {
    fn preflight(&mut self, _node: &StartedNode) -> Option<NodeExecutionPreflight> {
        None
    }

    fn execute(&mut self, node: StartedNode) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError>;

    fn execute_with_context(
        &mut self,
        node: StartedNode,
        _context: &NodeExecutionContext,
    ) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        self.execute(node)
    }
}

pub trait OutcomeNodeExecutor {
    fn preflight(&mut self, _node: &StartedNode) -> Option<NodeExecutionPreflight> {
        None
    }

    fn execute(&mut self, node: StartedNode) -> Outcome<Vec<(PortRef, Outcome<Value>)>>;

    fn execute_with_context(
        &mut self,
        node: StartedNode,
        _context: &NodeExecutionContext,
    ) -> Outcome<Vec<(PortRef, Outcome<Value>)>> {
        self.execute(node)
    }
}

#[derive(Clone, Debug)]
pub struct NodeExecutionCancellationToken {
    token: CancellationToken,
    deadline_started_at: Option<Instant>,
    deadline_duration: Option<Duration>,
    deadline_reason: Option<CancelReason>,
}

impl NodeExecutionCancellationToken {
    fn new(
        parent: Option<&CancellationToken>,
        node_id: &str,
        timeout_policy: Option<TimeoutPolicy>,
    ) -> Self {
        let token = parent.map_or_else(
            || {
                CancellationToken::new(
                    crate::cancellation::CancellationScope::Node,
                    crate::cancellation::CancellationGuarantee::Cooperative,
                )
            },
            |parent| {
                parent.child(
                    crate::cancellation::CancellationScope::Node,
                    crate::cancellation::CancellationGuarantee::Cooperative,
                )
            },
        );
        let deadline_duration = timeout_policy
            .map(TimeoutPolicy::duration_ms)
            .map(Duration::from_millis);
        let deadline_reason = timeout_policy.map(|_| {
            let mut reason = CancelReason::new(CancelCode::Timeout);
            reason.message = Some(format!("node {node_id} exceeded timeout deadline"));
            reason
        });
        Self {
            token,
            deadline_started_at: deadline_duration.map(|_| Instant::now()),
            deadline_duration,
            deadline_reason,
        }
    }

    pub fn scope(&self) -> crate::cancellation::CancellationScope {
        self.token.scope()
    }

    pub fn guarantee(&self) -> crate::cancellation::CancellationGuarantee {
        self.token.guarantee()
    }

    pub fn reason(&self) -> Option<CancelReason> {
        if let Some(reason) = self.token.reason() {
            return Some(reason);
        }
        if let (Some(started_at), Some(duration), Some(reason)) = (
            self.deadline_started_at,
            self.deadline_duration,
            self.deadline_reason.as_ref(),
        ) && started_at.elapsed() >= duration
        {
            self.token.cancel(reason.clone());
        }
        self.token.reason()
    }

    pub fn is_cancelled(&self) -> bool {
        self.reason().is_some()
    }

    fn cancel(&self, reason: CancelReason) -> bool {
        self.token.cancel(reason)
    }

    pub fn check_active(&self) -> Result<(), BlockError> {
        let Some(reason) = self.reason() else {
            return Ok(());
        };
        let (code, category, retryable) = if reason.code == CancelCode::Timeout {
            ("runtime.timeout", ErrorCategory::Timeout, true)
        } else {
            ("runtime.cancelled", ErrorCategory::Cancelled, false)
        };
        Err(BlockError::new(
            code,
            category,
            reason
                .message
                .unwrap_or_else(|| format!("node execution cancelled: {:?}", reason.code)),
            retryable,
        ))
    }
}

#[derive(Debug)]
pub struct NodeExecutionContext {
    run_id: String,
    node_id: String,
    attempt: u32,
    attempt_id: String,
    idempotency_key: Option<String>,
    deadline: Option<Deadline>,
    cancellation_token: NodeExecutionCancellationToken,
}

impl NodeExecutionContext {
    fn new(
        run_id: String,
        node_id: String,
        attempt: u32,
        attempt_id: String,
        idempotency_key: Option<String>,
        deadline: Option<Deadline>,
        cancellation_token: NodeExecutionCancellationToken,
    ) -> Self {
        Self {
            run_id,
            node_id,
            attempt,
            attempt_id,
            idempotency_key,
            deadline,
            cancellation_token,
        }
    }

    pub(crate) fn unbounded() -> Self {
        Self::new(
            "run".to_owned(),
            "node".to_owned(),
            1,
            "attempt-1".to_owned(),
            None,
            None,
            NodeExecutionCancellationToken::new(None, "node", None),
        )
    }

    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    pub fn node_id(&self) -> &str {
        &self.node_id
    }

    pub fn attempt(&self) -> u32 {
        self.attempt
    }

    pub fn attempt_id(&self) -> &str {
        &self.attempt_id
    }

    pub fn idempotency_key(&self) -> Option<&str> {
        self.idempotency_key.as_deref()
    }

    pub fn deadline(&self) -> Option<&Deadline> {
        self.deadline.as_ref()
    }

    pub fn cancellation_token(&self) -> &NodeExecutionCancellationToken {
        &self.cancellation_token
    }
}

pub struct LegacyNodeExecutorAdapter<'a, E: ?Sized> {
    executor: &'a mut E,
}

impl<'a, E: ?Sized> LegacyNodeExecutorAdapter<'a, E> {
    pub fn new(executor: &'a mut E) -> Self {
        Self { executor }
    }
}

impl<E> OutcomeNodeExecutor for LegacyNodeExecutorAdapter<'_, E>
where
    E: NodeExecutor + ?Sized,
{
    fn preflight(&mut self, node: &StartedNode) -> Option<NodeExecutionPreflight> {
        self.executor.preflight(node)
    }

    fn execute(&mut self, node: StartedNode) -> Outcome<Vec<(PortRef, Outcome<Value>)>> {
        match self.executor.execute(node) {
            Ok(outputs) => Outcome::Value(outputs),
            Err(error) => Outcome::Failed(error),
        }
    }

    fn execute_with_context(
        &mut self,
        node: StartedNode,
        context: &NodeExecutionContext,
    ) -> Outcome<Vec<(PortRef, Outcome<Value>)>> {
        match self.executor.execute_with_context(node, context) {
            Ok(outputs) => Outcome::Value(outputs),
            Err(error) => Outcome::Failed(error),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum NodeExecutionPreflight {
    Skipped {
        reason: SkipReason,
        outputs: Vec<(PortRef, Outcome<Value>)>,
    },
    Failed(BlockError),
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OutcomeRunStatus {
    Succeeded,
    Failed,
    Cancelled,
    Rejected,
    Paused,
    Exhausted,
}

impl OutcomeRunStatus {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::Rejected => "rejected",
            Self::Paused => "paused",
            Self::Exhausted => "exhausted",
        }
    }

    pub const fn terminal_kind(self) -> &'static str {
        match self {
            Self::Succeeded => "run_succeeded",
            Self::Failed => "run_failed",
            Self::Cancelled => "run_cancelled",
            Self::Rejected => "run_rejected",
            Self::Paused => "run_paused",
            Self::Exhausted => "run_exhausted",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct OutcomeRunResult {
    pub run_id: String,
    pub status: OutcomeRunStatus,
    pub journal: ExecutionJournal,
}

impl OutcomeRunResult {
    fn into_legacy(self) -> TestRunResult {
        let status = match self.status {
            OutcomeRunStatus::Succeeded => TestRunStatus::Succeeded,
            OutcomeRunStatus::Cancelled => TestRunStatus::Cancelled,
            OutcomeRunStatus::Failed
            | OutcomeRunStatus::Rejected
            | OutcomeRunStatus::Paused
            | OutcomeRunStatus::Exhausted => TestRunStatus::Failed,
        };
        TestRunResult {
            run_id: self.run_id,
            status,
            journal: self.journal,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum TestRuntimeError {
    Scheduler(SchedulerError),
    Journal(JournalError),
    RunStore(RunStoreError),
    RetryPolicy(RetryPolicyError),
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

impl From<RetryPolicyError> for TestRuntimeError {
    fn from(error: RetryPolicyError) -> Self {
        Self::RetryPolicy(error)
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

    pub fn with_initial_value(mut self, port: PortRef, value: Value) -> Self {
        self.scheduler.publish_signal(port, Outcome::Value(value));
        self
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
        self.validate_retry_boundaries()?;
        let mut executor = LegacyNodeExecutorAdapter::new(executor);
        self.run_with_cancellation_state(None, &mut executor)
            .map(OutcomeRunResult::into_legacy)
    }

    pub fn run_with_cancellation<E>(
        &mut self,
        cancellation_token: &CancellationToken,
        executor: &mut E,
    ) -> Result<TestRunResult, TestRuntimeError>
    where
        E: NodeExecutor,
    {
        self.validate_retry_boundaries()?;
        let mut executor = LegacyNodeExecutorAdapter::new(executor);
        self.run_with_cancellation_state(Some(cancellation_token), &mut executor)
            .map(OutcomeRunResult::into_legacy)
    }

    pub fn run_with_outcomes<E>(
        &mut self,
        executor: &mut E,
    ) -> Result<OutcomeRunResult, TestRuntimeError>
    where
        E: OutcomeNodeExecutor,
    {
        self.validate_retry_boundaries()?;
        self.run_with_cancellation_state(None, executor)
    }

    pub fn run_with_outcomes_and_cancellation<E>(
        &mut self,
        cancellation_token: &CancellationToken,
        executor: &mut E,
    ) -> Result<OutcomeRunResult, TestRuntimeError>
    where
        E: OutcomeNodeExecutor,
    {
        self.validate_retry_boundaries()?;
        self.run_with_cancellation_state(Some(cancellation_token), executor)
    }

    fn validate_retry_boundaries(&self) -> Result<(), RetryPolicyError> {
        for boundary in self.retry_boundaries.values() {
            boundary.policy.validate()?;
        }
        Ok(())
    }

    fn run_with_cancellation_state<E>(
        &mut self,
        cancellation_token: Option<&CancellationToken>,
        executor: &mut E,
    ) -> Result<OutcomeRunResult, TestRuntimeError>
    where
        E: OutcomeNodeExecutor,
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
            return Ok(OutcomeRunResult {
                run_id: self.journal.run_id().to_owned(),
                status: OutcomeRunStatus::Cancelled,
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
                return Ok(OutcomeRunResult {
                    run_id: self.journal.run_id().to_owned(),
                    status: OutcomeRunStatus::Cancelled,
                    journal: self.journal.clone(),
                });
            }
            let started = self.scheduler.start_node(&node_id)?;
            if let Some(preflight) = executor.preflight(&started) {
                match preflight {
                    NodeExecutionPreflight::Skipped { reason, outputs } => {
                        let newly_ready = self.scheduler.complete_node(&node_id, outputs)?;
                        self.journal.append_with_metadata(
                            "node_completed",
                            JournalMetadata::new().with_node_id(node_id.clone()),
                            Some(json!({
                                "skipped": true,
                                "reason": reason.code,
                                "message": reason.message,
                            })),
                        )?;
                        for node_id in newly_ready {
                            ready.push_back(node_id);
                        }
                    }
                    NodeExecutionPreflight::Failed(error) => {
                        let metadata = JournalMetadata::new().with_node_id(node_id.clone());
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
                        return Ok(OutcomeRunResult {
                            run_id: self.journal.run_id().to_owned(),
                            status: OutcomeRunStatus::Failed,
                            journal: self.journal.clone(),
                        });
                    }
                }
                continue;
            }
            let mut attempt = 1_u32;
            loop {
                let attempt_id = format!("attempt-{attempt}");
                let metadata = JournalMetadata::new()
                    .with_node_id(node_id.clone())
                    .with_attempt_id(attempt_id.clone());
                let started_payload = self.retry_boundaries.get(&node_id).and_then(|boundary| {
                    boundary.idempotency_key.as_ref().map(|idempotency_key| {
                        json!({
                            "attempt": attempt,
                            "idempotencyKey": idempotency_key,
                        })
                    })
                });
                self.journal.append_with_metadata(
                    "node_started",
                    metadata.clone(),
                    started_payload,
                )?;

                let started_at_ms = self.virtual_now_ms;
                let execution_started_at = Instant::now();
                let timeout_policy = self.timeout_policies.get(&node_id).copied();
                let deadline = timeout_policy
                    .and_then(|policy| Deadline::new(node_id.clone(), started_at_ms, policy).ok());
                let execution_context = NodeExecutionContext::new(
                    self.journal.run_id().to_owned(),
                    node_id.clone(),
                    attempt,
                    attempt_id,
                    self.retry_boundaries
                        .get(&node_id)
                        .and_then(|boundary| boundary.idempotency_key.clone()),
                    deadline.clone(),
                    NodeExecutionCancellationToken::new(
                        cancellation_token,
                        &node_id,
                        timeout_policy,
                    ),
                );
                let execution_result =
                    executor.execute_with_context(started.clone(), &execution_context);
                let measured_duration_ms =
                    u64::try_from(execution_started_at.elapsed().as_millis()).unwrap_or(u64::MAX);
                if let Some(token) = cancellation_token
                    && let Some(reason) = token.reason()
                {
                    self.journal.append_terminal_with_metadata(
                        "run_cancelled",
                        metadata,
                        Some(json!({
                            "code": format!("{:?}", reason.code),
                            "message": reason.message,
                            "requestedBy": reason.requested_by,
                            "policyDecisionRef": reason.policy_decision_ref,
                        })),
                    )?;
                    return Ok(OutcomeRunResult {
                        run_id: self.journal.run_id().to_owned(),
                        status: OutcomeRunStatus::Cancelled,
                        journal: self.journal.clone(),
                    });
                }
                let attempt_cancellation_reason = execution_context.cancellation_token().reason();
                if let Some(reason) = attempt_cancellation_reason.as_ref()
                    && reason.code != CancelCode::Timeout
                {
                    self.journal.append_terminal_with_metadata(
                        "run_cancelled",
                        metadata,
                        Some(json!({
                            "code": format!("{:?}", reason.code),
                            "message": reason.message,
                            "requestedBy": reason.requested_by,
                            "policyDecisionRef": reason.policy_decision_ref,
                        })),
                    )?;
                    return Ok(OutcomeRunResult {
                        run_id: self.journal.run_id().to_owned(),
                        status: OutcomeRunStatus::Cancelled,
                        journal: self.journal.clone(),
                    });
                }
                let duration_ms = self
                    .node_attempt_durations_ms
                    .get(&node_id)
                    .and_then(|durations| durations.get(attempt.saturating_sub(1) as usize))
                    .copied()
                    .or_else(|| self.node_durations_ms.get(&node_id).copied())
                    .unwrap_or(measured_duration_ms);
                self.virtual_now_ms = self.virtual_now_ms.saturating_add(duration_ms);

                if let Some(deadline) = deadline {
                    let wall_clock_timed_out = attempt_cancellation_reason
                        .as_ref()
                        .is_some_and(|reason| reason.code == CancelCode::Timeout);
                    if wall_clock_timed_out || self.virtual_now_ms >= deadline.deadline_ms() {
                        execution_context
                            .cancellation_token()
                            .cancel(CancelReason::new(CancelCode::Timeout));
                        let decision = if wall_clock_timed_out {
                            deadline.check(deadline.deadline_ms())
                        } else {
                            deadline.check(self.virtual_now_ms)
                        };
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
                                    let mut payload = json!({
                                        "attempt": attempt,
                                        "code": error.code,
                                        "category": format!("{:?}", error.category),
                                        "message": error.message,
                                        "details": error.details,
                                        "delayMs": delay_ms,
                                    });
                                    if let Some(idempotency_key) = &boundary.idempotency_key
                                        && let Some(payload) = payload.as_object_mut()
                                    {
                                        payload.insert(
                                            "idempotencyKey".to_owned(),
                                            json!(idempotency_key),
                                        );
                                    }
                                    self.journal.append_with_metadata(
                                        "node_retry",
                                        metadata,
                                        Some(payload),
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
                                    return Ok(OutcomeRunResult {
                                        run_id: self.journal.run_id().to_owned(),
                                        status: OutcomeRunStatus::Failed,
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
                        return Ok(OutcomeRunResult {
                            run_id: self.journal.run_id().to_owned(),
                            status: OutcomeRunStatus::Failed,
                            journal: self.journal.clone(),
                        });
                    }
                }

                let completion_reason = match &execution_result {
                    Outcome::Cancelled(reason) => reason.clone(),
                    _ => CancelReason::new(CancelCode::Superseded),
                };
                execution_context
                    .cancellation_token()
                    .cancel(completion_reason);

                match execution_result {
                    Outcome::Value(outputs) => {
                        let skipped = if !outputs.is_empty()
                            && outputs
                                .iter()
                                .all(|(_, outcome)| matches!(outcome, Outcome::Skipped(_)))
                        {
                            outputs.iter().find_map(|(_, outcome)| match outcome {
                                Outcome::Skipped(reason) => Some(reason.clone()),
                                _ => None,
                            })
                        } else {
                            None
                        };
                        let newly_ready = self.scheduler.complete_node(&node_id, outputs)?;
                        let completed_payload = skipped.map(|reason| {
                            json!({
                                "skipped": true,
                                "reason": reason.code,
                                "message": reason.message,
                            })
                        });
                        self.journal.append_with_metadata(
                            "node_completed",
                            metadata,
                            completed_payload,
                        )?;
                        for node_id in newly_ready {
                            ready.push_back(node_id);
                        }
                        break;
                    }
                    Outcome::Cancelled(reason) => {
                        self.journal.append_terminal_with_metadata(
                            "run_cancelled",
                            metadata,
                            Some(json!({
                                "code": format!("{:?}", reason.code),
                                "message": reason.message,
                                "requestedBy": reason.requested_by,
                                "policyDecisionRef": reason.policy_decision_ref,
                            })),
                        )?;
                        return Ok(OutcomeRunResult {
                            run_id: self.journal.run_id().to_owned(),
                            status: OutcomeRunStatus::Cancelled,
                            journal: self.journal.clone(),
                        });
                    }
                    Outcome::Denied(decision) => {
                        self.journal.append_terminal_with_metadata(
                            "run_rejected",
                            metadata,
                            Some(json!({"decisionId": decision.decision_id})),
                        )?;
                        return Ok(OutcomeRunResult {
                            run_id: self.journal.run_id().to_owned(),
                            status: OutcomeRunStatus::Rejected,
                            journal: self.journal.clone(),
                        });
                    }
                    Outcome::Paused(reason) => {
                        self.journal.append_terminal_with_metadata(
                            "run_paused",
                            metadata,
                            Some(json!({
                                "code": reason.code,
                                "message": reason.message,
                            })),
                        )?;
                        return Ok(OutcomeRunResult {
                            run_id: self.journal.run_id().to_owned(),
                            status: OutcomeRunStatus::Paused,
                            journal: self.journal.clone(),
                        });
                    }
                    Outcome::BudgetExhausted(reason) => {
                        self.journal.append_terminal_with_metadata(
                            "run_exhausted",
                            metadata,
                            Some(json!({
                                "code": reason.code,
                                "message": reason.message,
                            })),
                        )?;
                        return Ok(OutcomeRunResult {
                            run_id: self.journal.run_id().to_owned(),
                            status: OutcomeRunStatus::Exhausted,
                            journal: self.journal.clone(),
                        });
                    }
                    Outcome::Absent => {
                        let payload = json!({
                            "code": "runtime.invalid_absent_node_outcome",
                            "category": "Internal",
                            "message": "node execution returned absent as a run-level outcome",
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
                        return Ok(OutcomeRunResult {
                            run_id: self.journal.run_id().to_owned(),
                            status: OutcomeRunStatus::Failed,
                            journal: self.journal.clone(),
                        });
                    }
                    Outcome::Skipped(reason) => {
                        let payload = json!({
                            "code": "runtime.invalid_skipped_node_outcome",
                            "category": "Internal",
                            "message": "node execution returned skipped as a run-level outcome",
                            "skipCode": reason.code,
                            "skipMessage": reason.message,
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
                        return Ok(OutcomeRunResult {
                            run_id: self.journal.run_id().to_owned(),
                            status: OutcomeRunStatus::Failed,
                            journal: self.journal.clone(),
                        });
                    }
                    Outcome::Failed(error) => {
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
                                    let mut payload = json!({
                                        "attempt": attempt,
                                        "code": error.code,
                                        "category": format!("{:?}", error.category),
                                        "message": error.message,
                                        "delayMs": delay_ms,
                                    });
                                    if let Some(idempotency_key) = &boundary.idempotency_key
                                        && let Some(payload) = payload.as_object_mut()
                                    {
                                        payload.insert(
                                            "idempotencyKey".to_owned(),
                                            json!(idempotency_key),
                                        );
                                    }
                                    self.journal.append_with_metadata(
                                        "node_retry",
                                        metadata,
                                        Some(payload),
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
                                    return Ok(OutcomeRunResult {
                                        run_id: self.journal.run_id().to_owned(),
                                        status: OutcomeRunStatus::Failed,
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
                        return Ok(OutcomeRunResult {
                            run_id: self.journal.run_id().to_owned(),
                            status: OutcomeRunStatus::Failed,
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
            return Ok(OutcomeRunResult {
                run_id: self.journal.run_id().to_owned(),
                status: OutcomeRunStatus::Succeeded,
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
        Ok(OutcomeRunResult {
            run_id: self.journal.run_id().to_owned(),
            status: OutcomeRunStatus::Failed,
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
        self.validate_retry_boundaries()?;
        let run = store.create_run(graph_hash, inputs)?;
        store.set_status(&run.run_id, RunStatus::Running)?;
        self.journal = ExecutionJournal::new(run.run_id);
        let mut executor = LegacyNodeExecutorAdapter::new(executor);
        let result = self
            .run_with_cancellation_state(cancellation_token, &mut executor)?
            .into_legacy();
        let status = match result.status {
            TestRunStatus::Succeeded => RunStatus::Completed,
            TestRunStatus::Failed => RunStatus::Failed,
            TestRunStatus::Cancelled => RunStatus::Cancelled,
        };
        store.set_status(&result.run_id, status)?;
        Ok(result)
    }
}

use std::collections::BTreeMap;
use std::sync::Mutex;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::Value;

use crate::tool_schema::{ToolSchemaRegistry, ToolSchemaValidationError};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AsyncCallbackIngestionLimits {
    pub max_payload_bytes: usize,
}

impl AsyncCallbackIngestionLimits {
    pub const DEFAULT_MAX_PAYLOAD_BYTES: usize = 262_144;
}

impl Default for AsyncCallbackIngestionLimits {
    fn default() -> Self {
        Self {
            max_payload_bytes: Self::DEFAULT_MAX_PAYLOAD_BYTES,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AsyncOperationKind {
    Tool,
    SandboxTask,
    CiJob,
    BrowserTask,
    WorkspaceTrial,
    ExternalProviderJob,
    DocumentJob,
    ResearchTask,
    Custom,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AsyncOperationState {
    Created,
    Submitted,
    WaitingCallback,
    CallbackReceived,
    Polling,
    Resuming,
    Completed,
    Failed,
    Cancelled,
    Expired,
}

#[derive(Clone, Debug, PartialEq)]
pub struct AsyncOperation {
    pub operation_id: String,
    pub run_id: String,
    pub node_id: String,
    pub attempt_id: String,
    pub kind: AsyncOperationKind,
    pub provider_operation_id: Option<String>,
    pub state: AsyncOperationState,
    pub resume_token_hash: String,
    pub idempotency_key: String,
    pub expected_schema: String,
    pub created_at_unix_ms: u64,
    pub submitted_at_unix_ms: Option<u64>,
    pub expires_at_unix_ms: Option<u64>,
    pub completed_at_unix_ms: Option<u64>,
}

impl AsyncOperation {
    pub fn new(
        operation_id: impl Into<String>,
        run_id: impl Into<String>,
        node_id: impl Into<String>,
        attempt_id: impl Into<String>,
        kind: AsyncOperationKind,
        resume_token_hash: impl Into<String>,
        idempotency_key: impl Into<String>,
        expected_schema: impl Into<String>,
        created_at_unix_ms: u64,
    ) -> Self {
        Self {
            operation_id: operation_id.into(),
            run_id: run_id.into(),
            node_id: node_id.into(),
            attempt_id: attempt_id.into(),
            kind,
            provider_operation_id: None,
            state: AsyncOperationState::Created,
            resume_token_hash: resume_token_hash.into(),
            idempotency_key: idempotency_key.into(),
            expected_schema: expected_schema.into(),
            created_at_unix_ms,
            submitted_at_unix_ms: None,
            expires_at_unix_ms: None,
            completed_at_unix_ms: None,
        }
    }

    pub fn submitted(
        mut self,
        provider_operation_id: impl Into<String>,
        submitted_at_unix_ms: u64,
    ) -> Self {
        self.provider_operation_id = Some(provider_operation_id.into());
        self.submitted_at_unix_ms = Some(submitted_at_unix_ms);
        self.state = AsyncOperationState::Submitted;
        self
    }

    pub fn waiting_callback(mut self, expires_at_unix_ms: u64) -> Self {
        self.expires_at_unix_ms = Some(expires_at_unix_ms);
        self.state = AsyncOperationState::WaitingCallback;
        self
    }

    pub fn validate(&self) -> Result<(), AsyncOperationError> {
        for (field, value) in [
            ("operation_id", &self.operation_id),
            ("run_id", &self.run_id),
            ("node_id", &self.node_id),
            ("attempt_id", &self.attempt_id),
            ("resume_token_hash", &self.resume_token_hash),
            ("idempotency_key", &self.idempotency_key),
            ("expected_schema", &self.expected_schema),
        ] {
            if value.is_empty() {
                return Err(AsyncOperationError::EmptyField {
                    field: field.to_owned(),
                });
            }
        }

        if self.state == AsyncOperationState::WaitingCallback && self.expires_at_unix_ms.is_none() {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "waiting callback operations require an expiration".to_owned(),
            });
        }

        if let Some(expires_at_unix_ms) = self.expires_at_unix_ms
            && expires_at_unix_ms <= self.created_at_unix_ms
        {
            return Err(AsyncOperationError::InvalidExpiration {
                operation_id: self.operation_id.clone(),
                created_at_unix_ms: self.created_at_unix_ms,
                expires_at_unix_ms,
            });
        }

        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct AsyncCallbackSubmission {
    pub callback_id: String,
    pub operation_id: String,
    pub run_id: String,
    pub node_id: String,
    pub attempt_id: String,
    pub provider_operation_id: Option<String>,
    pub idempotency_key: String,
    pub payload: Value,
    pub received_at_unix_ms: u64,
    pub verified_by: String,
    pub policy_snapshot_id: String,
}

impl AsyncCallbackSubmission {
    pub fn new(
        callback_id: impl Into<String>,
        operation_id: impl Into<String>,
        run_id: impl Into<String>,
        node_id: impl Into<String>,
        attempt_id: impl Into<String>,
        idempotency_key: impl Into<String>,
        payload: Value,
        received_at_unix_ms: u64,
        verified_by: impl Into<String>,
        policy_snapshot_id: impl Into<String>,
    ) -> Self {
        Self {
            callback_id: callback_id.into(),
            operation_id: operation_id.into(),
            run_id: run_id.into(),
            node_id: node_id.into(),
            attempt_id: attempt_id.into(),
            provider_operation_id: None,
            idempotency_key: idempotency_key.into(),
            payload,
            received_at_unix_ms,
            verified_by: verified_by.into(),
            policy_snapshot_id: policy_snapshot_id.into(),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ExternalCallbackReceived {
    pub callback_id: String,
    pub operation_id: String,
    pub run_id: String,
    pub node_id: String,
    pub attempt_id: String,
    pub provider_operation_id: Option<String>,
    pub idempotency_key: String,
    pub payload: Value,
    pub payload_digest: String,
    pub received_at_unix_ms: u64,
    pub verified_by: String,
    pub policy_snapshot_id: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AsyncCallbackResumeDecision {
    Resume,
    PauseBudget {
        reason: String,
    },
    DenyPolicy {
        decision_id: String,
        reason: String,
    },
    PauseReleaseIncompatible {
        required_release_id: String,
        available_release_id: String,
    },
}

impl ExternalCallbackReceived {
    pub fn compute_payload_digest(&self) -> String {
        canonical_hash(&self.payload)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct AcceptedCallback {
    pub receipt: ExternalCallbackReceived,
    pub duplicate: bool,
    pub should_resume: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub enum AsyncOperationEvent {
    StateChanged {
        operation_id: String,
        from: AsyncOperationState,
        to: AsyncOperationState,
        occurred_at_unix_ms: u64,
    },
    ExternalCallbackReceived {
        receipt: ExternalCallbackReceived,
    },
    CallbackResumePaused {
        operation_id: String,
        reason: String,
        occurred_at_unix_ms: u64,
    },
    CallbackResumeDenied {
        operation_id: String,
        decision_id: String,
        reason: String,
        occurred_at_unix_ms: u64,
    },
    LateExternalCallbackReceived {
        receipt: ExternalCallbackReceived,
        terminal_state: AsyncOperationState,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub enum AsyncOperationError {
    EmptyField {
        field: String,
    },
    InvalidOperation {
        operation_id: String,
        reason: String,
    },
    InvalidExpiration {
        operation_id: String,
        created_at_unix_ms: u64,
        expires_at_unix_ms: u64,
    },
    DuplicateOperation {
        operation_id: String,
    },
    OperationNotFound {
        operation_id: String,
    },
    OperationIdentityMismatch {
        operation_id: String,
        field: String,
        expected: String,
        actual: String,
    },
    OperationNotWaitingCallback {
        operation_id: String,
        state: AsyncOperationState,
    },
    OperationTerminal {
        operation_id: String,
        state: AsyncOperationState,
    },
    StaleAttempt {
        operation_id: String,
        expected_attempt_id: String,
        actual_attempt_id: String,
    },
    CallbackSchemaMissing {
        schema_id: String,
    },
    CallbackSchemaInvalid {
        operation_id: String,
        schema_id: String,
        path: String,
        expected: String,
    },
    RequiredCallbackPropertyMissing {
        operation_id: String,
        schema_id: String,
        path: String,
        property: String,
    },
    CallbackPayloadTooLarge {
        operation_id: String,
        max_payload_bytes: usize,
        actual_payload_bytes: usize,
    },
}

#[derive(Debug, Default)]
pub struct AsyncOperationStore {
    inner: Mutex<AsyncOperationStoreInner>,
}

#[derive(Debug, Default)]
struct AsyncOperationStoreInner {
    operations: BTreeMap<String, AsyncOperation>,
    receipts_by_idempotency_key: BTreeMap<String, ExternalCallbackReceived>,
    events_by_operation: BTreeMap<String, Vec<AsyncOperationEvent>>,
}

impl AsyncOperationStore {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&self, operation: AsyncOperation) -> Result<(), AsyncOperationError> {
        operation.validate()?;

        let mut inner = self
            .inner
            .lock()
            .expect("async operation store lock poisoned");
        if inner.operations.contains_key(&operation.operation_id) {
            return Err(AsyncOperationError::DuplicateOperation {
                operation_id: operation.operation_id,
            });
        }

        let mut events = Vec::new();
        if let Some(submitted_at_unix_ms) = operation.submitted_at_unix_ms {
            events.push(AsyncOperationEvent::StateChanged {
                operation_id: operation.operation_id.clone(),
                from: AsyncOperationState::Created,
                to: AsyncOperationState::Submitted,
                occurred_at_unix_ms: submitted_at_unix_ms,
            });
        }
        if operation.state == AsyncOperationState::WaitingCallback {
            events.push(AsyncOperationEvent::StateChanged {
                operation_id: operation.operation_id.clone(),
                from: AsyncOperationState::Submitted,
                to: AsyncOperationState::WaitingCallback,
                occurred_at_unix_ms: operation
                    .submitted_at_unix_ms
                    .unwrap_or(operation.created_at_unix_ms),
            });
        }

        inner
            .events_by_operation
            .insert(operation.operation_id.clone(), events);
        inner
            .operations
            .insert(operation.operation_id.clone(), operation);
        Ok(())
    }

    pub fn accept_callback(
        &self,
        submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        self.accept_callback_with_resume_decision(
            submission,
            registry,
            AsyncCallbackResumeDecision::Resume,
        )
    }

    pub fn accept_callback_with_resume_decision(
        &self,
        submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
        resume_decision: AsyncCallbackResumeDecision,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        self.accept_callback_with_limits_and_resume_decision(
            submission,
            registry,
            AsyncCallbackIngestionLimits::default(),
            resume_decision,
        )
    }

    pub fn accept_callback_with_limits(
        &self,
        submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
        limits: AsyncCallbackIngestionLimits,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        self.accept_callback_with_limits_and_resume_decision(
            submission,
            registry,
            limits,
            AsyncCallbackResumeDecision::Resume,
        )
    }

    fn accept_callback_with_limits_and_resume_decision(
        &self,
        submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
        limits: AsyncCallbackIngestionLimits,
        resume_decision: AsyncCallbackResumeDecision,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        for (field, value) in [
            ("callback_id", &submission.callback_id),
            ("operation_id", &submission.operation_id),
            ("run_id", &submission.run_id),
            ("node_id", &submission.node_id),
            ("attempt_id", &submission.attempt_id),
            ("idempotency_key", &submission.idempotency_key),
            ("verified_by", &submission.verified_by),
            ("policy_snapshot_id", &submission.policy_snapshot_id),
        ] {
            if value.is_empty() {
                return Err(AsyncOperationError::EmptyField {
                    field: field.to_owned(),
                });
            }
        }
        match &resume_decision {
            AsyncCallbackResumeDecision::Resume => {}
            AsyncCallbackResumeDecision::PauseBudget { reason } => {
                if reason.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: "resume_pause_reason".to_owned(),
                    });
                }
            }
            AsyncCallbackResumeDecision::DenyPolicy {
                decision_id,
                reason,
            } => {
                if decision_id.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: "resume_policy_decision_id".to_owned(),
                    });
                }
                if reason.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: "resume_policy_reason".to_owned(),
                    });
                }
            }
            AsyncCallbackResumeDecision::PauseReleaseIncompatible {
                required_release_id,
                available_release_id,
            } => {
                if required_release_id.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: "required_release_id".to_owned(),
                    });
                }
                if available_release_id.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: "available_release_id".to_owned(),
                    });
                }
            }
        }

        let payload_size = callback_payload_size_bytes(&submission.payload);
        if payload_size > limits.max_payload_bytes {
            return Err(AsyncOperationError::CallbackPayloadTooLarge {
                operation_id: submission.operation_id,
                max_payload_bytes: limits.max_payload_bytes,
                actual_payload_bytes: payload_size,
            });
        }

        self.accept_validated_callback_with_resume_decision(submission, registry, resume_decision)
    }

    fn accept_validated_callback_with_resume_decision(
        &self,
        submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
        resume_decision: AsyncCallbackResumeDecision,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        let mut inner = self
            .inner
            .lock()
            .expect("async operation store lock poisoned");
        if let Some(receipt) = inner
            .receipts_by_idempotency_key
            .get(&submission.idempotency_key)
        {
            return Ok(AcceptedCallback {
                receipt: receipt.clone(),
                duplicate: true,
                should_resume: false,
            });
        }

        let operation = inner
            .operations
            .get(&submission.operation_id)
            .cloned()
            .ok_or_else(|| AsyncOperationError::OperationNotFound {
                operation_id: submission.operation_id.clone(),
            })?;

        if operation.run_id != submission.run_id {
            return Err(AsyncOperationError::OperationIdentityMismatch {
                operation_id: operation.operation_id,
                field: "run_id".to_owned(),
                expected: operation.run_id,
                actual: submission.run_id,
            });
        }
        if operation.node_id != submission.node_id {
            return Err(AsyncOperationError::OperationIdentityMismatch {
                operation_id: operation.operation_id,
                field: "node_id".to_owned(),
                expected: operation.node_id,
                actual: submission.node_id,
            });
        }
        if operation.attempt_id != submission.attempt_id {
            return Err(AsyncOperationError::StaleAttempt {
                operation_id: operation.operation_id,
                expected_attempt_id: operation.attempt_id,
                actual_attempt_id: submission.attempt_id,
            });
        }

        if let Err(error) = registry.validate(&operation.expected_schema, &submission.payload) {
            return Err(match error {
                ToolSchemaValidationError::SchemaMissing { schema_id } => {
                    AsyncOperationError::CallbackSchemaMissing { schema_id }
                }
                ToolSchemaValidationError::TypeMismatch {
                    schema_id,
                    path,
                    expected,
                } => AsyncOperationError::CallbackSchemaInvalid {
                    operation_id: operation.operation_id,
                    schema_id,
                    path,
                    expected,
                },
                ToolSchemaValidationError::RequiredPropertyMissing {
                    schema_id,
                    path,
                    property,
                } => AsyncOperationError::CallbackSchemaInvalid {
                    operation_id: operation.operation_id,
                    schema_id,
                    path,
                    expected: format!("required property {property}"),
                },
            });
        }

        let operation_id = operation.operation_id.clone();
        let operation_state = operation.state;
        let receipt = ExternalCallbackReceived {
            callback_id: submission.callback_id,
            operation_id: submission.operation_id.clone(),
            run_id: submission.run_id,
            node_id: submission.node_id,
            attempt_id: submission.attempt_id,
            provider_operation_id: submission.provider_operation_id,
            idempotency_key: submission.idempotency_key.clone(),
            payload_digest: canonical_hash(&submission.payload),
            payload: submission.payload,
            received_at_unix_ms: submission.received_at_unix_ms,
            verified_by: submission.verified_by,
            policy_snapshot_id: submission.policy_snapshot_id,
        };

        if matches!(
            operation_state,
            AsyncOperationState::Expired | AsyncOperationState::Cancelled
        ) {
            inner
                .events_by_operation
                .entry(submission.operation_id.clone())
                .or_default()
                .push(AsyncOperationEvent::LateExternalCallbackReceived {
                    receipt: receipt.clone(),
                    terminal_state: operation_state,
                });
            inner
                .receipts_by_idempotency_key
                .insert(submission.idempotency_key, receipt.clone());
            return Ok(AcceptedCallback {
                receipt,
                duplicate: false,
                should_resume: false,
            });
        }

        if operation_state != AsyncOperationState::WaitingCallback {
            return Err(AsyncOperationError::OperationNotWaitingCallback {
                operation_id,
                state: operation_state,
            });
        }

        inner
            .events_by_operation
            .entry(submission.operation_id.clone())
            .or_default()
            .push(AsyncOperationEvent::ExternalCallbackReceived {
                receipt: receipt.clone(),
            });

        let operation = inner
            .operations
            .get_mut(&submission.operation_id)
            .expect("operation exists after validation");
        let from = operation.state;
        operation.state = AsyncOperationState::CallbackReceived;
        inner
            .events_by_operation
            .entry(submission.operation_id.clone())
            .or_default()
            .push(AsyncOperationEvent::StateChanged {
                operation_id: submission.operation_id.clone(),
                from,
                to: AsyncOperationState::CallbackReceived,
                occurred_at_unix_ms: submission.received_at_unix_ms,
            });
        let should_resume = match resume_decision {
            AsyncCallbackResumeDecision::Resume => true,
            AsyncCallbackResumeDecision::PauseBudget { reason } => {
                inner
                    .events_by_operation
                    .entry(submission.operation_id.clone())
                    .or_default()
                    .push(AsyncOperationEvent::CallbackResumePaused {
                        operation_id: submission.operation_id,
                        reason,
                        occurred_at_unix_ms: submission.received_at_unix_ms,
                    });
                false
            }
            AsyncCallbackResumeDecision::DenyPolicy {
                decision_id,
                reason,
            } => {
                inner
                    .events_by_operation
                    .entry(submission.operation_id.clone())
                    .or_default()
                    .push(AsyncOperationEvent::CallbackResumeDenied {
                        operation_id: submission.operation_id,
                        decision_id,
                        reason,
                        occurred_at_unix_ms: submission.received_at_unix_ms,
                    });
                false
            }
            AsyncCallbackResumeDecision::PauseReleaseIncompatible {
                required_release_id,
                available_release_id,
            } => {
                inner
                    .events_by_operation
                    .entry(submission.operation_id.clone())
                    .or_default()
                    .push(AsyncOperationEvent::CallbackResumePaused {
                        operation_id: submission.operation_id,
                        reason: format!(
                            "release incompatible: required {required_release_id}, available {available_release_id}"
                        ),
                        occurred_at_unix_ms: submission.received_at_unix_ms,
                    });
                false
            }
        };
        inner
            .receipts_by_idempotency_key
            .insert(submission.idempotency_key, receipt.clone());

        Ok(AcceptedCallback {
            receipt,
            duplicate: false,
            should_resume,
        })
    }

    pub fn cancel_operation(
        &self,
        operation_id: &str,
        cancelled_at_unix_ms: u64,
    ) -> Result<(), AsyncOperationError> {
        let mut inner = self
            .inner
            .lock()
            .expect("async operation store lock poisoned");
        let operation = inner.operations.get_mut(operation_id).ok_or_else(|| {
            AsyncOperationError::OperationNotFound {
                operation_id: operation_id.to_owned(),
            }
        })?;

        if matches!(
            operation.state,
            AsyncOperationState::CallbackReceived
                | AsyncOperationState::Completed
                | AsyncOperationState::Failed
                | AsyncOperationState::Cancelled
                | AsyncOperationState::Expired
        ) {
            return Err(AsyncOperationError::OperationTerminal {
                operation_id: operation_id.to_owned(),
                state: operation.state,
            });
        }

        let from = operation.state;
        operation.state = AsyncOperationState::Cancelled;
        operation.completed_at_unix_ms = Some(cancelled_at_unix_ms);
        inner
            .events_by_operation
            .entry(operation_id.to_owned())
            .or_default()
            .push(AsyncOperationEvent::StateChanged {
                operation_id: operation_id.to_owned(),
                from,
                to: AsyncOperationState::Cancelled,
                occurred_at_unix_ms: cancelled_at_unix_ms,
            });
        Ok(())
    }

    pub fn expire_operation(
        &self,
        operation_id: &str,
        expired_at_unix_ms: u64,
    ) -> Result<(), AsyncOperationError> {
        let mut inner = self
            .inner
            .lock()
            .expect("async operation store lock poisoned");
        let operation = inner.operations.get_mut(operation_id).ok_or_else(|| {
            AsyncOperationError::OperationNotFound {
                operation_id: operation_id.to_owned(),
            }
        })?;

        if matches!(
            operation.state,
            AsyncOperationState::CallbackReceived
                | AsyncOperationState::Completed
                | AsyncOperationState::Failed
                | AsyncOperationState::Cancelled
                | AsyncOperationState::Expired
        ) {
            return Err(AsyncOperationError::OperationTerminal {
                operation_id: operation_id.to_owned(),
                state: operation.state,
            });
        }

        let from = operation.state;
        operation.state = AsyncOperationState::Expired;
        operation.completed_at_unix_ms = Some(expired_at_unix_ms);
        inner
            .events_by_operation
            .entry(operation_id.to_owned())
            .or_default()
            .push(AsyncOperationEvent::StateChanged {
                operation_id: operation_id.to_owned(),
                from,
                to: AsyncOperationState::Expired,
                occurred_at_unix_ms: expired_at_unix_ms,
            });
        Ok(())
    }

    pub fn events_for_operation(&self, operation_id: &str) -> Vec<AsyncOperationEvent> {
        let inner = self
            .inner
            .lock()
            .expect("async operation store lock poisoned");
        inner
            .events_by_operation
            .get(operation_id)
            .cloned()
            .unwrap_or_default()
    }

    pub fn operation_state(&self, operation_id: &str) -> Option<AsyncOperationState> {
        let inner = self
            .inner
            .lock()
            .expect("async operation store lock poisoned");
        inner
            .operations
            .get(operation_id)
            .map(|operation| operation.state)
    }
}

fn callback_payload_size_bytes(payload: &Value) -> usize {
    graphblocks_compiler::canonical::canonical_json(payload).len()
}

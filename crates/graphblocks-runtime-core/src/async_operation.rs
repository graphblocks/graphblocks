use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Mutex;

use graphblocks_compiler::canonical::canonical_hash;
use hmac::{Hmac, Mac};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use sha2::Sha256;

use crate::tool_schema::{ToolSchemaRegistry, ToolSchemaValidationError};

type HmacSha256 = Hmac<Sha256>;

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
    pub expected_callback_payload_bytes: Option<usize>,
    pub resume_policy_reevaluation: bool,
    pub callback_attempt_fencing: bool,
    pub resume_ownership_fence: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AsyncOperationConfigurationDiagnostic {
    pub code: &'static str,
    pub field: &'static str,
    pub message: String,
}

impl AsyncOperationConfigurationDiagnostic {
    pub fn for_operation(operation: &AsyncOperation) -> Vec<Self> {
        Self::for_operation_with_limits(operation, AsyncCallbackIngestionLimits::default())
    }

    pub fn for_operation_with_limits(
        operation: &AsyncOperation,
        limits: AsyncCallbackIngestionLimits,
    ) -> Vec<Self> {
        let mut diagnostics = Vec::new();
        if operation.state == AsyncOperationState::WaitingCallback
            && operation.expires_at_unix_ms.is_none()
        {
            diagnostics.push(Self {
                code: "GB6001",
                field: "expires_at_unix_ms",
                message: format!(
                    "async operation {} waits for callback without a timeout",
                    operation.operation_id
                ),
            });
        }
        if operation.idempotency_key.trim().is_empty() {
            diagnostics.push(Self {
                code: "GB6003",
                field: "idempotency_key",
                message: format!(
                    "async operation {} does not define an idempotency key",
                    operation.operation_id
                ),
            });
        }
        if operation.expected_schema.trim().is_empty() {
            diagnostics.push(Self {
                code: "GB6007",
                field: "expected_schema",
                message: format!(
                    "async operation {} callback has no expected schema",
                    operation.operation_id
                ),
            });
        }
        if let Some(expected_callback_payload_bytes) = operation.expected_callback_payload_bytes
            && expected_callback_payload_bytes > limits.max_payload_bytes
        {
            diagnostics.push(Self {
                code: "GB6010",
                field: "expected_callback_payload_bytes",
                message: format!(
                    "async operation {} may inline callback payloads of {expected_callback_payload_bytes} bytes above the configured {} byte limit",
                    operation.operation_id, limits.max_payload_bytes
                ),
            });
        }
        if operation.state == AsyncOperationState::WaitingCallback
            && !operation.resume_policy_reevaluation
        {
            diagnostics.push(Self {
                code: "GB6008",
                field: "resume_policy_reevaluation",
                message: format!(
                    "async operation {} can resume from callback without re-evaluating policy, budget, and release compatibility",
                    operation.operation_id
                ),
            });
        }
        if operation.state == AsyncOperationState::WaitingCallback
            && !operation.callback_attempt_fencing
        {
            diagnostics.push(Self {
                code: "GB6015",
                field: "callback_attempt_fencing",
                message: format!(
                    "async operation {} can accept stale callbacks without attempt fencing",
                    operation.operation_id
                ),
            });
        }
        if operation.state == AsyncOperationState::WaitingCallback
            && !operation.resume_ownership_fence
        {
            diagnostics.push(Self {
                code: "GB6016",
                field: "resume_ownership_fence",
                message: format!(
                    "async operation {} can resume without run ownership lease or fencing protection",
                    operation.operation_id
                ),
            });
        }
        diagnostics
    }
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
            expected_callback_payload_bytes: None,
            resume_policy_reevaluation: true,
            callback_attempt_fencing: true,
            resume_ownership_fence: true,
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

    pub fn with_expected_callback_payload_bytes(
        mut self,
        expected_callback_payload_bytes: usize,
    ) -> Self {
        self.expected_callback_payload_bytes = Some(expected_callback_payload_bytes);
        self
    }

    pub fn without_resume_policy_reevaluation(mut self) -> Self {
        self.resume_policy_reevaluation = false;
        self
    }

    pub fn without_callback_attempt_fencing(mut self) -> Self {
        self.callback_attempt_fencing = false;
        self
    }

    pub fn without_resume_ownership_fence(mut self) -> Self {
        self.resume_ownership_fence = false;
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
pub struct CallbackEndpointRef {
    pub endpoint_id: String,
    pub url: String,
    pub accepted_schema: String,
    pub auth: CallbackEndpointAuth,
    pub expires_at_unix_ms: Option<u64>,
}

impl CallbackEndpointRef {
    pub fn new(
        endpoint_id: impl Into<String>,
        url: impl Into<String>,
        accepted_schema: impl Into<String>,
        auth: CallbackEndpointAuth,
    ) -> Result<Self, AsyncOperationError> {
        let endpoint = Self {
            endpoint_id: endpoint_id.into(),
            url: url.into(),
            accepted_schema: accepted_schema.into(),
            auth,
            expires_at_unix_ms: None,
        };
        endpoint.validate()?;
        Ok(endpoint)
    }

    pub fn with_expiration(mut self, expires_at_unix_ms: u64) -> Self {
        self.expires_at_unix_ms = Some(expires_at_unix_ms);
        self
    }

    pub fn validate(&self) -> Result<(), AsyncOperationError> {
        for (field, value) in [
            ("endpoint_id", &self.endpoint_id),
            ("url", &self.url),
            ("accepted_schema", &self.accepted_schema),
        ] {
            if value.trim().is_empty() {
                return Err(AsyncOperationError::EmptyField {
                    field: field.to_owned(),
                });
            }
        }
        self.auth.validate()
    }

    pub fn sign_callback_headers(
        &self,
        timestamp_unix_ms: u64,
        payload: &Value,
    ) -> Result<BTreeMap<String, String>, AsyncOperationError> {
        self.auth.sign_headers(timestamp_unix_ms, payload)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn authenticate_and_build_submission(
        &self,
        callback_id: impl Into<String>,
        operation_id: impl Into<String>,
        run_id: impl Into<String>,
        node_id: impl Into<String>,
        attempt_id: impl Into<String>,
        idempotency_key: impl Into<String>,
        payload: Value,
        received_at_unix_ms: u64,
        policy_snapshot_id: impl Into<String>,
        headers: &BTreeMap<String, String>,
    ) -> Result<AsyncCallbackSubmission, AsyncOperationError> {
        if self
            .expires_at_unix_ms
            .is_some_and(|expires_at_unix_ms| received_at_unix_ms > expires_at_unix_ms)
        {
            return Err(AsyncOperationError::CallbackAuthenticationFailed {
                endpoint_id: self.endpoint_id.clone(),
                reason: "endpoint_expired".to_owned(),
            });
        }
        let verified_by =
            self.auth
                .verify(&self.endpoint_id, headers, &payload, received_at_unix_ms)?;
        Ok(AsyncCallbackSubmission::new(
            callback_id,
            operation_id,
            run_id,
            node_id,
            attempt_id,
            idempotency_key,
            payload,
            received_at_unix_ms,
            verified_by,
            policy_snapshot_id,
        ))
    }

    #[allow(clippy::too_many_arguments)]
    pub fn authenticate_ed25519_and_build_submission(
        &self,
        callback_id: impl Into<String>,
        operation_id: impl Into<String>,
        run_id: impl Into<String>,
        node_id: impl Into<String>,
        attempt_id: impl Into<String>,
        idempotency_key: impl Into<String>,
        payload: Value,
        received_at_unix_ms: u64,
        policy_snapshot_id: impl Into<String>,
        headers: &BTreeMap<String, String>,
        verifier: impl FnOnce(&str, &str, &str) -> bool,
    ) -> Result<AsyncCallbackSubmission, AsyncOperationError> {
        if self
            .expires_at_unix_ms
            .is_some_and(|expires_at_unix_ms| received_at_unix_ms > expires_at_unix_ms)
        {
            return Err(AsyncOperationError::CallbackAuthenticationFailed {
                endpoint_id: self.endpoint_id.clone(),
                reason: "endpoint_expired".to_owned(),
            });
        }
        let verified_by = self.auth.verify_ed25519(
            &self.endpoint_id,
            headers,
            &payload,
            received_at_unix_ms,
            verifier,
        )?;
        Ok(AsyncCallbackSubmission::new(
            callback_id,
            operation_id,
            run_id,
            node_id,
            attempt_id,
            idempotency_key,
            payload,
            received_at_unix_ms,
            verified_by,
            policy_snapshot_id,
        ))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum CallbackEndpointAuth {
    Bearer {
        token_ref: String,
        token: String,
    },
    HmacSha256 {
        secret_ref: String,
        secret: Vec<u8>,
        timestamp_header: String,
        signature_header: String,
        replay_window_ms: u64,
    },
    Ed25519 {
        public_key_ref: String,
        public_key: String,
        timestamp_header: String,
        signature_header: String,
        replay_window_ms: u64,
    },
    Mtls {
        trusted_identity: String,
    },
    Oidc {
        issuer: String,
        audience: String,
    },
}

impl CallbackEndpointAuth {
    pub fn bearer(token_ref: impl Into<String>, token: impl Into<String>) -> Self {
        Self::Bearer {
            token_ref: token_ref.into(),
            token: token.into(),
        }
    }

    pub fn hmac_sha256(
        secret_ref: impl Into<String>,
        secret: impl AsRef<[u8]>,
        replay_window_ms: u64,
    ) -> Result<Self, AsyncOperationError> {
        let auth = Self::HmacSha256 {
            secret_ref: secret_ref.into(),
            secret: secret.as_ref().to_vec(),
            timestamp_header: "GraphBlocks-Timestamp".to_owned(),
            signature_header: "GraphBlocks-Signature".to_owned(),
            replay_window_ms,
        };
        auth.validate()?;
        Ok(auth)
    }

    pub fn ed25519(
        public_key_ref: impl Into<String>,
        public_key: impl Into<String>,
        timestamp_header: impl Into<String>,
        signature_header: impl Into<String>,
        replay_window_ms: u64,
    ) -> Result<Self, AsyncOperationError> {
        let auth = Self::Ed25519 {
            public_key_ref: public_key_ref.into(),
            public_key: public_key.into(),
            timestamp_header: timestamp_header.into(),
            signature_header: signature_header.into(),
            replay_window_ms,
        };
        auth.validate()?;
        Ok(auth)
    }

    fn validate(&self) -> Result<(), AsyncOperationError> {
        match self {
            Self::Bearer { token_ref, token } => {
                if token_ref.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: "token_ref".to_owned(),
                    });
                }
                if token.is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: "token".to_owned(),
                    });
                }
            }
            Self::HmacSha256 {
                secret_ref,
                secret,
                timestamp_header,
                signature_header,
                replay_window_ms,
            } => {
                for (field, value) in [
                    ("secret_ref", secret_ref),
                    ("timestamp_header", timestamp_header),
                    ("signature_header", signature_header),
                ] {
                    if value.trim().is_empty() {
                        return Err(AsyncOperationError::EmptyField {
                            field: field.to_owned(),
                        });
                    }
                }
                if secret.is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: "secret".to_owned(),
                    });
                }
                if *replay_window_ms == 0 {
                    return Err(AsyncOperationError::InvalidOperation {
                        operation_id: "callback_endpoint_auth".to_owned(),
                        reason: "hmac replay window must be greater than zero".to_owned(),
                    });
                }
            }
            Self::Ed25519 {
                public_key_ref,
                public_key,
                timestamp_header,
                signature_header,
                replay_window_ms,
            } => {
                for (field, value) in [
                    ("public_key_ref", public_key_ref),
                    ("public_key", public_key),
                    ("timestamp_header", timestamp_header),
                    ("signature_header", signature_header),
                ] {
                    if value.trim().is_empty() {
                        return Err(AsyncOperationError::EmptyField {
                            field: field.to_owned(),
                        });
                    }
                }
                if *replay_window_ms == 0 {
                    return Err(AsyncOperationError::InvalidOperation {
                        operation_id: "callback_endpoint_auth".to_owned(),
                        reason: "ed25519 replay window must be greater than zero".to_owned(),
                    });
                }
            }
            Self::Mtls { trusted_identity } => {
                if trusted_identity.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: "trusted_identity".to_owned(),
                    });
                }
            }
            Self::Oidc { issuer, audience } => {
                if issuer.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: "issuer".to_owned(),
                    });
                }
                if audience.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: "audience".to_owned(),
                    });
                }
            }
        }
        Ok(())
    }

    fn sign_headers(
        &self,
        timestamp_unix_ms: u64,
        payload: &Value,
    ) -> Result<BTreeMap<String, String>, AsyncOperationError> {
        match self {
            Self::HmacSha256 {
                secret,
                timestamp_header,
                signature_header,
                ..
            } => {
                let mut headers = BTreeMap::new();
                headers.insert(timestamp_header.clone(), timestamp_unix_ms.to_string());
                headers.insert(
                    signature_header.clone(),
                    compute_callback_hmac_signature(secret, timestamp_unix_ms, payload)?,
                );
                Ok(headers)
            }
            _ => Err(AsyncOperationError::InvalidOperation {
                operation_id: "callback_endpoint_auth".to_owned(),
                reason: "only hmac-sha256 callback auth can sign headers".to_owned(),
            }),
        }
    }

    fn verify(
        &self,
        endpoint_id: &str,
        headers: &BTreeMap<String, String>,
        payload: &Value,
        received_at_unix_ms: u64,
    ) -> Result<String, AsyncOperationError> {
        match self {
            Self::Bearer { token, .. } => {
                let Some(header) = headers.get("Authorization") else {
                    return Err(callback_auth_failed(endpoint_id, "authorization_missing"));
                };
                let expected = format!("Bearer {token}");
                if !constant_time_eq(header.as_bytes(), expected.as_bytes()) {
                    return Err(callback_auth_failed(endpoint_id, "bearer_token_mismatch"));
                }
                Ok(format!("bearer:{endpoint_id}"))
            }
            Self::HmacSha256 {
                secret,
                timestamp_header,
                signature_header,
                replay_window_ms,
                ..
            } => {
                let timestamp = headers
                    .get(timestamp_header)
                    .ok_or_else(|| callback_auth_failed(endpoint_id, "timestamp_missing"))?
                    .parse::<u64>()
                    .map_err(|_| callback_auth_failed(endpoint_id, "timestamp_invalid"))?;
                if received_at_unix_ms.abs_diff(timestamp) > *replay_window_ms {
                    return Err(callback_auth_failed(
                        endpoint_id,
                        "timestamp_outside_replay_window",
                    ));
                }
                let signature = headers
                    .get(signature_header)
                    .ok_or_else(|| callback_auth_failed(endpoint_id, "signature_missing"))?;
                let expected = compute_callback_hmac_signature(secret, timestamp, payload)?;
                if !constant_time_eq(signature.as_bytes(), expected.as_bytes()) {
                    return Err(callback_auth_failed(endpoint_id, "signature_mismatch"));
                }
                Ok(format!("hmac-sha256:{endpoint_id}"))
            }
            Self::Mtls { .. } => Err(callback_auth_failed(endpoint_id, "mtls_not_bound")),
            Self::Oidc { .. } => Err(callback_auth_failed(endpoint_id, "oidc_not_bound")),
            Self::Ed25519 { .. } => Err(callback_auth_failed(endpoint_id, "ed25519_not_bound")),
        }
    }

    fn verify_ed25519(
        &self,
        endpoint_id: &str,
        headers: &BTreeMap<String, String>,
        payload: &Value,
        received_at_unix_ms: u64,
        verifier: impl FnOnce(&str, &str, &str) -> bool,
    ) -> Result<String, AsyncOperationError> {
        let Self::Ed25519 {
            public_key,
            timestamp_header,
            signature_header,
            replay_window_ms,
            ..
        } = self
        else {
            return Err(callback_auth_failed(endpoint_id, "ed25519_not_configured"));
        };
        let timestamp = headers
            .get(timestamp_header)
            .ok_or_else(|| callback_auth_failed(endpoint_id, "timestamp_missing"))?
            .parse::<u64>()
            .map_err(|_| callback_auth_failed(endpoint_id, "timestamp_invalid"))?;
        if received_at_unix_ms.abs_diff(timestamp) > *replay_window_ms {
            return Err(callback_auth_failed(
                endpoint_id,
                "timestamp_outside_replay_window",
            ));
        }
        let signature = headers
            .get(signature_header)
            .ok_or_else(|| callback_auth_failed(endpoint_id, "signature_missing"))?;
        let message = callback_signature_message(timestamp, payload);
        if !verifier(public_key, &message, signature) {
            return Err(callback_auth_failed(endpoint_id, "signature_mismatch"));
        }
        Ok(format!("ed25519:{endpoint_id}"))
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
    ExternalCallbackRejected {
        operation_id: String,
        callback_id: String,
        reason: String,
        occurred_at_unix_ms: u64,
        verified_by: String,
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
    CallbackAuthenticationFailed {
        endpoint_id: String,
        reason: String,
    },
    CallbackPayloadTooLarge {
        operation_id: String,
        max_payload_bytes: usize,
        actual_payload_bytes: usize,
    },
    Storage {
        message: String,
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
            self.record_callback_rejected(&submission, "payload_too_large");
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
            inner
                .events_by_operation
                .entry(submission.operation_id.clone())
                .or_default()
                .push(AsyncOperationEvent::ExternalCallbackRejected {
                    operation_id: operation.operation_id.clone(),
                    callback_id: submission.callback_id,
                    reason: "stale_attempt".to_owned(),
                    occurred_at_unix_ms: submission.received_at_unix_ms,
                    verified_by: submission.verified_by,
                });
            return Err(AsyncOperationError::StaleAttempt {
                operation_id: operation.operation_id,
                expected_attempt_id: operation.attempt_id,
                actual_attempt_id: submission.attempt_id,
            });
        }

        if let Err(error) = registry.validate(&operation.expected_schema, &submission.payload) {
            inner
                .events_by_operation
                .entry(submission.operation_id.clone())
                .or_default()
                .push(AsyncOperationEvent::ExternalCallbackRejected {
                    operation_id: operation.operation_id.clone(),
                    callback_id: submission.callback_id,
                    reason: "schema_invalid".to_owned(),
                    occurred_at_unix_ms: submission.received_at_unix_ms,
                    verified_by: submission.verified_by,
                });
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

    fn record_callback_rejected(&self, submission: &AsyncCallbackSubmission, reason: &str) {
        let mut inner = self
            .inner
            .lock()
            .expect("async operation store lock poisoned");
        if !inner.operations.contains_key(&submission.operation_id) {
            return;
        }
        inner
            .events_by_operation
            .entry(submission.operation_id.clone())
            .or_default()
            .push(AsyncOperationEvent::ExternalCallbackRejected {
                operation_id: submission.operation_id.clone(),
                callback_id: submission.callback_id.clone(),
                reason: reason.to_owned(),
                occurred_at_unix_ms: submission.received_at_unix_ms,
                verified_by: submission.verified_by.clone(),
            });
    }
}

#[derive(Debug)]
pub struct SqliteAsyncOperationStore {
    connection: Mutex<Connection>,
}

impl SqliteAsyncOperationStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, AsyncOperationError> {
        let connection = Connection::open(path).map_err(storage_error)?;
        let store = Self {
            connection: Mutex::new(connection),
        };
        store.initialize()?;
        Ok(store)
    }

    pub fn open_in_memory() -> Result<Self, AsyncOperationError> {
        let connection = Connection::open_in_memory().map_err(storage_error)?;
        let store = Self {
            connection: Mutex::new(connection),
        };
        store.initialize()?;
        Ok(store)
    }

    fn initialize(&self) -> Result<(), AsyncOperationError> {
        let connection = self
            .connection
            .lock()
            .expect("sqlite async operation store lock poisoned");
        connection
            .execute_batch(
                "
                CREATE TABLE IF NOT EXISTS async_operations (
                    operation_id TEXT PRIMARY KEY,
                    operation_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS async_callback_receipts (
                    idempotency_key TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    receipt_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS async_operation_events (
                    operation_id TEXT NOT NULL,
                    event_index INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (operation_id, event_index)
                );
                ",
            )
            .map_err(storage_error)?;
        Ok(())
    }

    pub fn register(&self, operation: AsyncOperation) -> Result<(), AsyncOperationError> {
        let memory = self.load_memory_store()?;
        memory.register(operation)?;
        self.replace_with_memory_store(&memory)
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
        let memory = self.load_memory_store()?;
        let accepted = memory.accept_callback_with_limits_and_resume_decision(
            submission,
            registry,
            limits,
            resume_decision,
        );
        match &accepted {
            Ok(accepted) if !accepted.duplicate => {
                self.replace_with_memory_store(&memory)?;
            }
            Err(_) => {
                self.replace_with_memory_store(&memory)?;
            }
            _ => {}
        }
        accepted
    }

    pub fn cancel_operation(
        &self,
        operation_id: &str,
        cancelled_at_unix_ms: u64,
    ) -> Result<(), AsyncOperationError> {
        let memory = self.load_memory_store()?;
        memory.cancel_operation(operation_id, cancelled_at_unix_ms)?;
        self.replace_with_memory_store(&memory)
    }

    pub fn expire_operation(
        &self,
        operation_id: &str,
        expired_at_unix_ms: u64,
    ) -> Result<(), AsyncOperationError> {
        let memory = self.load_memory_store()?;
        memory.expire_operation(operation_id, expired_at_unix_ms)?;
        self.replace_with_memory_store(&memory)
    }

    pub fn events_for_operation(&self, operation_id: &str) -> Vec<AsyncOperationEvent> {
        self.load_memory_store()
            .map(|store| store.events_for_operation(operation_id))
            .unwrap_or_default()
    }

    pub fn operation_state(&self, operation_id: &str) -> Option<AsyncOperationState> {
        self.load_memory_store()
            .ok()
            .and_then(|store| store.operation_state(operation_id))
    }

    fn load_memory_store(&self) -> Result<AsyncOperationStore, AsyncOperationError> {
        let store = AsyncOperationStore::new();
        let mut inner = store
            .inner
            .lock()
            .expect("async operation store lock poisoned");
        let connection = self
            .connection
            .lock()
            .expect("sqlite async operation store lock poisoned");

        {
            let mut statement = connection
                .prepare("SELECT operation_json FROM async_operations ORDER BY operation_id")
                .map_err(storage_error)?;
            let operations = statement
                .query_map([], |row| row.get::<_, String>(0))
                .map_err(storage_error)?;
            for operation_json in operations {
                let operation =
                    operation_from_value(parse_json(&operation_json.map_err(storage_error)?)?)?;
                inner
                    .operations
                    .insert(operation.operation_id.clone(), operation);
            }
        }

        {
            let mut statement = connection
                .prepare(
                    "SELECT receipt_json FROM async_callback_receipts ORDER BY idempotency_key",
                )
                .map_err(storage_error)?;
            let receipts = statement
                .query_map([], |row| row.get::<_, String>(0))
                .map_err(storage_error)?;
            for receipt_json in receipts {
                let receipt =
                    receipt_from_value(parse_json(&receipt_json.map_err(storage_error)?)?)?;
                inner
                    .receipts_by_idempotency_key
                    .insert(receipt.idempotency_key.clone(), receipt);
            }
        }

        {
            let mut statement = connection
                .prepare(
                    "
                    SELECT operation_id, event_json
                    FROM async_operation_events
                    ORDER BY operation_id, event_index
                    ",
                )
                .map_err(storage_error)?;
            let events = statement
                .query_map([], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
                })
                .map_err(storage_error)?;
            for event in events {
                let (operation_id, event_json) = event.map_err(storage_error)?;
                inner
                    .events_by_operation
                    .entry(operation_id)
                    .or_default()
                    .push(event_from_value(parse_json(&event_json)?)?);
            }
        }
        drop(inner);
        Ok(store)
    }

    fn replace_with_memory_store(
        &self,
        store: &AsyncOperationStore,
    ) -> Result<(), AsyncOperationError> {
        let mut connection = self
            .connection
            .lock()
            .expect("sqlite async operation store lock poisoned");
        let transaction = connection.transaction().map_err(storage_error)?;
        transaction
            .execute("DELETE FROM async_operation_events", [])
            .map_err(storage_error)?;
        transaction
            .execute("DELETE FROM async_callback_receipts", [])
            .map_err(storage_error)?;
        transaction
            .execute("DELETE FROM async_operations", [])
            .map_err(storage_error)?;

        let inner = store
            .inner
            .lock()
            .expect("async operation store lock poisoned");
        for operation in inner.operations.values() {
            transaction
                .execute(
                    "
                    INSERT INTO async_operations (operation_id, operation_json)
                    VALUES (?, ?)
                    ",
                    params![
                        &operation.operation_id,
                        storage_json(&operation_to_value(operation))?,
                    ],
                )
                .map_err(storage_error)?;
        }
        for receipt in inner.receipts_by_idempotency_key.values() {
            transaction
                .execute(
                    "
                    INSERT INTO async_callback_receipts (
                        idempotency_key,
                        operation_id,
                        receipt_json
                    )
                    VALUES (?, ?, ?)
                    ",
                    params![
                        &receipt.idempotency_key,
                        &receipt.operation_id,
                        storage_json(&receipt_to_value(receipt))?,
                    ],
                )
                .map_err(storage_error)?;
        }
        for (operation_id, events) in &inner.events_by_operation {
            for (index, event) in events.iter().enumerate() {
                transaction
                    .execute(
                        "
                        INSERT INTO async_operation_events (
                            operation_id,
                            event_index,
                            event_json
                        )
                        VALUES (?, ?, ?)
                        ",
                        params![
                            operation_id,
                            sqlite_usize_to_i64(index, "async operation event index")?,
                            storage_json(&event_to_value(event))?,
                        ],
                    )
                    .map_err(storage_error)?;
            }
        }
        drop(inner);
        transaction.commit().map_err(storage_error)?;
        Ok(())
    }
}

fn callback_payload_size_bytes(payload: &Value) -> usize {
    graphblocks_compiler::canonical::canonical_json(payload).len()
}

fn callback_auth_failed(endpoint_id: &str, reason: &str) -> AsyncOperationError {
    AsyncOperationError::CallbackAuthenticationFailed {
        endpoint_id: endpoint_id.to_owned(),
        reason: reason.to_owned(),
    }
}

fn compute_callback_hmac_signature(
    secret: &[u8],
    timestamp_unix_ms: u64,
    payload: &Value,
) -> Result<String, AsyncOperationError> {
    let mut mac =
        HmacSha256::new_from_slice(secret).map_err(|_| AsyncOperationError::InvalidOperation {
            operation_id: "callback_endpoint_auth".to_owned(),
            reason: "invalid hmac secret".to_owned(),
        })?;
    let message = callback_signature_message(timestamp_unix_ms, payload);
    mac.update(message.as_bytes());
    Ok(hex_encode(&mac.finalize().into_bytes()))
}

fn callback_signature_message(timestamp_unix_ms: u64, payload: &Value) -> String {
    format!(
        "{}.{}",
        timestamp_unix_ms,
        graphblocks_compiler::canonical::canonical_json(payload)
    )
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut diff = 0_u8;
    for index in 0..left.len() {
        diff |= left[index] ^ right[index];
    }
    diff == 0
}

fn operation_to_value(operation: &AsyncOperation) -> Value {
    json!({
        "operation_id": operation.operation_id,
        "run_id": operation.run_id,
        "node_id": operation.node_id,
        "attempt_id": operation.attempt_id,
        "kind": async_operation_kind_as_str(&operation.kind),
        "provider_operation_id": operation.provider_operation_id,
        "state": async_operation_state_as_str(operation.state),
        "resume_token_hash": operation.resume_token_hash,
        "idempotency_key": operation.idempotency_key,
        "expected_schema": operation.expected_schema,
        "created_at_unix_ms": operation.created_at_unix_ms,
        "submitted_at_unix_ms": operation.submitted_at_unix_ms,
        "expires_at_unix_ms": operation.expires_at_unix_ms,
        "completed_at_unix_ms": operation.completed_at_unix_ms,
        "expected_callback_payload_bytes": operation.expected_callback_payload_bytes,
        "resume_policy_reevaluation": operation.resume_policy_reevaluation,
        "callback_attempt_fencing": operation.callback_attempt_fencing,
        "resume_ownership_fence": operation.resume_ownership_fence,
    })
}

fn operation_from_value(value: Value) -> Result<AsyncOperation, AsyncOperationError> {
    Ok(AsyncOperation {
        operation_id: required_string(&value, "operation_id")?,
        run_id: required_string(&value, "run_id")?,
        node_id: required_string(&value, "node_id")?,
        attempt_id: required_string(&value, "attempt_id")?,
        kind: async_operation_kind_from_str(&required_string(&value, "kind")?)?,
        provider_operation_id: optional_string(&value, "provider_operation_id")?,
        state: async_operation_state_from_str(&required_string(&value, "state")?)?,
        resume_token_hash: required_string(&value, "resume_token_hash")?,
        idempotency_key: required_string(&value, "idempotency_key")?,
        expected_schema: required_string(&value, "expected_schema")?,
        created_at_unix_ms: required_u64(&value, "created_at_unix_ms")?,
        submitted_at_unix_ms: optional_u64(&value, "submitted_at_unix_ms")?,
        expires_at_unix_ms: optional_u64(&value, "expires_at_unix_ms")?,
        completed_at_unix_ms: optional_u64(&value, "completed_at_unix_ms")?,
        expected_callback_payload_bytes: optional_usize(&value, "expected_callback_payload_bytes")?,
        resume_policy_reevaluation: optional_bool(&value, "resume_policy_reevaluation")?
            .unwrap_or(true),
        callback_attempt_fencing: optional_bool(&value, "callback_attempt_fencing")?
            .unwrap_or(true),
        resume_ownership_fence: optional_bool(&value, "resume_ownership_fence")?.unwrap_or(true),
    })
}

fn receipt_to_value(receipt: &ExternalCallbackReceived) -> Value {
    json!({
        "callback_id": receipt.callback_id,
        "operation_id": receipt.operation_id,
        "run_id": receipt.run_id,
        "node_id": receipt.node_id,
        "attempt_id": receipt.attempt_id,
        "provider_operation_id": receipt.provider_operation_id,
        "idempotency_key": receipt.idempotency_key,
        "payload": receipt.payload,
        "payload_digest": receipt.payload_digest,
        "received_at_unix_ms": receipt.received_at_unix_ms,
        "verified_by": receipt.verified_by,
        "policy_snapshot_id": receipt.policy_snapshot_id,
    })
}

fn receipt_from_value(value: Value) -> Result<ExternalCallbackReceived, AsyncOperationError> {
    Ok(ExternalCallbackReceived {
        callback_id: required_string(&value, "callback_id")?,
        operation_id: required_string(&value, "operation_id")?,
        run_id: required_string(&value, "run_id")?,
        node_id: required_string(&value, "node_id")?,
        attempt_id: required_string(&value, "attempt_id")?,
        provider_operation_id: optional_string(&value, "provider_operation_id")?,
        idempotency_key: required_string(&value, "idempotency_key")?,
        payload: value
            .get("payload")
            .cloned()
            .ok_or_else(|| AsyncOperationError::Storage {
                message: "stored callback receipt is missing payload".to_owned(),
            })?,
        payload_digest: required_string(&value, "payload_digest")?,
        received_at_unix_ms: required_u64(&value, "received_at_unix_ms")?,
        verified_by: required_string(&value, "verified_by")?,
        policy_snapshot_id: required_string(&value, "policy_snapshot_id")?,
    })
}

fn event_to_value(event: &AsyncOperationEvent) -> Value {
    match event {
        AsyncOperationEvent::StateChanged {
            operation_id,
            from,
            to,
            occurred_at_unix_ms,
        } => json!({
            "type": "StateChanged",
            "operation_id": operation_id,
            "from": async_operation_state_as_str(*from),
            "to": async_operation_state_as_str(*to),
            "occurred_at_unix_ms": occurred_at_unix_ms,
        }),
        AsyncOperationEvent::ExternalCallbackReceived { receipt } => json!({
            "type": "ExternalCallbackReceived",
            "receipt": receipt_to_value(receipt),
        }),
        AsyncOperationEvent::ExternalCallbackRejected {
            operation_id,
            callback_id,
            reason,
            occurred_at_unix_ms,
            verified_by,
        } => json!({
            "type": "ExternalCallbackRejected",
            "operation_id": operation_id,
            "callback_id": callback_id,
            "reason": reason,
            "occurred_at_unix_ms": occurred_at_unix_ms,
            "verified_by": verified_by,
        }),
        AsyncOperationEvent::CallbackResumePaused {
            operation_id,
            reason,
            occurred_at_unix_ms,
        } => json!({
            "type": "CallbackResumePaused",
            "operation_id": operation_id,
            "reason": reason,
            "occurred_at_unix_ms": occurred_at_unix_ms,
        }),
        AsyncOperationEvent::CallbackResumeDenied {
            operation_id,
            decision_id,
            reason,
            occurred_at_unix_ms,
        } => json!({
            "type": "CallbackResumeDenied",
            "operation_id": operation_id,
            "decision_id": decision_id,
            "reason": reason,
            "occurred_at_unix_ms": occurred_at_unix_ms,
        }),
        AsyncOperationEvent::LateExternalCallbackReceived {
            receipt,
            terminal_state,
        } => json!({
            "type": "LateExternalCallbackReceived",
            "receipt": receipt_to_value(receipt),
            "terminal_state": async_operation_state_as_str(*terminal_state),
        }),
    }
}

fn event_from_value(value: Value) -> Result<AsyncOperationEvent, AsyncOperationError> {
    match required_string(&value, "type")?.as_str() {
        "StateChanged" => Ok(AsyncOperationEvent::StateChanged {
            operation_id: required_string(&value, "operation_id")?,
            from: async_operation_state_from_str(&required_string(&value, "from")?)?,
            to: async_operation_state_from_str(&required_string(&value, "to")?)?,
            occurred_at_unix_ms: required_u64(&value, "occurred_at_unix_ms")?,
        }),
        "ExternalCallbackReceived" => Ok(AsyncOperationEvent::ExternalCallbackReceived {
            receipt: receipt_from_value(required_value(&value, "receipt")?)?,
        }),
        "ExternalCallbackRejected" => Ok(AsyncOperationEvent::ExternalCallbackRejected {
            operation_id: required_string(&value, "operation_id")?,
            callback_id: required_string(&value, "callback_id")?,
            reason: required_string(&value, "reason")?,
            occurred_at_unix_ms: required_u64(&value, "occurred_at_unix_ms")?,
            verified_by: required_string(&value, "verified_by")?,
        }),
        "CallbackResumePaused" => Ok(AsyncOperationEvent::CallbackResumePaused {
            operation_id: required_string(&value, "operation_id")?,
            reason: required_string(&value, "reason")?,
            occurred_at_unix_ms: required_u64(&value, "occurred_at_unix_ms")?,
        }),
        "CallbackResumeDenied" => Ok(AsyncOperationEvent::CallbackResumeDenied {
            operation_id: required_string(&value, "operation_id")?,
            decision_id: required_string(&value, "decision_id")?,
            reason: required_string(&value, "reason")?,
            occurred_at_unix_ms: required_u64(&value, "occurred_at_unix_ms")?,
        }),
        "LateExternalCallbackReceived" => Ok(AsyncOperationEvent::LateExternalCallbackReceived {
            receipt: receipt_from_value(required_value(&value, "receipt")?)?,
            terminal_state: async_operation_state_from_str(&required_string(
                &value,
                "terminal_state",
            )?)?,
        }),
        event_type => Err(AsyncOperationError::Storage {
            message: format!("unknown async operation event type {event_type}"),
        }),
    }
}

fn async_operation_kind_as_str(kind: &AsyncOperationKind) -> &'static str {
    match kind {
        AsyncOperationKind::Tool => "tool",
        AsyncOperationKind::SandboxTask => "sandbox_task",
        AsyncOperationKind::CiJob => "ci_job",
        AsyncOperationKind::BrowserTask => "browser_task",
        AsyncOperationKind::WorkspaceTrial => "workspace_trial",
        AsyncOperationKind::ExternalProviderJob => "external_provider_job",
        AsyncOperationKind::DocumentJob => "document_job",
        AsyncOperationKind::ResearchTask => "research_task",
        AsyncOperationKind::Custom => "custom",
    }
}

fn async_operation_kind_from_str(kind: &str) -> Result<AsyncOperationKind, AsyncOperationError> {
    match kind {
        "tool" => Ok(AsyncOperationKind::Tool),
        "sandbox_task" => Ok(AsyncOperationKind::SandboxTask),
        "ci_job" => Ok(AsyncOperationKind::CiJob),
        "browser_task" => Ok(AsyncOperationKind::BrowserTask),
        "workspace_trial" => Ok(AsyncOperationKind::WorkspaceTrial),
        "external_provider_job" => Ok(AsyncOperationKind::ExternalProviderJob),
        "document_job" => Ok(AsyncOperationKind::DocumentJob),
        "research_task" => Ok(AsyncOperationKind::ResearchTask),
        "custom" => Ok(AsyncOperationKind::Custom),
        _ => Err(AsyncOperationError::Storage {
            message: format!("unknown async operation kind {kind}"),
        }),
    }
}

fn async_operation_state_as_str(state: AsyncOperationState) -> &'static str {
    match state {
        AsyncOperationState::Created => "created",
        AsyncOperationState::Submitted => "submitted",
        AsyncOperationState::WaitingCallback => "waiting_callback",
        AsyncOperationState::CallbackReceived => "callback_received",
        AsyncOperationState::Polling => "polling",
        AsyncOperationState::Resuming => "resuming",
        AsyncOperationState::Completed => "completed",
        AsyncOperationState::Failed => "failed",
        AsyncOperationState::Cancelled => "cancelled",
        AsyncOperationState::Expired => "expired",
    }
}

fn async_operation_state_from_str(state: &str) -> Result<AsyncOperationState, AsyncOperationError> {
    match state {
        "created" => Ok(AsyncOperationState::Created),
        "submitted" => Ok(AsyncOperationState::Submitted),
        "waiting_callback" => Ok(AsyncOperationState::WaitingCallback),
        "callback_received" => Ok(AsyncOperationState::CallbackReceived),
        "polling" => Ok(AsyncOperationState::Polling),
        "resuming" => Ok(AsyncOperationState::Resuming),
        "completed" => Ok(AsyncOperationState::Completed),
        "failed" => Ok(AsyncOperationState::Failed),
        "cancelled" => Ok(AsyncOperationState::Cancelled),
        "expired" => Ok(AsyncOperationState::Expired),
        _ => Err(AsyncOperationError::Storage {
            message: format!("unknown async operation state {state}"),
        }),
    }
}

fn required_value(value: &Value, field: &'static str) -> Result<Value, AsyncOperationError> {
    value
        .get(field)
        .cloned()
        .ok_or_else(|| AsyncOperationError::Storage {
            message: format!("stored async operation value is missing {field}"),
        })
}

fn required_string(value: &Value, field: &'static str) -> Result<String, AsyncOperationError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .ok_or_else(|| AsyncOperationError::Storage {
            message: format!("stored async operation value has invalid {field}"),
        })
}

fn optional_string(
    value: &Value,
    field: &'static str,
) -> Result<Option<String>, AsyncOperationError> {
    match value.get(field) {
        Some(Value::Null) | None => Ok(None),
        Some(Value::String(value)) => Ok(Some(value.clone())),
        _ => Err(AsyncOperationError::Storage {
            message: format!("stored async operation value has invalid {field}"),
        }),
    }
}

fn required_u64(value: &Value, field: &'static str) -> Result<u64, AsyncOperationError> {
    value
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| AsyncOperationError::Storage {
            message: format!("stored async operation value has invalid {field}"),
        })
}

fn optional_u64(value: &Value, field: &'static str) -> Result<Option<u64>, AsyncOperationError> {
    match value.get(field) {
        Some(Value::Null) | None => Ok(None),
        Some(value) => value
            .as_u64()
            .map(Some)
            .ok_or_else(|| AsyncOperationError::Storage {
                message: format!("stored async operation value has invalid {field}"),
            }),
    }
}

fn optional_usize(
    value: &Value,
    field: &'static str,
) -> Result<Option<usize>, AsyncOperationError> {
    match optional_u64(value, field)? {
        Some(value) => usize::try_from(value)
            .map(Some)
            .map_err(|_| AsyncOperationError::Storage {
                message: format!("stored async operation value has oversized {field}"),
            }),
        None => Ok(None),
    }
}

fn optional_bool(value: &Value, field: &'static str) -> Result<Option<bool>, AsyncOperationError> {
    match value.get(field) {
        Some(Value::Null) | None => Ok(None),
        Some(value) => value
            .as_bool()
            .map(Some)
            .ok_or_else(|| AsyncOperationError::Storage {
                message: format!("stored async operation value has invalid {field}"),
            }),
    }
}

fn storage_json(value: &Value) -> Result<String, AsyncOperationError> {
    serde_json::to_string(value).map_err(|error| AsyncOperationError::Storage {
        message: error.to_string(),
    })
}

fn parse_json(value: &str) -> Result<Value, AsyncOperationError> {
    serde_json::from_str(value).map_err(|error| AsyncOperationError::Storage {
        message: error.to_string(),
    })
}

fn storage_error(error: rusqlite::Error) -> AsyncOperationError {
    AsyncOperationError::Storage {
        message: error.to_string(),
    }
}

fn sqlite_usize_to_i64(value: usize, label: &'static str) -> Result<i64, AsyncOperationError> {
    i64::try_from(value).map_err(|_| AsyncOperationError::Storage {
        message: format!("{label} exceeds sqlite integer range"),
    })
}

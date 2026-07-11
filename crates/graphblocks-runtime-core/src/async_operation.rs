use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::sync::Mutex;

use graphblocks_compiler::canonical::canonical_hash;
use hmac::{Hmac, Mac};
use rusqlite::{Connection, TransactionBehavior, params};
use serde_json::{Value, json};
use sha2::Sha256;

use crate::tool_result::ToolEffectOutcome;
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
    pub infinite_wait_policy: Option<String>,
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
        if operation.expires_at_unix_ms.is_none()
            && operation.infinite_wait_policy.is_none()
            && matches!(
                operation.state,
                AsyncOperationState::WaitingCallback | AsyncOperationState::Polling
            )
        {
            let wait_kind = match operation.state {
                AsyncOperationState::WaitingCallback => "callback",
                AsyncOperationState::Polling => "polling",
                _ => unreachable!("matches limited to async wait states"),
            };
            diagnostics.push(Self {
                code: "GB6001",
                field: "expires_at_unix_ms",
                message: format!(
                    "async operation {} {wait_kind} wait has no timeout",
                    operation.operation_id,
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
    #[allow(clippy::too_many_arguments)]
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
            infinite_wait_policy: None,
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

    pub fn with_infinite_wait_policy(mut self, infinite_wait_policy: impl Into<String>) -> Self {
        self.infinite_wait_policy = Some(infinite_wait_policy.into());
        self
    }

    pub fn without_expiration(mut self) -> Self {
        self.expires_at_unix_ms = None;
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
            if value.trim().is_empty() {
                return Err(AsyncOperationError::EmptyField {
                    field: field.to_owned(),
                });
            }
        }

        if self.created_at_unix_ms == 0 {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "created_at must be positive".to_owned(),
            });
        }
        let resume_digest = self.resume_token_hash.strip_prefix("sha256:");
        if !matches!(
            resume_digest,
            Some(digest)
                if digest.len() == 64
                    && digest
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        ) {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "resume_token_hash must be a canonical sha256 digest".to_owned(),
            });
        }
        if self
            .provider_operation_id
            .as_ref()
            .is_some_and(|provider_operation_id| provider_operation_id.trim().is_empty())
        {
            return Err(AsyncOperationError::EmptyField {
                field: "provider_operation_id".to_owned(),
            });
        }
        if self
            .infinite_wait_policy
            .as_ref()
            .is_some_and(|infinite_wait_policy| infinite_wait_policy.trim().is_empty())
        {
            return Err(AsyncOperationError::EmptyField {
                field: "infinite_wait_policy".to_owned(),
            });
        }
        if matches!(
            self.state,
            AsyncOperationState::WaitingCallback
                | AsyncOperationState::CallbackReceived
                | AsyncOperationState::Polling
        ) && self.expires_at_unix_ms.is_some()
            && self.infinite_wait_policy.is_some()
        {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "async operation wait must not define both expires_at_unix_ms and infinite_wait_policy".to_owned(),
            });
        }

        if self.state == AsyncOperationState::WaitingCallback
            && self.expires_at_unix_ms.is_none()
            && self.infinite_wait_policy.is_none()
        {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "waiting callback operations require an expiration or infinite_wait_policy"
                    .to_owned(),
            });
        }
        if self.state == AsyncOperationState::CallbackReceived
            && self.expires_at_unix_ms.is_none()
            && self.infinite_wait_policy.is_none()
        {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason:
                    "callback_received operations require an expiration or infinite_wait_policy"
                        .to_owned(),
            });
        }
        if self.state == AsyncOperationState::Polling
            && self.expires_at_unix_ms.is_none()
            && self.infinite_wait_policy.is_none()
        {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "polling operations require an expiration or infinite_wait_policy"
                    .to_owned(),
            });
        }
        if self.state == AsyncOperationState::Created && self.provider_operation_id.is_some() {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "created operations cannot have provider_operation_id".to_owned(),
            });
        }
        if self.state == AsyncOperationState::Created && self.submitted_at_unix_ms.is_some() {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "created operations cannot have submitted_at".to_owned(),
            });
        }
        if self.state == AsyncOperationState::Created && self.completed_at_unix_ms.is_some() {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "created operations cannot have completed_at".to_owned(),
            });
        }
        if self.state == AsyncOperationState::Created && self.expires_at_unix_ms.is_some() {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "created operations cannot have expires_at".to_owned(),
            });
        }
        if self.state != AsyncOperationState::Created && self.submitted_at_unix_ms.is_none() {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "non-created operations require submitted_at".to_owned(),
            });
        }
        if let Some(submitted_at_unix_ms) = self.submitted_at_unix_ms
            && submitted_at_unix_ms < self.created_at_unix_ms
        {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "submitted_at precedes created_at".to_owned(),
            });
        }
        if self.completed_at_unix_ms.is_none() {
            let terminal_state = match self.state {
                AsyncOperationState::Completed => Some("completed"),
                AsyncOperationState::Failed => Some("failed"),
                AsyncOperationState::Cancelled => Some("cancelled"),
                AsyncOperationState::Expired => Some("expired"),
                _ => None,
            };
            if let Some(terminal_state) = terminal_state {
                return Err(AsyncOperationError::InvalidOperation {
                    operation_id: self.operation_id.clone(),
                    reason: format!("{terminal_state} operations require completed_at"),
                });
            }
        }
        if let (Some(submitted_at_unix_ms), Some(completed_at_unix_ms)) =
            (self.submitted_at_unix_ms, self.completed_at_unix_ms)
            && completed_at_unix_ms < submitted_at_unix_ms
        {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "completed_at precedes submitted_at".to_owned(),
            });
        }
        if let (Some(completed_at_unix_ms), Some(expires_at_unix_ms)) =
            (self.completed_at_unix_ms, self.expires_at_unix_ms)
            && self.state != AsyncOperationState::Expired
            && completed_at_unix_ms > expires_at_unix_ms
        {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "completed_at exceeds expires_at".to_owned(),
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
        if let (Some(submitted_at_unix_ms), Some(expires_at_unix_ms)) =
            (self.submitted_at_unix_ms, self.expires_at_unix_ms)
            && expires_at_unix_ms <= submitted_at_unix_ms
        {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.operation_id.clone(),
                reason: "expires_at must be after submitted_at".to_owned(),
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
    #[allow(clippy::too_many_arguments)]
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

    pub fn with_provider_operation_id(mut self, provider_operation_id: impl Into<String>) -> Self {
        self.provider_operation_id = Some(provider_operation_id.into());
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CallbackArtifactRef {
    pub artifact_id: String,
    pub uri: String,
    pub media_type: Option<String>,
    pub checksum: Option<String>,
}

impl CallbackArtifactRef {
    pub fn new(artifact_id: impl Into<String>, uri: impl Into<String>) -> Self {
        Self {
            artifact_id: artifact_id.into(),
            uri: uri.into(),
            media_type: None,
            checksum: None,
        }
    }

    pub fn with_media_type(mut self, media_type: impl Into<String>) -> Self {
        self.media_type = Some(media_type.into());
        self
    }

    pub fn with_checksum(mut self, checksum: impl Into<String>) -> Self {
        self.checksum = Some(checksum.into());
        self
    }

    pub fn validate(&self) -> Result<(), AsyncOperationError> {
        for (field, value) in [("artifact_id", &self.artifact_id), ("uri", &self.uri)] {
            if value.trim().is_empty() {
                return Err(AsyncOperationError::EmptyField {
                    field: field.to_owned(),
                });
            }
        }
        if self
            .media_type
            .as_ref()
            .is_some_and(|media_type| media_type.trim().is_empty())
        {
            return Err(AsyncOperationError::EmptyField {
                field: "media_type".to_owned(),
            });
        }
        if self
            .checksum
            .as_ref()
            .is_some_and(|checksum| checksum.trim().is_empty())
        {
            return Err(AsyncOperationError::EmptyField {
                field: "checksum".to_owned(),
            });
        }
        Ok(())
    }

    pub fn canonical_value(&self) -> Value {
        json!({
            "artifact_id": self.artifact_id,
            "uri": self.uri,
            "media_type": self.media_type,
            "checksum": self.checksum,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct CallbackEndpointRef {
    pub endpoint_id: String,
    pub url: String,
    pub accepted_schema: String,
    pub auth: CallbackEndpointAuth,
    pub operation_id: Option<String>,
    pub run_id: Option<String>,
    pub node_id: Option<String>,
    pub attempt_id: Option<String>,
    pub release_id: Option<String>,
    pub tenant_id: Option<String>,
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
            operation_id: None,
            run_id: None,
            node_id: None,
            attempt_id: None,
            release_id: None,
            tenant_id: None,
            expires_at_unix_ms: None,
        };
        endpoint.validate()?;
        Ok(endpoint)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn new_bound(
        endpoint_id: impl Into<String>,
        url: impl Into<String>,
        accepted_schema: impl Into<String>,
        auth: CallbackEndpointAuth,
        operation_id: impl Into<String>,
        run_id: impl Into<String>,
        node_id: impl Into<String>,
        attempt_id: impl Into<String>,
        release_id: impl Into<String>,
        tenant_id: Option<impl Into<String>>,
    ) -> Result<Self, AsyncOperationError> {
        let endpoint = Self {
            endpoint_id: endpoint_id.into(),
            url: url.into(),
            accepted_schema: accepted_schema.into(),
            auth,
            operation_id: Some(operation_id.into()),
            run_id: Some(run_id.into()),
            node_id: Some(node_id.into()),
            attempt_id: Some(attempt_id.into()),
            release_id: Some(release_id.into()),
            tenant_id: tenant_id.map(Into::into),
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
        if self.url.trim() != self.url {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.endpoint_id.clone(),
                reason: "callback endpoint url must not include surrounding whitespace".to_owned(),
            });
        }
        if !(self.url.starts_with("https://") || self.url.starts_with("http://")) {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.endpoint_id.clone(),
                reason: "callback endpoint url must use http or https".to_owned(),
            });
        }
        let authority = self
            .url
            .split_once("://")
            .map(|(_, rest)| rest.split(&['/', '?', '#'][..]).next().unwrap_or_default())
            .unwrap_or_default();
        if authority.is_empty()
            || authority
                .bytes()
                .any(|byte| byte.is_ascii_whitespace() || byte < 0x20 || byte == 0x7f)
            || authority.contains('@')
        {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.endpoint_id.clone(),
                reason: "callback endpoint url host is malformed".to_owned(),
            });
        }
        if let Some(bracketed_host) = authority.strip_prefix('[') {
            let Some((host, suffix)) = bracketed_host.split_once(']') else {
                return Err(AsyncOperationError::InvalidOperation {
                    operation_id: self.endpoint_id.clone(),
                    reason: "callback endpoint url host is malformed".to_owned(),
                });
            };
            if host.contains('%') || host.parse::<std::net::Ipv6Addr>().is_err() {
                return Err(AsyncOperationError::InvalidOperation {
                    operation_id: self.endpoint_id.clone(),
                    reason: "callback endpoint url host is malformed".to_owned(),
                });
            }
            if !suffix.is_empty() {
                let Some(port) = suffix.strip_prefix(':') else {
                    return Err(AsyncOperationError::InvalidOperation {
                        operation_id: self.endpoint_id.clone(),
                        reason: "callback endpoint url host is malformed".to_owned(),
                    });
                };
                if port.is_empty() || port.parse::<u16>().is_err() {
                    return Err(AsyncOperationError::InvalidOperation {
                        operation_id: self.endpoint_id.clone(),
                        reason: "callback endpoint url host is malformed".to_owned(),
                    });
                }
            }
        } else {
            let host = if let Some((host, port)) = authority.split_once(':') {
                if port.is_empty() || port.parse::<u16>().is_err() {
                    return Err(AsyncOperationError::InvalidOperation {
                        operation_id: self.endpoint_id.clone(),
                        reason: "callback endpoint url host is malformed".to_owned(),
                    });
                }
                host
            } else {
                authority
            };
            let host = host.trim_end_matches('.').to_ascii_lowercase();
            if host.is_empty()
                || host.contains(':')
                || !host
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.'))
                || host
                    .split('.')
                    .any(|label| label.is_empty() || label.starts_with('-') || label.ends_with('-'))
            {
                return Err(AsyncOperationError::InvalidOperation {
                    operation_id: self.endpoint_id.clone(),
                    reason: "callback endpoint url host is malformed".to_owned(),
                });
            }
        }
        for (field, value) in [
            ("operation_id", &self.operation_id),
            ("run_id", &self.run_id),
            ("node_id", &self.node_id),
            ("attempt_id", &self.attempt_id),
            ("release_id", &self.release_id),
            ("tenant_id", &self.tenant_id),
        ] {
            if let Some(value) = value {
                if value.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: field.to_owned(),
                    });
                }
                if value.trim() != value {
                    return Err(AsyncOperationError::InvalidOperation {
                        operation_id: self.endpoint_id.clone(),
                        reason: format!(
                            "callback endpoint bound identity field {field} must not include surrounding whitespace"
                        ),
                    });
                }
            }
        }
        let required_binding_fields = [
            self.operation_id.is_some(),
            self.run_id.is_some(),
            self.node_id.is_some(),
            self.attempt_id.is_some(),
            self.release_id.is_some(),
        ];
        if required_binding_fields.iter().any(|is_set| *is_set)
            && !required_binding_fields.iter().all(|is_set| *is_set)
        {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: self.endpoint_id.clone(),
                reason: "callback endpoint binding must include operation_id, run_id, node_id, attempt_id, and release_id together".to_owned(),
            });
        }
        if self.expires_at_unix_ms == Some(0) {
            return Err(AsyncOperationError::InvalidExpiration {
                operation_id: self.endpoint_id.clone(),
                created_at_unix_ms: 0,
                expires_at_unix_ms: 0,
            });
        }
        self.auth.validate()
    }

    pub fn binding_key(&self) -> String {
        callback_resume_binding_key(
            self.tenant_id.as_deref(),
            self.release_id.as_deref().unwrap_or(""),
            self.run_id.as_deref().unwrap_or(""),
            self.node_id.as_deref().unwrap_or(""),
            self.attempt_id.as_deref().unwrap_or(""),
            self.operation_id.as_deref().unwrap_or(""),
        )
    }

    pub fn receipt_binding_key(&self, submission: &AsyncCallbackSubmission) -> String {
        callback_resume_binding_key(
            self.tenant_id.as_deref(),
            self.release_id.as_deref().unwrap_or(""),
            &submission.run_id,
            &submission.node_id,
            &submission.attempt_id,
            &submission.operation_id,
        )
    }

    fn validate_bound_submission_identity(
        &self,
        submission: &AsyncCallbackSubmission,
    ) -> Result<(), AsyncOperationError> {
        for (field, expected, actual) in [
            (
                "operation_id",
                self.operation_id.as_deref(),
                submission.operation_id.as_str(),
            ),
            ("run_id", self.run_id.as_deref(), submission.run_id.as_str()),
            (
                "node_id",
                self.node_id.as_deref(),
                submission.node_id.as_str(),
            ),
            (
                "attempt_id",
                self.attempt_id.as_deref(),
                submission.attempt_id.as_str(),
            ),
        ] {
            if expected.is_some_and(|expected| expected != actual) {
                return Err(AsyncOperationError::CallbackAuthenticationFailed {
                    endpoint_id: self.endpoint_id.clone(),
                    reason: format!("callback_binding_mismatch:{field}"),
                });
            }
        }
        Ok(())
    }

    pub fn sign_callback_headers(
        &self,
        timestamp_unix_ms: u64,
        payload: &Value,
    ) -> Result<BTreeMap<String, String>, AsyncOperationError> {
        self.auth.sign_headers(timestamp_unix_ms, payload)
    }

    fn ensure_not_expired(&self, received_at_unix_ms: u64) -> Result<(), AsyncOperationError> {
        if self
            .expires_at_unix_ms
            .is_some_and(|expires_at_unix_ms| received_at_unix_ms >= expires_at_unix_ms)
        {
            return Err(AsyncOperationError::CallbackAuthenticationFailed {
                endpoint_id: self.endpoint_id.clone(),
                reason: "endpoint_expired".to_owned(),
            });
        }
        Ok(())
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
        self.ensure_not_expired(received_at_unix_ms)?;
        let verified_by =
            self.auth
                .verify(&self.endpoint_id, headers, &payload, received_at_unix_ms)?;
        let submission = AsyncCallbackSubmission::new(
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
        );
        validate_callback_submission_identity(&submission)?;
        self.validate_bound_submission_identity(&submission)?;
        Ok(submission)
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
        self.ensure_not_expired(received_at_unix_ms)?;
        let verified_by = self.auth.verify_ed25519(
            &self.endpoint_id,
            headers,
            &payload,
            received_at_unix_ms,
            verifier,
        )?;
        let submission = AsyncCallbackSubmission::new(
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
        );
        validate_callback_submission_identity(&submission)?;
        self.validate_bound_submission_identity(&submission)?;
        Ok(submission)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn authenticate_mtls_and_build_submission(
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
        client_identity: Option<&str>,
    ) -> Result<AsyncCallbackSubmission, AsyncOperationError> {
        self.ensure_not_expired(received_at_unix_ms)?;
        let verified_by = self
            .auth
            .verify_mtls(&self.endpoint_id, headers, client_identity)?;
        let submission = AsyncCallbackSubmission::new(
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
        );
        validate_callback_submission_identity(&submission)?;
        self.validate_bound_submission_identity(&submission)?;
        Ok(submission)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn authenticate_oidc_and_build_submission(
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
        self.ensure_not_expired(received_at_unix_ms)?;
        let verified_by = self
            .auth
            .verify_oidc(&self.endpoint_id, headers, verifier)?;
        let submission = AsyncCallbackSubmission::new(
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
        );
        validate_callback_submission_identity(&submission)?;
        self.validate_bound_submission_identity(&submission)?;
        Ok(submission)
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

    pub fn mtls(trusted_identity: impl Into<String>) -> Self {
        Self::Mtls {
            trusted_identity: trusted_identity.into(),
        }
    }

    pub fn oidc(issuer: impl Into<String>, audience: impl Into<String>) -> Self {
        Self::Oidc {
            issuer: issuer.into(),
            audience: audience.into(),
        }
    }

    fn validate(&self) -> Result<(), AsyncOperationError> {
        match self {
            Self::Bearer { token_ref, token } => {
                if token_ref.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: "token_ref".to_owned(),
                    });
                }
                if token.trim().is_empty() {
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

    fn verify_mtls(
        &self,
        endpoint_id: &str,
        headers: &BTreeMap<String, String>,
        client_identity: Option<&str>,
    ) -> Result<String, AsyncOperationError> {
        let Self::Mtls { trusted_identity } = self else {
            return Err(callback_auth_failed(endpoint_id, "mtls_not_configured"));
        };
        let client_identity = client_identity
            .or_else(|| {
                headers
                    .get("GraphBlocks-Client-Identity")
                    .map(String::as_str)
            })
            .ok_or_else(|| callback_auth_failed(endpoint_id, "mtls_identity_missing"))?;
        if client_identity != trusted_identity {
            return Err(callback_auth_failed(endpoint_id, "mtls_identity_mismatch"));
        }
        Ok(format!("mtls:{endpoint_id}"))
    }

    fn verify_oidc(
        &self,
        endpoint_id: &str,
        headers: &BTreeMap<String, String>,
        verifier: impl FnOnce(&str, &str, &str) -> bool,
    ) -> Result<String, AsyncOperationError> {
        let Self::Oidc { issuer, audience } = self else {
            return Err(callback_auth_failed(endpoint_id, "oidc_not_configured"));
        };
        let token = headers
            .get("Authorization")
            .and_then(|header| header.strip_prefix("Bearer "))
            .ok_or_else(|| callback_auth_failed(endpoint_id, "authorization_missing"))?;
        if token.trim().is_empty() {
            return Err(callback_auth_failed(endpoint_id, "oidc_token_empty"));
        }
        if !verifier(issuer, audience, token) {
            return Err(callback_auth_failed(endpoint_id, "oidc_token_invalid"));
        }
        Ok(format!("oidc:{endpoint_id}"))
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
    pub artifacts: Vec<CallbackArtifactRef>,
    pub received_at_unix_ms: u64,
    pub verified_by: String,
    pub policy_snapshot_id: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AsyncCallbackResumeDecision {
    ResumeAuthorized {
        authentication_verified: bool,
        policy_decision_id: String,
        budget_reservation_id: String,
        compatible_release_id: String,
        ownership_fence_token: String,
    },
    PauseAuthorizationRequired,
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QuarantinedCallback {
    pub operation_id: String,
    pub idempotency_key: String,
    pub duplicate: bool,
    pub expires_at_unix_ms: u64,
}

#[derive(Clone, Debug, PartialEq)]
struct QuarantinedCallbackRecord {
    submission: AsyncCallbackSubmission,
    expires_at_unix_ms: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AsyncOperationResultStatus {
    Completed,
    Failed,
    Cancelled,
    Expired,
    Incomplete,
}

impl AsyncOperationResultStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Completed => "completed",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::Expired => "expired",
            Self::Incomplete => "incomplete",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExternalEffectRecord {
    pub effect_id: String,
    pub target: String,
    pub operation: String,
    pub outcome: ToolEffectOutcome,
    pub idempotency_key: Option<String>,
    pub provider_effect_id: Option<String>,
}

impl ExternalEffectRecord {
    pub fn new(
        effect_id: impl Into<String>,
        target: impl Into<String>,
        operation: impl Into<String>,
        outcome: ToolEffectOutcome,
    ) -> Self {
        Self {
            effect_id: effect_id.into(),
            target: target.into(),
            operation: operation.into(),
            outcome,
            idempotency_key: None,
            provider_effect_id: None,
        }
    }

    pub fn with_idempotency_key(mut self, idempotency_key: impl Into<String>) -> Self {
        self.idempotency_key = Some(idempotency_key.into());
        self
    }

    pub fn with_provider_effect_id(mut self, provider_effect_id: impl Into<String>) -> Self {
        self.provider_effect_id = Some(provider_effect_id.into());
        self
    }

    pub fn validate(&self) -> Result<(), AsyncOperationError> {
        for (field, value) in [
            ("external_effect.effect_id", &self.effect_id),
            ("external_effect.target", &self.target),
            ("external_effect.operation", &self.operation),
        ] {
            if value.trim().is_empty() {
                return Err(AsyncOperationError::EmptyField {
                    field: field.to_owned(),
                });
            }
        }
        if self
            .idempotency_key
            .as_ref()
            .is_some_and(|idempotency_key| idempotency_key.trim().is_empty())
        {
            return Err(AsyncOperationError::EmptyField {
                field: "external_effect.idempotency_key".to_owned(),
            });
        }
        if self
            .provider_effect_id
            .as_ref()
            .is_some_and(|provider_effect_id| provider_effect_id.trim().is_empty())
        {
            return Err(AsyncOperationError::EmptyField {
                field: "external_effect.provider_effect_id".to_owned(),
            });
        }
        Ok(())
    }

    pub fn protocol_value(&self) -> Value {
        json!({
            "effectId": self.effect_id,
            "target": self.target,
            "operation": self.operation,
            "outcome": self.outcome.as_str(),
            "idempotencyKey": self.idempotency_key,
            "providerEffectId": self.provider_effect_id,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct AsyncOperationResult {
    pub operation_id: String,
    pub status: AsyncOperationResultStatus,
    pub output: Option<Value>,
    pub artifacts: Vec<CallbackArtifactRef>,
    pub diagnostics: Vec<Value>,
    pub metrics: Vec<Value>,
    pub checks: Vec<Value>,
    pub usage: Vec<Value>,
    pub external_effects: Vec<ExternalEffectRecord>,
}

impl AsyncOperationResult {
    pub fn completed(operation_id: impl Into<String>) -> Self {
        Self::new(operation_id, AsyncOperationResultStatus::Completed)
    }

    pub fn failed(operation_id: impl Into<String>) -> Self {
        Self::new(operation_id, AsyncOperationResultStatus::Failed)
    }

    pub fn cancelled(operation_id: impl Into<String>) -> Self {
        Self::new(operation_id, AsyncOperationResultStatus::Cancelled)
    }

    pub fn expired(operation_id: impl Into<String>) -> Self {
        Self::new(operation_id, AsyncOperationResultStatus::Expired)
    }

    pub fn incomplete(operation_id: impl Into<String>) -> Self {
        Self::new(operation_id, AsyncOperationResultStatus::Incomplete)
    }

    fn new(operation_id: impl Into<String>, status: AsyncOperationResultStatus) -> Self {
        Self {
            operation_id: operation_id.into(),
            status,
            output: None,
            artifacts: Vec::new(),
            diagnostics: Vec::new(),
            metrics: Vec::new(),
            checks: Vec::new(),
            usage: Vec::new(),
            external_effects: Vec::new(),
        }
    }

    pub fn with_output(mut self, output: Value) -> Self {
        self.output = Some(output);
        self
    }

    pub fn with_external_effects<I>(mut self, external_effects: I) -> Self
    where
        I: IntoIterator<Item = ExternalEffectRecord>,
    {
        self.external_effects = external_effects.into_iter().collect();
        self
    }

    pub fn external_effect_was_committed(&self) -> bool {
        self.external_effects
            .iter()
            .any(|effect| effect.outcome == ToolEffectOutcome::Committed)
    }

    pub fn validate(&self) -> Result<(), AsyncOperationError> {
        if self.operation_id.trim().is_empty() {
            return Err(AsyncOperationError::EmptyField {
                field: "operation_id".to_owned(),
            });
        }
        for (field, values) in [
            ("diagnostics", &self.diagnostics),
            ("metrics", &self.metrics),
            ("checks", &self.checks),
            ("usage", &self.usage),
        ] {
            if values.iter().any(|value| !value.is_object()) {
                return Err(AsyncOperationError::InvalidOperation {
                    operation_id: self.operation_id.clone(),
                    reason: format!("{field} entries must be JSON objects"),
                });
            }
        }
        let mut artifact_ids = BTreeSet::new();
        for artifact in &self.artifacts {
            artifact.validate()?;
            if !artifact_ids.insert(artifact.artifact_id.as_str()) {
                return Err(AsyncOperationError::InvalidOperation {
                    operation_id: self.operation_id.clone(),
                    reason: format!("duplicate artifact id {}", artifact.artifact_id),
                });
            }
        }
        let mut effect_ids = BTreeSet::new();
        let mut provider_effect_ids = BTreeSet::new();
        for effect in &self.external_effects {
            effect.validate()?;
            if !effect_ids.insert(effect.effect_id.as_str()) {
                return Err(AsyncOperationError::InvalidOperation {
                    operation_id: self.operation_id.clone(),
                    reason: format!("duplicate external effect id {}", effect.effect_id),
                });
            }
            if let Some(provider_effect_id) = &effect.provider_effect_id
                && !provider_effect_ids.insert(provider_effect_id.as_str())
            {
                return Err(AsyncOperationError::InvalidOperation {
                    operation_id: self.operation_id.clone(),
                    reason: format!("duplicate provider effect id {provider_effect_id}"),
                });
            }
            if effect.outcome == ToolEffectOutcome::Committed && effect.idempotency_key.is_none() {
                return Err(AsyncOperationError::InvalidOperation {
                    operation_id: self.operation_id.clone(),
                    reason: format!(
                        "committed external effect {} requires an idempotency key",
                        effect.effect_id
                    ),
                });
            }
            if effect.provider_effect_id.is_some() && effect.outcome != ToolEffectOutcome::Committed
            {
                return Err(AsyncOperationError::InvalidOperation {
                    operation_id: self.operation_id.clone(),
                    reason: format!(
                        "external effect {} has provider identity but no committed external effect",
                        effect.effect_id
                    ),
                });
            }
        }
        Ok(())
    }

    pub fn protocol_value(&self) -> Value {
        json!({
            "operationId": self.operation_id,
            "status": self.status.as_str(),
            "output": self.output,
            "artifacts": self.artifacts.iter().map(CallbackArtifactRef::canonical_value).collect::<Vec<_>>(),
            "diagnostics": self.diagnostics,
            "metrics": self.metrics,
            "checks": self.checks,
            "usage": self.usage,
            "externalEffects": self.external_effects.iter().map(ExternalEffectRecord::protocol_value).collect::<Vec<_>>(),
        })
    }
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
        payload_digest: String,
        verified_by: String,
        policy_snapshot_id: String,
    },
    CallbackResumePaused {
        operation_id: String,
        reason: String,
        occurred_at_unix_ms: u64,
    },
    CallbackResumeAuthorized {
        operation_id: String,
        policy_decision_id: String,
        budget_reservation_id: String,
        compatible_release_id: String,
        ownership_fence_token: String,
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
    CallbackIdempotencyConflict {
        operation_id: String,
        idempotency_key: String,
        field: String,
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
    receipts_by_operation_and_idempotency: BTreeMap<(String, String), ExternalCallbackReceived>,
    quarantined_callbacks: BTreeMap<(String, String), QuarantinedCallbackRecord>,
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

    pub fn quarantine_callback_before_operation_commit(
        &self,
        submission: AsyncCallbackSubmission,
        expires_at_unix_ms: u64,
    ) -> Result<QuarantinedCallback, AsyncOperationError> {
        validate_callback_submission_and_resume_decision(
            &submission,
            &AsyncCallbackResumeDecision::PauseAuthorizationRequired,
        )?;
        ensure_callback_submission_authenticated(&submission)?;
        if expires_at_unix_ms <= submission.received_at_unix_ms {
            return Err(AsyncOperationError::InvalidExpiration {
                operation_id: submission.operation_id,
                created_at_unix_ms: submission.received_at_unix_ms,
                expires_at_unix_ms,
            });
        }

        let mut inner = self
            .inner
            .lock()
            .expect("async operation store lock poisoned");
        if inner.operations.contains_key(&submission.operation_id) {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: submission.operation_id,
                reason: "operation already exists; submit callback through normal admission"
                    .to_owned(),
            });
        }

        let quarantine_key = (
            submission.operation_id.clone(),
            submission.idempotency_key.clone(),
        );
        if let Some(existing) = inner.quarantined_callbacks.get(&quarantine_key) {
            if let Some(field) =
                callback_submission_idempotency_conflict_field(&existing.submission, &submission)
            {
                return Err(AsyncOperationError::CallbackIdempotencyConflict {
                    operation_id: quarantine_key.0,
                    idempotency_key: quarantine_key.1,
                    field: field.to_owned(),
                });
            }
            return Ok(QuarantinedCallback {
                operation_id: existing.submission.operation_id.clone(),
                idempotency_key: existing.submission.idempotency_key.clone(),
                duplicate: true,
                expires_at_unix_ms: existing.expires_at_unix_ms,
            });
        }

        inner.quarantined_callbacks.insert(
            quarantine_key.clone(),
            QuarantinedCallbackRecord {
                submission,
                expires_at_unix_ms,
            },
        );
        Ok(QuarantinedCallback {
            operation_id: quarantine_key.0,
            idempotency_key: quarantine_key.1,
            duplicate: false,
            expires_at_unix_ms,
        })
    }

    pub fn quarantined_callback_count(&self, operation_id: &str) -> usize {
        let inner = self
            .inner
            .lock()
            .expect("async operation store lock poisoned");
        inner
            .quarantined_callbacks
            .keys()
            .filter(|(queued_operation_id, _)| queued_operation_id == operation_id)
            .count()
    }

    pub fn accept_quarantined_callbacks(
        &self,
        operation_id: &str,
        registry: &ToolSchemaRegistry,
    ) -> Result<Vec<AcceptedCallback>, AsyncOperationError> {
        self.accept_quarantined_callbacks_with_resume_decision(
            operation_id,
            registry,
            AsyncCallbackResumeDecision::PauseAuthorizationRequired,
        )
    }

    pub fn accept_quarantined_callbacks_with_resume_decision(
        &self,
        operation_id: &str,
        registry: &ToolSchemaRegistry,
        resume_decision: AsyncCallbackResumeDecision,
    ) -> Result<Vec<AcceptedCallback>, AsyncOperationError> {
        let submissions = {
            let mut inner = self
                .inner
                .lock()
                .expect("async operation store lock poisoned");
            let operation_created_at_unix_ms =
                if let Some(operation) = inner.operations.get(operation_id) {
                    operation.created_at_unix_ms
                } else {
                    return Err(AsyncOperationError::OperationNotFound {
                        operation_id: operation_id.to_owned(),
                    });
                };
            let keys = inner
                .quarantined_callbacks
                .keys()
                .filter(|(queued_operation_id, _)| queued_operation_id == operation_id)
                .cloned()
                .collect::<Vec<_>>();
            let mut submissions = Vec::new();
            for key in keys {
                if let Some(record) = inner.quarantined_callbacks.remove(&key) {
                    if record.expires_at_unix_ms <= operation_created_at_unix_ms {
                        let payload_digest = canonical_hash(&record.submission.payload);
                        let policy_snapshot_id = record.submission.policy_snapshot_id.clone();
                        inner
                            .events_by_operation
                            .entry(operation_id.to_owned())
                            .or_default()
                            .push(AsyncOperationEvent::ExternalCallbackRejected {
                                operation_id: record.submission.operation_id,
                                callback_id: record.submission.callback_id,
                                reason: "quarantined_callback_expired".to_owned(),
                                occurred_at_unix_ms: record.expires_at_unix_ms,
                                payload_digest,
                                verified_by: record.submission.verified_by,
                                policy_snapshot_id,
                            });
                    } else {
                        submissions.push(record.submission);
                    }
                }
            }
            submissions.sort_by(|left, right| {
                left.received_at_unix_ms
                    .cmp(&right.received_at_unix_ms)
                    .then_with(|| left.callback_id.cmp(&right.callback_id))
                    .then_with(|| left.idempotency_key.cmp(&right.idempotency_key))
            });
            submissions
        };

        let mut accepted = Vec::new();
        let mut resume_winner_seen = false;
        let mut first_error = None;
        for submission in submissions {
            if resume_winner_seen {
                self.record_callback_rejected(&submission, "quarantined_callback_superseded");
                continue;
            }
            let result = match self.accept_callback_with_resume_decision(
                submission,
                registry,
                resume_decision.clone(),
            ) {
                Ok(result) => result,
                Err(error) => {
                    if first_error.is_none() {
                        first_error = Some(error);
                    }
                    continue;
                }
            };
            if result.should_resume {
                resume_winner_seen = true;
            }
            accepted.push(result);
        }

        if accepted.is_empty()
            && let Some(error) = first_error
        {
            return Err(error);
        }

        Ok(accepted)
    }

    pub fn accept_callback(
        &self,
        submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        self.accept_callback_with_resume_decision(
            submission,
            registry,
            AsyncCallbackResumeDecision::PauseAuthorizationRequired,
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
            AsyncCallbackResumeDecision::PauseAuthorizationRequired,
        )
    }

    pub fn accept_callback_with_artifact_on_payload_limit(
        &self,
        submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
        limits: AsyncCallbackIngestionLimits,
        artifact: CallbackArtifactRef,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        self.accept_callback_with_artifact_on_payload_limit_and_resume_decision(
            submission,
            registry,
            limits,
            artifact,
            AsyncCallbackResumeDecision::PauseAuthorizationRequired,
        )
    }

    pub fn accept_callback_with_artifact_on_payload_limit_and_resume_decision(
        &self,
        mut submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
        limits: AsyncCallbackIngestionLimits,
        artifact: CallbackArtifactRef,
        resume_decision: AsyncCallbackResumeDecision,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        validate_callback_submission_and_resume_decision(&submission, &resume_decision)?;
        if let Err(error) = ensure_callback_submission_authenticated(&submission) {
            self.record_callback_rejected(&submission, "authentication_failed");
            return Err(error);
        }
        let artifacts =
            if callback_payload_size_bytes(&submission.payload) > limits.max_payload_bytes {
                artifact.validate()?;
                submission.payload =
                    compact_callback_payload_with_artifact(&submission.payload, &artifact);
                vec![artifact]
            } else {
                Vec::new()
            };
        self.accept_validated_callback_with_artifacts_and_resume_decision(
            submission,
            registry,
            artifacts,
            resume_decision,
        )
    }

    fn accept_callback_with_limits_and_resume_decision(
        &self,
        submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
        limits: AsyncCallbackIngestionLimits,
        resume_decision: AsyncCallbackResumeDecision,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        validate_callback_submission_and_resume_decision(&submission, &resume_decision)?;
        if let Err(error) = ensure_callback_submission_authenticated(&submission) {
            self.record_callback_rejected(&submission, "authentication_failed");
            return Err(error);
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

        self.accept_validated_callback_with_artifacts_and_resume_decision(
            submission,
            registry,
            Vec::new(),
            resume_decision,
        )
    }

    fn accept_validated_callback_with_artifacts_and_resume_decision(
        &self,
        submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
        artifacts: Vec<CallbackArtifactRef>,
        resume_decision: AsyncCallbackResumeDecision,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        let mut inner = self
            .inner
            .lock()
            .expect("async operation store lock poisoned");
        let receipt_key = (
            submission.operation_id.clone(),
            submission.idempotency_key.clone(),
        );
        let rejected_payload_digest = canonical_hash(&submission.payload);
        let rejected_verified_by = submission.verified_by.clone();
        let rejected_policy_snapshot_id = submission.policy_snapshot_id.clone();
        if let Some(receipt) = inner
            .receipts_by_operation_and_idempotency
            .get(&receipt_key)
        {
            if let Some(field) = callback_idempotency_conflict_field(receipt, &submission) {
                inner
                    .events_by_operation
                    .entry(submission.operation_id.clone())
                    .or_default()
                    .push(AsyncOperationEvent::ExternalCallbackRejected {
                        operation_id: submission.operation_id.clone(),
                        callback_id: submission.callback_id,
                        reason: format!("idempotency_conflict:{field}"),
                        occurred_at_unix_ms: submission.received_at_unix_ms,
                        payload_digest: rejected_payload_digest.clone(),
                        verified_by: rejected_verified_by.clone(),
                        policy_snapshot_id: rejected_policy_snapshot_id.clone(),
                    });
                return Err(AsyncOperationError::CallbackIdempotencyConflict {
                    operation_id: receipt_key.0,
                    idempotency_key: receipt_key.1,
                    field: field.to_owned(),
                });
            }
            return Ok(AcceptedCallback {
                receipt: receipt.clone(),
                duplicate: true,
                should_resume: false,
            });
        }

        let operation = if let Some(operation) = inner.operations.get(&submission.operation_id) {
            operation.clone()
        } else {
            inner
                .events_by_operation
                .entry(submission.operation_id.clone())
                .or_default()
                .push(AsyncOperationEvent::ExternalCallbackRejected {
                    operation_id: submission.operation_id.clone(),
                    callback_id: submission.callback_id,
                    reason: "operation_not_found".to_owned(),
                    occurred_at_unix_ms: submission.received_at_unix_ms,
                    payload_digest: rejected_payload_digest.clone(),
                    verified_by: rejected_verified_by.clone(),
                    policy_snapshot_id: rejected_policy_snapshot_id.clone(),
                });
            return Err(AsyncOperationError::OperationNotFound {
                operation_id: submission.operation_id,
            });
        };

        if operation.run_id != submission.run_id {
            inner
                .events_by_operation
                .entry(submission.operation_id.clone())
                .or_default()
                .push(AsyncOperationEvent::ExternalCallbackRejected {
                    operation_id: operation.operation_id.clone(),
                    callback_id: submission.callback_id,
                    reason: "identity_mismatch:run_id".to_owned(),
                    occurred_at_unix_ms: submission.received_at_unix_ms,
                    payload_digest: rejected_payload_digest.clone(),
                    verified_by: rejected_verified_by.clone(),
                    policy_snapshot_id: rejected_policy_snapshot_id.clone(),
                });
            return Err(AsyncOperationError::OperationIdentityMismatch {
                operation_id: operation.operation_id,
                field: "run_id".to_owned(),
                expected: operation.run_id,
                actual: submission.run_id,
            });
        }
        if operation.node_id != submission.node_id {
            inner
                .events_by_operation
                .entry(submission.operation_id.clone())
                .or_default()
                .push(AsyncOperationEvent::ExternalCallbackRejected {
                    operation_id: operation.operation_id.clone(),
                    callback_id: submission.callback_id,
                    reason: "identity_mismatch:node_id".to_owned(),
                    occurred_at_unix_ms: submission.received_at_unix_ms,
                    payload_digest: rejected_payload_digest.clone(),
                    verified_by: rejected_verified_by.clone(),
                    policy_snapshot_id: rejected_policy_snapshot_id.clone(),
                });
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
                    payload_digest: rejected_payload_digest.clone(),
                    verified_by: rejected_verified_by.clone(),
                    policy_snapshot_id: rejected_policy_snapshot_id.clone(),
                });
            return Err(AsyncOperationError::StaleAttempt {
                operation_id: operation.operation_id,
                expected_attempt_id: operation.attempt_id,
                actual_attempt_id: submission.attempt_id,
            });
        }
        if operation.provider_operation_id != submission.provider_operation_id {
            inner
                .events_by_operation
                .entry(submission.operation_id.clone())
                .or_default()
                .push(AsyncOperationEvent::ExternalCallbackRejected {
                    operation_id: operation.operation_id.clone(),
                    callback_id: submission.callback_id,
                    reason: "identity_mismatch:provider_operation_id".to_owned(),
                    occurred_at_unix_ms: submission.received_at_unix_ms,
                    payload_digest: rejected_payload_digest.clone(),
                    verified_by: rejected_verified_by.clone(),
                    policy_snapshot_id: rejected_policy_snapshot_id.clone(),
                });
            return Err(AsyncOperationError::OperationIdentityMismatch {
                operation_id: operation.operation_id,
                field: "provider_operation_id".to_owned(),
                expected: operation.provider_operation_id.unwrap_or_default(),
                actual: submission.provider_operation_id.unwrap_or_default(),
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
                    payload_digest: rejected_payload_digest.clone(),
                    verified_by: rejected_verified_by.clone(),
                    policy_snapshot_id: rejected_policy_snapshot_id.clone(),
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
        if !matches!(
            operation_state,
            AsyncOperationState::WaitingCallback
                | AsyncOperationState::Completed
                | AsyncOperationState::Failed
                | AsyncOperationState::Cancelled
                | AsyncOperationState::Expired
        ) {
            inner
                .events_by_operation
                .entry(submission.operation_id.clone())
                .or_default()
                .push(AsyncOperationEvent::ExternalCallbackRejected {
                    operation_id: operation_id.clone(),
                    callback_id: submission.callback_id,
                    reason: format!(
                        "operation_not_waiting_callback:{}",
                        async_operation_state_as_str(operation_state)
                    ),
                    occurred_at_unix_ms: submission.received_at_unix_ms,
                    payload_digest: rejected_payload_digest.clone(),
                    verified_by: rejected_verified_by.clone(),
                    policy_snapshot_id: rejected_policy_snapshot_id.clone(),
                });
            return Err(AsyncOperationError::OperationNotWaitingCallback {
                operation_id,
                state: operation_state,
            });
        }

        if operation_state == AsyncOperationState::WaitingCallback
            && operation
                .expires_at_unix_ms
                .is_some_and(|expires_at_unix_ms| {
                    submission.received_at_unix_ms >= expires_at_unix_ms
                })
        {
            inner
                .events_by_operation
                .entry(submission.operation_id.clone())
                .or_default()
                .push(AsyncOperationEvent::ExternalCallbackRejected {
                    operation_id: operation_id.clone(),
                    callback_id: submission.callback_id,
                    reason: "callback_after_expiration".to_owned(),
                    occurred_at_unix_ms: submission.received_at_unix_ms,
                    payload_digest: rejected_payload_digest.clone(),
                    verified_by: rejected_verified_by.clone(),
                    policy_snapshot_id: rejected_policy_snapshot_id.clone(),
                });
            return Err(AsyncOperationError::InvalidOperation {
                operation_id,
                reason: "callback received after operation expiration".to_owned(),
            });
        }

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
            artifacts,
            received_at_unix_ms: submission.received_at_unix_ms,
            verified_by: submission.verified_by,
            policy_snapshot_id: submission.policy_snapshot_id,
        };

        if matches!(
            operation_state,
            AsyncOperationState::Completed
                | AsyncOperationState::Failed
                | AsyncOperationState::Cancelled
                | AsyncOperationState::Expired
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
                .receipts_by_operation_and_idempotency
                .insert(receipt_key, receipt.clone());
            return Ok(AcceptedCallback {
                receipt,
                duplicate: false,
                should_resume: false,
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
            AsyncCallbackResumeDecision::ResumeAuthorized {
                authentication_verified,
                policy_decision_id,
                budget_reservation_id,
                compatible_release_id,
                ownership_fence_token,
            } if authentication_verified => {
                inner
                    .events_by_operation
                    .entry(submission.operation_id.clone())
                    .or_default()
                    .push(AsyncOperationEvent::CallbackResumeAuthorized {
                        operation_id: submission.operation_id.clone(),
                        policy_decision_id,
                        budget_reservation_id,
                        compatible_release_id,
                        ownership_fence_token,
                        occurred_at_unix_ms: submission.received_at_unix_ms,
                    });
                true
            }
            AsyncCallbackResumeDecision::ResumeAuthorized { .. }
            | AsyncCallbackResumeDecision::PauseAuthorizationRequired => {
                inner
                    .events_by_operation
                    .entry(submission.operation_id.clone())
                    .or_default()
                    .push(AsyncOperationEvent::CallbackResumePaused {
                        operation_id: submission.operation_id.clone(),
                        reason: "explicit callback resume authorization required".to_owned(),
                        occurred_at_unix_ms: submission.received_at_unix_ms,
                    });
                false
            }
            AsyncCallbackResumeDecision::PauseBudget { reason } => {
                inner
                    .events_by_operation
                    .entry(submission.operation_id.clone())
                    .or_default()
                    .push(AsyncOperationEvent::CallbackResumePaused {
                        operation_id: submission.operation_id.clone(),
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
                        operation_id: submission.operation_id.clone(),
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
                        operation_id: submission.operation_id.clone(),
                        reason: format!(
                            "release incompatible: required {required_release_id}, available {available_release_id}"
                        ),
                        occurred_at_unix_ms: submission.received_at_unix_ms,
                    });
                false
            }
        };
        inner
            .receipts_by_operation_and_idempotency
            .insert(receipt_key, receipt.clone());

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
        validate_terminal_transition_timestamp(operation, "cancelled_at", cancelled_at_unix_ms)?;

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
        validate_terminal_transition_timestamp(operation, "expired_at", expired_at_unix_ms)?;

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
                payload_digest: canonical_hash(&submission.payload),
                verified_by: submission.verified_by.clone(),
                policy_snapshot_id: submission.policy_snapshot_id.clone(),
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
        let mut connection = self
            .connection
            .lock()
            .expect("sqlite async operation store lock poisoned");
        connection
            .execute_batch(
                "
                PRAGMA busy_timeout = 5000;
                CREATE TABLE IF NOT EXISTS async_operations (
                    operation_id TEXT PRIMARY KEY,
                    operation_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS async_callback_receipts (
                    operation_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    PRIMARY KEY (operation_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS async_operation_events (
                    operation_id TEXT NOT NULL,
                    event_index INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (operation_id, event_index)
                );
                CREATE TABLE IF NOT EXISTS async_callback_quarantine (
                    operation_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    submission_json TEXT NOT NULL,
                    expires_at_unix_ms INTEGER NOT NULL,
                    PRIMARY KEY (operation_id, idempotency_key)
                );
                ",
            )
            .map_err(storage_error)?;
        migrate_callback_receipts_idempotency_scope(&mut connection)?;
        Ok(())
    }

    pub fn register(&self, operation: AsyncOperation) -> Result<(), AsyncOperationError> {
        self.mutate_memory_store(|memory| {
            let result = memory.register(operation);
            let persist = result.is_ok();
            (result, persist)
        })
    }

    pub fn accept_callback(
        &self,
        submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        self.accept_callback_with_resume_decision(
            submission,
            registry,
            AsyncCallbackResumeDecision::PauseAuthorizationRequired,
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
            AsyncCallbackResumeDecision::PauseAuthorizationRequired,
        )
    }

    pub fn accept_callback_with_artifact_on_payload_limit(
        &self,
        submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
        limits: AsyncCallbackIngestionLimits,
        artifact: CallbackArtifactRef,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        self.accept_callback_with_artifact_on_payload_limit_and_resume_decision(
            submission,
            registry,
            limits,
            artifact,
            AsyncCallbackResumeDecision::PauseAuthorizationRequired,
        )
    }

    pub fn accept_callback_with_artifact_on_payload_limit_and_resume_decision(
        &self,
        submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
        limits: AsyncCallbackIngestionLimits,
        artifact: CallbackArtifactRef,
        resume_decision: AsyncCallbackResumeDecision,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        self.mutate_memory_store(|memory| {
            let accepted = memory
                .accept_callback_with_artifact_on_payload_limit_and_resume_decision(
                    submission,
                    registry,
                    limits,
                    artifact,
                    resume_decision,
                );
            let persist = !matches!(&accepted, Ok(accepted) if accepted.duplicate);
            (accepted, persist)
        })
    }

    pub fn quarantine_callback_before_operation_commit(
        &self,
        submission: AsyncCallbackSubmission,
        expires_at_unix_ms: u64,
    ) -> Result<QuarantinedCallback, AsyncOperationError> {
        self.mutate_memory_store(|memory| {
            let quarantined =
                memory.quarantine_callback_before_operation_commit(submission, expires_at_unix_ms);
            let persist = !matches!(&quarantined, Ok(callback) if callback.duplicate);
            (quarantined, persist)
        })
    }

    /// Returns the number of quarantined callbacks, preserving storage failures.
    pub fn try_quarantined_callback_count(
        &self,
        operation_id: &str,
    ) -> Result<usize, AsyncOperationError> {
        Ok(self
            .load_memory_store()?
            .quarantined_callback_count(operation_id))
    }

    /// Returns the number of quarantined callbacks, or zero if storage cannot be read.
    ///
    /// Reliability-sensitive callers should use [`Self::try_quarantined_callback_count`].
    pub fn quarantined_callback_count(&self, operation_id: &str) -> usize {
        self.try_quarantined_callback_count(operation_id)
            .unwrap_or_default()
    }

    pub fn accept_quarantined_callbacks(
        &self,
        operation_id: &str,
        registry: &ToolSchemaRegistry,
    ) -> Result<Vec<AcceptedCallback>, AsyncOperationError> {
        self.accept_quarantined_callbacks_with_resume_decision(
            operation_id,
            registry,
            AsyncCallbackResumeDecision::PauseAuthorizationRequired,
        )
    }

    pub fn accept_quarantined_callbacks_with_resume_decision(
        &self,
        operation_id: &str,
        registry: &ToolSchemaRegistry,
        resume_decision: AsyncCallbackResumeDecision,
    ) -> Result<Vec<AcceptedCallback>, AsyncOperationError> {
        self.mutate_memory_store(|memory| {
            let accepted = memory.accept_quarantined_callbacks_with_resume_decision(
                operation_id,
                registry,
                resume_decision,
            );
            (accepted, true)
        })
    }

    fn accept_callback_with_limits_and_resume_decision(
        &self,
        submission: AsyncCallbackSubmission,
        registry: &ToolSchemaRegistry,
        limits: AsyncCallbackIngestionLimits,
        resume_decision: AsyncCallbackResumeDecision,
    ) -> Result<AcceptedCallback, AsyncOperationError> {
        self.mutate_memory_store(|memory| {
            let accepted = memory.accept_callback_with_limits_and_resume_decision(
                submission,
                registry,
                limits,
                resume_decision,
            );
            let persist = !matches!(&accepted, Ok(accepted) if accepted.duplicate);
            (accepted, persist)
        })
    }

    pub fn cancel_operation(
        &self,
        operation_id: &str,
        cancelled_at_unix_ms: u64,
    ) -> Result<(), AsyncOperationError> {
        self.mutate_memory_store(|memory| {
            let result = memory.cancel_operation(operation_id, cancelled_at_unix_ms);
            let persist = result.is_ok();
            (result, persist)
        })
    }

    pub fn expire_operation(
        &self,
        operation_id: &str,
        expired_at_unix_ms: u64,
    ) -> Result<(), AsyncOperationError> {
        self.mutate_memory_store(|memory| {
            let result = memory.expire_operation(operation_id, expired_at_unix_ms);
            let persist = result.is_ok();
            (result, persist)
        })
    }

    /// Returns operation events while preserving storage and decoding failures.
    pub fn try_events_for_operation(
        &self,
        operation_id: &str,
    ) -> Result<Vec<AsyncOperationEvent>, AsyncOperationError> {
        Ok(self.load_memory_store()?.events_for_operation(operation_id))
    }

    /// Returns operation events, or an empty list if storage cannot be read.
    ///
    /// Reliability-sensitive callers should use [`Self::try_events_for_operation`].
    pub fn events_for_operation(&self, operation_id: &str) -> Vec<AsyncOperationEvent> {
        self.try_events_for_operation(operation_id)
            .unwrap_or_default()
    }

    /// Returns operation state while preserving storage and decoding failures.
    pub fn try_operation_state(
        &self,
        operation_id: &str,
    ) -> Result<Option<AsyncOperationState>, AsyncOperationError> {
        Ok(self.load_memory_store()?.operation_state(operation_id))
    }

    /// Returns operation state, or `None` if storage cannot be read.
    ///
    /// Reliability-sensitive callers should use [`Self::try_operation_state`].
    pub fn operation_state(&self, operation_id: &str) -> Option<AsyncOperationState> {
        self.try_operation_state(operation_id).unwrap_or_default()
    }

    fn load_memory_store(&self) -> Result<AsyncOperationStore, AsyncOperationError> {
        let mut connection = self
            .connection
            .lock()
            .expect("sqlite async operation store lock poisoned");
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Deferred)
            .map_err(storage_error)?;
        let memory = Self::load_memory_store_from_connection(&transaction)?;
        transaction.commit().map_err(storage_error)?;
        Ok(memory)
    }

    fn mutate_memory_store<T>(
        &self,
        mutation: impl FnOnce(&AsyncOperationStore) -> (Result<T, AsyncOperationError>, bool),
    ) -> Result<T, AsyncOperationError> {
        let mut connection = self
            .connection
            .lock()
            .expect("sqlite async operation store lock poisoned");
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(storage_error)?;
        let memory = Self::load_memory_store_from_connection(&transaction)?;
        let (result, persist) = mutation(&memory);
        if persist {
            transaction
                .execute("DELETE FROM async_operation_events", [])
                .map_err(storage_error)?;
            transaction
                .execute("DELETE FROM async_callback_quarantine", [])
                .map_err(storage_error)?;
            transaction
                .execute("DELETE FROM async_callback_receipts", [])
                .map_err(storage_error)?;
            transaction
                .execute("DELETE FROM async_operations", [])
                .map_err(storage_error)?;

            let inner = memory
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
            for receipt in inner.receipts_by_operation_and_idempotency.values() {
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
            for record in inner.quarantined_callbacks.values() {
                transaction
                    .execute(
                        "
                        INSERT INTO async_callback_quarantine (
                            operation_id,
                            idempotency_key,
                            submission_json,
                            expires_at_unix_ms
                        )
                        VALUES (?, ?, ?, ?)
                        ",
                        params![
                            &record.submission.operation_id,
                            &record.submission.idempotency_key,
                            storage_json(&callback_submission_to_value(&record.submission))?,
                            u64_to_i64(
                                record.expires_at_unix_ms,
                                "quarantined callback expiration",
                            )?,
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
        }
        transaction.commit().map_err(storage_error)?;
        result
    }

    fn load_memory_store_from_connection(
        connection: &Connection,
    ) -> Result<AsyncOperationStore, AsyncOperationError> {
        let store = AsyncOperationStore::new();
        let mut inner = store
            .inner
            .lock()
            .expect("async operation store lock poisoned");

        {
            let mut statement = connection
                .prepare(
                    "
                    SELECT operation_id, operation_json
                    FROM async_operations
                    ORDER BY operation_id
                    ",
                )
                .map_err(storage_error)?;
            let operations = statement
                .query_map([], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
                })
                .map_err(storage_error)?;
            for operation_json in operations {
                let (row_operation_id, operation_json) = operation_json.map_err(storage_error)?;
                let operation = operation_from_value(parse_json(&operation_json)?)?;
                if operation.operation_id != row_operation_id {
                    return Err(AsyncOperationError::Storage {
                        message: "stored async operation identity does not match row key"
                            .to_owned(),
                    });
                }
                inner
                    .operations
                    .insert(operation.operation_id.clone(), operation);
            }
        }

        {
            let mut statement = connection
                .prepare(
                    "
                    SELECT operation_id, idempotency_key, receipt_json
                    FROM async_callback_receipts
                    ORDER BY operation_id, idempotency_key
                    ",
                )
                .map_err(storage_error)?;
            let receipts = statement
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                })
                .map_err(storage_error)?;
            for receipt_json in receipts {
                let (row_operation_id, row_idempotency_key, receipt_json) =
                    receipt_json.map_err(storage_error)?;
                let receipt = receipt_from_value(parse_json(&receipt_json)?)?;
                if receipt.operation_id != row_operation_id
                    || receipt.idempotency_key != row_idempotency_key
                {
                    return Err(AsyncOperationError::Storage {
                        message: "stored callback receipt identity does not match row key"
                            .to_owned(),
                    });
                }
                let operation = inner.operations.get(&receipt.operation_id).ok_or_else(|| {
                    AsyncOperationError::Storage {
                        message: "stored callback receipt has no matching operation".to_owned(),
                    }
                })?;
                if receipt.run_id != operation.run_id
                    || receipt.node_id != operation.node_id
                    || receipt.attempt_id != operation.attempt_id
                    || receipt.provider_operation_id != operation.provider_operation_id
                {
                    return Err(AsyncOperationError::Storage {
                        message:
                            "stored callback receipt operation metadata does not match operation"
                                .to_owned(),
                    });
                }
                inner.receipts_by_operation_and_idempotency.insert(
                    (
                        receipt.operation_id.clone(),
                        receipt.idempotency_key.clone(),
                    ),
                    receipt,
                );
            }
        }

        {
            let mut statement = connection
                .prepare(
                    "
                    SELECT operation_id, idempotency_key, submission_json, expires_at_unix_ms
                    FROM async_callback_quarantine
                    ORDER BY operation_id, idempotency_key
                    ",
                )
                .map_err(storage_error)?;
            let quarantined = statement
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, i64>(3)?,
                    ))
                })
                .map_err(storage_error)?;
            for callback in quarantined {
                let (row_operation_id, row_idempotency_key, submission_json, expires_at_unix_ms) =
                    callback.map_err(storage_error)?;
                let submission = callback_submission_from_value(parse_json(&submission_json)?)?;
                if submission.operation_id != row_operation_id
                    || submission.idempotency_key != row_idempotency_key
                {
                    return Err(AsyncOperationError::Storage {
                        message: "stored quarantined callback identity does not match row key"
                            .to_owned(),
                    });
                }
                inner.quarantined_callbacks.insert(
                    (
                        submission.operation_id.clone(),
                        submission.idempotency_key.clone(),
                    ),
                    QuarantinedCallbackRecord {
                        submission,
                        expires_at_unix_ms: sqlite_i64_to_u64(
                            expires_at_unix_ms,
                            "quarantined callback expiration",
                        )?,
                    },
                );
            }
        }

        {
            let mut statement = connection
                .prepare(
                    "
                    SELECT operation_id, event_index, event_json
                    FROM async_operation_events
                    ORDER BY operation_id, event_index
                    ",
                )
                .map_err(storage_error)?;
            let events = statement
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                })
                .map_err(storage_error)?;
            let mut expected_event_indexes: BTreeMap<String, i64> = BTreeMap::new();
            for event in events {
                let (operation_id, event_index, event_json) = event.map_err(storage_error)?;
                match expected_event_indexes.entry(operation_id.clone()) {
                    Entry::Vacant(entry) if event_index == 0 => {
                        entry.insert(1);
                    }
                    Entry::Occupied(mut entry) if event_index == *entry.get() => {
                        *entry.get_mut() += 1;
                    }
                    _ => {
                        return Err(AsyncOperationError::Storage {
                            message: "stored async operation event index is not contiguous"
                                .to_owned(),
                        });
                    }
                }
                let event = event_from_value(parse_json(&event_json)?)?;
                if event_operation_id(&event) != operation_id {
                    return Err(AsyncOperationError::Storage {
                        message: "stored async operation event identity does not match row key"
                            .to_owned(),
                    });
                }
                inner
                    .events_by_operation
                    .entry(operation_id)
                    .or_default()
                    .push(event);
            }
        }
        drop(inner);
        Ok(store)
    }
}

fn callback_payload_size_bytes(payload: &Value) -> usize {
    graphblocks_compiler::canonical::canonical_json(payload).len()
}

fn callback_idempotency_conflict_field(
    receipt: &ExternalCallbackReceived,
    submission: &AsyncCallbackSubmission,
) -> Option<&'static str> {
    if receipt.operation_id != submission.operation_id {
        return Some("operation_id");
    }
    if receipt.run_id != submission.run_id {
        return Some("run_id");
    }
    if receipt.node_id != submission.node_id {
        return Some("node_id");
    }
    if receipt.attempt_id != submission.attempt_id {
        return Some("attempt_id");
    }
    if receipt.provider_operation_id != submission.provider_operation_id {
        return Some("provider_operation_id");
    }
    if receipt.idempotency_key != submission.idempotency_key {
        return Some("idempotency_key");
    }
    if receipt.payload_digest != canonical_hash(&submission.payload) {
        return Some("payload_digest");
    }
    if receipt.verified_by != submission.verified_by {
        return Some("verified_by");
    }
    if receipt.policy_snapshot_id != submission.policy_snapshot_id {
        return Some("policy_snapshot_id");
    }
    None
}

fn callback_submission_idempotency_conflict_field(
    existing: &AsyncCallbackSubmission,
    incoming: &AsyncCallbackSubmission,
) -> Option<&'static str> {
    if existing.operation_id != incoming.operation_id {
        return Some("operation_id");
    }
    if existing.run_id != incoming.run_id {
        return Some("run_id");
    }
    if existing.node_id != incoming.node_id {
        return Some("node_id");
    }
    if existing.attempt_id != incoming.attempt_id {
        return Some("attempt_id");
    }
    if existing.provider_operation_id != incoming.provider_operation_id {
        return Some("provider_operation_id");
    }
    if existing.idempotency_key != incoming.idempotency_key {
        return Some("idempotency_key");
    }
    if canonical_hash(&existing.payload) != canonical_hash(&incoming.payload) {
        return Some("payload_digest");
    }
    if existing.verified_by != incoming.verified_by {
        return Some("verified_by");
    }
    if existing.policy_snapshot_id != incoming.policy_snapshot_id {
        return Some("policy_snapshot_id");
    }
    None
}

fn callback_resume_binding_key(
    tenant_id: Option<&str>,
    release_id: &str,
    run_id: &str,
    node_id: &str,
    attempt_id: &str,
    operation_id: &str,
) -> String {
    canonical_hash(&json!({
        "tenant_id": tenant_id.unwrap_or(""),
        "release_id": release_id,
        "run_id": run_id,
        "node_id": node_id,
        "attempt_id": attempt_id,
        "operation_id": operation_id,
    }))
}

fn migrate_callback_receipts_idempotency_scope(
    connection: &mut Connection,
) -> Result<(), AsyncOperationError> {
    let mut statement = connection
        .prepare("PRAGMA table_info(async_callback_receipts)")
        .map_err(storage_error)?;
    let columns = statement
        .query_map([], |row| {
            Ok((row.get::<_, String>(1)?, row.get::<_, i64>(5)?))
        })
        .map_err(storage_error)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(storage_error)?;
    drop(statement);

    let operation_pk = columns
        .iter()
        .find_map(|(name, pk)| (name == "operation_id").then_some(*pk))
        .unwrap_or(0);
    let idempotency_pk = columns
        .iter()
        .find_map(|(name, pk)| (name == "idempotency_key").then_some(*pk))
        .unwrap_or(0);
    if !(operation_pk == 0 && idempotency_pk > 0) {
        return Ok(());
    }

    let transaction = connection.transaction().map_err(storage_error)?;
    transaction
        .execute_batch(
            "
            ALTER TABLE async_callback_receipts RENAME TO async_callback_receipts_global_key;
            CREATE TABLE async_callback_receipts (
                operation_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                PRIMARY KEY (operation_id, idempotency_key)
            );
            INSERT OR IGNORE INTO async_callback_receipts (
                operation_id,
                idempotency_key,
                receipt_json
            )
            SELECT operation_id, idempotency_key, receipt_json
            FROM async_callback_receipts_global_key;
            DROP TABLE async_callback_receipts_global_key;
            ",
        )
        .map_err(storage_error)?;
    transaction.commit().map_err(storage_error)?;
    Ok(())
}

fn validate_callback_submission_and_resume_decision(
    submission: &AsyncCallbackSubmission,
    resume_decision: &AsyncCallbackResumeDecision,
) -> Result<(), AsyncOperationError> {
    validate_callback_submission_identity(submission)?;
    match resume_decision {
        AsyncCallbackResumeDecision::ResumeAuthorized {
            policy_decision_id,
            budget_reservation_id,
            compatible_release_id,
            ownership_fence_token,
            ..
        } => {
            for (field, value) in [
                ("resume_policy_decision_id", policy_decision_id),
                ("resume_budget_reservation_id", budget_reservation_id),
                ("resume_compatible_release_id", compatible_release_id),
                ("resume_ownership_fence_token", ownership_fence_token),
            ] {
                if value.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: field.to_owned(),
                    });
                }
            }
        }
        AsyncCallbackResumeDecision::PauseAuthorizationRequired => {}
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
    Ok(())
}

fn validate_terminal_transition_timestamp(
    operation: &AsyncOperation,
    field: &'static str,
    terminal_at_unix_ms: u64,
) -> Result<(), AsyncOperationError> {
    if terminal_at_unix_ms == 0 {
        return Err(AsyncOperationError::InvalidOperation {
            operation_id: operation.operation_id.clone(),
            reason: format!("{field} must be positive"),
        });
    }
    if let Some(submitted_at_unix_ms) = operation.submitted_at_unix_ms
        && terminal_at_unix_ms < submitted_at_unix_ms
    {
        return Err(AsyncOperationError::InvalidOperation {
            operation_id: operation.operation_id.clone(),
            reason: format!("{field} precedes submitted_at"),
        });
    }
    Ok(())
}

fn validate_callback_submission_identity(
    submission: &AsyncCallbackSubmission,
) -> Result<(), AsyncOperationError> {
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
        if value.trim().is_empty() {
            return Err(AsyncOperationError::EmptyField {
                field: field.to_owned(),
            });
        }
    }
    if submission
        .provider_operation_id
        .as_ref()
        .is_some_and(|provider_operation_id| provider_operation_id.trim().is_empty())
    {
        return Err(AsyncOperationError::EmptyField {
            field: "provider_operation_id".to_owned(),
        });
    }
    if submission.received_at_unix_ms == 0 {
        return Err(AsyncOperationError::InvalidOperation {
            operation_id: submission.operation_id.clone(),
            reason: "callback received_at_unix_ms must be non-zero".to_owned(),
        });
    }
    Ok(())
}

fn ensure_callback_submission_authenticated(
    submission: &AsyncCallbackSubmission,
) -> Result<(), AsyncOperationError> {
    if submission
        .verified_by
        .trim()
        .eq_ignore_ascii_case("unauthenticated")
    {
        return Err(callback_auth_failed(
            "async_callback",
            "unauthenticated_callback",
        ));
    }
    Ok(())
}

fn compact_callback_payload_with_artifact(
    payload: &Value,
    artifact: &CallbackArtifactRef,
) -> Value {
    let mut compact = serde_json::Map::new();
    if let Some(object) = payload.as_object() {
        for (key, value) in object {
            if callback_payload_size_bytes(value) <= 256
                && !matches!(
                    key.as_str(),
                    "log" | "logs" | "output" | "stdout" | "stderr"
                )
            {
                compact.insert(key.clone(), value.clone());
            }
        }
    }
    compact.insert("artifact".to_owned(), artifact.canonical_value());
    Value::Object(compact)
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
        "infinite_wait_policy": operation.infinite_wait_policy,
        "completed_at_unix_ms": operation.completed_at_unix_ms,
        "expected_callback_payload_bytes": operation.expected_callback_payload_bytes,
        "resume_policy_reevaluation": operation.resume_policy_reevaluation,
        "callback_attempt_fencing": operation.callback_attempt_fencing,
        "resume_ownership_fence": operation.resume_ownership_fence,
    })
}

fn operation_from_value(value: Value) -> Result<AsyncOperation, AsyncOperationError> {
    let operation = AsyncOperation {
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
        infinite_wait_policy: optional_string(&value, "infinite_wait_policy")?,
        completed_at_unix_ms: optional_u64(&value, "completed_at_unix_ms")?,
        expected_callback_payload_bytes: optional_usize(&value, "expected_callback_payload_bytes")?,
        resume_policy_reevaluation: optional_bool(&value, "resume_policy_reevaluation")?
            .unwrap_or(true),
        callback_attempt_fencing: optional_bool(&value, "callback_attempt_fencing")?
            .unwrap_or(true),
        resume_ownership_fence: optional_bool(&value, "resume_ownership_fence")?.unwrap_or(true),
    };
    operation.validate()?;
    Ok(operation)
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
        "artifacts": receipt.artifacts.iter().map(callback_artifact_to_value).collect::<Vec<_>>(),
        "received_at_unix_ms": receipt.received_at_unix_ms,
        "verified_by": receipt.verified_by,
        "policy_snapshot_id": receipt.policy_snapshot_id,
    })
}

fn receipt_from_value(value: Value) -> Result<ExternalCallbackReceived, AsyncOperationError> {
    let receipt = ExternalCallbackReceived {
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
        artifacts: callback_artifacts_from_value(value.get("artifacts"))?,
        received_at_unix_ms: required_u64(&value, "received_at_unix_ms")?,
        verified_by: required_string(&value, "verified_by")?,
        policy_snapshot_id: required_string(&value, "policy_snapshot_id")?,
    };
    if receipt.payload_digest != receipt.compute_payload_digest() {
        return Err(AsyncOperationError::Storage {
            message: "stored callback receipt payload_digest does not match payload".to_owned(),
        });
    }
    for (field, value) in [
        ("callback_id", &receipt.callback_id),
        ("operation_id", &receipt.operation_id),
        ("run_id", &receipt.run_id),
        ("node_id", &receipt.node_id),
        ("attempt_id", &receipt.attempt_id),
        ("idempotency_key", &receipt.idempotency_key),
        ("payload_digest", &receipt.payload_digest),
        ("verified_by", &receipt.verified_by),
        ("policy_snapshot_id", &receipt.policy_snapshot_id),
    ] {
        if value.trim().is_empty() {
            return Err(AsyncOperationError::EmptyField {
                field: field.to_owned(),
            });
        }
    }
    if receipt
        .provider_operation_id
        .as_ref()
        .is_some_and(|provider_operation_id| provider_operation_id.trim().is_empty())
    {
        return Err(AsyncOperationError::EmptyField {
            field: "provider_operation_id".to_owned(),
        });
    }
    if receipt.received_at_unix_ms == 0 {
        return Err(AsyncOperationError::InvalidOperation {
            operation_id: receipt.operation_id.clone(),
            reason: "callback receipt received_at_unix_ms must be non-zero".to_owned(),
        });
    }
    let mut artifact_ids = BTreeSet::new();
    for artifact in &receipt.artifacts {
        if !artifact_ids.insert(artifact.artifact_id.as_str()) {
            return Err(AsyncOperationError::InvalidOperation {
                operation_id: receipt.operation_id.clone(),
                reason: format!("duplicate callback artifact id {}", artifact.artifact_id),
            });
        }
    }
    Ok(receipt)
}

fn callback_submission_to_value(submission: &AsyncCallbackSubmission) -> Value {
    json!({
        "callback_id": submission.callback_id,
        "operation_id": submission.operation_id,
        "run_id": submission.run_id,
        "node_id": submission.node_id,
        "attempt_id": submission.attempt_id,
        "provider_operation_id": submission.provider_operation_id,
        "idempotency_key": submission.idempotency_key,
        "payload": submission.payload,
        "received_at_unix_ms": submission.received_at_unix_ms,
        "verified_by": submission.verified_by,
        "policy_snapshot_id": submission.policy_snapshot_id,
    })
}

fn callback_submission_from_value(
    value: Value,
) -> Result<AsyncCallbackSubmission, AsyncOperationError> {
    let submission = AsyncCallbackSubmission {
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
                message: "stored callback submission is missing payload".to_owned(),
            })?,
        received_at_unix_ms: required_u64(&value, "received_at_unix_ms")?,
        verified_by: required_string(&value, "verified_by")?,
        policy_snapshot_id: required_string(&value, "policy_snapshot_id")?,
    };
    validate_callback_submission_identity(&submission)?;
    Ok(submission)
}

fn callback_artifact_to_value(artifact: &CallbackArtifactRef) -> Value {
    artifact.canonical_value()
}

fn callback_artifacts_from_value(
    value: Option<&Value>,
) -> Result<Vec<CallbackArtifactRef>, AsyncOperationError> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let Some(items) = value.as_array() else {
        return Err(AsyncOperationError::Storage {
            message: "stored callback receipt artifacts must be a list".to_owned(),
        });
    };
    items
        .iter()
        .map(|item| {
            let artifact = CallbackArtifactRef {
                artifact_id: required_string(item, "artifact_id")?,
                uri: required_string(item, "uri")?,
                media_type: optional_string(item, "media_type")?,
                checksum: optional_string(item, "checksum")?,
            };
            artifact.validate()?;
            Ok(artifact)
        })
        .collect()
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
            payload_digest,
            verified_by,
            policy_snapshot_id,
        } => json!({
            "type": "ExternalCallbackRejected",
            "operation_id": operation_id,
            "callback_id": callback_id,
            "reason": reason,
            "occurred_at_unix_ms": occurred_at_unix_ms,
            "payload_digest": payload_digest,
            "verified_by": verified_by,
            "policy_snapshot_id": policy_snapshot_id,
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
        AsyncOperationEvent::CallbackResumeAuthorized {
            operation_id,
            policy_decision_id,
            budget_reservation_id,
            compatible_release_id,
            ownership_fence_token,
            occurred_at_unix_ms,
        } => json!({
            "type": "CallbackResumeAuthorized",
            "operation_id": operation_id,
            "policy_decision_id": policy_decision_id,
            "budget_reservation_id": budget_reservation_id,
            "compatible_release_id": compatible_release_id,
            "ownership_fence_token": ownership_fence_token,
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

fn event_operation_id(event: &AsyncOperationEvent) -> &str {
    match event {
        AsyncOperationEvent::StateChanged { operation_id, .. }
        | AsyncOperationEvent::ExternalCallbackRejected { operation_id, .. }
        | AsyncOperationEvent::CallbackResumePaused { operation_id, .. }
        | AsyncOperationEvent::CallbackResumeAuthorized { operation_id, .. }
        | AsyncOperationEvent::CallbackResumeDenied { operation_id, .. } => operation_id,
        AsyncOperationEvent::ExternalCallbackReceived { receipt }
        | AsyncOperationEvent::LateExternalCallbackReceived { receipt, .. } => {
            &receipt.operation_id
        }
    }
}

fn event_from_value(value: Value) -> Result<AsyncOperationEvent, AsyncOperationError> {
    match required_string(&value, "type")?.as_str() {
        "StateChanged" => {
            let operation_id = required_string(&value, "operation_id")?;
            if operation_id.trim().is_empty() {
                return Err(AsyncOperationError::EmptyField {
                    field: "operation_id".to_owned(),
                });
            }
            let occurred_at_unix_ms = required_u64(&value, "occurred_at_unix_ms")?;
            if occurred_at_unix_ms == 0 {
                return Err(AsyncOperationError::Storage {
                    message: "stored async operation event occurred_at_unix_ms must be non-zero"
                        .to_owned(),
                });
            }
            Ok(AsyncOperationEvent::StateChanged {
                operation_id,
                from: async_operation_state_from_str(&required_string(&value, "from")?)?,
                to: async_operation_state_from_str(&required_string(&value, "to")?)?,
                occurred_at_unix_ms,
            })
        }
        "ExternalCallbackReceived" => Ok(AsyncOperationEvent::ExternalCallbackReceived {
            receipt: receipt_from_value(required_value(&value, "receipt")?)?,
        }),
        "ExternalCallbackRejected" => {
            let operation_id = required_string(&value, "operation_id")?;
            let callback_id = required_string(&value, "callback_id")?;
            let reason = required_string(&value, "reason")?;
            let occurred_at_unix_ms = required_u64(&value, "occurred_at_unix_ms")?;
            let payload_digest = required_string(&value, "payload_digest")?;
            let verified_by = required_string(&value, "verified_by")?;
            let policy_snapshot_id = required_string(&value, "policy_snapshot_id")?;
            for (field, value) in [
                ("operation_id", &operation_id),
                ("callback_id", &callback_id),
                ("reason", &reason),
                ("payload_digest", &payload_digest),
                ("verified_by", &verified_by),
                ("policy_snapshot_id", &policy_snapshot_id),
            ] {
                if value.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: field.to_owned(),
                    });
                }
            }
            if occurred_at_unix_ms == 0 {
                return Err(AsyncOperationError::Storage {
                    message: "stored async operation event occurred_at_unix_ms must be non-zero"
                        .to_owned(),
                });
            }
            Ok(AsyncOperationEvent::ExternalCallbackRejected {
                operation_id,
                callback_id,
                reason,
                occurred_at_unix_ms,
                payload_digest,
                verified_by,
                policy_snapshot_id,
            })
        }
        "CallbackResumePaused" => {
            let operation_id = required_string(&value, "operation_id")?;
            let reason = required_string(&value, "reason")?;
            let occurred_at_unix_ms = required_u64(&value, "occurred_at_unix_ms")?;
            for (field, value) in [("operation_id", &operation_id), ("reason", &reason)] {
                if value.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: field.to_owned(),
                    });
                }
            }
            if occurred_at_unix_ms == 0 {
                return Err(AsyncOperationError::Storage {
                    message: "stored async operation event occurred_at_unix_ms must be non-zero"
                        .to_owned(),
                });
            }
            Ok(AsyncOperationEvent::CallbackResumePaused {
                operation_id,
                reason,
                occurred_at_unix_ms,
            })
        }
        "CallbackResumeAuthorized" => {
            let operation_id = required_string(&value, "operation_id")?;
            let policy_decision_id = required_string(&value, "policy_decision_id")?;
            let budget_reservation_id = required_string(&value, "budget_reservation_id")?;
            let compatible_release_id = required_string(&value, "compatible_release_id")?;
            let ownership_fence_token = required_string(&value, "ownership_fence_token")?;
            let occurred_at_unix_ms = required_u64(&value, "occurred_at_unix_ms")?;
            for (field, value) in [
                ("operation_id", &operation_id),
                ("policy_decision_id", &policy_decision_id),
                ("budget_reservation_id", &budget_reservation_id),
                ("compatible_release_id", &compatible_release_id),
                ("ownership_fence_token", &ownership_fence_token),
            ] {
                if value.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: field.to_owned(),
                    });
                }
            }
            if occurred_at_unix_ms == 0 {
                return Err(AsyncOperationError::Storage {
                    message: "stored async operation event occurred_at_unix_ms must be non-zero"
                        .to_owned(),
                });
            }
            Ok(AsyncOperationEvent::CallbackResumeAuthorized {
                operation_id,
                policy_decision_id,
                budget_reservation_id,
                compatible_release_id,
                ownership_fence_token,
                occurred_at_unix_ms,
            })
        }
        "CallbackResumeDenied" => {
            let operation_id = required_string(&value, "operation_id")?;
            let decision_id = required_string(&value, "decision_id")?;
            let reason = required_string(&value, "reason")?;
            let occurred_at_unix_ms = required_u64(&value, "occurred_at_unix_ms")?;
            for (field, value) in [
                ("operation_id", &operation_id),
                ("decision_id", &decision_id),
                ("reason", &reason),
            ] {
                if value.trim().is_empty() {
                    return Err(AsyncOperationError::EmptyField {
                        field: field.to_owned(),
                    });
                }
            }
            if occurred_at_unix_ms == 0 {
                return Err(AsyncOperationError::Storage {
                    message: "stored async operation event occurred_at_unix_ms must be non-zero"
                        .to_owned(),
                });
            }
            Ok(AsyncOperationEvent::CallbackResumeDenied {
                operation_id,
                decision_id,
                reason,
                occurred_at_unix_ms,
            })
        }
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

fn u64_to_i64(value: u64, label: &'static str) -> Result<i64, AsyncOperationError> {
    i64::try_from(value).map_err(|_| AsyncOperationError::Storage {
        message: format!("{label} exceeds sqlite integer range"),
    })
}

fn sqlite_i64_to_u64(value: i64, label: &'static str) -> Result<u64, AsyncOperationError> {
    u64::try_from(value).map_err(|_| AsyncOperationError::Storage {
        message: format!("{label} is negative"),
    })
}

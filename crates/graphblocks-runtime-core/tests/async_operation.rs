use std::sync::{Arc, Barrier};
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

use graphblocks_runtime_core::async_operation::{
    AsyncCallbackIngestionLimits, AsyncCallbackResumeDecision, AsyncCallbackSubmission,
    AsyncOperation, AsyncOperationConfigurationDiagnostic, AsyncOperationError,
    AsyncOperationEvent, AsyncOperationKind, AsyncOperationResult, AsyncOperationResultStatus,
    AsyncOperationState, AsyncOperationStore, CallbackArtifactRef, CallbackEndpointAuth,
    CallbackEndpointRef, ExternalEffectRecord, SqliteAsyncOperationStore,
};
use graphblocks_runtime_core::tool_result::ToolEffectOutcome;
use graphblocks_runtime_core::tool_schema::{JsonSchema, JsonSchemaNode, ToolSchemaRegistry};
use serde_json::json;
use std::collections::BTreeMap;
use std::path::PathBuf;

fn sqlite_async_operation_path(label: &str) -> PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "graphblocks-async-operation-{label}-{unique}.sqlite3"
    ))
}

fn callback_schema_registry() -> ToolSchemaRegistry {
    ToolSchemaRegistry::new([JsonSchema::new(
        "schemas/CICallback@1",
        JsonSchemaNode::object()
            .required_property("status", JsonSchemaNode::string())
            .required_property("workflow_run_id", JsonSchemaNode::string()),
    )])
    .expect("schema registry should be valid")
}

fn waiting_operation() -> AsyncOperation {
    AsyncOperation::new(
        "op-1",
        "run-1",
        "node-ci",
        "attempt-1",
        AsyncOperationKind::CiJob,
        "sha256:resume-token",
        "idem-op-1",
        "schemas/CICallback@1",
        1_000,
    )
    .submitted("gha-run-1", 1_050)
    .waiting_callback(2_000)
}

fn late_committed_waiting_operation() -> AsyncOperation {
    AsyncOperation::new(
        "op-1",
        "run-1",
        "node-ci",
        "attempt-1",
        AsyncOperationKind::CiJob,
        "sha256:resume-token",
        "idem-op-1",
        "schemas/CICallback@1",
        1_300,
    )
    .submitted("gha-run-1", 1_350)
    .waiting_callback(2_000)
}

fn valid_submission(callback_id: &str, idempotency_key: &str) -> AsyncCallbackSubmission {
    AsyncCallbackSubmission::new(
        callback_id,
        "op-1",
        "run-1",
        "node-ci",
        "attempt-1",
        idempotency_key,
        json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
        1_200,
        "hmac:callback-endpoint-1",
        "policy-snapshot-1",
    )
}

#[test]
fn bearer_callback_endpoint_authenticates_and_builds_submission() {
    let endpoint = CallbackEndpointRef::new(
        "callback-endpoint-1",
        "https://graphblocks.example.com/v1/callbacks/op-1",
        "schemas/CICallback@1",
        CallbackEndpointAuth::bearer("secret://callbacks/op-1", "top-secret"),
    )
    .expect("endpoint is valid");
    let mut headers = BTreeMap::new();
    headers.insert("Authorization".to_owned(), "Bearer top-secret".to_owned());

    let submission = endpoint
        .authenticate_and_build_submission(
            "cb-1",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-1",
            json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
            1_200,
            "policy-snapshot-1",
            &headers,
        )
        .expect("bearer token authenticates");

    assert_eq!(submission.verified_by, "bearer:callback-endpoint-1");
    assert_eq!(submission.operation_id, "op-1");
    assert_eq!(submission.policy_snapshot_id, "policy-snapshot-1");

    headers.insert("Authorization".to_owned(), "Bearer wrong".to_owned());
    assert_eq!(
        endpoint.authenticate_and_build_submission(
            "cb-2",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-2",
            json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
            1_201,
            "policy-snapshot-1",
            &headers,
        ),
        Err(AsyncOperationError::CallbackAuthenticationFailed {
            endpoint_id: "callback-endpoint-1".to_owned(),
            reason: "bearer_token_mismatch".to_owned(),
        })
    );
}

#[test]
fn callback_endpoint_binds_submission_to_current_operation_identity() {
    let endpoint = CallbackEndpointRef::new_bound(
        "callback-endpoint-1",
        "https://graphblocks.example.com/v1/callbacks/op-1",
        "schemas/CICallback@1",
        CallbackEndpointAuth::bearer("secret://callbacks/op-1", "top-secret"),
        "op-1",
        "run-1",
        "node-ci",
        "attempt-1",
        "release-1",
        Some("tenant-1"),
    )
    .expect("endpoint is valid");
    let binding_key = endpoint.binding_key();
    let mut headers = BTreeMap::new();
    headers.insert("Authorization".to_owned(), "Bearer top-secret".to_owned());

    let submission = endpoint
        .authenticate_and_build_submission(
            "cb-1",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-1",
            json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
            1_200,
            "policy-snapshot-1",
            &headers,
        )
        .expect("matching callback identity authenticates");

    assert_eq!(endpoint.binding_key(), binding_key);
    assert_eq!(endpoint.receipt_binding_key(&submission), binding_key);

    assert_eq!(
        endpoint.authenticate_and_build_submission(
            "cb-stale",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-old",
            "idem-cb-stale",
            json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
            1_201,
            "policy-snapshot-1",
            &headers,
        ),
        Err(AsyncOperationError::CallbackAuthenticationFailed {
            endpoint_id: "callback-endpoint-1".to_owned(),
            reason: "callback_binding_mismatch:attempt_id".to_owned(),
        })
    );
}

#[test]
fn callback_endpoint_rejects_partial_operation_binding() {
    let mut endpoint = CallbackEndpointRef::new(
        "callback-endpoint-1",
        "https://graphblocks.example.com/v1/callbacks/op-1",
        "schemas/CICallback@1",
        CallbackEndpointAuth::bearer("secret://callbacks/op-1", "top-secret"),
    )
    .expect("unbound endpoint remains valid");
    endpoint.operation_id = Some("op-1".to_owned());
    endpoint.run_id = Some("run-1".to_owned());
    endpoint.node_id = Some("node-ci".to_owned());
    endpoint.release_id = Some("release-1".to_owned());

    assert_eq!(
        endpoint.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "callback-endpoint-1".to_owned(),
            reason: "callback endpoint binding must include operation_id, run_id, node_id, attempt_id, and release_id together".to_owned(),
        })
    );
}

#[test]
fn callback_endpoint_rejects_zero_expiration() {
    let endpoint = CallbackEndpointRef::new(
        "callback-endpoint-1",
        "https://graphblocks.example.com/v1/callbacks/op-1",
        "schemas/CICallback@1",
        CallbackEndpointAuth::bearer("secret://callbacks/op-1", "top-secret"),
    )
    .expect("endpoint is valid")
    .with_expiration(0);

    assert_eq!(
        endpoint.validate(),
        Err(AsyncOperationError::InvalidExpiration {
            operation_id: "callback-endpoint-1".to_owned(),
            created_at_unix_ms: 0,
            expires_at_unix_ms: 0,
        })
    );
}

#[test]
fn hmac_callback_endpoint_authenticates_and_rejects_replay_or_tampering() {
    let auth = CallbackEndpointAuth::hmac_sha256("secret://callbacks/op-1", b"top-secret", 300_000)
        .expect("hmac auth is valid");
    let endpoint = CallbackEndpointRef::new(
        "callback-endpoint-1",
        "https://graphblocks.example.com/v1/callbacks/op-1",
        "schemas/CICallback@1",
        auth,
    )
    .expect("endpoint is valid");
    let payload = json!({"status": "completed", "workflow_run_id": "gha-run-1"});
    let headers = endpoint
        .sign_callback_headers(1_200, &payload)
        .expect("headers sign");

    let submission = endpoint
        .authenticate_and_build_submission(
            "cb-1",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-1",
            payload.clone(),
            1_200,
            "policy-snapshot-1",
            &headers,
        )
        .expect("hmac signature authenticates");

    assert_eq!(submission.verified_by, "hmac-sha256:callback-endpoint-1");

    assert_eq!(
        endpoint.authenticate_and_build_submission(
            "cb-2",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-2",
            json!({"status": "failed", "workflow_run_id": "gha-run-1"}),
            1_201,
            "policy-snapshot-1",
            &headers,
        ),
        Err(AsyncOperationError::CallbackAuthenticationFailed {
            endpoint_id: "callback-endpoint-1".to_owned(),
            reason: "signature_mismatch".to_owned(),
        })
    );
    assert_eq!(
        endpoint.authenticate_and_build_submission(
            "cb-3",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-3",
            payload,
            401_201,
            "policy-snapshot-1",
            &headers,
        ),
        Err(AsyncOperationError::CallbackAuthenticationFailed {
            endpoint_id: "callback-endpoint-1".to_owned(),
            reason: "timestamp_outside_replay_window".to_owned(),
        })
    );
}

#[test]
fn callback_endpoint_rejects_whitespace_identity_fields_before_building_submission() {
    let endpoint = CallbackEndpointRef::new(
        "callback-endpoint-1",
        "https://graphblocks.example.com/v1/callbacks/op-1",
        "schemas/CICallback@1",
        CallbackEndpointAuth::hmac_sha256("secret://callbacks/op-1", b"top-secret", 300_000)
            .expect("hmac auth is valid"),
    )
    .expect("endpoint is valid");
    let payload = json!({"status": "completed", "workflow_run_id": "gha-run-1"});
    let headers = endpoint
        .sign_callback_headers(1_200, &payload)
        .expect("headers sign");

    for field in [
        "callback_id",
        "operation_id",
        "run_id",
        "node_id",
        "attempt_id",
        "idempotency_key",
        "policy_snapshot_id",
    ] {
        let callback_id = if field == "callback_id" {
            " \t"
        } else {
            "cb-1"
        };
        let operation_id = if field == "operation_id" {
            " \t"
        } else {
            "op-1"
        };
        let run_id = if field == "run_id" { " \t" } else { "run-1" };
        let node_id = if field == "node_id" {
            " \t"
        } else {
            "node-ci"
        };
        let attempt_id = if field == "attempt_id" {
            " \t"
        } else {
            "attempt-1"
        };
        let idempotency_key = if field == "idempotency_key" {
            " \t"
        } else {
            "idem-cb-1"
        };
        let policy_snapshot_id = if field == "policy_snapshot_id" {
            " \t"
        } else {
            "policy-snapshot-1"
        };

        assert_eq!(
            endpoint.authenticate_and_build_submission(
                callback_id,
                operation_id,
                run_id,
                node_id,
                attempt_id,
                idempotency_key,
                payload.clone(),
                1_200,
                policy_snapshot_id,
                &headers,
            ),
            Err(AsyncOperationError::EmptyField {
                field: field.to_owned(),
            }),
            "{field} should reject whitespace-only values at callback endpoint boundary",
        );
    }
}

#[test]
fn ed25519_callback_endpoint_authenticates_with_injected_verifier() {
    let endpoint = CallbackEndpointRef::new(
        "callback-endpoint-ed25519",
        "https://graphblocks.example.com/v1/callbacks/op-1",
        "schemas/CICallback@1",
        CallbackEndpointAuth::ed25519(
            "key://callbacks/op-1",
            "public-key-1",
            "GraphBlocks-Timestamp",
            "GraphBlocks-Signature",
            300_000,
        )
        .expect("ed25519 auth is valid"),
    )
    .expect("endpoint is valid");
    let payload = json!({"workflow_run_id": "gha-run-1", "status": "completed"});
    let mut headers = BTreeMap::new();
    headers.insert("GraphBlocks-Timestamp".to_owned(), "1200".to_owned());
    headers.insert("GraphBlocks-Signature".to_owned(), "sig-ok".to_owned());

    let submission = endpoint
        .authenticate_ed25519_and_build_submission(
            "cb-1",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-1",
            payload.clone(),
            1_250,
            "policy-snapshot-1",
            &headers,
            |public_key, message, signature| {
                assert_eq!(public_key, "public-key-1");
                assert_eq!(signature, "sig-ok");
                assert!(message.contains("\"status\":\"completed\""));
                true
            },
        )
        .expect("ed25519 signature authenticates");

    assert_eq!(submission.verified_by, "ed25519:callback-endpoint-ed25519");
    assert_eq!(submission.operation_id, "op-1");

    headers.insert("GraphBlocks-Signature".to_owned(), "sig-bad".to_owned());
    assert_eq!(
        endpoint.authenticate_ed25519_and_build_submission(
            "cb-2",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-2",
            payload,
            1_250,
            "policy-snapshot-1",
            &headers,
            |_public_key, _message, _signature| false,
        ),
        Err(AsyncOperationError::CallbackAuthenticationFailed {
            endpoint_id: "callback-endpoint-ed25519".to_owned(),
            reason: "signature_mismatch".to_owned(),
        })
    );
}

#[test]
fn mtls_callback_endpoint_authenticates_with_bound_client_identity() {
    let endpoint = CallbackEndpointRef::new(
        "callback-endpoint-mtls",
        "https://graphblocks.example.com/v1/callbacks/op-1",
        "schemas/CICallback@1",
        CallbackEndpointAuth::mtls("spiffe://tenant-a/provider/ci"),
    )
    .expect("endpoint is valid");
    let headers = BTreeMap::new();

    let submission = endpoint
        .authenticate_mtls_and_build_submission(
            "cb-1",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-1",
            json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
            1_250,
            "policy-snapshot-1",
            &headers,
            Some("spiffe://tenant-a/provider/ci"),
        )
        .expect("mtls identity authenticates");

    assert_eq!(submission.verified_by, "mtls:callback-endpoint-mtls");
    assert_eq!(submission.operation_id, "op-1");

    assert_eq!(
        endpoint.authenticate_mtls_and_build_submission(
            "cb-2",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-2",
            json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
            1_250,
            "policy-snapshot-1",
            &headers,
            Some("spiffe://tenant-b/provider/ci"),
        ),
        Err(AsyncOperationError::CallbackAuthenticationFailed {
            endpoint_id: "callback-endpoint-mtls".to_owned(),
            reason: "mtls_identity_mismatch".to_owned(),
        })
    );
}

#[test]
fn oidc_callback_endpoint_authenticates_with_injected_verifier() {
    let endpoint = CallbackEndpointRef::new(
        "callback-endpoint-oidc",
        "https://graphblocks.example.com/v1/callbacks/op-1",
        "schemas/CICallback@1",
        CallbackEndpointAuth::oidc("https://issuer.example.com", "graphblocks-callbacks"),
    )
    .expect("endpoint is valid");
    let mut headers = BTreeMap::new();
    headers.insert("Authorization".to_owned(), "Bearer jwt-ok".to_owned());

    let submission = endpoint
        .authenticate_oidc_and_build_submission(
            "cb-1",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-1",
            json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
            1_250,
            "policy-snapshot-1",
            &headers,
            |issuer, audience, token| {
                assert_eq!(issuer, "https://issuer.example.com");
                assert_eq!(audience, "graphblocks-callbacks");
                token == "jwt-ok"
            },
        )
        .expect("oidc token authenticates");

    assert_eq!(submission.verified_by, "oidc:callback-endpoint-oidc");
    assert_eq!(submission.operation_id, "op-1");

    headers.insert("Authorization".to_owned(), "Bearer jwt-bad".to_owned());
    assert_eq!(
        endpoint.authenticate_oidc_and_build_submission(
            "cb-2",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-2",
            json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
            1_250,
            "policy-snapshot-1",
            &headers,
            |_issuer, _audience, _token| false,
        ),
        Err(AsyncOperationError::CallbackAuthenticationFailed {
            endpoint_id: "callback-endpoint-oidc".to_owned(),
            reason: "oidc_token_invalid".to_owned(),
        })
    );
}

#[test]
fn async_operation_diagnostics_report_missing_timeout_schema_and_idempotency() {
    let mut operation = AsyncOperation::new(
        "op-missing",
        "run-1",
        "node-ci",
        "attempt-1",
        AsyncOperationKind::CiJob,
        "sha256:resume-token",
        " ",
        " ",
        1_000,
    )
    .submitted("gha-run-1", 1_050);
    operation.state = AsyncOperationState::WaitingCallback;

    let diagnostics = AsyncOperationConfigurationDiagnostic::for_operation(&operation);
    let codes = diagnostics
        .iter()
        .map(|diagnostic| diagnostic.code)
        .collect::<Vec<_>>();

    assert_eq!(codes, vec!["GB6001", "GB6003", "GB6007"]);
    assert_eq!(diagnostics[0].field, "expires_at_unix_ms");
    assert_eq!(diagnostics[1].field, "idempotency_key");
    assert_eq!(diagnostics[2].field, "expected_schema");
}

#[test]
fn async_operation_diagnostics_report_polling_wait_without_timeout() {
    let mut operation = waiting_operation();
    operation.state = AsyncOperationState::Polling;
    operation.expires_at_unix_ms = None;

    let diagnostics = AsyncOperationConfigurationDiagnostic::for_operation(&operation);

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6001");
    assert_eq!(diagnostics[0].field, "expires_at_unix_ms");
    assert!(
        diagnostics[0]
            .message
            .contains("async operation op-1 polling wait has no timeout")
    );
}

#[test]
fn async_operation_diagnostics_do_not_report_valid_waiting_operation() {
    assert_eq!(
        AsyncOperationConfigurationDiagnostic::for_operation(&waiting_operation()),
        Vec::new()
    );
}

#[test]
fn async_operation_diagnostics_report_callback_payload_larger_than_limit() {
    let operation = waiting_operation().with_expected_callback_payload_bytes(4_096);

    let diagnostics = AsyncOperationConfigurationDiagnostic::for_operation_with_limits(
        &operation,
        AsyncCallbackIngestionLimits {
            max_payload_bytes: 1_024,
        },
    );

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6010");
    assert_eq!(diagnostics[0].field, "expected_callback_payload_bytes");
    assert!(diagnostics[0].message.contains("4096"));
    assert!(diagnostics[0].message.contains("1024"));
}

#[test]
fn async_operation_diagnostics_allow_callback_payload_within_limit() {
    let operation = waiting_operation().with_expected_callback_payload_bytes(1_024);

    assert_eq!(
        AsyncOperationConfigurationDiagnostic::for_operation_with_limits(
            &operation,
            AsyncCallbackIngestionLimits {
                max_payload_bytes: 1_024,
            },
        ),
        Vec::new()
    );
}

#[test]
fn async_operation_validate_rejects_terminal_state_without_completed_at() {
    for state in [
        AsyncOperationState::Completed,
        AsyncOperationState::Failed,
        AsyncOperationState::Cancelled,
        AsyncOperationState::Expired,
    ] {
        let mut operation = waiting_operation();
        operation.state = state;
        operation.completed_at_unix_ms = None;
        let state_name = match state {
            AsyncOperationState::Completed => "completed",
            AsyncOperationState::Failed => "failed",
            AsyncOperationState::Cancelled => "cancelled",
            AsyncOperationState::Expired => "expired",
            _ => unreachable!("test only iterates terminal states"),
        };

        assert_eq!(
            operation.validate(),
            Err(AsyncOperationError::InvalidOperation {
                operation_id: "op-1".to_owned(),
                reason: format!("{state_name} operations require completed_at"),
            })
        );
    }
}

#[test]
fn async_operation_validate_rejects_inconsistent_state_timestamps_and_provider_identity() {
    let mut created_with_provider = AsyncOperation::new(
        "op-created",
        "run-1",
        "node-ci",
        "attempt-1",
        AsyncOperationKind::CiJob,
        "sha256:resume-token",
        "idem-op-created",
        "schemas/CICallback@1",
        1_000,
    );
    created_with_provider.provider_operation_id = Some("gha-run-1".to_owned());

    assert_eq!(
        created_with_provider.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-created".to_owned(),
            reason: "created operations cannot have provider_operation_id".to_owned(),
        })
    );

    for state in [
        AsyncOperationState::Submitted,
        AsyncOperationState::WaitingCallback,
        AsyncOperationState::CallbackReceived,
        AsyncOperationState::Polling,
        AsyncOperationState::Resuming,
        AsyncOperationState::Completed,
        AsyncOperationState::Failed,
        AsyncOperationState::Cancelled,
        AsyncOperationState::Expired,
    ] {
        let mut operation = waiting_operation();
        operation.state = state;
        operation.submitted_at_unix_ms = None;
        if matches!(
            state,
            AsyncOperationState::Completed
                | AsyncOperationState::Failed
                | AsyncOperationState::Cancelled
                | AsyncOperationState::Expired
        ) {
            operation.completed_at_unix_ms = Some(2_000);
        }

        assert_eq!(
            operation.validate(),
            Err(AsyncOperationError::InvalidOperation {
                operation_id: "op-1".to_owned(),
                reason: "non-created operations require submitted_at".to_owned(),
            }),
            "state {state:?}"
        );
    }
}

#[test]
fn async_operation_validate_rejects_zero_creation_timestamp() {
    let operation = AsyncOperation::new(
        "op-zero-created",
        "run-1",
        "node-ci",
        "attempt-1",
        AsyncOperationKind::CiJob,
        "sha256:resume-token",
        "idem-op-zero-created",
        "schemas/CICallback@1",
        0,
    );

    assert_eq!(
        operation.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-zero-created".to_owned(),
            reason: "created_at must be positive".to_owned(),
        })
    );
}

#[test]
fn async_operation_validate_rejects_out_of_order_state_timestamps() {
    let submitted_before_created = AsyncOperation::new(
        "op-submitted",
        "run-1",
        "node-ci",
        "attempt-1",
        AsyncOperationKind::CiJob,
        "sha256:resume-token",
        "idem-op-submitted",
        "schemas/CICallback@1",
        1_000,
    )
    .submitted("gha-run-1", 999);
    let mut completed_before_submitted = waiting_operation();
    completed_before_submitted.state = AsyncOperationState::Completed;
    completed_before_submitted.completed_at_unix_ms =
        completed_before_submitted.submitted_at_unix_ms.map(|submitted_at| submitted_at - 1);

    assert_eq!(
        submitted_before_created.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-submitted".to_owned(),
            reason: "submitted_at precedes created_at".to_owned(),
        })
    );
    assert_eq!(
        completed_before_submitted.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-1".to_owned(),
            reason: "completed_at precedes submitted_at".to_owned(),
        })
    );
}

#[test]
fn async_operation_validate_rejects_completion_after_expiration() {
    let mut operation = waiting_operation();
    operation.state = AsyncOperationState::Completed;
    operation.expires_at_unix_ms = Some(1_900);
    operation.completed_at_unix_ms = Some(2_000);

    assert_eq!(
        operation.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-1".to_owned(),
            reason: "completed_at exceeds expires_at".to_owned(),
        })
    );
}

#[test]
fn async_operation_validate_rejects_callback_received_without_valid_receipt_time() {
    let mut missing_receipt_time = waiting_operation();
    missing_receipt_time.state = AsyncOperationState::CallbackReceived;
    missing_receipt_time.completed_at_unix_ms = None;

    assert_eq!(
        missing_receipt_time.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-1".to_owned(),
            reason: "callback_received operations require completed_at".to_owned(),
        })
    );

    let mut receipt_after_expiry = waiting_operation();
    receipt_after_expiry.state = AsyncOperationState::CallbackReceived;
    receipt_after_expiry.expires_at_unix_ms = Some(1_900);
    receipt_after_expiry.completed_at_unix_ms = Some(2_000);

    assert_eq!(
        receipt_after_expiry.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-1".to_owned(),
            reason: "completed_at exceeds expires_at".to_owned(),
        })
    );
}

#[test]
fn async_operation_validate_rejects_callback_received_without_expiration() {
    let mut operation = waiting_operation();
    operation.state = AsyncOperationState::CallbackReceived;
    operation.expires_at_unix_ms = None;
    operation.completed_at_unix_ms = Some(1_500);

    assert_eq!(
        operation.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-1".to_owned(),
            reason: "callback_received operations require an expiration".to_owned(),
        })
    );
}

#[test]
fn async_operation_validate_rejects_polling_without_expiration() {
    let mut operation = waiting_operation();
    operation.state = AsyncOperationState::Polling;
    operation.expires_at_unix_ms = None;

    assert_eq!(
        operation.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-1".to_owned(),
            reason: "polling operations require an expiration".to_owned(),
        })
    );
}

#[test]
fn async_operation_diagnostics_report_resume_without_policy_reevaluation() {
    let operation = waiting_operation().without_resume_policy_reevaluation();

    let diagnostics = AsyncOperationConfigurationDiagnostic::for_operation(&operation);

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6008");
    assert_eq!(diagnostics[0].field, "resume_policy_reevaluation");
    assert!(diagnostics[0].message.contains("op-1"));
}

#[test]
fn async_operation_diagnostics_require_resume_policy_reevaluation_by_default() {
    let operation = waiting_operation();

    assert_eq!(
        AsyncOperationConfigurationDiagnostic::for_operation(&operation),
        Vec::new()
    );
}

#[test]
fn async_operation_diagnostics_report_stale_callback_can_resume() {
    let operation = waiting_operation().without_callback_attempt_fencing();

    let diagnostics = AsyncOperationConfigurationDiagnostic::for_operation(&operation);

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6015");
    assert_eq!(diagnostics[0].field, "callback_attempt_fencing");
    assert!(diagnostics[0].message.contains("op-1"));
}

#[test]
fn async_operation_diagnostics_require_callback_attempt_fencing_by_default() {
    let operation = waiting_operation();

    assert_eq!(
        AsyncOperationConfigurationDiagnostic::for_operation(&operation),
        Vec::new()
    );
}

#[test]
fn async_operation_diagnostics_report_resume_without_ownership_fence() {
    let operation = waiting_operation().without_resume_ownership_fence();

    let diagnostics = AsyncOperationConfigurationDiagnostic::for_operation(&operation);

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6016");
    assert_eq!(diagnostics[0].field, "resume_ownership_fence");
    assert!(diagnostics[0].message.contains("op-1"));
}

#[test]
fn async_operation_diagnostics_require_resume_ownership_fence_by_default() {
    let operation = waiting_operation();

    assert_eq!(
        AsyncOperationConfigurationDiagnostic::for_operation(&operation),
        Vec::new()
    );
}

#[test]
fn external_callback_is_journaled_before_operation_can_resume() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");

    let accepted = store
        .accept_callback(
            valid_submission("cb-1", "idem-cb-1"),
            &callback_schema_registry(),
        )
        .expect("callback is accepted");
    let events = store.events_for_operation("op-1");

    assert!(accepted.should_resume);
    assert!(!accepted.duplicate);
    assert_eq!(accepted.receipt.operation_id, "op-1");
    assert_eq!(
        accepted.receipt.payload_digest,
        accepted.receipt.compute_payload_digest()
    );
    assert_eq!(events.len(), 4);
    assert!(matches!(
        events[2],
        AsyncOperationEvent::ExternalCallbackReceived { .. }
    ));
    assert!(matches!(
        events[3],
        AsyncOperationEvent::StateChanged {
            to: graphblocks_runtime_core::async_operation::AsyncOperationState::CallbackReceived,
            ..
        }
    ));
}

#[test]
fn duplicate_callback_is_idempotent_and_does_not_resume_twice() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");

    let first = store
        .accept_callback(
            valid_submission("cb-1", "idem-cb-1"),
            &callback_schema_registry(),
        )
        .expect("first callback is accepted");
    let duplicate = store
        .accept_callback(
            valid_submission("cb-duplicate", "idem-cb-1"),
            &callback_schema_registry(),
        )
        .expect("duplicate callback is idempotent");

    assert!(first.should_resume);
    assert!(!first.duplicate);
    assert!(!duplicate.should_resume);
    assert!(duplicate.duplicate);
    assert_eq!(duplicate.receipt.callback_id, "cb-1");
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        1
    );
}

#[test]
fn callback_for_non_waiting_operation_is_rejected_with_audit_event() {
    let store = AsyncOperationStore::new();
    store
        .register(
            AsyncOperation::new(
                "op-1",
                "run-1",
                "node-ci",
                "attempt-1",
                AsyncOperationKind::CiJob,
                "sha256:resume-token",
                "idem-op-1",
                "schemas/CICallback@1",
                1_000,
            )
            .submitted("gha-run-1", 1_050),
        )
        .expect("operation registers");

    assert_eq!(
        store.accept_callback(
            valid_submission("cb-not-waiting", "idem-not-waiting"),
            &callback_schema_registry(),
        ),
        Err(AsyncOperationError::OperationNotWaitingCallback {
            operation_id: "op-1".to_owned(),
            state: AsyncOperationState::Submitted,
        })
    );
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::Submitted)
    );
    assert!(store
        .events_for_operation("op-1")
        .iter()
        .any(|event| matches!(
            event,
            AsyncOperationEvent::ExternalCallbackRejected {
                callback_id,
                reason,
                ..
            } if callback_id == "cb-not-waiting"
                && reason == "operation_not_waiting_callback:submitted"
        )));
}

#[test]
fn callback_for_unknown_operation_is_rejected_with_audit_event() {
    let store = AsyncOperationStore::new();
    let submission = AsyncCallbackSubmission::new(
        "cb-unknown-operation",
        "op-missing",
        "run-1",
        "node-ci",
        "attempt-1",
        "idem-unknown-operation",
        json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
        1_200,
        "hmac:callback-endpoint-1",
        "policy-snapshot-1",
    );

    assert_eq!(
        store.accept_callback(submission, &callback_schema_registry()),
        Err(AsyncOperationError::OperationNotFound {
            operation_id: "op-missing".to_owned(),
        })
    );
    assert!(store
        .events_for_operation("op-missing")
        .iter()
        .any(|event| matches!(
            event,
            AsyncOperationEvent::ExternalCallbackRejected {
                operation_id,
                callback_id,
                reason,
                verified_by,
                ..
            } if operation_id == "op-missing"
                && callback_id == "cb-unknown-operation"
                && reason == "operation_not_found"
                && verified_by == "hmac:callback-endpoint-1"
        )));
}

#[test]
fn early_callback_is_quarantined_until_operation_registers() {
    let store = AsyncOperationStore::new();
    let quarantined = store
        .quarantine_callback_before_operation_commit(
            valid_submission("cb-early", "provider-delivery-early"),
            5_000,
        )
        .expect("early callback is quarantined");

    assert!(!quarantined.duplicate);
    assert_eq!(quarantined.operation_id, "op-1");
    assert_eq!(
        store.quarantined_callback_count("op-1"),
        1,
        "pending callback must be retained before the operation is committed"
    );

    store
        .register(waiting_operation())
        .expect("operation is registered after callback ingress");
    let accepted = store
        .accept_quarantined_callbacks("op-1", &callback_schema_registry())
        .expect("quarantined callback is consumed after registration");

    assert_eq!(accepted.len(), 1);
    assert_eq!(accepted[0].receipt.callback_id, "cb-early");
    assert!(accepted[0].should_resume);
    assert_eq!(store.quarantined_callback_count("op-1"), 0);
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::CallbackReceived)
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        1
    );
}

#[test]
fn expired_early_callback_quarantine_is_not_replayed_after_operation_registers() {
    let store = AsyncOperationStore::new();
    store
        .quarantine_callback_before_operation_commit(
            valid_submission("cb-early-expired", "provider-delivery-expired"),
            1_250,
        )
        .expect("early callback is quarantined");
    store
        .register(late_committed_waiting_operation())
        .expect("operation is registered after callback ingress");

    let accepted = store
        .accept_quarantined_callbacks("op-1", &callback_schema_registry())
        .expect("expired quarantine entry is discarded without resuming");

    assert!(accepted.is_empty());
    assert_eq!(store.quarantined_callback_count("op-1"), 0);
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback)
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        0
    );
    assert!(store.events_for_operation("op-1").iter().any(|event| {
        matches!(
            event,
            AsyncOperationEvent::ExternalCallbackRejected {
                callback_id,
                reason,
                ..
            } if callback_id == "cb-early-expired" && reason == "quarantined_callback_expired"
        )
    }));
}

#[test]
fn duplicate_early_callback_is_quarantined_once_and_consumed_once() {
    let store = AsyncOperationStore::new();
    let first = store
        .quarantine_callback_before_operation_commit(
            valid_submission("cb-early", "provider-delivery-early"),
            5_000,
        )
        .expect("first early callback is quarantined");
    let duplicate = store
        .quarantine_callback_before_operation_commit(
            valid_submission("cb-early-duplicate", "provider-delivery-early"),
            5_001,
        )
        .expect("duplicate early callback is recognized");

    assert!(!first.duplicate);
    assert!(duplicate.duplicate);
    assert_eq!(store.quarantined_callback_count("op-1"), 1);

    store
        .register(waiting_operation())
        .expect("operation is registered after callback ingress");
    let accepted = store
        .accept_quarantined_callbacks("op-1", &callback_schema_registry())
        .expect("quarantined duplicate set is consumed once");

    assert_eq!(accepted.len(), 1);
    assert_eq!(accepted[0].receipt.callback_id, "cb-early");
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        1
    );
}

#[test]
fn conflicting_early_callback_replay_does_not_overwrite_quarantine() {
    let store = AsyncOperationStore::new();
    store
        .quarantine_callback_before_operation_commit(
            valid_submission("cb-early", "provider-delivery-early"),
            5_000,
        )
        .expect("first early callback is quarantined");

    let conflict = store.quarantine_callback_before_operation_commit(
        AsyncCallbackSubmission::new(
            "cb-early-conflict",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "provider-delivery-early",
            json!({"status": "failed", "workflow_run_id": "gha-run-1"}),
            1_201,
            "hmac:callback-endpoint-1",
            "policy-snapshot-1",
        ),
        5_001,
    );

    assert_eq!(
        conflict,
        Err(AsyncOperationError::CallbackIdempotencyConflict {
            operation_id: "op-1".to_owned(),
            idempotency_key: "provider-delivery-early".to_owned(),
            field: "payload_digest".to_owned(),
        })
    );
    assert_eq!(store.quarantined_callback_count("op-1"), 1);

    store
        .register(waiting_operation())
        .expect("operation is registered after callback ingress");
    let accepted = store
        .accept_quarantined_callbacks("op-1", &callback_schema_registry())
        .expect("original quarantined callback remains authoritative");

    assert_eq!(accepted.len(), 1);
    assert_eq!(accepted[0].receipt.callback_id, "cb-early");
    assert_eq!(
        accepted[0].receipt.payload,
        json!({"status": "completed", "workflow_run_id": "gha-run-1"})
    );
}

#[test]
fn early_callback_quarantine_fuzz_sequence_consumes_each_idempotency_key_once() {
    for seed in 0..64_u64 {
        let store = AsyncOperationStore::new();
        let mut expected_unique_keys = Vec::new();
        for index in 0..32_u64 {
            let idempotency_key = format!("provider-delivery-{}", (seed * 17 + index * 7) % 11);
            if !expected_unique_keys.contains(&idempotency_key) {
                expected_unique_keys.push(idempotency_key.clone());
            }
            let callback_id = format!("cb-{seed}-{index}");
            let result = store.quarantine_callback_before_operation_commit(
                valid_submission(&callback_id, &idempotency_key),
                5_000 + index,
            );
            assert!(
                result.is_ok(),
                "seed {seed} index {index} should quarantine or deduplicate"
            );
        }

        assert_eq!(
            store.quarantined_callback_count("op-1"),
            expected_unique_keys.len()
        );
        store
            .register(waiting_operation())
            .expect("operation is registered after callback ingress");
        let accepted = store
            .accept_quarantined_callbacks("op-1", &callback_schema_registry())
            .expect("quarantined callbacks are consumed");

        assert_eq!(accepted.len(), 1);
        assert_eq!(
            store.quarantined_callback_count("op-1"),
            0,
            "seed {seed} should drain quarantine after one accepted resume"
        );
        assert_eq!(
            store
                .events_for_operation("op-1")
                .iter()
                .filter(|event| {
                    matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. })
                })
                .count(),
            1
        );
    }
}

#[test]
fn early_callback_quarantine_conflict_fuzz_preserves_first_submission() {
    for seed in 0..64_u64 {
        let store = AsyncOperationStore::new();
        let first_callback_id = format!("cb-quarantine-first-{seed}");
        store
            .quarantine_callback_before_operation_commit(
                valid_submission(&first_callback_id, "provider-delivery-conflict"),
                5_000,
            )
            .expect("first early callback is quarantined");
        let mut state = seed ^ 0x517c_c1b7_2722_0a95;

        for index in 0..64_u64 {
            state = state
                .wrapping_mul(3202034522624059733)
                .wrapping_add(4354685564936845354);
            let payload = if state % 7 == 0 {
                json!({"status": "completed", "workflow_run_id": "gha-run-1"})
            } else {
                json!({"status": format!("mutated-{seed}-{index}"), "workflow_run_id": "gha-run-1"})
            };
            let result = store.quarantine_callback_before_operation_commit(
                AsyncCallbackSubmission::new(
                    format!("cb-quarantine-conflict-{seed}-{index}"),
                    "op-1",
                    "run-1",
                    "node-ci",
                    "attempt-1",
                    "provider-delivery-conflict",
                    payload,
                    1_300 + index,
                    "hmac:callback-endpoint-1",
                    "policy-snapshot-1",
                ),
                5_001 + index,
            );

            match result {
                Ok(quarantined) => {
                    assert!(quarantined.duplicate, "seed {seed} index {index}");
                }
                Err(AsyncOperationError::CallbackIdempotencyConflict { field, .. }) => {
                    assert_eq!(field, "payload_digest", "seed {seed} index {index}");
                }
                Err(error) => panic!("unexpected error for seed {seed} index {index}: {error:?}"),
            }
        }

        assert_eq!(store.quarantined_callback_count("op-1"), 1);
        store
            .register(waiting_operation())
            .expect("operation is registered after callback ingress");
        let accepted = store
            .accept_quarantined_callbacks("op-1", &callback_schema_registry())
            .expect("original quarantined callback remains authoritative");

        assert_eq!(accepted.len(), 1, "seed {seed}");
        assert_eq!(accepted[0].receipt.callback_id, first_callback_id);
        assert_eq!(
            accepted[0].receipt.payload,
            json!({"status": "completed", "workflow_run_id": "gha-run-1"})
        );
    }
}

#[test]
fn callback_idempotency_key_conflict_rejects_mutated_payload_without_overwriting_receipt() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");
    let registry = callback_schema_registry();

    let first = store
        .accept_callback(valid_submission("cb-1", "idem-cb-1"), &registry)
        .expect("first callback is accepted");
    let conflict = store.accept_callback(
        AsyncCallbackSubmission::new(
            "cb-conflict",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-1",
            json!({"status": "failed", "workflow_run_id": "gha-run-1"}),
            1_201,
            "hmac:callback-endpoint-1",
            "policy-snapshot-1",
        ),
        &registry,
    );
    let duplicate = store
        .accept_callback(valid_submission("cb-duplicate", "idem-cb-1"), &registry)
        .expect("original duplicate remains idempotent");

    assert!(first.should_resume);
    assert_eq!(
        conflict,
        Err(AsyncOperationError::CallbackIdempotencyConflict {
            operation_id: "op-1".to_owned(),
            idempotency_key: "idem-cb-1".to_owned(),
            field: "payload_digest".to_owned(),
        })
    );
    assert!(duplicate.duplicate);
    assert_eq!(duplicate.receipt.callback_id, "cb-1");
    assert_eq!(
        duplicate.receipt.payload,
        json!({"status": "completed", "workflow_run_id": "gha-run-1"})
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        1
    );
    assert!(
        store
            .events_for_operation("op-1")
            .iter()
            .any(|event| matches!(
                event,
                AsyncOperationEvent::ExternalCallbackRejected {
                    callback_id,
                    reason,
                    ..
                } if callback_id == "cb-conflict" && reason == "idempotency_conflict:payload_digest"
            ))
    );
}

#[test]
fn callback_idempotency_key_is_scoped_to_operation() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("first operation registers");
    store
        .register(
            AsyncOperation::new(
                "op-2",
                "run-1",
                "node-ci",
                "attempt-1",
                AsyncOperationKind::CiJob,
                "sha256:resume-token-2",
                "idem-op-2",
                "schemas/CICallback@1",
                1_000,
            )
            .submitted("gha-run-2", 1_050)
            .waiting_callback(2_000),
        )
        .expect("second operation registers");
    let registry = callback_schema_registry();

    let first = store
        .accept_callback(valid_submission("cb-1", "provider-delivery-1"), &registry)
        .expect("first callback is accepted");
    let second = store
        .accept_callback(
            AsyncCallbackSubmission::new(
                "cb-2",
                "op-2",
                "run-1",
                "node-ci",
                "attempt-1",
                "provider-delivery-1",
                json!({"status": "completed", "workflow_run_id": "gha-run-2"}),
                1_201,
                "hmac:callback-endpoint-1",
                "policy-snapshot-1",
            ),
            &registry,
        )
        .expect("same provider idempotency key is accepted for another operation");

    assert!(first.should_resume);
    assert!(!first.duplicate);
    assert!(second.should_resume);
    assert!(!second.duplicate);
    assert_eq!(second.receipt.operation_id, "op-2");
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        1
    );
    assert_eq!(
        store
            .events_for_operation("op-2")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        1
    );
}

#[test]
fn callback_schema_failure_and_stale_attempt_do_not_resume_run() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");

    let stale_attempt = AsyncCallbackSubmission::new(
        "cb-stale",
        "op-1",
        "run-1",
        "node-ci",
        "attempt-0",
        "idem-stale",
        json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
        1_200,
        "hmac:callback-endpoint-1",
        "policy-snapshot-1",
    );
    let invalid_payload = AsyncCallbackSubmission::new(
        "cb-invalid",
        "op-1",
        "run-1",
        "node-ci",
        "attempt-1",
        "idem-invalid",
        json!({"status": 7}),
        1_200,
        "hmac:callback-endpoint-1",
        "policy-snapshot-1",
    );
    let wrong_run = AsyncCallbackSubmission::new(
        "cb-wrong-run",
        "op-1",
        "run-other",
        "node-ci",
        "attempt-1",
        "idem-wrong-run",
        json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
        1_201,
        "hmac:callback-endpoint-1",
        "policy-snapshot-1",
    );
    let wrong_node = AsyncCallbackSubmission::new(
        "cb-wrong-node",
        "op-1",
        "run-1",
        "node-other",
        "attempt-1",
        "idem-wrong-node",
        json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
        1_202,
        "hmac:callback-endpoint-1",
        "policy-snapshot-1",
    );

    assert_eq!(
        store.accept_callback(stale_attempt, &callback_schema_registry()),
        Err(AsyncOperationError::StaleAttempt {
            operation_id: "op-1".to_owned(),
            expected_attempt_id: "attempt-1".to_owned(),
            actual_attempt_id: "attempt-0".to_owned(),
        })
    );
    assert!(matches!(
        store.accept_callback(invalid_payload, &callback_schema_registry()),
        Err(AsyncOperationError::CallbackSchemaInvalid { .. })
    ));
    assert_eq!(
        store.accept_callback(wrong_run, &callback_schema_registry()),
        Err(AsyncOperationError::OperationIdentityMismatch {
            operation_id: "op-1".to_owned(),
            field: "run_id".to_owned(),
            expected: "run-1".to_owned(),
            actual: "run-other".to_owned(),
        })
    );
    assert_eq!(
        store.accept_callback(wrong_node, &callback_schema_registry()),
        Err(AsyncOperationError::OperationIdentityMismatch {
            operation_id: "op-1".to_owned(),
            field: "node_id".to_owned(),
            expected: "node-ci".to_owned(),
            actual: "node-other".to_owned(),
        })
    );
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback)
    );
    let events = store.events_for_operation("op-1");
    assert_eq!(
        events
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackRejected { .. }))
            .count(),
        4
    );
    assert!(events.iter().any(|event| matches!(
        event,
        AsyncOperationEvent::ExternalCallbackRejected {
            callback_id,
            reason,
            ..
        } if callback_id == "cb-stale" && reason == "stale_attempt"
    )));
    assert!(events.iter().any(|event| matches!(
        event,
        AsyncOperationEvent::ExternalCallbackRejected {
            callback_id,
            reason,
            ..
        } if callback_id == "cb-invalid" && reason == "schema_invalid"
    )));
    assert!(events.iter().any(|event| matches!(
        event,
        AsyncOperationEvent::ExternalCallbackRejected {
            callback_id,
            reason,
            ..
        } if callback_id == "cb-wrong-run" && reason == "identity_mismatch:run_id"
    )));
    assert!(events.iter().any(|event| matches!(
        event,
        AsyncOperationEvent::ExternalCallbackRejected {
            callback_id,
            reason,
            ..
        } if callback_id == "cb-wrong-node" && reason == "identity_mismatch:node_id"
    )));
}

#[test]
fn callback_payload_limit_rejects_oversized_payload_before_journal_or_resume() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");
    let mut submission = valid_submission("cb-oversized", "idem-oversized");
    submission.payload = json!({
        "status": "completed",
        "workflow_run_id": "gha-run-1",
        "log": "x".repeat(512),
    });

    assert!(matches!(
        store.accept_callback_with_limits(
            submission,
            &callback_schema_registry(),
            graphblocks_runtime_core::async_operation::AsyncCallbackIngestionLimits {
                max_payload_bytes: 128
            },
        ),
        Err(AsyncOperationError::CallbackPayloadTooLarge {
            operation_id,
            max_payload_bytes: 128,
            actual_payload_bytes,
        }) if operation_id == "op-1" && actual_payload_bytes > 128
    ));
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback)
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        0
    );
    assert!(
        store
            .events_for_operation("op-1")
            .iter()
            .any(|event| matches!(
                event,
                AsyncOperationEvent::ExternalCallbackRejected {
                    callback_id,
                    reason,
                    ..
                } if callback_id == "cb-oversized" && reason == "payload_too_large"
            ))
    );
}

#[test]
fn oversized_callback_payload_can_be_converted_to_artifact_ref_before_journal() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");
    let mut submission = valid_submission("cb-artifact", "idem-artifact");
    submission.payload = json!({
        "status": "completed",
        "workflow_run_id": "gha-run-1",
        "log": "x".repeat(512),
    });

    let accepted = store
        .accept_callback_with_artifact_on_payload_limit(
            submission,
            &callback_schema_registry(),
            AsyncCallbackIngestionLimits {
                max_payload_bytes: 128,
            },
            CallbackArtifactRef::new("artifact-ci-log", "blob://callbacks/op-1/cb-artifact.json")
                .with_media_type("application/json")
                .with_checksum("sha256:callback-log"),
        )
        .expect("oversized callback can be stored as artifact ref");

    assert!(accepted.should_resume);
    assert_eq!(
        accepted.receipt.payload,
        json!({
            "status": "completed",
            "workflow_run_id": "gha-run-1",
            "artifact": {
                "artifact_id": "artifact-ci-log",
                "uri": "blob://callbacks/op-1/cb-artifact.json",
                "media_type": "application/json",
                "checksum": "sha256:callback-log",
            }
        })
    );
    assert_eq!(
        accepted.receipt.artifacts,
        vec![
            CallbackArtifactRef::new("artifact-ci-log", "blob://callbacks/op-1/cb-artifact.json")
                .with_media_type("application/json")
                .with_checksum("sha256:callback-log")
        ]
    );
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::CallbackReceived)
    );
}

#[test]
fn callback_default_payload_limit_allows_normal_payload() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");

    let accepted = store
        .accept_callback_with_limits(
            valid_submission("cb-normal", "idem-normal"),
            &callback_schema_registry(),
            graphblocks_runtime_core::async_operation::AsyncCallbackIngestionLimits::default(),
        )
        .expect("normal callback is accepted");

    assert!(accepted.should_resume);
    assert!(!accepted.duplicate);
}

#[test]
fn callback_during_budget_exhaustion_is_journaled_but_resume_is_paused() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");

    let accepted = store
        .accept_callback_with_resume_decision(
            valid_submission("cb-budget", "idem-budget"),
            &callback_schema_registry(),
            AsyncCallbackResumeDecision::PauseBudget {
                reason: "budget exhausted while waiting for callback".to_owned(),
            },
        )
        .expect("callback is recorded even when resume pauses");
    let events = store.events_for_operation("op-1");

    assert!(!accepted.should_resume);
    assert!(!accepted.duplicate);
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::CallbackReceived)
    );
    assert!(matches!(
        events[2],
        AsyncOperationEvent::ExternalCallbackReceived { .. }
    ));
    assert!(events.iter().any(|event| matches!(
        event,
        AsyncOperationEvent::CallbackResumePaused {
            operation_id,
            reason,
            ..
        } if operation_id == "op-1" && reason == "budget exhausted while waiting for callback"
    )));
}

#[test]
fn callback_resume_policy_denial_is_journaled_without_resuming() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");

    let accepted = store
        .accept_callback_with_resume_decision(
            valid_submission("cb-policy", "idem-policy"),
            &callback_schema_registry(),
            AsyncCallbackResumeDecision::DenyPolicy {
                decision_id: "decision-deny-resume".to_owned(),
                reason: "tenant no longer entitled".to_owned(),
            },
        )
        .expect("callback is recorded before policy denial is applied");
    let events = store.events_for_operation("op-1");

    assert!(!accepted.should_resume);
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::CallbackReceived)
    );
    assert!(events.iter().any(|event| matches!(
        event,
        AsyncOperationEvent::CallbackResumeDenied {
            operation_id,
            decision_id,
            reason,
            ..
        } if operation_id == "op-1"
            && decision_id == "decision-deny-resume"
            && reason == "tenant no longer entitled"
    )));
}

#[test]
fn callback_resume_release_incompatibility_pauses_operator_resume() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");

    let accepted = store
        .accept_callback_with_resume_decision(
            valid_submission("cb-release", "idem-release"),
            &callback_schema_registry(),
            AsyncCallbackResumeDecision::PauseReleaseIncompatible {
                required_release_id: "release-old".to_owned(),
                available_release_id: "release-new".to_owned(),
            },
        )
        .expect("callback is recorded before release incompatibility pause");
    let events = store.events_for_operation("op-1");

    assert!(!accepted.should_resume);
    assert!(events.iter().any(|event| matches!(
        event,
        AsyncOperationEvent::CallbackResumePaused {
            operation_id,
            reason,
            ..
        } if operation_id == "op-1"
            && reason == "release incompatible: required release-old, available release-new"
    )));
}

#[test]
fn callback_resume_gates_require_auditable_policy_and_release_fields() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");

    assert_eq!(
        store.accept_callback_with_resume_decision(
            valid_submission("cb-policy", "idem-policy"),
            &callback_schema_registry(),
            AsyncCallbackResumeDecision::DenyPolicy {
                decision_id: " ".to_owned(),
                reason: "denied".to_owned(),
            },
        ),
        Err(AsyncOperationError::EmptyField {
            field: "resume_policy_decision_id".to_owned(),
        })
    );
    assert_eq!(
        store.accept_callback_with_resume_decision(
            valid_submission("cb-release", "idem-release"),
            &callback_schema_registry(),
            AsyncCallbackResumeDecision::PauseReleaseIncompatible {
                required_release_id: "release-old".to_owned(),
                available_release_id: " ".to_owned(),
            },
        ),
        Err(AsyncOperationError::EmptyField {
            field: "available_release_id".to_owned(),
        })
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        0
    );
}

#[test]
fn callback_resume_pause_requires_reason_before_journaling() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");

    assert_eq!(
        store.accept_callback_with_resume_decision(
            valid_submission("cb-budget", "idem-budget"),
            &callback_schema_registry(),
            AsyncCallbackResumeDecision::PauseBudget {
                reason: " ".to_owned(),
            },
        ),
        Err(AsyncOperationError::EmptyField {
            field: "resume_pause_reason".to_owned(),
        })
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        0
    );
}

#[test]
fn callback_submission_whitespace_identity_fields_are_rejected_before_journal() {
    for field in [
        "callback_id",
        "operation_id",
        "run_id",
        "node_id",
        "attempt_id",
        "provider_operation_id",
        "idempotency_key",
        "verified_by",
        "policy_snapshot_id",
    ] {
        let store = AsyncOperationStore::new();
        store
            .register(waiting_operation())
            .expect("operation registers");
        let mut submission = valid_submission("cb-whitespace", "idem-whitespace");
        match field {
            "callback_id" => submission.callback_id = " \t".to_owned(),
            "operation_id" => submission.operation_id = " \t".to_owned(),
            "run_id" => submission.run_id = " \t".to_owned(),
            "node_id" => submission.node_id = " \t".to_owned(),
            "attempt_id" => submission.attempt_id = " \t".to_owned(),
            "provider_operation_id" => submission.provider_operation_id = Some(" \t".to_owned()),
            "idempotency_key" => submission.idempotency_key = " \t".to_owned(),
            "verified_by" => submission.verified_by = " \t".to_owned(),
            "policy_snapshot_id" => submission.policy_snapshot_id = " \t".to_owned(),
            _ => unreachable!("test field is declared above"),
        }

        assert_eq!(
            store.accept_callback(submission, &callback_schema_registry()),
            Err(AsyncOperationError::EmptyField {
                field: field.to_owned(),
            }),
            "{field} should reject whitespace-only values",
        );
        assert_eq!(
            store
                .events_for_operation("op-1")
                .iter()
                .filter(|event| matches!(
                    event,
                    AsyncOperationEvent::ExternalCallbackReceived { .. }
                ))
                .count(),
            0,
            "{field} should not journal a received callback",
        );
    }
}

#[test]
fn callback_submission_zero_received_time_is_rejected_before_journal() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");
    let mut submission = valid_submission("cb-zero-time", "idem-zero-time");
    submission.received_at_unix_ms = 0;

    assert_eq!(
        store.accept_callback(submission, &callback_schema_registry()),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-1".to_owned(),
            reason: "callback received_at_unix_ms must be non-zero".to_owned(),
        })
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        0
    );
}

#[test]
fn async_operation_whitespace_identity_fields_are_rejected_before_registration() {
    for field in [
        "operation_id",
        "run_id",
        "node_id",
        "attempt_id",
        "resume_token_hash",
        "idempotency_key",
        "expected_schema",
    ] {
        let store = AsyncOperationStore::new();
        let mut operation = waiting_operation();
        match field {
            "operation_id" => operation.operation_id = " \t".to_owned(),
            "run_id" => operation.run_id = " \t".to_owned(),
            "node_id" => operation.node_id = " \t".to_owned(),
            "attempt_id" => operation.attempt_id = " \t".to_owned(),
            "resume_token_hash" => operation.resume_token_hash = " \t".to_owned(),
            "idempotency_key" => operation.idempotency_key = " \t".to_owned(),
            "expected_schema" => operation.expected_schema = " \t".to_owned(),
            _ => unreachable!("test field is declared above"),
        }

        assert_eq!(
            store.register(operation),
            Err(AsyncOperationError::EmptyField {
                field: field.to_owned(),
            }),
            "{field} should reject whitespace-only values",
        );
        assert!(store.operation_state("op-1").is_none());
    }
}

#[test]
fn concurrent_duplicate_callbacks_have_one_resume_winner() {
    let store = Arc::new(AsyncOperationStore::new());
    store
        .register(waiting_operation())
        .expect("operation registers");
    let registry = Arc::new(callback_schema_registry());
    let workers = 32;
    let barrier = Arc::new(Barrier::new(workers + 1));

    let handles = (0..workers)
        .map(|index| {
            let store = Arc::clone(&store);
            let registry = Arc::clone(&registry);
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                barrier.wait();
                store.accept_callback(
                    valid_submission(&format!("cb-{index}"), "idem-cb-race"),
                    &registry,
                )
            })
        })
        .collect::<Vec<_>>();

    barrier.wait();
    let results = handles
        .into_iter()
        .map(|handle| handle.join().expect("callback worker joins"))
        .collect::<Vec<_>>();

    assert_eq!(
        results
            .iter()
            .filter(|result| result.as_ref().is_ok_and(|accepted| accepted.should_resume))
            .count(),
        1
    );
    assert_eq!(
        results
            .iter()
            .filter(|result| result.as_ref().is_ok_and(|accepted| accepted.duplicate))
            .count(),
        workers - 1
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        1
    );
}

#[test]
fn sqlite_concurrent_duplicate_callbacks_have_one_resume_winner() -> Result<(), AsyncOperationError>
{
    let path = sqlite_async_operation_path("duplicate-callback-race");
    let store = Arc::new(SqliteAsyncOperationStore::open(&path)?);
    store.register(waiting_operation())?;
    let registry = Arc::new(callback_schema_registry());
    let workers = 32;
    let barrier = Arc::new(Barrier::new(workers + 1));

    let handles = (0..workers)
        .map(|index| {
            let store = Arc::clone(&store);
            let registry = Arc::clone(&registry);
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                barrier.wait();
                store.accept_callback(
                    valid_submission(&format!("cb-sqlite-{index}"), "idem-cb-sqlite-race"),
                    &registry,
                )
            })
        })
        .collect::<Vec<_>>();

    barrier.wait();
    let results = handles
        .into_iter()
        .map(|handle| handle.join().expect("callback worker joins"))
        .collect::<Vec<_>>();

    assert_eq!(
        results
            .iter()
            .filter(|result| result.as_ref().is_ok_and(|accepted| accepted.should_resume))
            .count(),
        1
    );
    assert_eq!(
        results
            .iter()
            .filter(|result| result.as_ref().is_ok_and(|accepted| accepted.duplicate))
            .count(),
        workers - 1
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        1
    );

    Ok(())
}

#[test]
fn callback_after_timeout_records_late_callback_without_resume() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");
    store
        .expire_operation("op-1", 2_001)
        .expect("operation expires");

    let accepted = store
        .accept_callback(
            valid_submission("cb-late", "idem-late"),
            &callback_schema_registry(),
        )
        .expect("late callback is recorded");
    let events = store.events_for_operation("op-1");

    assert!(!accepted.should_resume);
    assert!(!accepted.duplicate);
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::Expired)
    );
    assert!(events.iter().any(|event| matches!(
        event,
        AsyncOperationEvent::LateExternalCallbackReceived {
            terminal_state: AsyncOperationState::Expired,
            ..
        }
    )));
    assert!(!events.iter().any(|event| matches!(
        event,
        AsyncOperationEvent::StateChanged {
            to: AsyncOperationState::CallbackReceived,
            ..
        }
    )));
}

#[test]
fn callback_after_cancellation_records_late_callback_without_committing_result() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");
    store
        .cancel_operation("op-1", 1_300)
        .expect("operation cancels");

    let accepted = store
        .accept_callback(
            valid_submission("cb-cancelled", "idem-cancelled"),
            &callback_schema_registry(),
        )
        .expect("cancelled callback is recorded for diagnostics");

    assert!(!accepted.should_resume);
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::Cancelled)
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| {
                matches!(
                    event,
                    AsyncOperationEvent::LateExternalCallbackReceived {
                        terminal_state: AsyncOperationState::Cancelled,
                        ..
                    }
                )
            })
            .count(),
        1
    );
}

#[test]
fn callback_after_completion_records_late_callback_without_resume() {
    let store = AsyncOperationStore::new();
    let mut operation = waiting_operation();
    operation.state = AsyncOperationState::Completed;
    operation.completed_at_unix_ms = Some(1_500);
    store.register(operation).expect("completed operation registers");

    let accepted = store
        .accept_callback(
            valid_submission("cb-completed-late", "idem-completed-late"),
            &callback_schema_registry(),
        )
        .expect("completed callback is recorded for diagnostics");

    assert!(!accepted.should_resume);
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::Completed)
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| {
                matches!(
                    event,
                    AsyncOperationEvent::LateExternalCallbackReceived {
                        terminal_state: AsyncOperationState::Completed,
                        ..
                    }
                )
            })
            .count(),
        1
    );
}

#[test]
fn cancelled_async_operation_result_preserves_committed_external_effect() {
    let result = AsyncOperationResult::cancelled("op-1").with_external_effects([
        ExternalEffectRecord::new(
            "effect-ticket-1",
            "ticket-system",
            "ticket.create",
            ToolEffectOutcome::Committed,
        )
        .with_idempotency_key("idem-ticket-1")
        .with_provider_effect_id("ticket-123"),
    ]);

    assert_eq!(result.status, AsyncOperationResultStatus::Cancelled);
    assert_eq!(result.validate(), Ok(()));
    assert!(result.external_effect_was_committed());
    assert_eq!(result.external_effects[0].outcome, ToolEffectOutcome::Committed);
    assert_eq!(
        result.external_effects[0].idempotency_key.as_deref(),
        Some("idem-ticket-1")
    );
}

#[test]
fn async_operation_result_projects_protocol_json() {
    let result = AsyncOperationResult::cancelled("op-1")
        .with_output(json!({"status": "cancelled_after_commit"}))
        .with_external_effects([
            ExternalEffectRecord::new(
                "effect-ticket-1",
                "ticket-system",
                "ticket.create",
                ToolEffectOutcome::Committed,
            )
            .with_idempotency_key("idem-ticket-1")
            .with_provider_effect_id("ticket-123"),
        ]);

    assert_eq!(
        result.protocol_value(),
        json!({
            "operationId": "op-1",
            "status": "cancelled",
            "output": {"status": "cancelled_after_commit"},
            "artifacts": [],
            "diagnostics": [],
            "metrics": [],
            "checks": [],
            "usage": [],
            "externalEffects": [
                {
                    "effectId": "effect-ticket-1",
                    "target": "ticket-system",
                    "operation": "ticket.create",
                    "outcome": "committed",
                    "idempotencyKey": "idem-ticket-1",
                    "providerEffectId": "ticket-123"
                }
            ]
        })
    );
}

#[test]
fn incomplete_async_operation_result_preserves_committed_external_effect_after_late_callback() {
    let result = AsyncOperationResult::incomplete("op-1").with_external_effects([
        ExternalEffectRecord::new(
            "effect-ci-1",
            "github-actions",
            "workflow_dispatch",
            ToolEffectOutcome::Committed,
        )
        .with_idempotency_key("idem-ci-1")
        .with_provider_effect_id("gha-run-1"),
    ]);

    assert_eq!(result.status, AsyncOperationResultStatus::Incomplete);
    assert_eq!(result.validate(), Ok(()));
    assert!(result.external_effect_was_committed());
}

#[test]
fn async_operation_result_rejects_invalid_external_effect_records() {
    let blank_effect = AsyncOperationResult::completed("op-1").with_external_effects([
        ExternalEffectRecord::new(
            " ",
            "ticket-system",
            "ticket.create",
            ToolEffectOutcome::Committed,
        ),
    ]);
    let impossible_denied_effect = AsyncOperationResult::failed("op-2").with_external_effects([
        ExternalEffectRecord::new(
            "effect-denied",
            "ticket-system",
            "ticket.create",
            ToolEffectOutcome::NoExternalEffect,
        )
        .with_provider_effect_id("ticket-123"),
    ]);

    assert_eq!(
        blank_effect.validate(),
        Err(AsyncOperationError::EmptyField {
            field: "external_effect.effect_id".to_owned(),
        })
    );
    assert_eq!(
        impossible_denied_effect.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-2".to_owned(),
            reason: "external effect effect-denied has provider identity but no committed external effect".to_owned(),
        })
    );
}

#[test]
fn async_operation_result_rejects_committed_external_effect_without_idempotency_key() {
    let missing_idempotency = AsyncOperationResult::cancelled("op-1").with_external_effects([
        ExternalEffectRecord::new(
            "effect-ticket-1",
            "ticket-system",
            "ticket.create",
            ToolEffectOutcome::Committed,
        )
        .with_provider_effect_id("ticket-123"),
    ]);

    assert_eq!(
        missing_idempotency.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-1".to_owned(),
            reason: "committed external effect effect-ticket-1 requires an idempotency key"
                .to_owned(),
        })
    );
}

#[test]
fn async_operation_result_rejects_duplicate_external_effect_identities() {
    let duplicate_effect_id = AsyncOperationResult::completed("op-1").with_external_effects([
        ExternalEffectRecord::new(
            "effect-ticket-1",
            "ticket-system",
            "ticket.create",
            ToolEffectOutcome::Committed,
        )
        .with_idempotency_key("idem-ticket-1"),
        ExternalEffectRecord::new(
            "effect-ticket-1",
            "ticket-system",
            "ticket.update",
            ToolEffectOutcome::Committed,
        )
        .with_idempotency_key("idem-ticket-2"),
    ]);
    let duplicate_provider_effect_id =
        AsyncOperationResult::completed("op-2").with_external_effects([
            ExternalEffectRecord::new(
                "effect-ticket-1",
                "ticket-system",
                "ticket.create",
                ToolEffectOutcome::Committed,
            )
            .with_idempotency_key("idem-ticket-1")
            .with_provider_effect_id("ticket-123"),
            ExternalEffectRecord::new(
                "effect-ticket-2",
                "ticket-system",
                "ticket.update",
                ToolEffectOutcome::Committed,
            )
            .with_idempotency_key("idem-ticket-2")
            .with_provider_effect_id("ticket-123"),
        ]);

    assert_eq!(
        duplicate_effect_id.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-1".to_owned(),
            reason: "duplicate external effect id effect-ticket-1".to_owned(),
        })
    );
    assert_eq!(
        duplicate_provider_effect_id.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-2".to_owned(),
            reason: "duplicate provider effect id ticket-123".to_owned(),
        })
    );
}

#[test]
fn callback_and_cancel_race_has_single_terminal_winner() {
    for seed in 0..64_u64 {
        let store = Arc::new(AsyncOperationStore::new());
        store
            .register(waiting_operation())
            .expect("operation registers");
        let registry = Arc::new(callback_schema_registry());
        let workers = 2;
        let barrier = Arc::new(Barrier::new(workers + 1));

        let callback_store = Arc::clone(&store);
        let callback_registry = Arc::clone(&registry);
        let callback_barrier = Arc::clone(&barrier);
        let callback = thread::spawn(move || {
            callback_barrier.wait();
            callback_store.accept_callback(
                valid_submission(&format!("cb-race-{seed}"), "idem-race"),
                &callback_registry,
            )
        });

        let cancel_store = Arc::clone(&store);
        let cancel_barrier = Arc::clone(&barrier);
        let cancel = thread::spawn(move || {
            cancel_barrier.wait();
            cancel_store.cancel_operation("op-1", 1_250)
        });

        barrier.wait();
        let callback_result = callback.join().expect("callback worker joins");
        let cancel_result = cancel.join().expect("cancel worker joins");
        let state = store
            .operation_state("op-1")
            .expect("operation state exists");
        let events = store.events_for_operation("op-1");
        let callback_received = events
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count();
        let late_callbacks = events
            .iter()
            .filter(|event| {
                matches!(
                    event,
                    AsyncOperationEvent::LateExternalCallbackReceived { .. }
                )
            })
            .count();
        let callback_resume = callback_result
            .as_ref()
            .is_ok_and(|accepted| accepted.should_resume);

        assert!(cancel_result.is_ok() || callback_resume, "seed {seed}");
        assert!(matches!(
            state,
            AsyncOperationState::CallbackReceived | AsyncOperationState::Cancelled
        ));
        assert_eq!(
            callback_received + late_callbacks,
            1,
            "seed {seed} recorded the callback more than once"
        );
        if state == AsyncOperationState::CallbackReceived {
            assert!(callback_resume, "seed {seed}");
        }
    }
}

#[test]
fn callback_idempotency_fuzz_sequence_never_records_duplicate_receipts() {
    for seed in 0..128_u64 {
        let store = AsyncOperationStore::new();
        store
            .register(waiting_operation())
            .expect("operation registers");
        let registry = callback_schema_registry();
        let mut state = seed;

        for index in 0..64 {
            state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
            let key = format!("idem-fuzz-{}", state % 7);
            let callback_id = format!("cb-fuzz-{seed}-{index}");
            let _ = store.accept_callback(valid_submission(&callback_id, &key), &registry);
        }

        let receipts = store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count();
        assert_eq!(receipts, 1, "seed {seed} recorded more than one receipt");
    }
}

#[test]
fn callback_idempotency_conflict_fuzz_preserves_first_receipt() {
    for seed in 0..64_u64 {
        let store = AsyncOperationStore::new();
        store
            .register(waiting_operation())
            .expect("operation registers");
        let registry = callback_schema_registry();
        let first = store
            .accept_callback(
                valid_submission(&format!("cb-fuzz-first-{seed}"), "idem-fuzz-conflict"),
                &registry,
            )
            .expect("first callback is accepted");
        let original_digest = first.receipt.payload_digest.clone();
        let mut state = seed ^ 0x9e37_79b9_7f4a_7c15;

        for index in 0..64 {
            state = state
                .wrapping_mul(2862933555777941757)
                .wrapping_add(3037000493);
            let payload = if state % 5 == 0 {
                json!({"status": "completed", "workflow_run_id": "gha-run-1"})
            } else {
                json!({"status": format!("mutated-{index}"), "workflow_run_id": "gha-run-1"})
            };
            let result = store.accept_callback(
                AsyncCallbackSubmission::new(
                    format!("cb-fuzz-conflict-{seed}-{index}"),
                    "op-1",
                    "run-1",
                    "node-ci",
                    "attempt-1",
                    "idem-fuzz-conflict",
                    payload,
                    1_300 + index,
                    "hmac:callback-endpoint-1",
                    "policy-snapshot-1",
                ),
                &registry,
            );
            match result {
                Ok(accepted) => {
                    assert!(accepted.duplicate, "seed {seed} index {index}");
                    assert_eq!(accepted.receipt.payload_digest, original_digest);
                }
                Err(AsyncOperationError::CallbackIdempotencyConflict { field, .. }) => {
                    assert_eq!(field, "payload_digest", "seed {seed} index {index}");
                }
                Err(error) => panic!("unexpected error for seed {seed} index {index}: {error:?}"),
            }
        }

        let events = store.events_for_operation("op-1");
        assert_eq!(
            events
                .iter()
                .filter(|event| {
                    matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. })
                })
                .count(),
            1,
            "seed {seed} recorded more than one receipt"
        );
        assert_eq!(
            store
                .accept_callback(
                    valid_submission("cb-fuzz-final-duplicate", "idem-fuzz-conflict"),
                    &registry,
                )
                .expect("final original duplicate remains accepted")
                .receipt
                .payload_digest,
            original_digest
        );
    }
}

#[test]
fn sqlite_async_operation_store_persists_waiting_operation_across_reopen()
-> Result<(), AsyncOperationError> {
    let path = sqlite_async_operation_path("waiting-reopen");
    {
        let store = SqliteAsyncOperationStore::open(&path)?;
        store.register(waiting_operation())?;
    }

    let store = SqliteAsyncOperationStore::open(&path)?;

    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback)
    );
    let events = store.events_for_operation("op-1");
    assert_eq!(events.len(), 2);
    assert!(matches!(
        events[1],
        AsyncOperationEvent::StateChanged {
            to: AsyncOperationState::WaitingCallback,
            ..
        }
    ));
    Ok(())
}

#[test]
fn sqlite_async_operation_store_persists_callback_receipt_and_duplicate_guard_across_reopen()
-> Result<(), AsyncOperationError> {
    let path = sqlite_async_operation_path("callback-reopen");
    {
        let store = SqliteAsyncOperationStore::open(&path)?;
        store.register(waiting_operation())?;
        let accepted = store.accept_callback(
            valid_submission("cb-1", "idem-cb-1"),
            &callback_schema_registry(),
        )?;
        assert!(accepted.should_resume);
    }

    let store = SqliteAsyncOperationStore::open(&path)?;
    let duplicate = store.accept_callback(
        valid_submission("cb-duplicate", "idem-cb-1"),
        &callback_schema_registry(),
    )?;

    assert!(duplicate.duplicate);
    assert!(!duplicate.should_resume);
    assert_eq!(duplicate.receipt.callback_id, "cb-1");
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::CallbackReceived)
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        1
    );
    Ok(())
}

#[test]
fn sqlite_async_operation_store_rejects_idempotency_conflict_after_reopen()
-> Result<(), AsyncOperationError> {
    let path = sqlite_async_operation_path("callback-idempotency-conflict");
    {
        let store = SqliteAsyncOperationStore::open(&path)?;
        store.register(waiting_operation())?;
        store.accept_callback(
            valid_submission("cb-1", "idem-cb-1"),
            &callback_schema_registry(),
        )?;
    }

    let store = SqliteAsyncOperationStore::open(&path)?;
    let conflict = store.accept_callback(
        AsyncCallbackSubmission::new(
            "cb-conflict",
            "op-1",
            "run-1",
            "node-ci",
            "attempt-1",
            "idem-cb-1",
            json!({"status": "failed", "workflow_run_id": "gha-run-1"}),
            1_201,
            "hmac:callback-endpoint-1",
            "policy-snapshot-1",
        ),
        &callback_schema_registry(),
    );
    let duplicate = store.accept_callback(
        valid_submission("cb-duplicate", "idem-cb-1"),
        &callback_schema_registry(),
    )?;

    assert_eq!(
        conflict,
        Err(AsyncOperationError::CallbackIdempotencyConflict {
            operation_id: "op-1".to_owned(),
            idempotency_key: "idem-cb-1".to_owned(),
            field: "payload_digest".to_owned(),
        })
    );
    assert!(duplicate.duplicate);
    assert_eq!(duplicate.receipt.callback_id, "cb-1");
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        1
    );
    Ok(())
}

#[test]
fn sqlite_async_operation_store_scopes_callback_idempotency_to_operation_after_reopen()
-> Result<(), AsyncOperationError> {
    let path = sqlite_async_operation_path("callback-idempotency-scope");
    {
        let store = SqliteAsyncOperationStore::open(&path)?;
        store.register(waiting_operation())?;
        store.register(
            AsyncOperation::new(
                "op-2",
                "run-1",
                "node-ci",
                "attempt-1",
                AsyncOperationKind::CiJob,
                "sha256:resume-token-2",
                "idem-op-2",
                "schemas/CICallback@1",
                1_000,
            )
            .submitted("gha-run-2", 1_050)
            .waiting_callback(2_000),
        )?;
        let accepted = store.accept_callback(
            valid_submission("cb-1", "provider-delivery-1"),
            &callback_schema_registry(),
        )?;
        assert!(accepted.should_resume);
    }

    let store = SqliteAsyncOperationStore::open(&path)?;
    let accepted = store.accept_callback(
        AsyncCallbackSubmission::new(
            "cb-2",
            "op-2",
            "run-1",
            "node-ci",
            "attempt-1",
            "provider-delivery-1",
            json!({"status": "completed", "workflow_run_id": "gha-run-2"}),
            1_201,
            "hmac:callback-endpoint-1",
            "policy-snapshot-1",
        ),
        &callback_schema_registry(),
    )?;

    assert!(accepted.should_resume);
    assert!(!accepted.duplicate);
    assert_eq!(accepted.receipt.operation_id, "op-2");
    assert_eq!(
        store.operation_state("op-2"),
        Some(AsyncOperationState::CallbackReceived)
    );
    Ok(())
}

#[test]
fn sqlite_async_operation_store_persists_artifact_backed_callback_receipt_across_reopen()
-> Result<(), AsyncOperationError> {
    let path = sqlite_async_operation_path("callback-artifact-reopen");
    {
        let store = SqliteAsyncOperationStore::open(&path)?;
        store.register(waiting_operation())?;
        let mut submission = valid_submission("cb-artifact", "idem-artifact");
        submission.payload = json!({
            "status": "completed",
            "workflow_run_id": "gha-run-1",
            "log": "x".repeat(512),
        });
        store.accept_callback_with_artifact_on_payload_limit(
            submission,
            &callback_schema_registry(),
            AsyncCallbackIngestionLimits {
                max_payload_bytes: 128,
            },
            CallbackArtifactRef::new("artifact-ci-log", "blob://callbacks/op-1/cb-artifact.json")
                .with_media_type("application/json"),
        )?;
    }

    let store = SqliteAsyncOperationStore::open(&path)?;
    let mut duplicate_submission = valid_submission("cb-duplicate", "idem-artifact");
    duplicate_submission.payload = json!({
        "status": "completed",
        "workflow_run_id": "gha-run-1",
        "log": "x".repeat(512),
    });
    let duplicate = store.accept_callback_with_artifact_on_payload_limit(
        duplicate_submission,
        &callback_schema_registry(),
        AsyncCallbackIngestionLimits {
            max_payload_bytes: 128,
        },
        CallbackArtifactRef::new("artifact-ci-log", "blob://callbacks/op-1/cb-artifact.json")
            .with_media_type("application/json"),
    )?;

    assert!(duplicate.duplicate);
    assert_eq!(duplicate.receipt.callback_id, "cb-artifact");
    assert_eq!(
        duplicate.receipt.artifacts,
        vec![
            CallbackArtifactRef::new("artifact-ci-log", "blob://callbacks/op-1/cb-artifact.json")
                .with_media_type("application/json")
        ]
    );
    assert!(duplicate.receipt.payload.get("log").is_none());
    assert_eq!(
        duplicate.receipt.payload["artifact"]["artifact_id"],
        "artifact-ci-log"
    );
    Ok(())
}

#[test]
fn sqlite_async_operation_store_persists_quarantined_callback_across_reopen()
-> Result<(), AsyncOperationError> {
    let path = sqlite_async_operation_path("callback-quarantine-reopen");
    {
        let store = SqliteAsyncOperationStore::open(&path)?;
        store.quarantine_callback_before_operation_commit(
            valid_submission("cb-early", "provider-delivery-early"),
            5_000,
        )?;
        assert_eq!(store.quarantined_callback_count("op-1"), 1);
    }

    let store = SqliteAsyncOperationStore::open(&path)?;
    assert_eq!(
        store.quarantined_callback_count("op-1"),
        1,
        "early callback must survive process restart before operation commit"
    );
    store.register(waiting_operation())?;
    let accepted = store.accept_quarantined_callbacks("op-1", &callback_schema_registry())?;

    assert_eq!(accepted.len(), 1);
    assert_eq!(accepted[0].receipt.callback_id, "cb-early");
    assert!(accepted[0].should_resume);
    assert_eq!(store.quarantined_callback_count("op-1"), 0);
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::CallbackReceived)
    );
    Ok(())
}

#[test]
fn sqlite_async_operation_store_discards_expired_quarantined_callback_after_reopen()
-> Result<(), AsyncOperationError> {
    let path = sqlite_async_operation_path("callback-quarantine-expired-reopen");
    {
        let store = SqliteAsyncOperationStore::open(&path)?;
        store.quarantine_callback_before_operation_commit(
            valid_submission("cb-early-expired", "provider-delivery-expired"),
            1_250,
        )?;
        assert_eq!(store.quarantined_callback_count("op-1"), 1);
    }

    let store = SqliteAsyncOperationStore::open(&path)?;
    store.register(late_committed_waiting_operation())?;
    let accepted = store.accept_quarantined_callbacks("op-1", &callback_schema_registry())?;

    assert!(accepted.is_empty());
    assert_eq!(store.quarantined_callback_count("op-1"), 0);
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback)
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        0
    );
    assert!(store.events_for_operation("op-1").iter().any(|event| {
        matches!(
            event,
            AsyncOperationEvent::ExternalCallbackRejected {
                callback_id,
                reason,
                ..
            } if callback_id == "cb-early-expired" && reason == "quarantined_callback_expired"
        )
    }));
    Ok(())
}

#[test]
fn sqlite_async_operation_store_preserves_quarantine_after_conflicting_replay()
-> Result<(), AsyncOperationError> {
    let path = sqlite_async_operation_path("callback-quarantine-conflict-reopen");
    {
        let store = SqliteAsyncOperationStore::open(&path)?;
        store.quarantine_callback_before_operation_commit(
            valid_submission("cb-early", "provider-delivery-early"),
            5_000,
        )?;
    }

    {
        let store = SqliteAsyncOperationStore::open(&path)?;
        let conflict = store.quarantine_callback_before_operation_commit(
            AsyncCallbackSubmission::new(
                "cb-early-conflict",
                "op-1",
                "run-1",
                "node-ci",
                "attempt-1",
                "provider-delivery-early",
                json!({"status": "failed", "workflow_run_id": "gha-run-1"}),
                1_201,
                "hmac:callback-endpoint-1",
                "policy-snapshot-1",
            ),
            5_001,
        );

        assert_eq!(
            conflict,
            Err(AsyncOperationError::CallbackIdempotencyConflict {
                operation_id: "op-1".to_owned(),
                idempotency_key: "provider-delivery-early".to_owned(),
                field: "payload_digest".to_owned(),
            })
        );
        assert_eq!(store.quarantined_callback_count("op-1"), 1);
    }

    let store = SqliteAsyncOperationStore::open(&path)?;
    assert_eq!(store.quarantined_callback_count("op-1"), 1);
    store.register(waiting_operation())?;
    let accepted = store.accept_quarantined_callbacks("op-1", &callback_schema_registry())?;

    assert_eq!(accepted.len(), 1);
    assert_eq!(accepted[0].receipt.callback_id, "cb-early");
    assert_eq!(
        accepted[0].receipt.payload,
        json!({"status": "completed", "workflow_run_id": "gha-run-1"})
    );
    assert_eq!(store.quarantined_callback_count("op-1"), 0);
    Ok(())
}

#[test]
fn sqlite_async_operation_store_persists_callback_rejection_events_across_reopen()
-> Result<(), AsyncOperationError> {
    let path = sqlite_async_operation_path("callback-rejection-reopen");
    {
        let store = SqliteAsyncOperationStore::open(&path)?;
        store.register(waiting_operation())?;
        let mut submission = valid_submission("cb-invalid", "idem-invalid");
        submission.payload = json!({"status": 7});
        assert!(matches!(
            store.accept_callback(submission, &callback_schema_registry()),
            Err(AsyncOperationError::CallbackSchemaInvalid { .. })
        ));
        let wrong_run = AsyncCallbackSubmission::new(
            "cb-wrong-run",
            "op-1",
            "run-other",
            "node-ci",
            "attempt-1",
            "idem-wrong-run",
            json!({"status": "completed", "workflow_run_id": "gha-run-1"}),
            1_201,
            "hmac:callback-endpoint-1",
            "policy-snapshot-1",
        );
        assert!(matches!(
            store.accept_callback(wrong_run, &callback_schema_registry()),
            Err(AsyncOperationError::OperationIdentityMismatch { .. })
        ));
    }

    let store = SqliteAsyncOperationStore::open(&path)?;
    let events = store.events_for_operation("op-1");

    assert!(events.iter().any(|event| matches!(
        event,
        AsyncOperationEvent::ExternalCallbackRejected {
            operation_id,
            callback_id,
            reason,
            verified_by,
            ..
        } if operation_id == "op-1"
            && callback_id == "cb-invalid"
            && reason == "schema_invalid"
            && verified_by == "hmac:callback-endpoint-1"
    )));
    assert!(events.iter().any(|event| matches!(
        event,
        AsyncOperationEvent::ExternalCallbackRejected {
            operation_id,
            callback_id,
            reason,
            verified_by,
            ..
        } if operation_id == "op-1"
            && callback_id == "cb-wrong-run"
            && reason == "identity_mismatch:run_id"
            && verified_by == "hmac:callback-endpoint-1"
    )));
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback)
    );
    Ok(())
}

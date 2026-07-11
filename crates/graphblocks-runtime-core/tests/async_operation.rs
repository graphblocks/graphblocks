#![allow(
    clippy::panic,
    reason = "seeded concurrency tests include seed and index in unexpected-error panics"
)]

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
use rusqlite::{Connection, params};
use serde_json::json;
use std::collections::BTreeMap;
use std::path::PathBuf;

const VALID_RESUME_TOKEN_HASH: &str =
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const VALID_RESUME_TOKEN_HASH_2: &str =
    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

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
        VALID_RESUME_TOKEN_HASH,
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
        VALID_RESUME_TOKEN_HASH,
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
    .with_provider_operation_id("gha-run-1")
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
fn bearer_callback_endpoint_rejects_whitespace_token() {
    assert_eq!(
        CallbackEndpointRef::new(
            "callback-endpoint-1",
            "https://graphblocks.example.com/v1/callbacks/op-1",
            "schemas/CICallback@1",
            CallbackEndpointAuth::bearer("secret://callbacks/op-1", " \t"),
        ),
        Err(AsyncOperationError::EmptyField {
            field: "token".to_owned(),
        })
    );
}

#[test]
fn callback_endpoint_rejects_non_http_url_scheme() {
    assert_eq!(
        CallbackEndpointRef::new(
            "callback-endpoint-1",
            "file:///tmp/callback",
            "schemas/CICallback@1",
            CallbackEndpointAuth::bearer("secret://callbacks/op-1", "top-secret"),
        ),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "callback-endpoint-1".to_owned(),
            reason: "callback endpoint url must use http or https".to_owned(),
        })
    );
}

#[test]
fn callback_endpoint_rejects_url_with_surrounding_whitespace() {
    assert_eq!(
        CallbackEndpointRef::new(
            "callback-endpoint-1",
            " https://graphblocks.example.com/v1/callbacks/op-1 ",
            "schemas/CICallback@1",
            CallbackEndpointAuth::bearer("secret://callbacks/op-1", "top-secret"),
        ),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "callback-endpoint-1".to_owned(),
            reason: "callback endpoint url must not include surrounding whitespace".to_owned(),
        })
    );
}

#[test]
fn callback_endpoint_rejects_malformed_url_host_syntax() {
    for url in [
        "https://hooks example.com/v1/callbacks/op-1",
        "https://graphblocks.example.com\t/v1/callbacks/op-1",
        "https://graphblocks.example.com%2fevil/v1/callbacks/op-1",
        "https://[not-ipv6]/v1/callbacks/op-1",
        "https://[fe80::1%25eth0]/v1/callbacks/op-1",
    ] {
        assert_eq!(
            CallbackEndpointRef::new(
                "callback-endpoint-1",
                url,
                "schemas/CICallback@1",
                CallbackEndpointAuth::bearer("secret://callbacks/op-1", "top-secret"),
            ),
            Err(AsyncOperationError::InvalidOperation {
                operation_id: "callback-endpoint-1".to_owned(),
                reason: "callback endpoint url host is malformed".to_owned(),
            }),
            "{url} should fail before callback endpoint registration",
        );
    }
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
fn callback_endpoint_rejects_surrounding_whitespace_in_bound_identity() {
    for field in [
        "operation_id",
        "run_id",
        "node_id",
        "attempt_id",
        "release_id",
        "tenant_id",
    ] {
        let mut endpoint = CallbackEndpointRef::new_bound(
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
        .expect("endpoint is initially valid");

        match field {
            "operation_id" => endpoint.operation_id = Some(" op-1".to_owned()),
            "run_id" => endpoint.run_id = Some("run-1 ".to_owned()),
            "node_id" => endpoint.node_id = Some("\tnode-ci".to_owned()),
            "attempt_id" => endpoint.attempt_id = Some("attempt-1\n".to_owned()),
            "release_id" => endpoint.release_id = Some(" release-1 ".to_owned()),
            "tenant_id" => endpoint.tenant_id = Some(" tenant-1 ".to_owned()),
            _ => unreachable!("test field list is exhaustive"),
        }

        assert_eq!(
            endpoint.validate(),
            Err(AsyncOperationError::InvalidOperation {
                operation_id: "callback-endpoint-1".to_owned(),
                reason: format!(
                    "callback endpoint bound identity field {field} must not include surrounding whitespace"
                ),
            }),
            "{field} should preserve exact signed callback route identity",
        );
    }
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
        let node_id = if field == "node_id" { " \t" } else { "node-ci" };
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
fn oidc_callback_endpoint_rejects_blank_bearer_token_before_verifier() {
    let endpoint = CallbackEndpointRef::new(
        "callback-endpoint-oidc",
        "https://graphblocks.example.com/v1/callbacks/op-1",
        "schemas/CICallback@1",
        CallbackEndpointAuth::oidc("https://issuer.example.com", "graphblocks-callbacks"),
    )
    .expect("endpoint is valid");
    let mut headers = BTreeMap::new();
    headers.insert("Authorization".to_owned(), "Bearer \t".to_owned());

    assert_eq!(
        endpoint.authenticate_oidc_and_build_submission(
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
            |_issuer, _audience, _token| true,
        ),
        Err(AsyncOperationError::CallbackAuthenticationFailed {
            endpoint_id: "callback-endpoint-oidc".to_owned(),
            reason: "oidc_token_empty".to_owned(),
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
        VALID_RESUME_TOKEN_HASH,
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
fn async_operation_diagnostics_allow_explicit_infinite_wait_policy() {
    let operation = waiting_operation()
        .with_infinite_wait_policy("operator_review_required")
        .without_expiration();

    assert_eq!(
        AsyncOperationConfigurationDiagnostic::for_operation(&operation),
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
        VALID_RESUME_TOKEN_HASH,
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

    let mut created_with_submitted_at = created_with_provider.clone();
    created_with_submitted_at.provider_operation_id = None;
    created_with_submitted_at.submitted_at_unix_ms = Some(1_001);

    assert_eq!(
        created_with_submitted_at.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-created".to_owned(),
            reason: "created operations cannot have submitted_at".to_owned(),
        })
    );

    let mut created_with_completed_at = created_with_provider.clone();
    created_with_completed_at.provider_operation_id = None;
    created_with_completed_at.completed_at_unix_ms = Some(1_002);

    assert_eq!(
        created_with_completed_at.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-created".to_owned(),
            reason: "created operations cannot have completed_at".to_owned(),
        })
    );

    let mut created_with_expires_at = created_with_provider.clone();
    created_with_expires_at.provider_operation_id = None;
    created_with_expires_at.expires_at_unix_ms = Some(2_000);

    assert_eq!(
        created_with_expires_at.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-created".to_owned(),
            reason: "created operations cannot have expires_at".to_owned(),
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
        VALID_RESUME_TOKEN_HASH,
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
fn async_operation_validate_rejects_noncanonical_resume_token_hash() {
    for resume_token_hash in ["sha256:resume-token".to_owned(), "a".repeat(64)] {
        let operation = AsyncOperation::new(
            "op-bad-resume-token",
            "run-1",
            "node-ci",
            "attempt-1",
            AsyncOperationKind::CiJob,
            resume_token_hash,
            "idem-op-bad-resume-token",
            "schemas/CICallback@1",
            1_000,
        );

        assert_eq!(
            operation.validate(),
            Err(AsyncOperationError::InvalidOperation {
                operation_id: "op-bad-resume-token".to_owned(),
                reason: "resume_token_hash must be a canonical sha256 digest".to_owned(),
            })
        );
    }
}

#[test]
fn async_operation_validate_rejects_out_of_order_state_timestamps() {
    let submitted_before_created = AsyncOperation::new(
        "op-submitted",
        "run-1",
        "node-ci",
        "attempt-1",
        AsyncOperationKind::CiJob,
        VALID_RESUME_TOKEN_HASH,
        "idem-op-submitted",
        "schemas/CICallback@1",
        1_000,
    )
    .submitted("gha-run-1", 999);
    let mut completed_before_submitted = waiting_operation();
    completed_before_submitted.state = AsyncOperationState::Completed;
    completed_before_submitted.completed_at_unix_ms = completed_before_submitted
        .submitted_at_unix_ms
        .map(|submitted_at| submitted_at - 1);
    let mut expires_before_submitted = waiting_operation();
    expires_before_submitted.expires_at_unix_ms = expires_before_submitted
        .submitted_at_unix_ms
        .map(|submitted_at| submitted_at - 1);

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
    assert_eq!(
        expires_before_submitted.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-1".to_owned(),
            reason: "expires_at must be after submitted_at".to_owned(),
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
fn async_operation_validate_allows_callback_received_without_terminal_completion_time() {
    let mut resumable = waiting_operation();
    resumable.state = AsyncOperationState::CallbackReceived;
    resumable.completed_at_unix_ms = None;

    assert_eq!(resumable.validate(), Ok(()));
}

#[test]
fn async_operation_validate_rejects_callback_received_receipt_time_after_expiration() {
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
            reason: "callback_received operations require an expiration or infinite_wait_policy"
                .to_owned(),
        })
    );
}

#[test]
fn async_operation_validate_accepts_callback_received_with_infinite_wait_policy() {
    let mut operation = waiting_operation()
        .with_infinite_wait_policy("operator_review_required")
        .without_expiration();
    operation.state = AsyncOperationState::CallbackReceived;
    operation.completed_at_unix_ms = Some(1_500);

    assert_eq!(operation.validate(), Ok(()));
}

#[test]
fn async_operation_validate_rejects_ambiguous_expiration_and_infinite_wait_policy() {
    let callback_wait = waiting_operation().with_infinite_wait_policy("operator_review_required");
    let mut polling_wait = waiting_operation().with_infinite_wait_policy("provider_has_no_timeout");
    polling_wait.state = AsyncOperationState::Polling;

    for operation in [callback_wait, polling_wait] {
        assert_eq!(
            operation.validate(),
            Err(AsyncOperationError::InvalidOperation {
                operation_id: "op-1".to_owned(),
                reason: "async operation wait must not define both expires_at_unix_ms and infinite_wait_policy".to_owned(),
            })
        );
    }
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
            reason: "polling operations require an expiration or infinite_wait_policy".to_owned(),
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
fn callback_received_after_operation_expiration_is_rejected_without_resume() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");
    let mut submission = valid_submission("cb-after-expiration", "idem-after-expiration");
    submission.received_at_unix_ms = 2_001;

    assert_eq!(
        store.accept_callback(submission, &callback_schema_registry()),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-1".to_owned(),
            reason: "callback received after operation expiration".to_owned(),
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
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        0
    );
    assert!(events.iter().any(|event| matches!(
        event,
        AsyncOperationEvent::ExternalCallbackRejected {
            callback_id,
            reason,
            ..
        } if callback_id == "cb-after-expiration" && reason == "callback_after_expiration"
    )));
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
fn idempotency_conflict_rejection_records_callback_receipt_metadata() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");
    store
        .accept_callback(
            valid_submission("cb-1", "idem-cb-1"),
            &callback_schema_registry(),
        )
        .expect("first callback is accepted");

    let mut mutated = valid_submission("cb-mutated", "idem-cb-1");
    mutated.payload = json!({"status": "failed", "workflow_run_id": "gha-run-1"});
    let expected_payload_digest = graphblocks_compiler::canonical::canonical_hash(&mutated.payload);

    assert_eq!(
        store.accept_callback(mutated, &callback_schema_registry()),
        Err(AsyncOperationError::CallbackIdempotencyConflict {
            operation_id: "op-1".to_owned(),
            idempotency_key: "idem-cb-1".to_owned(),
            field: "payload_digest".to_owned(),
        })
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
                    payload_digest,
                    verified_by,
                    policy_snapshot_id,
                    ..
                } if callback_id == "cb-mutated"
                    && reason == "idempotency_conflict:payload_digest"
                    && payload_digest == &expected_payload_digest
                    && verified_by == "hmac:callback-endpoint-1"
                    && policy_snapshot_id == "policy-snapshot-1"
            ))
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
                VALID_RESUME_TOKEN_HASH,
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
                } if callback_id == "cb-not-waiting"
                    && reason == "operation_not_waiting_callback:submitted"
            ))
    );
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
    assert!(
        store
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
            ))
    );
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
fn superseded_quarantined_callbacks_are_audited_after_first_resume() {
    let store = AsyncOperationStore::new();
    store
        .quarantine_callback_before_operation_commit(
            valid_submission("cb-early-first", "provider-delivery-first"),
            5_000,
        )
        .expect("first early callback is quarantined");
    store
        .quarantine_callback_before_operation_commit(
            valid_submission("cb-early-second", "provider-delivery-second"),
            5_000,
        )
        .expect("second early callback is quarantined");

    store
        .register(waiting_operation())
        .expect("operation is registered after callback ingress");
    let accepted = store
        .accept_quarantined_callbacks("op-1", &callback_schema_registry())
        .expect("quarantined callbacks are consumed");

    assert_eq!(accepted.len(), 1);
    assert_eq!(accepted[0].receipt.callback_id, "cb-early-first");
    assert!(accepted[0].should_resume);
    assert_eq!(store.quarantined_callback_count("op-1"), 0);
    assert!(store.events_for_operation("op-1").iter().any(|event| {
        matches!(
            event,
            AsyncOperationEvent::ExternalCallbackRejected {
                callback_id,
                reason,
                ..
            } if callback_id == "cb-early-second"
                && reason == "quarantined_callback_superseded"
        )
    }));
}

#[test]
fn invalid_quarantined_callback_does_not_block_later_valid_callback() {
    let store = AsyncOperationStore::new();
    store
        .quarantine_callback_before_operation_commit(
            AsyncCallbackSubmission::new(
                "cb-early-invalid",
                "op-1",
                "run-1",
                "node-ci",
                "attempt-1",
                "provider-delivery-0-invalid",
                json!({"status": 7}),
                1_200,
                "hmac:callback-endpoint-1",
                "policy-snapshot-1",
            )
            .with_provider_operation_id("gha-run-1"),
            5_000,
        )
        .expect("invalid early callback is quarantined before schema is known");
    store
        .quarantine_callback_before_operation_commit(
            valid_submission("cb-early-valid", "provider-delivery-1-valid"),
            5_000,
        )
        .expect("valid early callback is quarantined");

    store
        .register(waiting_operation())
        .expect("operation is registered after callback ingress");
    let accepted = store
        .accept_quarantined_callbacks("op-1", &callback_schema_registry())
        .expect("invalid quarantined callback is rejected and valid callback still resumes");

    assert_eq!(accepted.len(), 1);
    assert_eq!(accepted[0].receipt.callback_id, "cb-early-valid");
    assert!(accepted[0].should_resume);
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::CallbackReceived)
    );
    assert!(store.events_for_operation("op-1").iter().any(|event| {
        matches!(
            event,
            AsyncOperationEvent::ExternalCallbackRejected {
                callback_id,
                reason,
                ..
            } if callback_id == "cb-early-invalid" && reason == "schema_invalid"
        )
    }));
}

#[test]
fn quarantined_callbacks_replay_in_arrival_order_not_idempotency_key_order() {
    let store = AsyncOperationStore::new();
    let mut first_arrival = valid_submission("cb-early-first-arrival", "provider-z-first");
    first_arrival.received_at_unix_ms = 1_200;
    let mut second_arrival = valid_submission("cb-early-second-arrival", "provider-a-second");
    second_arrival.received_at_unix_ms = 1_201;

    store
        .quarantine_callback_before_operation_commit(first_arrival, 5_000)
        .expect("first arrival is quarantined");
    store
        .quarantine_callback_before_operation_commit(second_arrival, 5_000)
        .expect("second arrival is quarantined");

    store
        .register(waiting_operation())
        .expect("operation is registered after callback ingress");
    let accepted = store
        .accept_quarantined_callbacks("op-1", &callback_schema_registry())
        .expect("quarantined callbacks are replayed");

    assert_eq!(accepted.len(), 1);
    assert_eq!(accepted[0].receipt.callback_id, "cb-early-first-arrival");
    assert!(store.events_for_operation("op-1").iter().any(|event| {
        matches!(
            event,
            AsyncOperationEvent::ExternalCallbackRejected {
                callback_id,
                reason,
                ..
            } if callback_id == "cb-early-second-arrival"
                && reason == "quarantined_callback_superseded"
        )
    }));
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
        )
        .with_provider_operation_id("gha-run-1"),
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
                )
                .with_provider_operation_id("gha-run-1"),
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
        )
        .with_provider_operation_id("gha-run-1"),
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
                VALID_RESUME_TOKEN_HASH_2,
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
            )
            .with_provider_operation_id("gha-run-2"),
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
    )
    .with_provider_operation_id("gha-run-1");
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
    )
    .with_provider_operation_id("gha-run-1");
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
    )
    .with_provider_operation_id("gha-run-1");
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
    )
    .with_provider_operation_id("gha-run-1");

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
fn callback_provider_operation_mismatch_does_not_resume_run() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");
    let mut submission = valid_submission("cb-wrong-provider-operation", "idem-wrong-provider");
    submission.provider_operation_id = Some("gha-run-other".to_owned());

    assert_eq!(
        store.accept_callback(submission, &callback_schema_registry()),
        Err(AsyncOperationError::OperationIdentityMismatch {
            operation_id: "op-1".to_owned(),
            field: "provider_operation_id".to_owned(),
            expected: "gha-run-1".to_owned(),
            actual: "gha-run-other".to_owned(),
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
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        0
    );
    assert!(events.iter().any(|event| matches!(
        event,
        AsyncOperationEvent::ExternalCallbackRejected {
            callback_id,
            reason,
            ..
        } if callback_id == "cb-wrong-provider-operation"
            && reason == "identity_mismatch:provider_operation_id"
    )));
}

#[test]
fn callback_missing_provider_operation_identity_does_not_resume_run() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");
    let mut submission = valid_submission("cb-missing-provider-operation", "idem-missing-provider");
    submission.provider_operation_id = None;

    assert_eq!(
        store.accept_callback(submission, &callback_schema_registry()),
        Err(AsyncOperationError::OperationIdentityMismatch {
            operation_id: "op-1".to_owned(),
            field: "provider_operation_id".to_owned(),
            expected: "gha-run-1".to_owned(),
            actual: String::new(),
        })
    );
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback)
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
                } if callback_id == "cb-missing-provider-operation"
                    && reason == "identity_mismatch:provider_operation_id"
            ))
    );
}

#[test]
fn unauthenticated_callback_submission_does_not_resume_run() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");
    let mut submission = valid_submission("cb-unauthenticated", "idem-unauthenticated");
    submission.verified_by = "unauthenticated".to_owned();

    assert_eq!(
        store.accept_callback(submission, &callback_schema_registry()),
        Err(AsyncOperationError::CallbackAuthenticationFailed {
            endpoint_id: "async_callback".to_owned(),
            reason: "unauthenticated_callback".to_owned(),
        })
    );
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback)
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
                    verified_by,
                    ..
                } if callback_id == "cb-unauthenticated"
                    && reason == "authentication_failed"
                    && verified_by == "unauthenticated"
            ))
    );
}

#[test]
fn unauthenticated_artifact_backed_callback_submission_does_not_resume_run() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");
    let mut submission = valid_submission(
        "cb-unauthenticated-artifact",
        "idem-unauthenticated-artifact",
    );
    submission.verified_by = "unauthenticated".to_owned();
    submission.payload = json!({
        "status": "completed",
        "workflow_run_id": "gha-run-1",
        "log": "x".repeat(512),
    });

    assert_eq!(
        store.accept_callback_with_artifact_on_payload_limit(
            submission,
            &callback_schema_registry(),
            AsyncCallbackIngestionLimits {
                max_payload_bytes: 128,
            },
            CallbackArtifactRef::new(
                "artifact-ci-log",
                "blob://callbacks/op-1/cb-unauthenticated.json"
            ),
        ),
        Err(AsyncOperationError::CallbackAuthenticationFailed {
            endpoint_id: "async_callback".to_owned(),
            reason: "unauthenticated_callback".to_owned(),
        })
    );
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback)
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
            } if callback_id == "cb-unauthenticated-artifact" && reason == "authentication_failed"
        )));
}

#[test]
fn unauthenticated_early_callback_is_not_quarantined() {
    let store = AsyncOperationStore::new();
    let mut submission = valid_submission("cb-early-unauthenticated", "idem-early-unauthenticated");
    submission.verified_by = "unauthenticated".to_owned();

    assert_eq!(
        store.quarantine_callback_before_operation_commit(submission, 5_000),
        Err(AsyncOperationError::CallbackAuthenticationFailed {
            endpoint_id: "async_callback".to_owned(),
            reason: "unauthenticated_callback".to_owned(),
        })
    );
    assert_eq!(store.quarantined_callback_count("op-1"), 0);
}

#[test]
fn sqlite_callback_missing_provider_operation_identity_does_not_resume_run()
-> Result<(), AsyncOperationError> {
    let path = sqlite_async_operation_path("missing-provider-operation");
    let store = SqliteAsyncOperationStore::open(&path)?;
    store.register(waiting_operation())?;
    let mut submission = valid_submission(
        "cb-sqlite-missing-provider-operation",
        "idem-sqlite-missing-provider",
    );
    submission.provider_operation_id = None;

    assert_eq!(
        store.accept_callback(submission, &callback_schema_registry()),
        Err(AsyncOperationError::OperationIdentityMismatch {
            operation_id: "op-1".to_owned(),
            field: "provider_operation_id".to_owned(),
            expected: "gha-run-1".to_owned(),
            actual: String::new(),
        })
    );
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback)
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
                } if callback_id == "cb-sqlite-missing-provider-operation"
                    && reason == "identity_mismatch:provider_operation_id"
            ))
    );
    Ok(())
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
        "provider_operation_id",
        "infinite_wait_policy",
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
            "provider_operation_id" => operation.provider_operation_id = Some(" \t".to_owned()),
            "infinite_wait_policy" => operation.infinite_wait_policy = Some(" \t".to_owned()),
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
fn callback_at_operation_deadline_is_rejected_without_resume() {
    let store = AsyncOperationStore::new();
    store
        .register(waiting_operation())
        .expect("operation registers");
    let mut submission = valid_submission("cb-at-deadline", "idem-at-deadline");
    submission.received_at_unix_ms = 2_000;

    let error = store
        .accept_callback(submission, &callback_schema_registry())
        .expect_err("callback received at deadline must be rejected");

    assert_eq!(
        error,
        AsyncOperationError::InvalidOperation {
            operation_id: "op-1".to_owned(),
            reason: "callback received after operation expiration".to_owned(),
        }
    );
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback)
    );
    assert!(store.events_for_operation("op-1").iter().any(|event| {
        matches!(
            event,
            AsyncOperationEvent::ExternalCallbackRejected {
                callback_id,
                reason,
                ..
            } if callback_id == "cb-at-deadline" && reason == "callback_after_expiration"
        )
    }));
    assert!(!store.events_for_operation("op-1").iter().any(|event| {
        matches!(
            event,
            AsyncOperationEvent::StateChanged {
                to: AsyncOperationState::CallbackReceived,
                ..
            }
        )
    }));
}

#[test]
fn sqlite_expired_operation_after_deadline_survives_reopen() -> Result<(), AsyncOperationError> {
    let path = sqlite_async_operation_path("expired-after-deadline-reopen");
    {
        let store = SqliteAsyncOperationStore::open(&path)?;
        store.register(waiting_operation())?;
        store.expire_operation("op-1", 2_001)?;
    }

    let store = SqliteAsyncOperationStore::open(&path)?;
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::Expired)
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| {
                matches!(
                    event,
                    AsyncOperationEvent::StateChanged {
                        to: AsyncOperationState::Expired,
                        ..
                    }
                )
            })
            .count(),
        1
    );

    let _ = std::fs::remove_file(path);
    Ok(())
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
    store
        .register(operation)
        .expect("completed operation registers");

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
fn cancel_and_expire_reject_zero_or_regressed_terminal_timestamps() {
    for (label, terminal_at) in [("zero", 0), ("before_submitted", 1_049)] {
        let store = AsyncOperationStore::new();
        store
            .register(waiting_operation())
            .expect("operation registers");

        assert_eq!(
            store.cancel_operation("op-1", terminal_at),
            Err(AsyncOperationError::InvalidOperation {
                operation_id: "op-1".to_owned(),
                reason: if terminal_at == 0 {
                    "cancelled_at must be positive".to_owned()
                } else {
                    "cancelled_at precedes submitted_at".to_owned()
                },
            }),
            "cancel timestamp case {label} should be rejected",
        );
        assert_eq!(
            store.operation_state("op-1"),
            Some(AsyncOperationState::WaitingCallback)
        );
    }

    for (label, terminal_at) in [("zero", 0), ("before_submitted", 1_049)] {
        let store = AsyncOperationStore::new();
        store
            .register(waiting_operation())
            .expect("operation registers");

        assert_eq!(
            store.expire_operation("op-1", terminal_at),
            Err(AsyncOperationError::InvalidOperation {
                operation_id: "op-1".to_owned(),
                reason: if terminal_at == 0 {
                    "expired_at must be positive".to_owned()
                } else {
                    "expired_at precedes submitted_at".to_owned()
                },
            }),
            "expire timestamp case {label} should be rejected",
        );
        assert_eq!(
            store.operation_state("op-1"),
            Some(AsyncOperationState::WaitingCallback)
        );
    }
}

#[test]
fn cancelled_async_operation_result_preserves_committed_external_effect() {
    let result =
        AsyncOperationResult::cancelled("op-1").with_external_effects([ExternalEffectRecord::new(
            "effect-ticket-1",
            "ticket-system",
            "ticket.create",
            ToolEffectOutcome::Committed,
        )
        .with_idempotency_key("idem-ticket-1")
        .with_provider_effect_id("ticket-123")]);

    assert_eq!(result.status, AsyncOperationResultStatus::Cancelled);
    assert_eq!(result.validate(), Ok(()));
    assert!(result.external_effect_was_committed());
    assert_eq!(
        result.external_effects[0].outcome,
        ToolEffectOutcome::Committed
    );
    assert_eq!(
        result.external_effects[0].idempotency_key.as_deref(),
        Some("idem-ticket-1")
    );
}

#[test]
fn async_operation_result_projects_protocol_json() {
    let result = AsyncOperationResult::cancelled("op-1")
        .with_output(json!({"status": "cancelled_after_commit"}))
        .with_external_effects([ExternalEffectRecord::new(
            "effect-ticket-1",
            "ticket-system",
            "ticket.create",
            ToolEffectOutcome::Committed,
        )
        .with_idempotency_key("idem-ticket-1")
        .with_provider_effect_id("ticket-123")]);

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
    let blank_effect =
        AsyncOperationResult::completed("op-1").with_external_effects([ExternalEffectRecord::new(
            " ",
            "ticket-system",
            "ticket.create",
            ToolEffectOutcome::Committed,
        )]);
    let impossible_denied_effect =
        AsyncOperationResult::failed("op-2").with_external_effects([ExternalEffectRecord::new(
            "effect-denied",
            "ticket-system",
            "ticket.create",
            ToolEffectOutcome::NoExternalEffect,
        )
        .with_provider_effect_id("ticket-123")]);

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
    let missing_idempotency =
        AsyncOperationResult::cancelled("op-1").with_external_effects([ExternalEffectRecord::new(
            "effect-ticket-1",
            "ticket-system",
            "ticket.create",
            ToolEffectOutcome::Committed,
        )
        .with_provider_effect_id("ticket-123")]);

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
    let duplicate_provider_effect_id = AsyncOperationResult::completed("op-2")
        .with_external_effects([
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
fn async_operation_result_rejects_duplicate_artifact_ids() {
    let mut result = AsyncOperationResult::completed("op-1");
    result.artifacts = vec![
        CallbackArtifactRef::new("artifact-ci-log", "blob://callbacks/op-1/log-1.json"),
        CallbackArtifactRef::new("artifact-ci-log", "blob://callbacks/op-1/log-2.json"),
    ];

    assert_eq!(
        result.validate(),
        Err(AsyncOperationError::InvalidOperation {
            operation_id: "op-1".to_owned(),
            reason: "duplicate artifact id artifact-ci-log".to_owned(),
        })
    );
}

#[test]
fn async_operation_result_rejects_non_object_record_lists() {
    for (field, value) in [
        ("diagnostics", json!("warning")),
        ("metrics", json!(42)),
        ("checks", json!(true)),
        ("usage", json!("tokens")),
    ] {
        let mut result = AsyncOperationResult::completed("op-1");
        match field {
            "diagnostics" => result.diagnostics.push(value),
            "metrics" => result.metrics.push(value),
            "checks" => result.checks.push(value),
            "usage" => result.usage.push(value),
            _ => unreachable!("test fields are exhaustive"),
        }

        assert_eq!(
            result.validate(),
            Err(AsyncOperationError::InvalidOperation {
                operation_id: "op-1".to_owned(),
                reason: format!("{field} entries must be JSON objects"),
            }),
            "{field} should reject scalar entries",
        );
    }
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
                )
                .with_provider_operation_id("gha-run-1"),
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
fn sqlite_async_operation_store_rejects_operation_row_identity_mismatch_on_reopen() {
    let path = sqlite_async_operation_path("operation-row-identity");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store opens");
        store
            .register(waiting_operation())
            .expect("operation registers");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let operation_json: String = connection
            .query_row(
                "SELECT operation_json FROM async_operations WHERE operation_id = ?1",
                params!["op-1"],
                |row| row.get(0),
            )
            .expect("operation row exists");
        let mut operation: serde_json::Value =
            serde_json::from_str(&operation_json).expect("operation json parses");
        operation["operation_id"] = json!("op-forged");
        connection
            .execute(
                "UPDATE async_operations SET operation_json = ?1 WHERE operation_id = ?2",
                params![
                    serde_json::to_string(&operation).expect("operation serializes"),
                    "op-1"
                ],
            )
            .expect("operation row is tampered");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store reopens");
    let error = store
        .register(waiting_operation())
        .expect_err("operation row identity mismatch must fail durable replay");

    assert!(
        matches!(
            error,
            AsyncOperationError::Storage { ref message }
                if message.contains("stored async operation identity does not match row key")
        ),
        "unexpected error: {error:?}"
    );
}

#[test]
fn sqlite_async_operation_store_rejects_invalid_operation_json_on_reopen() {
    let path = sqlite_async_operation_path("operation-replay-validation");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store opens");
        store
            .register(waiting_operation())
            .expect("operation registers");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let operation_json: String = connection
            .query_row(
                "SELECT operation_json FROM async_operations WHERE operation_id = ?1",
                params!["op-1"],
                |row| row.get(0),
            )
            .expect("operation row exists");
        let mut operation: serde_json::Value =
            serde_json::from_str(&operation_json).expect("operation json parses");
        operation["run_id"] = json!(" \t");
        connection
            .execute(
                "UPDATE async_operations SET operation_json = ?1 WHERE operation_id = ?2",
                params![
                    serde_json::to_string(&operation).expect("operation serializes"),
                    "op-1"
                ],
            )
            .expect("operation row is tampered");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store reopens");
    let error = store
        .register(waiting_operation())
        .expect_err("invalid stored operation must fail durable replay");

    assert_eq!(
        error,
        AsyncOperationError::EmptyField {
            field: "run_id".to_owned()
        }
    );
}

#[test]
fn sqlite_async_operation_store_rejects_event_row_identity_mismatch_on_reopen() {
    let path = sqlite_async_operation_path("event-row-identity");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store opens");
        store
            .register(waiting_operation())
            .expect("operation registers");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let event_json: String = connection
            .query_row(
                "SELECT event_json FROM async_operation_events WHERE operation_id = ?1 AND event_index = ?2",
                params!["op-1", 1_i64],
                |row| row.get(0),
            )
            .expect("event row exists");
        let mut event: serde_json::Value =
            serde_json::from_str(&event_json).expect("event json parses");
        event["operation_id"] = json!("op-forged");
        connection
            .execute(
                "UPDATE async_operation_events SET event_json = ?1 WHERE operation_id = ?2 AND event_index = ?3",
                params![
                    serde_json::to_string(&event).expect("event serializes"),
                    "op-1",
                    1_i64
                ],
            )
            .expect("event row is tampered");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store reopens");
    let error = store
        .register(waiting_operation())
        .expect_err("event row identity mismatch must fail durable replay");

    assert!(
        matches!(
            error,
            AsyncOperationError::Storage { ref message }
                if message.contains("stored async operation event identity does not match row key")
        ),
        "unexpected error: {error:?}"
    );
}

#[test]
fn sqlite_async_operation_store_rejects_event_index_gap_on_reopen() {
    let path = sqlite_async_operation_path("event-index-gap");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store opens");
        store
            .register(waiting_operation())
            .expect("operation registers");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        connection
            .execute(
                "UPDATE async_operation_events SET event_index = ?1 WHERE operation_id = ?2 AND event_index = ?3",
                params![2_i64, "op-1", 1_i64],
            )
            .expect("event index is tampered");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store reopens");
    let error = store
        .register(waiting_operation())
        .expect_err("event index gap must fail durable replay");

    assert!(
        matches!(
            error,
            AsyncOperationError::Storage { ref message }
                if message.contains("stored async operation event index is not contiguous")
        ),
        "unexpected error: {error:?}"
    );
}

#[test]
fn sqlite_async_operation_store_rejects_invalid_event_metadata_on_reopen() {
    let path = sqlite_async_operation_path("event-metadata-validation");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store opens");
        store
            .register(waiting_operation())
            .expect("operation registers");
        let mut submission = valid_submission("cb-invalid-event", "idem-invalid-event");
        submission.payload = json!({"status": 7});
        assert!(matches!(
            store.accept_callback(submission, &callback_schema_registry()),
            Err(AsyncOperationError::CallbackSchemaInvalid { .. })
        ));
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let event_json: String = connection
            .query_row(
                "SELECT event_json FROM async_operation_events WHERE operation_id = ?1 AND event_index = ?2",
                params!["op-1", 2_i64],
                |row| row.get(0),
            )
            .expect("callback rejection event row exists");
        let mut event: serde_json::Value =
            serde_json::from_str(&event_json).expect("event json parses");
        event["callback_id"] = json!("  ");
        connection
            .execute(
                "UPDATE async_operation_events SET event_json = ?1 WHERE operation_id = ?2 AND event_index = ?3",
                params![
                    serde_json::to_string(&event).expect("event serializes"),
                    "op-1",
                    2_i64
                ],
            )
            .expect("event row is tampered");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store reopens");
    let error = store
        .register(waiting_operation())
        .expect_err("invalid event metadata must fail durable replay");

    assert!(
        matches!(error, AsyncOperationError::EmptyField { ref field } if field == "callback_id"),
        "unexpected error: {error:?}"
    );
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
fn sqlite_async_operation_store_preserves_concurrent_registers_across_handles() {
    let path = sqlite_async_operation_path("concurrent-register");
    SqliteAsyncOperationStore::open(&path).expect("database initializes");
    let barrier = Arc::new(Barrier::new(17));
    let mut workers = Vec::new();
    for index in 0..16 {
        let store = SqliteAsyncOperationStore::open(&path).expect("worker store opens");
        let barrier = barrier.clone();
        workers.push(thread::spawn(move || {
            let mut operation = waiting_operation();
            operation.operation_id = format!("op-{index}");
            operation.idempotency_key = format!("idem-op-{index}");
            barrier.wait();
            store.register(operation)
        }));
    }

    barrier.wait();
    for worker in workers {
        worker
            .join()
            .expect("worker joins")
            .expect("concurrent registration succeeds");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("store reopens");
    for index in 0..16 {
        assert_eq!(
            store.operation_state(&format!("op-{index}")),
            Some(AsyncOperationState::WaitingCallback),
            "operation from worker {index} must not be lost",
        );
    }
}

#[test]
fn sqlite_async_operation_reads_one_coherent_database_snapshot() {
    let path = sqlite_async_operation_path("coherent-read-snapshot");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("target store opens");
        store
            .register(waiting_operation())
            .expect("anchor operation registers");
    }
    {
        let mut connection = Connection::open(&path).expect("target sqlite connection opens");
        connection
            .execute_batch("PRAGMA journal_mode = WAL; PRAGMA busy_timeout = 5000;")
            .expect("WAL mode enables concurrent reader and writer");
        let anchor_json: String = connection
            .query_row(
                "SELECT operation_json FROM async_operations WHERE operation_id = ?1",
                params!["op-1"],
                |row| row.get(0),
            )
            .expect("anchor operation row exists");
        let mut anchor: serde_json::Value =
            serde_json::from_str(&anchor_json).expect("anchor operation parses");
        let transaction = connection.transaction().expect("filler transaction starts");
        for index in 0..5_000 {
            let operation_id = format!("op-filler-{index:05}");
            anchor["operation_id"] = json!(operation_id);
            anchor["idempotency_key"] = json!(format!("idem-filler-{index:05}"));
            transaction
                .execute(
                    "INSERT INTO async_operations (operation_id, operation_json) VALUES (?1, ?2)",
                    params![
                        anchor["operation_id"]
                            .as_str()
                            .expect("operation id is a string"),
                        serde_json::to_string(&anchor).expect("filler operation serializes"),
                    ],
                )
                .expect("filler operation inserts");
        }
        transaction.commit().expect("filler transaction commits");
    }

    let source_path = sqlite_async_operation_path("coherent-read-source");
    {
        let store = SqliteAsyncOperationStore::open(&source_path).expect("source store opens");
        let mut operation = waiting_operation();
        operation.operation_id = "op-concurrent".to_owned();
        operation.idempotency_key = "idem-op-concurrent".to_owned();
        store
            .register(operation)
            .expect("source operation registers");
        let mut submission = valid_submission("cb-concurrent", "idem-cb-concurrent");
        submission.operation_id = "op-concurrent".to_owned();
        store
            .accept_callback(submission, &callback_schema_registry())
            .expect("source callback commits");
    }
    let source = Connection::open(&source_path).expect("source sqlite connection opens");
    let operation_json: String = source
        .query_row(
            "SELECT operation_json FROM async_operations WHERE operation_id = ?1",
            params!["op-concurrent"],
            |row| row.get(0),
        )
        .expect("concurrent operation row exists");
    let receipt_json: String = source
        .query_row(
            "SELECT receipt_json FROM async_callback_receipts WHERE operation_id = ?1",
            params!["op-concurrent"],
            |row| row.get(0),
        )
        .expect("concurrent receipt row exists");

    let reader = SqliteAsyncOperationStore::open(&path).expect("reader store opens");
    let barrier = Arc::new(Barrier::new(3));
    let reader_barrier = barrier.clone();
    let read = thread::spawn(move || {
        reader_barrier.wait();
        reader.operation_state("op-1")
    });
    let writer_barrier = barrier.clone();
    let writer_path = path.clone();
    let write = thread::spawn(move || {
        let mut connection = Connection::open(writer_path).expect("writer connection opens");
        connection
            .execute_batch("PRAGMA busy_timeout = 5000;")
            .expect("writer timeout configures");
        writer_barrier.wait();
        thread::sleep(std::time::Duration::from_millis(2));
        let transaction = connection.transaction().expect("writer transaction starts");
        transaction
            .execute(
                "INSERT INTO async_operations (operation_id, operation_json) VALUES (?1, ?2)",
                params!["op-concurrent", operation_json],
            )
            .expect("concurrent operation inserts");
        transaction
            .execute(
                "INSERT INTO async_callback_receipts (operation_id, idempotency_key, receipt_json) VALUES (?1, ?2, ?3)",
                params!["op-concurrent", "idem-cb-concurrent", receipt_json],
            )
            .expect("concurrent receipt inserts");
        transaction.commit().expect("writer transaction commits");
    });

    barrier.wait();
    write.join().expect("writer joins");
    assert_eq!(
        read.join().expect("reader joins"),
        Some(AsyncOperationState::WaitingCallback),
        "a read must not combine pre-commit operations with post-commit receipts",
    );
}

#[test]
fn sqlite_async_operation_store_rejects_tampered_callback_receipt_digest_on_reopen() {
    let path = sqlite_async_operation_path("callback-receipt-digest-tamper");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store opens");
        store
            .register(waiting_operation())
            .expect("operation registers");
        store
            .accept_callback(
                valid_submission("cb-1", "idem-cb-1"),
                &callback_schema_registry(),
            )
            .expect("callback is accepted");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let receipt_json: String = connection
            .query_row(
                "SELECT receipt_json FROM async_callback_receipts WHERE operation_id = ?1 AND idempotency_key = ?2",
                params!["op-1", "idem-cb-1"],
                |row| row.get(0),
            )
            .expect("receipt row exists");
        let mut receipt: serde_json::Value =
            serde_json::from_str(&receipt_json).expect("receipt json parses");
        receipt["payload_digest"] = json!("sha256:tampered-receipt");
        connection
            .execute(
                "UPDATE async_callback_receipts SET receipt_json = ?1 WHERE operation_id = ?2 AND idempotency_key = ?3",
                params![
                    serde_json::to_string(&receipt).expect("receipt serializes"),
                    "op-1",
                    "idem-cb-1"
                ],
            )
            .expect("receipt row is tampered");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store reopens");
    let error = store
        .accept_callback(
            valid_submission("cb-duplicate", "idem-cb-1"),
            &callback_schema_registry(),
        )
        .expect_err("tampered callback receipt digest must fail durable replay");

    assert!(
        matches!(
            error,
            AsyncOperationError::Storage { ref message }
                if message.contains("stored callback receipt payload_digest does not match payload")
        ),
        "unexpected error: {error:?}"
    );
}

#[test]
fn sqlite_async_operation_store_rejects_invalid_callback_receipt_metadata_on_reopen() {
    let path = sqlite_async_operation_path("callback-receipt-metadata-validation");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store opens");
        store
            .register(waiting_operation())
            .expect("operation registers");
        store
            .accept_callback(
                valid_submission("cb-1", "idem-cb-1"),
                &callback_schema_registry(),
            )
            .expect("callback is accepted");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let receipt_json: String = connection
            .query_row(
                "SELECT receipt_json FROM async_callback_receipts WHERE operation_id = ?1 AND idempotency_key = ?2",
                params!["op-1", "idem-cb-1"],
                |row| row.get(0),
            )
            .expect("receipt row exists");
        let mut receipt: serde_json::Value =
            serde_json::from_str(&receipt_json).expect("receipt json parses");
        receipt["verified_by"] = json!(" \t");
        connection
            .execute(
                "UPDATE async_callback_receipts SET receipt_json = ?1 WHERE operation_id = ?2 AND idempotency_key = ?3",
                params![
                    serde_json::to_string(&receipt).expect("receipt serializes"),
                    "op-1",
                    "idem-cb-1"
                ],
            )
            .expect("receipt row is tampered");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store reopens");
    let error = store
        .accept_callback(
            valid_submission("cb-duplicate", "idem-cb-1"),
            &callback_schema_registry(),
        )
        .expect_err("invalid callback receipt metadata must fail durable replay");

    assert_eq!(
        error,
        AsyncOperationError::EmptyField {
            field: "verified_by".to_owned()
        }
    );
}

#[test]
fn sqlite_async_operation_store_rejects_callback_receipt_row_identity_mismatch_on_reopen() {
    let path = sqlite_async_operation_path("callback-receipt-row-identity");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store opens");
        store
            .register(waiting_operation())
            .expect("operation registers");
        store
            .accept_callback(
                valid_submission("cb-1", "idem-cb-1"),
                &callback_schema_registry(),
            )
            .expect("callback is accepted");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let receipt_json: String = connection
            .query_row(
                "SELECT receipt_json FROM async_callback_receipts WHERE operation_id = ?1 AND idempotency_key = ?2",
                params!["op-1", "idem-cb-1"],
                |row| row.get(0),
            )
            .expect("receipt row exists");
        let mut receipt: serde_json::Value =
            serde_json::from_str(&receipt_json).expect("receipt json parses");
        receipt["operation_id"] = json!("op-forged");
        connection
            .execute(
                "UPDATE async_callback_receipts SET receipt_json = ?1 WHERE operation_id = ?2 AND idempotency_key = ?3",
                params![
                    serde_json::to_string(&receipt).expect("receipt serializes"),
                    "op-1",
                    "idem-cb-1"
                ],
            )
            .expect("receipt row is tampered");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store reopens");
    let error = store
        .accept_callback(
            valid_submission("cb-duplicate", "idem-cb-1"),
            &callback_schema_registry(),
        )
        .expect_err("receipt row identity mismatch must fail durable replay");

    assert!(
        matches!(
            error,
            AsyncOperationError::Storage { ref message }
                if message.contains("stored callback receipt identity does not match row key")
        ),
        "unexpected error: {error:?}"
    );
}

#[test]
fn sqlite_async_operation_store_rejects_callback_receipt_operation_metadata_mismatch_on_reopen() {
    let path = sqlite_async_operation_path("callback-receipt-operation-metadata");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store opens");
        store
            .register(waiting_operation())
            .expect("operation registers");
        store
            .accept_callback(
                valid_submission("cb-1", "idem-cb-1"),
                &callback_schema_registry(),
            )
            .expect("callback is accepted");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let receipt_json: String = connection
            .query_row(
                "SELECT receipt_json FROM async_callback_receipts WHERE operation_id = ?1 AND idempotency_key = ?2",
                params!["op-1", "idem-cb-1"],
                |row| row.get(0),
            )
            .expect("receipt row exists");
        let mut receipt: serde_json::Value =
            serde_json::from_str(&receipt_json).expect("receipt json parses");
        receipt["attempt_id"] = json!("attempt-forged");
        connection
            .execute(
                "UPDATE async_callback_receipts SET receipt_json = ?1 WHERE operation_id = ?2 AND idempotency_key = ?3",
                params![
                    serde_json::to_string(&receipt).expect("receipt serializes"),
                    "op-1",
                    "idem-cb-1"
                ],
            )
            .expect("receipt row is tampered");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store reopens");
    let error = store
        .accept_callback(
            valid_submission("cb-duplicate", "idem-cb-1"),
            &callback_schema_registry(),
        )
        .expect_err("receipt operation metadata mismatch must fail durable replay");

    assert!(
        matches!(
            error,
            AsyncOperationError::Storage { ref message }
                if message.contains("stored callback receipt operation metadata does not match operation")
        ),
        "unexpected error: {error:?}"
    );
}

#[test]
fn sqlite_async_operation_store_rejects_callback_receipt_missing_provider_identity_on_reopen() {
    let path = sqlite_async_operation_path("callback-receipt-provider-missing");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store opens");
        store
            .register(waiting_operation())
            .expect("operation registers");
        store
            .accept_callback(
                valid_submission("cb-1", "idem-cb-1"),
                &callback_schema_registry(),
            )
            .expect("callback is accepted");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let receipt_json: String = connection
            .query_row(
                "SELECT receipt_json FROM async_callback_receipts WHERE operation_id = ?1 AND idempotency_key = ?2",
                params!["op-1", "idem-cb-1"],
                |row| row.get(0),
            )
            .expect("receipt row exists");
        let mut receipt: serde_json::Value =
            serde_json::from_str(&receipt_json).expect("receipt json parses");
        receipt["provider_operation_id"] = serde_json::Value::Null;
        connection
            .execute(
                "UPDATE async_callback_receipts SET receipt_json = ?1 WHERE operation_id = ?2 AND idempotency_key = ?3",
                params![
                    serde_json::to_string(&receipt).expect("receipt serializes"),
                    "op-1",
                    "idem-cb-1"
                ],
            )
            .expect("receipt row is tampered");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store reopens");
    let error = store
        .accept_callback(
            valid_submission("cb-duplicate", "idem-cb-1"),
            &callback_schema_registry(),
        )
        .expect_err("missing receipt provider identity must fail durable replay");

    assert!(
        matches!(
            error,
            AsyncOperationError::Storage { ref message }
                if message.contains("stored callback receipt operation metadata does not match operation")
        ),
        "unexpected error: {error:?}"
    );
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
        )
        .with_provider_operation_id("gha-run-1"),
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
                VALID_RESUME_TOKEN_HASH_2,
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
        )
        .with_provider_operation_id("gha-run-2"),
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
fn sqlite_async_operation_store_rejects_duplicate_callback_receipt_artifacts_on_reopen() {
    let path = sqlite_async_operation_path("callback-receipt-duplicate-artifacts");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store opens");
        store
            .register(waiting_operation())
            .expect("operation registers");
        let mut submission = valid_submission("cb-artifact", "idem-artifact");
        submission.payload = json!({
            "status": "completed",
            "workflow_run_id": "gha-run-1",
            "log": "x".repeat(512),
        });
        store
            .accept_callback_with_artifact_on_payload_limit(
                submission,
                &callback_schema_registry(),
                AsyncCallbackIngestionLimits {
                    max_payload_bytes: 128,
                },
                CallbackArtifactRef::new(
                    "artifact-ci-log",
                    "blob://callbacks/op-1/cb-artifact.json",
                ),
            )
            .expect("artifact-backed callback is accepted");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let receipt_json: String = connection
            .query_row(
                "SELECT receipt_json FROM async_callback_receipts WHERE operation_id = ?1 AND idempotency_key = ?2",
                params!["op-1", "idem-artifact"],
                |row| row.get(0),
            )
            .expect("receipt row exists");
        let mut receipt: serde_json::Value =
            serde_json::from_str(&receipt_json).expect("receipt json parses");
        let duplicate_artifact = receipt["artifacts"][0].clone();
        receipt["artifacts"]
            .as_array_mut()
            .expect("receipt artifacts are an array")
            .push(duplicate_artifact);
        connection
            .execute(
                "UPDATE async_callback_receipts SET receipt_json = ?1 WHERE operation_id = ?2 AND idempotency_key = ?3",
                params![
                    serde_json::to_string(&receipt).expect("receipt serializes"),
                    "op-1",
                    "idem-artifact"
                ],
            )
            .expect("receipt row is tampered");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store reopens");
    let mut duplicate_submission = valid_submission("cb-duplicate", "idem-artifact");
    duplicate_submission.payload = json!({
        "status": "completed",
        "workflow_run_id": "gha-run-1",
        "log": "x".repeat(512),
    });
    let error = store
        .accept_callback_with_artifact_on_payload_limit(
            duplicate_submission,
            &callback_schema_registry(),
            AsyncCallbackIngestionLimits {
                max_payload_bytes: 128,
            },
            CallbackArtifactRef::new("artifact-ci-log", "blob://callbacks/op-1/cb-artifact.json"),
        )
        .expect_err("duplicate callback receipt artifact ids must fail durable replay");

    assert!(
        matches!(
            error,
            AsyncOperationError::InvalidOperation { ref reason, .. }
                if reason.contains("duplicate callback artifact id artifact-ci-log")
        ),
        "unexpected error: {error:?}"
    );
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
fn sqlite_async_operation_store_rejects_quarantined_callback_row_identity_mismatch_on_reopen() {
    let path = sqlite_async_operation_path("callback-quarantine-row-identity");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store opens");
        store
            .quarantine_callback_before_operation_commit(
                valid_submission("cb-early", "provider-delivery-early"),
                5_000,
            )
            .expect("callback is quarantined");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let submission_json: String = connection
            .query_row(
                "SELECT submission_json FROM async_callback_quarantine WHERE operation_id = ?1 AND idempotency_key = ?2",
                params!["op-1", "provider-delivery-early"],
                |row| row.get(0),
            )
            .expect("quarantine row exists");
        let mut submission: serde_json::Value =
            serde_json::from_str(&submission_json).expect("submission json parses");
        submission["operation_id"] = json!("op-forged");
        connection
            .execute(
                "UPDATE async_callback_quarantine SET submission_json = ?1 WHERE operation_id = ?2 AND idempotency_key = ?3",
                params![
                    serde_json::to_string(&submission).expect("submission serializes"),
                    "op-1",
                    "provider-delivery-early"
                ],
            )
            .expect("quarantine row is tampered");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store reopens");
    let error = store
        .register(waiting_operation())
        .expect_err("quarantined callback row identity mismatch must fail durable replay");

    assert!(
        matches!(
            error,
            AsyncOperationError::Storage { ref message }
                if message.contains("stored quarantined callback identity does not match row key")
        ),
        "unexpected error: {error:?}"
    );
}

#[test]
fn sqlite_async_operation_store_rejects_invalid_quarantined_callback_submission_on_reopen() {
    let path = sqlite_async_operation_path("callback-quarantine-submission-validation");
    {
        let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store opens");
        store
            .quarantine_callback_before_operation_commit(
                valid_submission("cb-early", "provider-delivery-early"),
                5_000,
            )
            .expect("callback is quarantined");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let submission_json: String = connection
            .query_row(
                "SELECT submission_json FROM async_callback_quarantine WHERE operation_id = ?1 AND idempotency_key = ?2",
                params!["op-1", "provider-delivery-early"],
                |row| row.get(0),
            )
            .expect("quarantine row exists");
        let mut submission: serde_json::Value =
            serde_json::from_str(&submission_json).expect("submission json parses");
        submission["run_id"] = json!(" \t");
        connection
            .execute(
                "UPDATE async_callback_quarantine SET submission_json = ?1 WHERE operation_id = ?2 AND idempotency_key = ?3",
                params![
                    serde_json::to_string(&submission).expect("submission serializes"),
                    "op-1",
                    "provider-delivery-early"
                ],
            )
            .expect("quarantine row is tampered");
    }

    let store = SqliteAsyncOperationStore::open(&path).expect("sqlite store reopens");
    let error = store
        .register(waiting_operation())
        .expect_err("invalid quarantined submission must fail durable replay");

    assert_eq!(
        error,
        AsyncOperationError::EmptyField {
            field: "run_id".to_owned()
        }
    );
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
            )
            .with_provider_operation_id("gha-run-1"),
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
        )
        .with_provider_operation_id("gha-run-1");
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

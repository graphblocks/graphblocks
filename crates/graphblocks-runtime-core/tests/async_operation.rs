use std::sync::{Arc, Barrier};
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

use graphblocks_runtime_core::async_operation::{
    AsyncCallbackIngestionLimits, AsyncCallbackResumeDecision, AsyncCallbackSubmission,
    AsyncOperation, AsyncOperationConfigurationDiagnostic, AsyncOperationError,
    AsyncOperationEvent, AsyncOperationKind, AsyncOperationState, AsyncOperationStore,
    CallbackEndpointAuth, CallbackEndpointRef, SqliteAsyncOperationStore,
};
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
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback)
    );
    let events = store.events_for_operation("op-1");
    assert_eq!(
        events
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackRejected { .. }))
            .count(),
        2
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
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback)
    );
    Ok(())
}

use std::collections::BTreeMap;

use graphblocks_protocol::{
    BlockCapability, RunOwnershipLease, RunOwnershipLeaseError, WORKER_PROTOCOL_VERSION,
    WorkerAdmissionPolicy, WorkerAdvertisement, WorkerDrainDecision, WorkerDrainDisposition,
    WorkerDrainError, WorkerDrainPlan, WorkerDrainPolicy, WorkerDrainTask, WorkerDrainWorkloadKind,
    WorkerInvocationContext, WorkerInvocationContextError, WorkerInvokeRequest,
    WorkerInvokeRequestError, WorkerInvokeResult, WorkerInvokeResultError, WorkerProtocolError,
    WorkerProtocolErrorPayload, WorkerProtocolMessage, WorkerProtocolMessageError,
    WorkerProtocolMessageKind, WorkerProtocolMessagePayload, WorkerResultError,
    WorkerSelectionError, WorkerState, admit_worker, admit_worker_with_policy,
    evaluate_worker_admission, select_worker_for_block, validate_worker_result,
};
use serde_json::{Value, json};

#[test]
fn worker_advertisement_round_trips_and_admits_current_protocol() -> Result<(), serde_json::Error> {
    let advertisement = WorkerAdvertisement::new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [
            BlockCapability::new("prompt.render@1"),
            BlockCapability::new("model.generate@1"),
        ],
    );

    let encoded = serde_json::to_value(&advertisement)?;
    let decoded = serde_json::from_value::<WorkerAdvertisement>(encoded.clone())?;

    assert_eq!(encoded["targetId"], json!("doc-cpu"));
    assert_eq!(encoded["packageLockHash"], json!("sha256:package-lock"));
    assert_eq!(encoded["imageDigest"], json!("sha256:image"));
    assert_eq!(encoded["state"], json!("ready"));
    assert_eq!(decoded.protocol_version, WORKER_PROTOCOL_VERSION);
    assert_eq!(decoded.worker_id, "worker-local-1");
    assert_eq!(decoded.target_id, "doc-cpu");
    assert_eq!(decoded.supported_blocks.len(), 2);
    assert_eq!(admit_worker(&decoded), Ok(()));
    Ok(())
}

#[test]
fn worker_admission_rejects_incompatible_protocol_version() {
    let mut advertisement = WorkerAdvertisement::new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("prompt.render@1")],
    );
    advertisement.protocol_version = WORKER_PROTOCOL_VERSION + 1;

    assert_eq!(
        admit_worker(&advertisement),
        Err(WorkerProtocolError::IncompatibleVersion {
            expected: WORKER_PROTOCOL_VERSION,
            actual: WORKER_PROTOCOL_VERSION + 1,
        }),
    );
}

#[test]
fn worker_admission_rejects_incompatible_package_lock() {
    let advertisement = WorkerAdvertisement::new(
        "worker-local-1",
        "doc-cpu",
        "sha256:actual-package-lock",
        "sha256:image",
        [BlockCapability::new("prompt.render@1")],
    );
    let policy = WorkerAdmissionPolicy::current().require_package_lock_hash("sha256:expected-lock");

    assert_eq!(
        admit_worker_with_policy(&policy, &advertisement),
        Err(WorkerProtocolError::IncompatiblePackageLock {
            expected: "sha256:expected-lock".to_owned(),
            actual: "sha256:actual-package-lock".to_owned(),
        }),
    );
}

#[test]
fn worker_admission_rejects_whitespace_advertisement_identity_fields() {
    let mut advertisement = WorkerAdvertisement::new(
        " ",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("prompt.render@1")],
    );
    assert_eq!(
        admit_worker(&advertisement),
        Err(WorkerProtocolError::EmptyWorkerId),
    );

    advertisement.worker_id = "worker-local-1".to_owned();
    advertisement.target_id = " ".to_owned();
    assert_eq!(
        admit_worker(&advertisement),
        Err(WorkerProtocolError::EmptyTargetId),
    );

    advertisement.target_id = "doc-cpu".to_owned();
    advertisement.package_lock_hash = " ".to_owned();
    assert_eq!(
        admit_worker(&advertisement),
        Err(WorkerProtocolError::EmptyPackageLockHash),
    );

    advertisement.package_lock_hash = "sha256:package-lock".to_owned();
    advertisement.image_digest = " ".to_owned();
    assert_eq!(
        admit_worker(&advertisement),
        Err(WorkerProtocolError::EmptyImageDigest),
    );
}

#[test]
fn worker_admission_rejects_blank_block_capability() {
    let advertisement = WorkerAdvertisement::new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new(" ")],
    );

    assert_eq!(
        admit_worker(&advertisement),
        Err(WorkerProtocolError::EmptyBlockCapability),
    );
}

#[test]
fn worker_admission_decision_reports_drain_and_missing_capability() -> Result<(), serde_json::Error>
{
    let advertisement = WorkerAdvertisement::new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("prompt.render@1")],
    )
    .with_state(WorkerState::Draining);
    let policy = WorkerAdmissionPolicy::current()
        .require_package_lock_hash("sha256:package-lock")
        .require_block("model.generate@1");

    let decision = evaluate_worker_admission(&policy, &advertisement);

    assert!(!decision.admitted);
    assert_eq!(decision.worker_id, "worker-local-1");
    assert_eq!(decision.target_id, "doc-cpu");
    assert_eq!(decision.required_block.as_deref(), Some("model.generate@1"));
    assert_eq!(
        decision.reason_codes,
        vec![
            "worker.not_ready".to_owned(),
            "worker.missing_required_block".to_owned(),
        ],
    );
    let encoded = serde_json::to_value(&decision)?;
    assert_eq!(
        encoded["reasonCodes"],
        json!(["worker.not_ready", "worker.missing_required_block"]),
    );
    assert_eq!(
        serde_json::from_value::<graphblocks_protocol::WorkerAdmissionDecision>(encoded)?,
        decision,
    );
    Ok(())
}

#[test]
fn worker_admission_decision_reports_blank_capabilities_and_trimmed_identity_fields() {
    let advertisement = WorkerAdvertisement::new(" ", " ", " ", " ", [BlockCapability::new(" ")]);

    let decision = evaluate_worker_admission(&WorkerAdmissionPolicy::current(), &advertisement);

    assert!(!decision.admitted);
    assert_eq!(
        decision.reason_codes,
        vec![
            "worker.empty_worker_id".to_owned(),
            "worker.empty_target_id".to_owned(),
            "worker.empty_package_lock_hash".to_owned(),
            "worker.empty_image_digest".to_owned(),
            "worker.empty_block_capability".to_owned(),
        ],
    );
}

#[test]
fn worker_admission_allows_saturated_workers_to_remain_registered() {
    let advertisement = WorkerAdvertisement::new(
        "worker-saturated",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("model.generate@1")],
    )
    .with_state(WorkerState::Saturated);

    let decision = evaluate_worker_admission(&WorkerAdmissionPolicy::current(), &advertisement);

    assert!(decision.admitted);
    assert_eq!(decision.state, WorkerState::Saturated);
    assert!(decision.reason_codes.is_empty());
}

#[test]
fn worker_admission_decision_allows_ready_matching_worker() {
    let advertisement = WorkerAdvertisement::new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("prompt.render@1")],
    );
    let policy = WorkerAdmissionPolicy::current().require_block("prompt.render@1");

    let decision = evaluate_worker_admission(&policy, &advertisement);

    assert!(decision.admitted);
    assert!(decision.reason_codes.is_empty());
}

#[test]
fn worker_selection_skips_draining_and_saturated_workers() {
    let ready_late = WorkerAdvertisement::new(
        "worker-z",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-z",
        [BlockCapability::new("model.generate@1")],
    );
    let draining = WorkerAdvertisement::new(
        "worker-a",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-a",
        [BlockCapability::new("model.generate@1")],
    )
    .with_state(WorkerState::Draining);
    let saturated = WorkerAdvertisement::new(
        "worker-b",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-b",
        [BlockCapability::new("model.generate@1")],
    )
    .with_state(WorkerState::Saturated);
    let ready_early = WorkerAdvertisement::new(
        "worker-c",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-c",
        [BlockCapability::new("model.generate@1")],
    );
    let workers = [ready_late, draining, saturated, ready_early];

    let selected = select_worker_for_block(workers.iter(), "model.generate@1")
        .expect("a ready worker should be eligible");

    assert_eq!(selected.worker_id, "worker-c");
}

#[test]
fn worker_selection_skips_invalid_advertisements() {
    let blank_worker_id = WorkerAdvertisement::new(
        " ",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-a",
        [BlockCapability::new("model.generate@1")],
    );
    let blank_capability = WorkerAdvertisement::new(
        "worker-a",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-b",
        [BlockCapability::new(" ")],
    );
    let valid = WorkerAdvertisement::new(
        "worker-b",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-c",
        [BlockCapability::new("model.generate@1")],
    );
    let workers = [blank_worker_id, blank_capability, valid];

    let selected = select_worker_for_block(workers.iter(), "model.generate@1")
        .expect("the valid worker should be eligible");

    assert_eq!(selected.worker_id, "worker-b");
}

#[test]
fn worker_selection_reports_when_no_ready_worker_supports_block() {
    let workers = [
        WorkerAdvertisement::new(
            "worker-a",
            "model-cpu",
            "sha256:package-lock",
            "sha256:image-a",
            [BlockCapability::new("model.generate@1")],
        )
        .with_state(WorkerState::Draining),
        WorkerAdvertisement::new(
            "worker-b",
            "model-cpu",
            "sha256:package-lock",
            "sha256:image-b",
            [BlockCapability::new("prompt.render@1")],
        ),
    ];

    assert_eq!(
        select_worker_for_block(workers.iter(), "model.generate@1"),
        Err(WorkerSelectionError::NoEligibleWorker {
            block: "model.generate@1".to_owned(),
        }),
    );
}

#[test]
fn worker_invocation_envelopes_preserve_json_payloads() -> Result<(), serde_json::Error> {
    let request = WorkerInvokeRequest {
        invocation_id: "invoke-000001".to_owned(),
        run_id: "run-000001".to_owned(),
        node_id: "render".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        block: "prompt.render@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-1"),
        inputs: json!({"message": {"text": "Hello"}}),
        config: json!({"template": "Echo {message.text}"}),
    };
    let mut outputs = BTreeMap::new();
    outputs.insert("prompt".to_owned(), json!("Echo Hello"));
    let result = WorkerInvokeResult {
        invocation_id: request.invocation_id.clone(),
        node_attempt_id: request.node_attempt_id.clone(),
        lease_epoch: request.lease_epoch,
        outputs,
    };

    let encoded_request = serde_json::to_value(&request)?;
    assert_eq!(encoded_request["nodeAttemptId"], json!("render-attempt-1"));
    assert_eq!(encoded_request["leaseEpoch"], json!(7));
    assert_eq!(
        serde_json::from_value::<WorkerInvokeRequest>(encoded_request)?,
        request,
    );
    assert_eq!(request.validate(), Ok(()));
    assert_eq!(
        serde_json::from_str::<WorkerInvokeResult>(&serde_json::to_string(&result)?)?,
        result,
    );
    assert_eq!(result.validate(), Ok(()));
    assert_eq!(validate_worker_result(&request, &result), Ok(()));
    Ok(())
}

#[test]
fn worker_protocol_message_round_trips_typed_invoke_request_payload()
-> Result<(), Box<dyn std::error::Error>> {
    let request = WorkerInvokeRequest {
        invocation_id: "invoke-000001".to_owned(),
        run_id: "run-000001".to_owned(),
        node_id: "render".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        block: "prompt.render@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-1"),
        inputs: json!({"message": {"text": "Hello"}}),
        config: json!({"template": "Echo {message.text}"}),
    };
    let message = WorkerProtocolMessage::invoke_request("message-000001", 42, request.clone());

    let encoded = serde_json::to_value(&message)?;
    let digest = message.content_digest()?;
    let decoded = serde_json::from_value::<WorkerProtocolMessage>(encoded.clone())?;

    assert_eq!(message.kind, WorkerProtocolMessageKind::InvokeRequest);
    assert_eq!(message.correlation_id.as_deref(), Some("invoke-000001"));
    assert_eq!(encoded["protocolVersion"], json!(WORKER_PROTOCOL_VERSION));
    assert_eq!(encoded["kind"], json!("invoke_request"));
    assert_eq!(encoded["sequence"], json!(42));
    assert_eq!(encoded["correlationId"], json!("invoke-000001"));
    assert_eq!(encoded["causationId"], Value::Null);
    assert_eq!(
        encoded["payload"]["nodeAttemptId"],
        json!("render-attempt-1")
    );
    assert!(digest.starts_with("sha256:"));
    assert_eq!(decoded, message);
    assert_eq!(decoded.content_digest()?, digest);
    assert_eq!(
        decoded.payload,
        WorkerProtocolMessagePayload::InvokeRequest(Box::new(request)),
    );
    Ok(())
}

#[test]
fn worker_protocol_message_error_payload_is_validated_and_wire_stable()
-> Result<(), Box<dyn std::error::Error>> {
    let message = WorkerProtocolMessage::new(
        "message-error",
        43,
        WorkerProtocolMessagePayload::Error(
            WorkerProtocolErrorPayload::new("worker.failed", "worker failed")
                .retryable(true)
                .with_detail("invocationId", json!("invoke-000001")),
        ),
    )
    .with_correlation_id("invoke-000001")
    .with_causation_id("message-000001");

    let encoded = message.to_wire_value()?;
    let decoded = serde_json::from_value::<WorkerProtocolMessage>(encoded.clone())?;

    assert_eq!(encoded["kind"], json!("error"));
    assert_eq!(encoded["payload"]["code"], json!("worker.failed"));
    assert_eq!(encoded["payload"]["retryable"], json!(true));
    assert_eq!(
        encoded["payload"]["details"]["invocationId"],
        json!("invoke-000001")
    );
    assert_eq!(decoded, message);
    Ok(())
}

#[test]
fn worker_protocol_message_rejects_invalid_kind_specific_payload() {
    let error = serde_json::from_value::<WorkerProtocolMessage>(json!({
        "protocolVersion": WORKER_PROTOCOL_VERSION,
        "messageId": "message-000001",
        "kind": "invoke_request",
        "sequence": 1,
        "correlationId": null,
        "causationId": null,
        "payload": {
            "invocationId": " ",
            "runId": "run-000001",
            "nodeId": "render",
            "nodeAttemptId": "render-attempt-1",
            "leaseEpoch": 7,
            "block": "prompt.render@1",
            "context": {
                "releaseId": "release-1",
                "deploymentRevisionId": "rev-1",
                "traceId": null,
                "parentSpanId": null,
                "policySnapshotId": null,
                "policySnapshotDigest": null,
                "budgetPermitId": null,
                "budgetPermitDigest": null,
                "attributes": {}
            },
            "inputs": {},
            "config": {}
        }
    }))
    .expect_err("blank invocation id is invalid");

    assert!(
        error.to_string().contains("InvalidInvokeRequest"),
        "{error}"
    );
}

#[test]
fn worker_protocol_message_validation_rejects_kind_payload_mismatch() {
    let mut message = WorkerProtocolMessage::advertisement(
        "message-000001",
        1,
        WorkerAdvertisement::new(
            "worker-local-1",
            "doc-cpu",
            "sha256:package-lock",
            "sha256:image",
            [BlockCapability::new("prompt.render@1")],
        ),
    );
    message.kind = WorkerProtocolMessageKind::InvokeRequest;

    assert_eq!(
        message.validate(),
        Err(WorkerProtocolMessageError::KindPayloadMismatch {
            kind: WorkerProtocolMessageKind::InvokeRequest,
            payload_kind: WorkerProtocolMessageKind::Advertisement,
        }),
    );
}

#[test]
fn worker_invocation_context_round_trips_release_policy_budget_and_trace()
-> Result<(), serde_json::Error> {
    let request = WorkerInvokeRequest {
        invocation_id: "invoke-000002".to_owned(),
        run_id: "run-000002".to_owned(),
        node_id: "generate".to_owned(),
        node_attempt_id: "generate-attempt-1".to_owned(),
        lease_epoch: 11,
        block: "model.generate@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-1")
            .with_trace("trace-1", "span-parent")
            .with_policy_snapshot("policy-snapshot-1", "sha256:policy")
            .with_budget_permit("permit-1", "sha256:budget-permit")
            .with_attribute("tenant", "acme"),
        inputs: json!({"prompt": "Hello"}),
        config: json!({"model": "scripted"}),
    };

    let encoded = serde_json::to_value(&request)?;

    assert_eq!(encoded["context"]["releaseId"], json!("release-1"));
    assert_eq!(encoded["context"]["deploymentRevisionId"], json!("rev-1"));
    assert_eq!(encoded["context"]["traceId"], json!("trace-1"));
    assert_eq!(encoded["context"]["parentSpanId"], json!("span-parent"));
    assert_eq!(
        encoded["context"]["policySnapshotId"],
        json!("policy-snapshot-1")
    );
    assert_eq!(encoded["context"]["budgetPermitId"], json!("permit-1"));
    assert_eq!(encoded["context"]["attributes"]["tenant"], json!("acme"));
    assert_eq!(
        serde_json::from_value::<WorkerInvokeRequest>(encoded)?,
        request,
    );
    assert_eq!(request.context.validate(), Ok(()));
    Ok(())
}

#[test]
fn worker_invocation_context_validation_rejects_empty_required_fields() {
    let mut context = WorkerInvocationContext::new(" ", "rev-1");
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::EmptyRequiredField {
            field: "release_id".to_owned(),
        }),
    );

    context.release_id = "release-1".to_owned();
    context.deployment_revision_id.clear();
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::EmptyRequiredField {
            field: "deployment_revision_id".to_owned(),
        }),
    );
}

#[test]
fn worker_invocation_context_validation_rejects_empty_optional_fields() {
    let mut context = WorkerInvocationContext::new("release-1", "rev-1");
    context.trace_id = Some(" ".to_owned());
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::EmptyOptionalField {
            field: "trace_id".to_owned(),
        }),
    );

    context.trace_id = Some("trace-1".to_owned());
    context.policy_snapshot_digest = Some(String::new());
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::EmptyOptionalField {
            field: "policy_snapshot_digest".to_owned(),
        }),
    );
}

#[test]
fn worker_invocation_context_validation_requires_bound_policy_and_budget_pairs() {
    let mut context = WorkerInvocationContext::new("release-1", "rev-1");
    context.policy_snapshot_id = Some("policy-snapshot-1".to_owned());
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::MissingPolicySnapshotDigest),
    );

    context.policy_snapshot_id = None;
    context.policy_snapshot_digest = Some("sha256:policy".to_owned());
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::MissingPolicySnapshotId),
    );

    context.policy_snapshot_id = Some("policy-snapshot-1".to_owned());
    context.budget_permit_id = Some("permit-1".to_owned());
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::MissingBudgetPermitDigest),
    );

    context.budget_permit_id = None;
    context.budget_permit_digest = Some("sha256:budget".to_owned());
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::MissingBudgetPermitId),
    );
}

#[test]
fn worker_invocation_context_validation_rejects_empty_attribute_keys() {
    let context =
        WorkerInvocationContext::new("release-1", "rev-1").with_attribute(" ", "tenant-acme");

    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::EmptyAttributeKey),
    );
}

#[test]
fn worker_invoke_request_validation_rejects_blank_envelope_fields() {
    let mut request = WorkerInvokeRequest {
        invocation_id: " ".to_owned(),
        run_id: "run-000001".to_owned(),
        node_id: "render".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        block: "prompt.render@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-1"),
        inputs: json!({"message": {"text": "Hello"}}),
        config: json!({"template": "Echo {message.text}"}),
    };

    assert_eq!(
        request.validate(),
        Err(WorkerInvokeRequestError::EmptyField {
            field: "invocation_id".to_owned(),
        }),
    );

    request.invocation_id = "invoke-000001".to_owned();
    request.run_id.clear();
    assert_eq!(
        request.validate(),
        Err(WorkerInvokeRequestError::EmptyField {
            field: "run_id".to_owned(),
        }),
    );

    request.run_id = "run-000001".to_owned();
    request.node_id = " ".to_owned();
    assert_eq!(
        request.validate(),
        Err(WorkerInvokeRequestError::EmptyField {
            field: "node_id".to_owned(),
        }),
    );

    request.node_id = "render".to_owned();
    request.node_attempt_id.clear();
    assert_eq!(
        request.validate(),
        Err(WorkerInvokeRequestError::EmptyField {
            field: "node_attempt_id".to_owned(),
        }),
    );

    request.node_attempt_id = "render-attempt-1".to_owned();
    request.block = " ".to_owned();
    assert_eq!(
        request.validate(),
        Err(WorkerInvokeRequestError::EmptyField {
            field: "block".to_owned(),
        }),
    );
}

#[test]
fn worker_invoke_request_validation_rejects_invalid_context() {
    let request = WorkerInvokeRequest {
        invocation_id: "invoke-000001".to_owned(),
        run_id: "run-000001".to_owned(),
        node_id: "render".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        block: "prompt.render@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", " "),
        inputs: json!({"message": {"text": "Hello"}}),
        config: json!({"template": "Echo {message.text}"}),
    };

    assert_eq!(
        request.validate(),
        Err(WorkerInvokeRequestError::InvalidContext {
            source: WorkerInvocationContextError::EmptyRequiredField {
                field: "deployment_revision_id".to_owned(),
            },
        }),
    );
}

#[test]
fn worker_drain_policy_round_trips_default_deadline_contract() -> Result<(), serde_json::Error> {
    let policy = WorkerDrainPolicy::default();

    let encoded = serde_json::to_value(&policy)?;

    assert_eq!(encoded["onlineRequestTimeoutMs"], json!(30_000u64));
    assert_eq!(encoded["durableTaskTimeoutMs"], json!(300_000u64));
    assert_eq!(encoded["onDeadline"]["onlineRequest"], json!("cancel"));
    assert_eq!(encoded["onDeadline"]["durableTask"], json!("checkpoint"));
    assert_eq!(
        encoded["onDeadline"]["realtimeSession"],
        json!("disconnect_with_resume_token")
    );
    assert_eq!(
        serde_json::from_value::<WorkerDrainPolicy>(encoded)?,
        policy
    );
    assert_eq!(policy.validate(), Ok(()));
    Ok(())
}

#[test]
fn worker_drain_policy_deserializes_missing_defaults() -> Result<(), serde_json::Error> {
    let policy = serde_json::from_value::<WorkerDrainPolicy>(json!({
        "onDeadline": {
            "onlineRequest": "finish_in_place"
        }
    }))?;

    assert_eq!(policy.online_request_timeout_ms, 30_000);
    assert_eq!(policy.durable_task_timeout_ms, 300_000);
    assert_eq!(policy.realtime_session_timeout_ms, 600_000);
    assert_eq!(
        policy.on_deadline.online_request,
        WorkerDrainDisposition::FinishInPlace
    );
    assert_eq!(
        policy.on_deadline.durable_task,
        WorkerDrainDisposition::Checkpoint
    );
    assert_eq!(
        policy.on_deadline.realtime_session,
        WorkerDrainDisposition::DisconnectWithResumeToken
    );
    assert_eq!(policy.validate(), Ok(()));
    Ok(())
}

#[test]
fn worker_drain_task_and_plan_deserialize_missing_defaults() -> Result<(), serde_json::Error> {
    let task = serde_json::from_value::<WorkerDrainTask>(json!({
        "workload": "durable_task",
        "request": {
            "invocationId": "invoke-durable",
            "runId": "run-durable",
            "nodeId": "embed",
            "nodeAttemptId": "embed-attempt-1",
            "leaseEpoch": 13,
            "block": "embedding.generate@1",
            "context": {
                "releaseId": "release-1",
                "deploymentRevisionId": "rev-1",
                "attributes": {}
            },
            "inputs": {"text": "hello"},
            "config": {}
        },
        "startedAtUnixMs": 1_000
    }))?;
    let plan = serde_json::from_value::<WorkerDrainPlan>(json!({
        "workerId": "worker-a",
        "targetId": "model-cpu",
        "drainStartedAtUnixMs": 2_000
    }))?;

    assert_eq!(task.workload, WorkerDrainWorkloadKind::DurableTask);
    assert!(!task.checkpointable);
    assert_eq!(task.validate(), Ok(()));
    assert_eq!(plan.worker_state, WorkerState::Draining);
    assert!(plan.admission_closed);
    assert!(plan.decisions.is_empty());
    assert_eq!(plan.validate(), Ok(()));
    Ok(())
}

#[test]
fn worker_drain_plan_closes_admission_and_preserves_inflight_affinity()
-> Result<(), serde_json::Error> {
    let worker = WorkerAdvertisement::new(
        "worker-a",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-a",
        [BlockCapability::new("model.generate@1")],
    );
    let online_request = WorkerInvokeRequest {
        invocation_id: "invoke-online".to_owned(),
        run_id: "run-online".to_owned(),
        node_id: "model".to_owned(),
        node_attempt_id: "model-attempt-1".to_owned(),
        lease_epoch: 7,
        block: "model.generate@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-old"),
        inputs: json!({"prompt": "hello"}),
        config: json!({}),
    };
    let durable_request = WorkerInvokeRequest {
        invocation_id: "invoke-durable".to_owned(),
        run_id: "run-durable".to_owned(),
        node_id: "embed".to_owned(),
        node_attempt_id: "embed-attempt-2".to_owned(),
        lease_epoch: 13,
        block: "embedding.generate@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-old"),
        inputs: json!({"text": "hello"}),
        config: json!({}),
    };

    let plan = WorkerDrainPlan::for_worker(
        &worker,
        &WorkerDrainPolicy::default(),
        [
            WorkerDrainTask {
                workload: WorkerDrainWorkloadKind::OnlineRequest,
                request: online_request,
                started_at_unix_ms: 1_000,
                checkpointable: false,
            },
            WorkerDrainTask {
                workload: WorkerDrainWorkloadKind::DurableTask,
                request: durable_request,
                started_at_unix_ms: 1_000,
                checkpointable: true,
            },
        ],
        2_000,
        302_000,
    )
    .expect("drain plan should be valid");

    assert_eq!(plan.worker_id, "worker-a");
    assert_eq!(plan.worker_state, WorkerState::Draining);
    assert!(plan.admission_closed);
    assert_eq!(plan.decisions[0].run_id, "run-online");
    assert_eq!(plan.decisions[0].lease_epoch, 7);
    assert_eq!(plan.decisions[0].deployment_revision_id, "rev-old");
    assert_eq!(
        plan.decisions[0].disposition,
        WorkerDrainDisposition::Cancel
    );
    assert_eq!(plan.decisions[0].reason, "deadline_reached");
    assert_eq!(plan.decisions[1].run_id, "run-durable");
    assert_eq!(plan.decisions[1].lease_epoch, 13);
    assert_eq!(
        plan.decisions[1].disposition,
        WorkerDrainDisposition::Checkpoint
    );

    let encoded = serde_json::to_value(&plan)?;
    assert_eq!(encoded["workerState"], json!("draining"));
    assert_eq!(encoded["admissionClosed"], json!(true));
    assert_eq!(
        encoded["decisions"][0]["nodeAttemptId"],
        json!("model-attempt-1")
    );
    assert_eq!(serde_json::from_value::<WorkerDrainPlan>(encoded)?, plan);
    assert_eq!(plan.validate(), Ok(()));
    Ok(())
}

#[test]
fn worker_drain_plan_cancels_uncheckpointable_tasks_after_checkpoint_deadline() {
    let worker = WorkerAdvertisement::new(
        "worker-a",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-a",
        [BlockCapability::new("embedding.generate@1")],
    );
    let durable_request = WorkerInvokeRequest {
        invocation_id: "invoke-durable".to_owned(),
        run_id: "run-durable".to_owned(),
        node_id: "embed".to_owned(),
        node_attempt_id: "embed-attempt-2".to_owned(),
        lease_epoch: 13,
        block: "embedding.generate@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-old"),
        inputs: json!({"text": "hello"}),
        config: json!({}),
    };

    let plan = WorkerDrainPlan::for_worker(
        &worker,
        &WorkerDrainPolicy::default(),
        [WorkerDrainTask {
            workload: WorkerDrainWorkloadKind::DurableTask,
            request: durable_request,
            started_at_unix_ms: 1_000,
            checkpointable: false,
        }],
        2_000,
        302_000,
    )
    .expect("drain plan should be valid");

    assert_eq!(
        plan.decisions[0].disposition,
        WorkerDrainDisposition::Cancel
    );
    assert_eq!(plan.decisions[0].reason, "checkpoint_unavailable");
}

#[test]
fn worker_drain_validation_rejects_invalid_wire_shapes() {
    let policy = WorkerDrainPolicy {
        online_request_timeout_ms: 0,
        ..WorkerDrainPolicy::default()
    };
    assert_eq!(
        policy.validate(),
        Err(WorkerDrainError::NonPositiveTimeout {
            field: "online_request_timeout_ms",
        }),
    );

    let invalid_task = WorkerDrainTask {
        workload: WorkerDrainWorkloadKind::OnlineRequest,
        request: WorkerInvokeRequest {
            invocation_id: "invoke-000001".to_owned(),
            run_id: " ".to_owned(),
            node_id: "render".to_owned(),
            node_attempt_id: "render-attempt-1".to_owned(),
            lease_epoch: 7,
            block: "prompt.render@1".to_owned(),
            context: WorkerInvocationContext::new("release-1", "rev-old"),
            inputs: json!({}),
            config: json!({}),
        },
        started_at_unix_ms: 0,
        checkpointable: false,
    };
    assert_eq!(
        invalid_task.validate(),
        Err(WorkerDrainError::InvalidTaskRequest {
            source: WorkerInvokeRequestError::EmptyField {
                field: "run_id".to_owned(),
            },
        }),
    );

    let decision = WorkerDrainDecision {
        workload: WorkerDrainWorkloadKind::OnlineRequest,
        run_id: "run-000001".to_owned(),
        invocation_id: "invoke-000001".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        release_id: "release-1".to_owned(),
        deployment_revision_id: "rev-old".to_owned(),
        disposition: WorkerDrainDisposition::Cancel,
        deadline_unix_ms: 30_000,
        reason: "deadline_reached".to_owned(),
    };
    let mut blank_decision = decision.clone();
    blank_decision.run_id = " ".to_owned();
    assert_eq!(
        blank_decision.validate(),
        Err(WorkerDrainError::EmptyDecisionField { field: "run_id" }),
    );

    let mut plan = WorkerDrainPlan {
        worker_id: "worker-1".to_owned(),
        target_id: "target-1".to_owned(),
        worker_state: WorkerState::Ready,
        admission_closed: true,
        drain_started_at_unix_ms: 0,
        decisions: vec![decision],
    };
    assert_eq!(
        plan.validate(),
        Err(WorkerDrainError::WorkerStateNotDraining {
            state: WorkerState::Ready,
        }),
    );

    plan.worker_state = WorkerState::Draining;
    plan.worker_id = " ".to_owned();
    assert_eq!(
        plan.validate(),
        Err(WorkerDrainError::EmptyPlanField { field: "worker_id" }),
    );
}

#[test]
fn worker_invoke_result_validation_rejects_blank_envelope_fields_and_outputs() {
    let mut outputs = BTreeMap::new();
    outputs.insert("prompt".to_owned(), json!("Echo Hello"));
    let mut result = WorkerInvokeResult {
        invocation_id: " ".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        outputs,
    };

    assert_eq!(
        result.validate(),
        Err(WorkerInvokeResultError::EmptyField {
            field: "invocation_id".to_owned(),
        }),
    );

    result.invocation_id = "invoke-000001".to_owned();
    result.node_attempt_id.clear();
    assert_eq!(
        result.validate(),
        Err(WorkerInvokeResultError::EmptyField {
            field: "node_attempt_id".to_owned(),
        }),
    );

    result.node_attempt_id = "render-attempt-1".to_owned();
    result.outputs.clear();
    result.outputs.insert(" ".to_owned(), json!("Echo Hello"));
    assert_eq!(
        result.validate(),
        Err(WorkerInvokeResultError::EmptyOutputKey)
    );
}

#[test]
fn worker_result_validation_rejects_invalid_request_or_result_envelopes() {
    let mut request = WorkerInvokeRequest {
        invocation_id: "invoke-000001".to_owned(),
        run_id: " ".to_owned(),
        node_id: "render".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        block: "prompt.render@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-1"),
        inputs: json!({"message": {"text": "Hello"}}),
        config: json!({"template": "Echo {message.text}"}),
    };
    let mut outputs = BTreeMap::new();
    outputs.insert("prompt".to_owned(), json!("Echo Hello"));
    let mut result = WorkerInvokeResult {
        invocation_id: "invoke-000001".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        outputs,
    };

    assert_eq!(
        validate_worker_result(&request, &result),
        Err(WorkerResultError::InvalidRequest {
            source: WorkerInvokeRequestError::EmptyField {
                field: "run_id".to_owned(),
            },
        }),
    );

    request.run_id = "run-000001".to_owned();
    result.outputs.clear();
    result.outputs.insert(" ".to_owned(), json!("Echo Hello"));
    assert_eq!(
        validate_worker_result(&request, &result),
        Err(WorkerResultError::InvalidResult {
            source: WorkerInvokeResultError::EmptyOutputKey,
        }),
    );
}

#[test]
fn run_ownership_lease_round_trips_with_fencing_epoch() -> Result<(), serde_json::Error> {
    let lease = RunOwnershipLease {
        run_id: "run-000001".to_owned(),
        owner_instance_id: "control-plane-a".to_owned(),
        lease_epoch: 42,
        expires_at_unix_ms: 1_820_000_000_000,
        last_checkpoint: Some("checkpoint-000004".to_owned()),
    };

    let encoded = serde_json::to_value(&lease)?;
    assert_eq!(encoded["leaseEpoch"], json!(42));
    assert_eq!(encoded["expiresAtUnixMs"], json!(1_820_000_000_000u64));

    assert_eq!(serde_json::from_value::<RunOwnershipLease>(encoded)?, lease);
    assert_eq!(lease.validate(), Ok(()));
    Ok(())
}

#[test]
fn run_ownership_lease_validation_rejects_blank_identity_fields() {
    let mut lease = RunOwnershipLease {
        run_id: " ".to_owned(),
        owner_instance_id: "control-plane-a".to_owned(),
        lease_epoch: 42,
        expires_at_unix_ms: 1_820_000_000_000,
        last_checkpoint: Some("checkpoint-000004".to_owned()),
    };

    assert_eq!(lease.validate(), Err(RunOwnershipLeaseError::EmptyRunId));

    lease.run_id = "run-000001".to_owned();
    lease.owner_instance_id = " ".to_owned();
    assert_eq!(
        lease.validate(),
        Err(RunOwnershipLeaseError::EmptyOwnerInstanceId),
    );
}

#[test]
fn run_ownership_lease_validation_rejects_blank_checkpoint() {
    let lease = RunOwnershipLease {
        run_id: "run-000001".to_owned(),
        owner_instance_id: "control-plane-a".to_owned(),
        lease_epoch: 42,
        expires_at_unix_ms: 1_820_000_000_000,
        last_checkpoint: Some(" ".to_owned()),
    };

    assert_eq!(
        lease.validate(),
        Err(RunOwnershipLeaseError::EmptyLastCheckpoint),
    );
}

#[test]
fn worker_result_validation_rejects_mismatched_attempt_or_lease_epoch() {
    let request = WorkerInvokeRequest {
        invocation_id: "invoke-000001".to_owned(),
        run_id: "run-000001".to_owned(),
        node_id: "render".to_owned(),
        node_attempt_id: "render-attempt-2".to_owned(),
        lease_epoch: 9,
        block: "prompt.render@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-1"),
        inputs: json!({"message": {"text": "Hello"}}),
        config: json!({"template": "Echo {message.text}"}),
    };
    let mut mismatched_attempt = WorkerInvokeResult {
        invocation_id: request.invocation_id.clone(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: request.lease_epoch,
        outputs: BTreeMap::new(),
    };

    assert_eq!(
        validate_worker_result(&request, &mismatched_attempt),
        Err(WorkerResultError::MismatchedNodeAttempt {
            expected: "render-attempt-2".to_owned(),
            actual: "render-attempt-1".to_owned(),
        }),
    );

    mismatched_attempt.node_attempt_id = request.node_attempt_id.clone();
    mismatched_attempt.lease_epoch = 8;

    assert_eq!(
        validate_worker_result(&request, &mismatched_attempt),
        Err(WorkerResultError::StaleLeaseEpoch {
            expected: 9,
            actual: 8,
        }),
    );
}

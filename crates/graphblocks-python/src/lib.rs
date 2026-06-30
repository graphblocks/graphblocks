use std::collections::{BTreeMap, BTreeSet};

use graphblocks_compiler::compiler::{BlockCatalog, compile_graph, compile_graph_with_catalog};
use graphblocks_compiler::diagnostics::Severity;
use graphblocks_protocol::{
    RemotePayload, RemotePayloadError, RemotePayloadLimits, WorkerAdmissionPolicy,
    WorkerAdvertisement, WorkerProtocolError, WorkerProtocolMessage, WorkerProtocolMessageKind,
    admit_worker_with_policy, validate_remote_payload,
};
use graphblocks_runtime_core::agent::{AgentLoopController, AgentLoopDecision, AgentSpec};
use graphblocks_runtime_core::application_event::{
    ApplicationCommandKind, ApplicationEvent, ApplicationEventKind, ApplicationEventMetadata,
    ApplicationEventStreamState, ApplicationProtocolCapabilities, ApplicationProtocolError,
    ApplicationProtocolEvent, ApplicationProtocolEventKind, ApplicationProtocolEventMetadata,
    ApplicationProtocolLog, ApplicationProtocolStreamState,
};
use graphblocks_runtime_core::audit::{
    AuditEvent, ToolEffectAuditContext, ToolEffectPrecondition, ToolEffectPreconditionContext,
};
use graphblocks_runtime_core::budget::{BudgetPermit, UsageAmount};
use graphblocks_runtime_core::cancellation::{
    CancellationGuarantee, CancellationScope, CancellationToken,
};
use graphblocks_runtime_core::connectors::{ConnectionSpec, SecretRef, ensure_capabilities};
use graphblocks_runtime_core::exhaustion::{
    ContinuationEnvelope, ExhaustionController, ExhaustionPolicy, ExhaustionPreset, ExhaustionUnit,
    WorkKind,
};
use graphblocks_runtime_core::lifecycle::{LifecycleError, NodeLifecycle, NodeStatus};
use graphblocks_runtime_core::observability::{
    CaptureDecision, CaptureMode, CapturedContent, RedactionRule,
};
use graphblocks_runtime_core::outcome::{
    BlockError, CancelCode, CancelReason, ErrorCategory, Outcome,
};
use graphblocks_runtime_core::output_policy::{
    DeclarativeOutputPolicyEvaluator, DeclarativeOutputPolicyRule, DraftDisposition, DurableResult,
    FlushBoundary, GenerationChunk, OutputCutoff, OutputDeliveryGate, OutputDeliveryPolicy,
    OutputDisposition, OutputPolicyDecision, PendingToolCallsDisposition, ProviderCancellation,
    RedactionInstruction, TerminalReason, ViolationAction,
};
use graphblocks_runtime_core::policy::{PrincipalRef, ResourceRef};
use graphblocks_runtime_core::readiness::{InputDependency, PortRef};
use graphblocks_runtime_core::retry::{
    Backoff, EffectKind, PartialOutputPolicy, ProviderLimitDecision, ProviderLimitIncident,
    ProviderLimitKind, ProviderLimitPolicy, RetryDecision, RetryPolicy, RetryRequest,
};
use graphblocks_runtime_core::scheduler::{ScheduledNode, StartedNode};
use graphblocks_runtime_core::task_group::{
    ChildTaskState, SiblingCancellationPolicy, TaskGroupDecision, TaskGroupFailure,
    TaskGroupFailurePolicy, TaskGroupPolicy, TaskGroupState,
};
use graphblocks_runtime_core::test_runtime::{InProcessTestRuntime, NodeExecutor, TestRunStatus};
use graphblocks_runtime_core::tool::{
    BlockToolImplementation, GraphToolImplementation, McpToolImplementation,
    OpenApiToolImplementation, RemoteToolImplementation, ResolvedTool, ToolApproval, ToolBinding,
    ToolCancellation, ToolDefinition, ToolEffect, ToolIdempotency, ToolImplementation,
    ToolResultMode,
};
use graphblocks_runtime_core::tool_approval::{
    ToolApprovalError, ToolApprovalRecord, ToolApprovalRequest, ToolApprovalStatus,
};
use graphblocks_runtime_core::tool_call::{
    ToolCall, ToolCallDraft, ToolCallDraftStatus, ToolCallStatus,
};
use graphblocks_runtime_core::tool_execution::{
    ToolExecutionCancellationPolicy, ToolExecutionFailurePolicy, ToolExecutionPlan,
    ToolExecutionPlanError, ToolExecutionState, ToolPlanCall,
};
use graphblocks_runtime_core::tool_result::{
    ArtifactRef, ContentPart, ContentPartKind, Diagnostic, DiagnosticSeverity, ToolEffectOutcome,
    ToolResult, ToolResultContentPolicy, ToolResultEvent, ToolResultStatus, ToolResultStreamError,
    ToolResultStreamState, ToolResultValidation, ToolResultValidationRequest,
};
use graphblocks_runtime_core::tool_schema::{JsonSchema, JsonSchemaNode, ToolSchemaRegistry};
use graphblocks_runtime_core::usage::{
    InMemoryUsageLedger, UsageAmount as LedgerUsageAmount, UsageConfidence, UsageLedgerError,
    UsageRecord, UsageSource,
};
use graphblocks_runtime_durable::{
    DurableOutputCutoffDraftDisposition, DurableOutputCutoffDurableResult,
    DurableOutputCutoffTerminalReason, DurableResponsePolicyStopRecord, DurableToolTerminalRecord,
    DurableToolTerminalState, InMemoryDurableToolTerminalStore, ToolTerminalStoreError,
};
use graphblocks_runtime_seq::tool_queue::{SequentialToolQueue, SequentialToolQueueError};
use graphblocksd::{DaemonConfig, DaemonStatus, WorkerRegistry, WorkerRegistryError};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use serde_json::{Value, json};

#[pyfunction]
fn binding_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pyfunction]
fn finalize_tool_call_json(
    draft_json: &str,
    resolved_tool_id: &str,
    created_at_unix_ms: u64,
) -> PyResult<String> {
    let draft_value = parse_json_argument(draft_json, "tool call draft")?;
    let draft_object = json_object(&draft_value, "draft")?;
    let status = match required_alias_string(draft_object, "status", "status", "draft")? {
        "proposed" => ToolCallDraftStatus::Proposed,
        "arguments_streaming" => ToolCallDraftStatus::ArgumentsStreaming,
        "arguments_complete" => ToolCallDraftStatus::ArgumentsComplete,
        value => {
            return Err(PyValueError::new_err(format!(
                "draft.status has unknown status {value:?}"
            )));
        }
    };
    let expected_sequence = required_alias_u64(draft_object, "sequence", "sequence", "draft")?;
    let fragments = draft_object
        .get("argumentFragments")
        .or_else(|| draft_object.get("argument_fragments"))
        .ok_or_else(|| PyValueError::new_err("draft.argumentFragments is required"))?
        .as_array()
        .ok_or_else(|| PyValueError::new_err("draft.argumentFragments must be an array"))?;

    let mut draft = ToolCallDraft::proposed(
        required_alias_string(draft_object, "responseId", "response_id", "draft")?,
        required_alias_string(draft_object, "toolCallId", "tool_call_id", "draft")?,
        required_alias_string(draft_object, "toolName", "tool_name", "draft")?,
    );
    for (fragment_index, fragment) in fragments.iter().enumerate() {
        let Some(fragment) = fragment.as_str() else {
            return Err(PyValueError::new_err(format!(
                "draft.argumentFragments[{fragment_index}] must be a string"
            )));
        };
        draft.append_argument_fragment(fragment).map_err(|error| {
            PyValueError::new_err(format!(
                "failed to append tool argument fragment {fragment_index}: {error:?}"
            ))
        })?;
    }
    if status == ToolCallDraftStatus::ArgumentsComplete {
        draft.complete_arguments().map_err(|error| {
            PyValueError::new_err(format!("failed to complete tool arguments: {error:?}"))
        })?;
    }
    if draft.status != status {
        return Err(PyValueError::new_err(format!(
            "draft.status does not match argument fragments: expected {status:?}, reconstructed {:?}",
            draft.status
        )));
    }
    if draft.sequence != expected_sequence {
        return Err(PyValueError::new_err(format!(
            "draft.sequence does not match argument fragments: expected {expected_sequence}, reconstructed {}",
            draft.sequence
        )));
    }

    let call = draft
        .into_tool_call(resolved_tool_id, created_at_unix_ms)
        .map_err(|error| PyValueError::new_err(format!("invalid tool call draft: {error:?}")))?;
    let status = match call.status {
        ToolCallStatus::Validated => "validated",
        ToolCallStatus::PolicyPending => "policy_pending",
        ToolCallStatus::ApprovalPending => "approval_pending",
        ToolCallStatus::Admitted => "admitted",
        ToolCallStatus::Running => "running",
        ToolCallStatus::Completed => "completed",
        ToolCallStatus::Failed => "failed",
        ToolCallStatus::Denied => "denied",
        ToolCallStatus::Cancelled => "cancelled",
        ToolCallStatus::PolicyStopped => "policy_stopped",
        ToolCallStatus::Expired => "expired",
    };
    let payload = json!({
        "toolCallId": call.tool_call_id,
        "responseId": call.response_id,
        "resolvedToolId": call.resolved_tool_id,
        "name": call.name,
        "arguments": call.arguments,
        "argumentsDigest": call.arguments_digest,
        "revision": call.revision,
        "status": status,
        "dependsOn": call.depends_on,
        "createdAtUnixMs": call.created_at_unix_ms,
        "admittedAtUnixMs": call.admitted_at_unix_ms,
        "completedAtUnixMs": call.completed_at_unix_ms,
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!("failed to serialize finalized tool call: {error}"))
    })
}

#[pyfunction]
#[pyo3(signature = (document_json, block_catalog_json=None))]
fn compile_graph_json(document_json: &str, block_catalog_json: Option<&str>) -> PyResult<String> {
    let document = serde_json::from_str::<Value>(document_json)
        .map_err(|error| PyValueError::new_err(format!("invalid graph document JSON: {error}")))?;
    let block_catalog = block_catalog_json
        .map(|catalog_json| {
            let catalog = serde_json::from_str::<Value>(catalog_json).map_err(|error| {
                PyValueError::new_err(format!("invalid block catalog JSON: {error}"))
            })?;
            BlockCatalog::from_blocks(&catalog).map_err(PyValueError::new_err)
        })
        .transpose()?;
    let plan = if let Some(block_catalog) = &block_catalog {
        compile_graph_with_catalog(&document, block_catalog)
    } else {
        compile_graph(&document)
    };
    let diagnostics = plan
        .diagnostics
        .iter()
        .map(|diagnostic| {
            let severity = match diagnostic.severity {
                Severity::Error => "error",
                Severity::Warning => "warning",
                Severity::Info => "info",
            };
            json!({
                "code": diagnostic.code.as_str(),
                "message": diagnostic.message.as_str(),
                "path": diagnostic.path.as_str(),
                "severity": severity,
            })
        })
        .collect::<Vec<_>>();
    let payload = json!({
        "hash": plan.graph_hash,
        "ok": plan.ok(),
        "diagnostics": diagnostics,
        "graph": plan.normalized,
    });

    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!("failed to serialize compiler result: {error}"))
    })
}

#[pyfunction]
fn validate_worker_advertisement_json(
    advertisement_json: &str,
    expected_package_lock_hash: Option<&str>,
) -> PyResult<String> {
    let advertisement =
        serde_json::from_str::<WorkerAdvertisement>(advertisement_json).map_err(|error| {
            PyValueError::new_err(format!("invalid worker advertisement JSON: {error}"))
        })?;
    let mut policy = WorkerAdmissionPolicy::current();
    if let Some(package_lock_hash) = expected_package_lock_hash {
        policy = policy.require_package_lock_hash(package_lock_hash);
    }
    let payload = match admit_worker_with_policy(&policy, &advertisement) {
        Ok(()) => json!({"ok": true}),
        Err(error) => {
            let error_payload = match error {
                WorkerProtocolError::IncompatibleVersion { expected, actual } => json!({
                    "code": "worker.incompatible_protocol_version",
                    "expected": expected,
                    "actual": actual,
                }),
                WorkerProtocolError::IncompatiblePackageLock { expected, actual } => json!({
                    "code": "worker.incompatible_package_lock",
                    "expected": expected,
                    "actual": actual,
                }),
                WorkerProtocolError::EmptyWorkerId => json!({"code": "worker.empty_worker_id"}),
                WorkerProtocolError::EmptyTargetId => json!({"code": "worker.empty_target_id"}),
                WorkerProtocolError::EmptyPackageLockHash => {
                    json!({"code": "worker.empty_package_lock_hash"})
                }
                WorkerProtocolError::EmptyImageDigest => {
                    json!({"code": "worker.empty_image_digest"})
                }
                WorkerProtocolError::EmptySupportedBlocks => {
                    json!({"code": "worker.empty_supported_blocks"})
                }
                WorkerProtocolError::EmptyBlockCapability => {
                    json!({"code": "worker.empty_block_capability"})
                }
                WorkerProtocolError::MissingRequiredBlock { required_block } => json!({
                    "code": "worker.missing_required_block",
                    "requiredBlock": required_block,
                }),
            };
            json!({"ok": false, "error": error_payload})
        }
    };

    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize worker protocol result: {error}"
        ))
    })
}

#[pyfunction]
fn validate_worker_protocol_message_json(message_json: &str) -> PyResult<String> {
    let message_value = serde_json::from_str::<Value>(message_json)
        .map_err(|error| PyValueError::new_err(format!("invalid worker message JSON: {error}")))?;
    let message = match serde_json::from_value::<WorkerProtocolMessage>(message_value) {
        Ok(message) => message,
        Err(error) => {
            let payload = json!({
                "ok": false,
                "error": {
                    "code": "worker_protocol_message.invalid",
                    "message": error.to_string(),
                },
            });
            return serde_json::to_string(&payload).map_err(|error| {
                PyRuntimeError::new_err(format!(
                    "failed to serialize worker protocol message result: {error}"
                ))
            });
        }
    };
    let content_digest = message.content_digest().map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to compute worker protocol message digest: {error}"
        ))
    })?;
    let payload = json!({
        "ok": true,
        "contentDigest": content_digest,
        "kind": message.kind,
        "messageId": message.message_id,
        "sequence": message.sequence,
        "correlationId": message.correlation_id,
        "causationId": message.causation_id,
    });

    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize worker protocol message result: {error}"
        ))
    })
}

#[pyfunction]
#[pyo3(signature = (
    message_json,
    daemon_config_json=None,
    response_message_id="message-daemon-1",
    response_sequence=1
))]
fn admit_worker_message_json(
    message_json: &str,
    daemon_config_json: Option<&str>,
    response_message_id: &str,
    response_sequence: u64,
) -> PyResult<String> {
    let message_value = parse_json_argument(message_json, "worker message")?;
    let daemon_config_value = daemon_config_json
        .map(|config_json| parse_json_argument(config_json, "daemon config"))
        .transpose()?;
    let daemon_config = parse_daemon_config(daemon_config_value.as_ref())?;
    let mut registry = WorkerRegistry::new(daemon_config)
        .map_err(|error| PyValueError::new_err(format!("invalid daemon config: {error:?}")))?;
    let payload = match registry.admit_worker_message_wire_value(
        &message_value,
        response_message_id,
        response_sequence,
    ) {
        Ok(response) => {
            let status = registry.status();
            json!({
                "ok": true,
                "response": response,
                "status": daemon_status_json(&status),
            })
        }
        Err(error) => {
            json!({
                "ok": false,
                "error": worker_registry_error_json(&error),
            })
        }
    };

    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize worker admission result: {error}"
        ))
    })
}

#[pyfunction]
fn validate_remote_payload_json(payload_json: &str, max_inline_bytes: usize) -> PyResult<String> {
    let payload = serde_json::from_str::<RemotePayload>(payload_json)
        .map_err(|error| PyValueError::new_err(format!("invalid remote payload JSON: {error}")))?;
    let limits = RemotePayloadLimits { max_inline_bytes };
    let result_payload = match validate_remote_payload(&payload, &limits) {
        Ok(()) => json!({"ok": true}),
        Err(error) => {
            let error_payload = match error {
                RemotePayloadError::InvalidSchema => {
                    json!({"code": "remote_payload.invalid_schema"})
                }
                RemotePayloadError::OversizedInlinePayload {
                    max_inline_bytes,
                    actual_inline_bytes,
                } => json!({
                    "code": "remote_payload.oversized_inline",
                    "maxInlineBytes": max_inline_bytes,
                    "actualInlineBytes": actual_inline_bytes,
                }),
                RemotePayloadError::InvalidArtifactRef { field } => json!({
                    "code": "remote_payload.invalid_artifact_ref",
                    "field": field,
                }),
                RemotePayloadError::InlineJsonEncoding => {
                    json!({"code": "remote_payload.inline_json_encoding"})
                }
            };
            json!({"ok": false, "error": error_payload})
        }
    };

    serde_json::to_string(&result_payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize remote payload result: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_connector_capabilities_json(
    connection_json: &str,
    required_capabilities_json: &str,
) -> PyResult<String> {
    let connection_value = parse_json_argument(connection_json, "connection")?;
    let required_value = parse_json_argument(required_capabilities_json, "required capabilities")?;
    let connection_object = json_object(&connection_value, "connection")?;
    let connection = parse_connection_spec(&connection_value, "connection")?;
    let supported = parse_connector_supported_capabilities(connection_object)?;
    let required = parse_required_capabilities(&required_value)?;
    let safe_connection = connection.safe_config_value();
    let result = match ensure_capabilities(
        &connection.connection_id,
        supported.clone(),
        required.clone(),
    ) {
        Ok(()) => json!({
            "ok": true,
            "connection": safe_connection,
            "requiredCapabilities": required,
            "supportedCapabilities": supported,
            "missingCapabilities": [],
            "error": Value::Null,
        }),
        Err(error) => {
            let connection_id = error.connection_id;
            let missing = error.missing;
            let supported = error.supported;
            json!({
                "ok": false,
                "connection": safe_connection,
                "requiredCapabilities": required,
                "supportedCapabilities": supported.clone(),
                "missingCapabilities": missing.clone(),
                "error": {
                    "code": "ConnectorCapabilityMissing",
                    "connectionId": connection_id,
                    "missingCapabilities": missing,
                    "supportedCapabilities": supported,
                },
            })
        }
    };
    serde_json::to_string(&result).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize connector capability evaluation: {error}"
        ))
    })
}

#[pyfunction]
#[pyo3(signature = (
    call_json,
    result_json,
    resolved_tool_json,
    schema_registry_json,
    content_policy_json=None
))]
fn prepare_tool_result_for_model_json(
    call_json: &str,
    result_json: &str,
    resolved_tool_json: &str,
    schema_registry_json: &str,
    content_policy_json: Option<&str>,
) -> PyResult<String> {
    let call_value = parse_json_argument(call_json, "tool call")?;
    let result_value = parse_json_argument(result_json, "tool result")?;
    let resolved_tool_value = parse_json_argument(resolved_tool_json, "resolved tool")?;
    let schema_registry_value = parse_json_argument(schema_registry_json, "schema registry")?;
    let content_policy_value = content_policy_json
        .map(|text| parse_json_argument(text, "tool result content policy"))
        .transpose()?;

    let call = parse_tool_call(&call_value, "tool call")?;
    let result = parse_tool_result(&result_value, "tool result")?;
    let resolved_tool = parse_resolved_tool(&resolved_tool_value, "resolved tool")?;
    let schema_registry = parse_tool_schema_registry(&schema_registry_value, "schema registry")?;
    let content_policy =
        parse_tool_result_content_policy(content_policy_value.as_ref(), "content policy")?;

    let output = ToolResultValidation::prepare_for_model_with_content_policy(
        ToolResultValidationRequest {
            call: &call,
            result: &result,
            resolved_tool: &resolved_tool,
            schema_registry: &schema_registry,
        },
        &content_policy,
    )
    .map_err(|error| PyValueError::new_err(format!("tool result validation failed: {error:?}")))?;
    let payload = json!({
        "ok": true,
        "output": output.iter().map(serialize_content_part).collect::<Vec<_>>(),
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize prepared tool result output: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_tool_approval_json(
    record_json: &str,
    resolved_tool_json: &str,
    call_json: &str,
    principal_id: &str,
    now_unix_ms: u64,
) -> PyResult<String> {
    let record_value = parse_json_argument(record_json, "tool approval record")?;
    let resolved_tool_value = parse_json_argument(resolved_tool_json, "resolved tool")?;
    let call_value = parse_json_argument(call_json, "tool call")?;
    let record = parse_tool_approval_record(&record_value, "tool approval record")?;
    let resolved_tool = parse_resolved_tool(&resolved_tool_value, "resolved tool")?;
    let call = parse_tool_call(&call_value, "tool call")?;
    let validation_error = record.validate().err();
    let payload = json!({
        "ok": true,
        "recordValid": validation_error.is_none(),
        "validForCall": record.is_valid_for(&resolved_tool, &call, principal_id, now_unix_ms),
        "approvalId": record.approval_id,
        "toolCallId": record.request.tool_call_id,
        "toolName": record.request.tool_name,
        "revision": record.request.revision,
        "status": tool_approval_status_name(record.status),
        "definitionDigest": record.request.definition_digest,
        "bindingDigest": record.request.binding_digest,
        "argumentsDigest": record.request.arguments_digest,
        "policySnapshotId": record.request.policy_snapshot_id,
        "principalId": record.request.principal_id,
        "expiresAtUnixMs": record.request.expires_at_unix_ms,
        "validationError": validation_error
            .as_ref()
            .map(tool_approval_error_json)
            .unwrap_or(Value::Null),
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize tool approval evaluation: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_retry_policy_json(policy_json: &str, request_json: &str) -> PyResult<String> {
    let policy_value = parse_json_argument(policy_json, "retry policy")?;
    let request_value = parse_json_argument(request_json, "retry request")?;
    let policy = parse_retry_policy(&policy_value, "retry policy")?;
    let request = parse_retry_request(&request_value, "retry request")?;
    let decision = policy.decide(&request);
    let payload = match decision {
        RetryDecision::Retry { delay_ms } => json!({
            "ok": true,
            "decision": "retry",
            "delayMs": delay_ms,
            "reason": Value::Null,
        }),
        RetryDecision::Stop { reason } => json!({
            "ok": true,
            "decision": "stop",
            "delayMs": Value::Null,
            "reason": reason,
        }),
    };
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!("failed to serialize retry decision: {error}"))
    })
}

#[pyfunction]
fn evaluate_provider_limit_policy_json(policy_json: &str, incident_json: &str) -> PyResult<String> {
    let policy_value = parse_json_argument(policy_json, "provider limit policy")?;
    let incident_value = parse_json_argument(incident_json, "provider limit incident")?;
    let policy = parse_provider_limit_policy(&policy_value, "provider limit policy")?;
    let incident = parse_provider_limit_incident(&incident_value, "provider limit incident")?;
    let payload = provider_limit_decision_json(policy.decide(&incident));
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize provider limit decision: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_cancellation_scope_json(root_json: &str, operations_json: &str) -> PyResult<String> {
    let parse_scope = |value: &str, label: &str| -> PyResult<CancellationScope> {
        match value {
            "provider_call" | "providerCall" => Ok(CancellationScope::ProviderCall),
            "node" => Ok(CancellationScope::Node),
            "branch" => Ok(CancellationScope::Branch),
            "task_group" | "taskGroup" => Ok(CancellationScope::TaskGroup),
            "agent_step" | "agentStep" => Ok(CancellationScope::AgentStep),
            "turn" => Ok(CancellationScope::Turn),
            "map_item" | "mapItem" => Ok(CancellationScope::MapItem),
            "task" => Ok(CancellationScope::Task),
            "trial" => Ok(CancellationScope::Trial),
            "run" => Ok(CancellationScope::Run),
            "job" => Ok(CancellationScope::Job),
            "session" => Ok(CancellationScope::Session),
            value => Err(PyValueError::new_err(format!(
                "{label} has unknown cancellation scope {value:?}"
            ))),
        }
    };
    let parse_guarantee = |value: &str, label: &str| -> PyResult<CancellationGuarantee> {
        match value {
            "immediate_local" | "immediateLocal" => Ok(CancellationGuarantee::ImmediateLocal),
            "cooperative" => Ok(CancellationGuarantee::Cooperative),
            "best_effort_remote" | "bestEffortRemote" => {
                Ok(CancellationGuarantee::BestEffortRemote)
            }
            "non_cancellable_atomic_section" | "nonCancellableAtomicSection" => {
                Ok(CancellationGuarantee::NonCancellableAtomicSection)
            }
            value => Err(PyValueError::new_err(format!(
                "{label} has unknown cancellation guarantee {value:?}"
            ))),
        }
    };
    let scope_name = |scope: CancellationScope| match scope {
        CancellationScope::ProviderCall => "provider_call",
        CancellationScope::Node => "node",
        CancellationScope::Branch => "branch",
        CancellationScope::TaskGroup => "task_group",
        CancellationScope::AgentStep => "agent_step",
        CancellationScope::Turn => "turn",
        CancellationScope::MapItem => "map_item",
        CancellationScope::Task => "task",
        CancellationScope::Trial => "trial",
        CancellationScope::Run => "run",
        CancellationScope::Job => "job",
        CancellationScope::Session => "session",
    };
    let guarantee_name = |guarantee: CancellationGuarantee| match guarantee {
        CancellationGuarantee::ImmediateLocal => "immediate_local",
        CancellationGuarantee::Cooperative => "cooperative",
        CancellationGuarantee::BestEffortRemote => "best_effort_remote",
        CancellationGuarantee::NonCancellableAtomicSection => "non_cancellable_atomic_section",
    };
    let root_value = parse_json_argument(root_json, "cancellation root")?;
    let operations_value = parse_json_argument(operations_json, "cancellation operations")?;
    let root_object = json_object(&root_value, "cancellation root")?;
    let operations = operations_value
        .as_array()
        .ok_or_else(|| PyValueError::new_err("cancellation operations must be an array"))?;
    let root_id =
        required_alias_string(root_object, "tokenId", "token_id", "cancellation root")?.to_owned();
    let root_scope = parse_scope(
        required_alias_string(root_object, "scope", "scope", "cancellation root")?,
        "cancellation root.scope",
    )?;
    let root_guarantee = parse_guarantee(
        required_alias_string(root_object, "guarantee", "guarantee", "cancellation root")?,
        "cancellation root.guarantee",
    )?;
    let mut tokens = BTreeMap::from([(
        root_id.clone(),
        CancellationToken::new(root_scope, root_guarantee),
    )]);
    let mut operation_results = Vec::new();

    for (operation_index, operation) in operations.iter().enumerate() {
        let label = format!("cancellation operations[{operation_index}]");
        let operation = json_object(operation, &label)?;
        match required_alias_string(operation, "op", "kind", &label)? {
            "child" => {
                let parent_id = required_alias_string(operation, "parentId", "parent_id", &label)?;
                let token_id = required_alias_string(operation, "tokenId", "token_id", &label)?;
                if tokens.contains_key(token_id) {
                    return Err(PyValueError::new_err(format!(
                        "{label}.tokenId duplicates existing token {token_id:?}"
                    )));
                }
                let parent = tokens.get(parent_id).cloned().ok_or_else(|| {
                    PyValueError::new_err(format!(
                        "{label}.parentId references unknown token {parent_id:?}"
                    ))
                })?;
                let child_scope = parse_scope(
                    required_alias_string(operation, "scope", "scope", &label)?,
                    &format!("{label}.scope"),
                )?;
                let child_guarantee = parse_guarantee(
                    required_alias_string(operation, "guarantee", "guarantee", &label)?,
                    &format!("{label}.guarantee"),
                )?;
                let child = parent.child(child_scope, child_guarantee);
                let cancelled = child.is_cancelled();
                tokens.insert(token_id.to_owned(), child);
                operation_results.push(json!({
                    "op": "child",
                    "parentId": parent_id,
                    "tokenId": token_id,
                    "cancelled": cancelled,
                }));
            }
            "cancel" => {
                let token_id = required_alias_string(operation, "tokenId", "token_id", &label)?;
                let reason_value = alias_value(operation, "reason", "reason")
                    .ok_or_else(|| PyValueError::new_err(format!("{label}.reason is required")))?;
                let reason = parse_cancel_reason(reason_value, &format!("{label}.reason"))?;
                let token = tokens.get(token_id).cloned().ok_or_else(|| {
                    PyValueError::new_err(format!(
                        "{label}.tokenId references unknown token {token_id:?}"
                    ))
                })?;
                let accepted = token.cancel(reason);
                operation_results.push(json!({
                    "op": "cancel",
                    "tokenId": token_id,
                    "accepted": accepted,
                    "cancelled": token.is_cancelled(),
                    "reason": token.reason().map(|reason| serialize_cancel_reason(&reason)).unwrap_or(Value::Null),
                }));
            }
            "effective_guarantee" | "effectiveGuarantee" => {
                let requested = parse_guarantee(
                    required_alias_string(operation, "requested", "requested", &label)?,
                    &format!("{label}.requested"),
                )?;
                let capability = parse_guarantee(
                    required_alias_string(operation, "capability", "capability", &label)?,
                    &format!("{label}.capability"),
                )?;
                operation_results.push(json!({
                    "op": "effective_guarantee",
                    "requested": guarantee_name(requested),
                    "capability": guarantee_name(capability),
                    "effective": guarantee_name(CancellationGuarantee::effective(requested, capability)),
                }));
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.op has unknown operation {value:?}"
                )));
            }
        }
    }

    let states = tokens
        .iter()
        .map(|(token_id, token)| {
            json!({
                "tokenId": token_id,
                "scope": scope_name(token.scope()),
                "guarantee": guarantee_name(token.guarantee()),
                "cancelled": token.is_cancelled(),
                "reason": token.reason().map(|reason| serialize_cancel_reason(&reason)).unwrap_or(Value::Null),
            })
        })
        .collect::<Vec<_>>();
    let state_by_token_id = states
        .iter()
        .filter_map(|state| {
            state
                .get("tokenId")
                .and_then(Value::as_str)
                .map(|token_id| (token_id.to_owned(), state.clone()))
        })
        .collect::<serde_json::Map<_, _>>();
    let payload = json!({
        "ok": true,
        "rootTokenId": root_id,
        "operations": operation_results,
        "states": states,
        "stateByTokenId": state_by_token_id,
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize cancellation scope evaluation: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_task_group_json(group_json: &str, operations_json: &str) -> PyResult<String> {
    let parse_failure_policy = |value: &str, label: &str| -> PyResult<TaskGroupFailurePolicy> {
        match value {
            "collect" => Ok(TaskGroupFailurePolicy::Collect),
            "fail_fast" | "failFast" => Ok(TaskGroupFailurePolicy::FailFast),
            value => Err(PyValueError::new_err(format!(
                "{label} has unknown task group failure policy {value:?}"
            ))),
        }
    };
    let parse_cancellation_policy =
        |value: &str, label: &str| -> PyResult<SiblingCancellationPolicy> {
            match value {
                "keep_running" | "keepRunning" => Ok(SiblingCancellationPolicy::KeepRunning),
                "cancel_siblings_on_fatal" | "cancelSiblingsOnFatal" => {
                    Ok(SiblingCancellationPolicy::CancelSiblingsOnFatal)
                }
                value => Err(PyValueError::new_err(format!(
                    "{label} has unknown sibling cancellation policy {value:?}"
                ))),
            }
        };
    let decision_json = |decision: &TaskGroupDecision| -> Value {
        match decision {
            TaskGroupDecision::Pending => json!({"status": "pending"}),
            TaskGroupDecision::Succeeded {
                successes,
                failures,
            } => json!({
                "status": "succeeded",
                "successes": successes,
                "failures": failures,
            }),
            TaskGroupDecision::Failed {
                failure,
                cancel_siblings,
            } => {
                let failure = match failure {
                    TaskGroupFailure::ChildFailed { child_id, error } => json!({
                        "kind": "child_failed",
                        "childId": child_id,
                        "error": serialize_block_error(error),
                    }),
                    TaskGroupFailure::InsufficientSuccesses {
                        successes,
                        required,
                    } => json!({
                        "kind": "insufficient_successes",
                        "successes": successes,
                        "required": required,
                    }),
                    TaskGroupFailure::DeadlineExceeded {
                        deadline_ms,
                        now_ms,
                    } => json!({
                        "kind": "deadline_exceeded",
                        "deadlineMs": deadline_ms,
                        "nowMs": now_ms,
                    }),
                };
                json!({
                    "status": "failed",
                    "failure": failure,
                    "cancelSiblings": cancel_siblings,
                })
            }
        }
    };

    let group_value = parse_json_argument(group_json, "task group")?;
    let operations_value = parse_json_argument(operations_json, "task group operations")?;
    let group_object = json_object(&group_value, "task group")?;
    let operations = operations_value
        .as_array()
        .ok_or_else(|| PyValueError::new_err("task group operations must be an array"))?;
    let children_value = alias_value(group_object, "children", "children")
        .ok_or_else(|| PyValueError::new_err("task group.children is required"))?;
    let children = children_value
        .as_array()
        .ok_or_else(|| PyValueError::new_err("task group.children must be an array"))?
        .iter()
        .enumerate()
        .map(|(index, child)| {
            child.as_str().map(str::to_owned).ok_or_else(|| {
                PyValueError::new_err(format!("task group.children[{index}] must be a string"))
            })
        })
        .collect::<PyResult<Vec<_>>>()?;
    let policy_object = alias_value(group_object, "policy", "policy")
        .map(|value| json_object(value, "task group.policy"))
        .transpose()?;
    let policy_source = policy_object.unwrap_or(group_object);
    let minimum_successes = required_alias_u64(
        policy_source,
        "minimumSuccesses",
        "minimum_successes",
        "task group.policy",
    )?
    .try_into()
    .map_err(|_| PyValueError::new_err("task group.policy.minimumSuccesses is too large"))?;
    let mut policy = TaskGroupPolicy::new(minimum_successes);
    if let Some(failure) =
        optional_nullable_alias_string(policy_source, "failure", "failure", "task group.policy")?
    {
        policy = policy.with_failure(parse_failure_policy(failure, "task group.policy.failure")?);
    }
    if let Some(cancellation) = optional_nullable_alias_string(
        policy_source,
        "cancellation",
        "cancellation",
        "task group.policy",
    )? {
        policy = policy.with_cancellation(parse_cancellation_policy(
            cancellation,
            "task group.policy.cancellation",
        )?);
    }
    if let Some(deadline_ms) = optional_nullable_alias_u64(
        policy_source,
        "deadlineMs",
        "deadline_ms",
        "task group.policy",
    )? {
        policy = policy.with_deadline_ms(deadline_ms);
    }
    let mut group = TaskGroupState::new(children.iter().map(String::as_str), policy)
        .map_err(|error| PyValueError::new_err(format!("invalid task group: {error:?}")))?;
    let mut operation_results = Vec::new();
    let mut final_decision = TaskGroupDecision::Pending;

    for (operation_index, operation) in operations.iter().enumerate() {
        let label = format!("task group operations[{operation_index}]");
        let operation = json_object(operation, &label)?;
        let decision = match required_alias_string(operation, "op", "kind", &label)? {
            "start" | "started" => {
                let child_id = required_alias_string(operation, "childId", "child_id", &label)?;
                group.record_started(child_id)
            }
            "succeed" | "success" | "succeeded" => {
                let child_id = required_alias_string(operation, "childId", "child_id", &label)?;
                group.record_success(child_id)
            }
            "fail" | "failure" | "failed" => {
                let child_id = required_alias_string(operation, "childId", "child_id", &label)?;
                let error_value = alias_value(operation, "error", "error")
                    .ok_or_else(|| PyValueError::new_err(format!("{label}.error is required")))?;
                let error = parse_block_error(error_value, &format!("{label}.error"))?;
                group.record_failure(child_id, error)
            }
            "deadline" | "check_deadline" | "checkDeadline" => {
                let now_ms = required_alias_u64(operation, "nowMs", "now_ms", &label)?;
                group.check_deadline(now_ms)
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.op has unknown operation {value:?}"
                )));
            }
        }
        .map_err(|error| PyValueError::new_err(format!("{label} failed: {error:?}")))?;
        final_decision = decision.clone();
        operation_results.push(json!({
            "op": required_alias_string(operation, "op", "kind", &label)?,
            "decision": decision_json(&decision),
        }));
    }

    let children_state = children
        .iter()
        .map(|child_id| {
            let state = group.child_state(child_id).ok_or_else(|| {
                PyRuntimeError::new_err(format!("task group lost child state {child_id:?}"))
            })?;
            let state_json = match state {
                ChildTaskState::Pending => json!({"status": "pending"}),
                ChildTaskState::Running => json!({"status": "running"}),
                ChildTaskState::Succeeded => json!({"status": "succeeded"}),
                ChildTaskState::Failed(error) => json!({
                    "status": "failed",
                    "error": serialize_block_error(error),
                }),
                ChildTaskState::Cancelled(reason) => json!({
                    "status": "cancelled",
                    "reason": format!("{:?}", reason.code),
                }),
            };
            Ok((child_id.clone(), state_json))
        })
        .collect::<PyResult<serde_json::Map<_, _>>>()?;
    let payload = json!({
        "ok": true,
        "decision": decision_json(&final_decision),
        "operations": operation_results,
        "children": children_state,
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize task group evaluation: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_node_lifecycle_json(state_json: &str, operations_json: &str) -> PyResult<String> {
    let parse_node_status = |value: &str, label: &str| -> PyResult<NodeStatus> {
        match value {
            "pending" => Ok(NodeStatus::Pending),
            "ready" => Ok(NodeStatus::Ready),
            "waiting_budget" | "waitingBudget" => Ok(NodeStatus::WaitingBudget),
            "waiting_lease" | "waitingLease" => Ok(NodeStatus::WaitingLease),
            "waiting_approval" | "waitingApproval" => Ok(NodeStatus::WaitingApproval),
            "running" => Ok(NodeStatus::Running),
            "completed" => Ok(NodeStatus::Completed),
            "failed" => Ok(NodeStatus::Failed),
            "cancelled" => Ok(NodeStatus::Cancelled),
            "skipped" => Ok(NodeStatus::Skipped),
            "paused" => Ok(NodeStatus::Paused),
            "policy_stopped" | "policyStopped" => Ok(NodeStatus::PolicyStopped),
            value => Err(PyValueError::new_err(format!(
                "{label} has unknown node lifecycle status {value:?}"
            ))),
        }
    };
    let status_name = |status: NodeStatus| match status {
        NodeStatus::Pending => "pending",
        NodeStatus::Ready => "ready",
        NodeStatus::WaitingBudget => "waiting_budget",
        NodeStatus::WaitingLease => "waiting_lease",
        NodeStatus::WaitingApproval => "waiting_approval",
        NodeStatus::Running => "running",
        NodeStatus::Completed => "completed",
        NodeStatus::Failed => "failed",
        NodeStatus::Cancelled => "cancelled",
        NodeStatus::Skipped => "skipped",
        NodeStatus::Paused => "paused",
        NodeStatus::PolicyStopped => "policy_stopped",
    };
    let lifecycle_error_json = |error: LifecycleError| match error {
        LifecycleError::AlreadyTerminal { current } => json!({
            "code": "AlreadyTerminal",
            "current": status_name(current),
        }),
        LifecycleError::OutputAfterTerminal { current } => json!({
            "code": "OutputAfterTerminal",
            "current": status_name(current),
        }),
        LifecycleError::PatchAfterTerminal { current } => json!({
            "code": "PatchAfterTerminal",
            "current": status_name(current),
        }),
        LifecycleError::TerminalTransitionRequiresOutcome { requested } => json!({
            "code": "TerminalTransitionRequiresOutcome",
            "requested": status_name(requested),
        }),
    };
    let lifecycle_state_json = |lifecycle: &NodeLifecycle| {
        json!({
            "status": status_name(lifecycle.status()),
            "terminal": lifecycle.status().is_terminal(),
            "outputs": lifecycle.outputs().iter().map(|output| {
                json!({
                    "port": output.port.as_str(),
                    "value": &output.value,
                })
            }).collect::<Vec<_>>(),
            "statePatches": lifecycle.state_patches(),
            "terminalError": lifecycle.terminal_error().map(serialize_block_error).unwrap_or(Value::Null),
            "cancelReason": lifecycle.cancel_reason().map(serialize_cancel_reason).unwrap_or(Value::Null),
        })
    };

    let state_value = parse_json_argument(state_json, "node lifecycle state")?;
    let operations_value = parse_json_argument(operations_json, "node lifecycle operations")?;
    let state_object = json_object(&state_value, "node lifecycle state")?;
    let operations = operations_value
        .as_array()
        .ok_or_else(|| PyValueError::new_err("node lifecycle operations must be an array"))?;
    let mut lifecycle = NodeLifecycle::new();
    if let Some(initial_status) = optional_nullable_alias_string(
        state_object,
        "initialStatus",
        "initial_status",
        "node lifecycle state",
    )? {
        let status = parse_node_status(initial_status, "node lifecycle state.initialStatus")?;
        if status != NodeStatus::Pending {
            lifecycle.transition(status).map_err(|error| {
                PyValueError::new_err(format!("invalid node lifecycle state: {error:?}"))
            })?;
        }
    }
    let mut operation_results = Vec::new();

    for (operation_index, operation) in operations.iter().enumerate() {
        let label = format!("node lifecycle operations[{operation_index}]");
        let operation = json_object(operation, &label)?;
        let op = required_alias_string(operation, "op", "kind", &label)?;
        let result = match op {
            "transition" => {
                let status = parse_node_status(
                    required_alias_string(operation, "status", "status", &label)?,
                    &format!("{label}.status"),
                )?;
                lifecycle.transition(status).map(|()| Value::Null)
            }
            "output" | "record_output" | "recordOutput" => {
                let port = required_alias_string(operation, "port", "port", &label)?;
                let value = alias_value(operation, "value", "value")
                    .ok_or_else(|| PyValueError::new_err(format!("{label}.value is required")))?;
                lifecycle
                    .record_output(port, value.clone())
                    .map(|()| Value::Null)
            }
            "patch" | "state_patch" | "statePatch" => {
                let patch = alias_value(operation, "patch", "patch")
                    .ok_or_else(|| PyValueError::new_err(format!("{label}.patch is required")))?;
                lifecycle
                    .apply_state_patch(patch.clone())
                    .map(|()| Value::Null)
            }
            "complete" => lifecycle.complete().map(|changed| json!(changed)),
            "skip" => lifecycle.skip().map(|changed| json!(changed)),
            "pause" => lifecycle.pause().map(|changed| json!(changed)),
            "policy_stop" | "policyStop" => lifecycle.policy_stop().map(|changed| json!(changed)),
            "fail" => {
                let error_value = alias_value(operation, "error", "error")
                    .ok_or_else(|| PyValueError::new_err(format!("{label}.error is required")))?;
                let error = parse_block_error(error_value, &format!("{label}.error"))?;
                lifecycle.fail(error).map(|changed| json!(changed))
            }
            "cancel" => {
                let reason_value = alias_value(operation, "reason", "reason")
                    .ok_or_else(|| PyValueError::new_err(format!("{label}.reason is required")))?;
                let reason = parse_cancel_reason(reason_value, &format!("{label}.reason"))?;
                lifecycle.cancel(reason).map(|changed| json!(changed))
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.op has unknown operation {value:?}"
                )));
            }
        };
        match result {
            Ok(changed) => operation_results.push(json!({
                "op": op,
                "ok": true,
                "changed": changed,
                "status": status_name(lifecycle.status()),
            })),
            Err(error) => operation_results.push(json!({
                "op": op,
                "ok": false,
                "error": lifecycle_error_json(error),
                "status": status_name(lifecycle.status()),
            })),
        }
    }

    let payload = json!({
        "ok": true,
        "state": lifecycle_state_json(&lifecycle),
        "operations": operation_results,
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize node lifecycle evaluation: {error}"
        ))
    })
}

#[pyfunction]
#[pyo3(signature = (
    resolved_tool_json,
    call_json,
    effect_key=None,
    idempotency_key=None,
    policy_decision_id=None,
    execution_target=None,
    sandbox_id=None
))]
fn record_tool_effect_precondition_json(
    resolved_tool_json: &str,
    call_json: &str,
    effect_key: Option<&str>,
    idempotency_key: Option<&str>,
    policy_decision_id: Option<&str>,
    execution_target: Option<&str>,
    sandbox_id: Option<&str>,
) -> PyResult<String> {
    let resolved_tool_value = parse_json_argument(resolved_tool_json, "resolved tool")?;
    let call_value = parse_json_argument(call_json, "tool call")?;
    let resolved_tool = parse_resolved_tool(&resolved_tool_value, "resolved tool")?;
    let call = parse_tool_call(&call_value, "tool call")?;

    let precondition = ToolEffectPrecondition::from_admitted_call(ToolEffectPreconditionContext {
        resolved_tool: &resolved_tool,
        call: &call,
        effect_key,
        idempotency_key,
        policy_decision_id,
        execution_target,
        sandbox_id,
    })
    .map_err(|error| PyValueError::new_err(format!("invalid tool effect precondition: {error}")))?;
    let payload = json!({
        "payload": precondition.payload,
        "digest": precondition.digest,
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize tool effect precondition: {error}"
        ))
    })
}

#[pyfunction]
#[pyo3(signature = (
    event_id,
    occurred_at,
    actor_json,
    resolved_tool_json,
    call_json,
    result_json,
    effect_key=None,
    precondition_digest=None,
    idempotency_key=None,
    policy_decision_id=None
))]
fn record_tool_effect_audit_event_json(
    event_id: &str,
    occurred_at: &str,
    actor_json: &str,
    resolved_tool_json: &str,
    call_json: &str,
    result_json: &str,
    effect_key: Option<&str>,
    precondition_digest: Option<&str>,
    idempotency_key: Option<&str>,
    policy_decision_id: Option<&str>,
) -> PyResult<String> {
    let actor_value = parse_json_argument(actor_json, "actor")?;
    let resolved_tool_value = parse_json_argument(resolved_tool_json, "resolved tool")?;
    let call_value = parse_json_argument(call_json, "tool call")?;
    let result_value = parse_json_argument(result_json, "tool result")?;
    let actor = parse_principal_ref(&actor_value, "actor")?;
    let resolved_tool = parse_resolved_tool(&resolved_tool_value, "resolved tool")?;
    let call = parse_tool_call(&call_value, "tool call")?;
    let result = parse_tool_result(&result_value, "tool result")?;

    let event = AuditEvent::tool_effect_outcome(ToolEffectAuditContext {
        event_id,
        occurred_at,
        actor,
        resolved_tool: &resolved_tool,
        call: &call,
        result: &result,
        effect_key,
        precondition_digest,
        idempotency_key,
        policy_decision_id,
    })
    .map_err(|error| PyValueError::new_err(format!("invalid tool effect audit event: {error}")))?;
    let payload = serialize_audit_event(&event);
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize tool effect audit event: {error}"
        ))
    })
}

#[pyfunction]
fn capture_telemetry_content_json(decision_json: &str, content_json: &str) -> PyResult<String> {
    let decision_value = parse_json_argument(decision_json, "telemetry capture decision")?;
    let content_value = parse_json_argument(content_json, "telemetry captured content")?;
    let decision = parse_capture_decision(&decision_value, "telemetry capture decision")?;
    let content = json_object(&content_value, "telemetry captured content")?;
    let content_kind = required_alias_string(
        content,
        "contentKind",
        "content_kind",
        "telemetry captured content",
    )?;
    let text = required_string(content, "text", "telemetry captured content")?;
    let content_ref = optional_nullable_alias_string(
        content,
        "contentRef",
        "content_ref",
        "telemetry captured content",
    )?;
    let redactions = if let Some(redactions) = content.get("redactions") {
        let Some(redactions) = redactions.as_array() else {
            return Err(PyValueError::new_err(
                "telemetry captured content.redactions must be an array",
            ));
        };
        redactions
            .iter()
            .enumerate()
            .map(|(index, redaction)| {
                let label = format!("telemetry captured content.redactions[{index}]");
                let redaction = json_object(redaction, &label)?;
                Ok(RedactionRule::literal(
                    required_string(redaction, "pattern", &label)?,
                    required_string(redaction, "replacement", &label)?,
                ))
            })
            .collect::<PyResult<Vec<_>>>()?
    } else {
        Vec::new()
    };

    let captured = decision.capture_text(content_kind, text, content_ref, redactions);
    serde_json::to_string(&serialize_captured_content(&captured)).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize telemetry captured content: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_tool_result_stream_json(state_json: &str, operations_json: &str) -> PyResult<String> {
    let state_value = parse_json_argument(state_json, "tool result stream state")?;
    let operations_value = parse_json_argument(operations_json, "tool result stream operations")?;
    let state_object = json_object(&state_value, "tool result stream state")?;
    let mut stream = ToolResultStreamState::new();
    if let Some(events) = state_object
        .get("acceptedEvents")
        .or_else(|| state_object.get("accepted_events"))
    {
        let Some(events) = events.as_array() else {
            return Err(PyValueError::new_err(
                "tool result stream state.acceptedEvents must be an array",
            ));
        };
        for (event_index, event) in events.iter().enumerate() {
            let event = parse_tool_result_event(
                event,
                &format!("tool result stream state.acceptedEvents[{event_index}]"),
            )?;
            stream.accept(event).map_err(|error| {
                PyValueError::new_err(format!(
                    "invalid tool result stream state event {event_index}: {error:?}"
                ))
            })?;
        }
    }

    let operations = operations_value.as_array().ok_or_else(|| {
        PyValueError::new_err("tool result stream operations JSON must be an array")
    })?;
    let mut updates = Vec::new();
    for (operation_index, operation) in operations.iter().enumerate() {
        let label = format!("operations[{operation_index}]");
        let operation = json_object(operation, &label)?;
        match required_string(operation, "kind", &label)? {
            "event" => {
                let event_value = operation
                    .get("event")
                    .ok_or_else(|| PyValueError::new_err(format!("{label}.event is required")))?;
                let event = parse_tool_result_event(event_value, &format!("{label}.event"))?;
                match stream.accept(event) {
                    Ok(accepted) => updates.push(json!({
                        "operationIndex": operation_index,
                        "kind": "accepted",
                        "event": serialize_tool_result_event(&accepted),
                        "final": accepted.is_final_durable_result(),
                    })),
                    Err(error) => updates.push(json!({
                        "operationIndex": operation_index,
                        "kind": "error",
                        "error": serialize_tool_result_stream_error(&error),
                    })),
                }
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.kind has unknown kind {value:?}"
                )));
            }
        }
    }

    let accepted_events = stream
        .accepted_events()
        .iter()
        .map(serialize_tool_result_event)
        .collect::<Vec<_>>();
    let mut last_sequences = BTreeMap::new();
    let mut final_results = BTreeMap::new();
    for event in stream.accepted_events() {
        last_sequences.insert(event.tool_call_id().to_owned(), event.sequence());
        if let Some(result) = event.clone().into_result() {
            final_results.insert(
                event.tool_call_id().to_owned(),
                serialize_tool_result(&result),
            );
        }
    }
    let payload = json!({
        "ok": updates.iter().all(|update| update.get("kind").and_then(Value::as_str) != Some("error")),
        "updates": updates,
        "state": {
            "acceptedEvents": accepted_events,
            "lastSequences": last_sequences,
            "finalResults": final_results,
        },
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize tool result stream evaluation: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_application_event_stream_json(
    state_json: &str,
    operations_json: &str,
) -> PyResult<String> {
    let state_value = parse_json_argument(state_json, "application event stream state")?;
    let operations_value =
        parse_json_argument(operations_json, "application event stream operations")?;
    let state_object = json_object(&state_value, "application event stream state")?;
    let mut stream = ApplicationEventStreamState::default();
    if let Some(events) = state_object
        .get("acceptedEvents")
        .or_else(|| state_object.get("accepted_events"))
    {
        let Some(events) = events.as_array() else {
            return Err(PyValueError::new_err(
                "application event stream state.acceptedEvents must be an array",
            ));
        };
        for (event_index, event) in events.iter().enumerate() {
            let event = parse_application_event(
                event,
                &format!("application event stream state.acceptedEvents[{event_index}]"),
            )?;
            stream.accept(event).ok_or_else(|| {
                PyValueError::new_err(format!(
                    "invalid application event stream state event {event_index}"
                ))
            })?;
        }
    }

    let operations = operations_value.as_array().ok_or_else(|| {
        PyValueError::new_err("application event stream operations JSON must be an array")
    })?;
    let mut updates = Vec::new();
    for (operation_index, operation) in operations.iter().enumerate() {
        let label = format!("operations[{operation_index}]");
        let operation = json_object(operation, &label)?;
        match required_string(operation, "kind", &label)? {
            "event" => {
                let event_value = operation
                    .get("event")
                    .ok_or_else(|| PyValueError::new_err(format!("{label}.event is required")))?;
                let event = parse_application_event(event_value, &format!("{label}.event"))?;
                let event_payload = serialize_application_event(&event);
                if let Some(accepted) = stream.accept(event) {
                    updates.push(json!({
                        "operationIndex": operation_index,
                        "kind": "accepted",
                        "event": serialize_application_event(&accepted),
                    }));
                } else {
                    updates.push(json!({
                        "operationIndex": operation_index,
                        "kind": "dropped",
                        "event": event_payload,
                    }));
                }
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.kind has unknown kind {value:?}"
                )));
            }
        }
    }

    let accepted_events = stream
        .accepted_events()
        .iter()
        .map(serialize_application_event)
        .collect::<Vec<_>>();
    let cutoff_responses = stream
        .accepted_events()
        .iter()
        .filter(|event| event.kind == ApplicationEventKind::OutputCutoff)
        .map(|event| {
            event
                .payload
                .get("response_id")
                .and_then(Value::as_str)
                .unwrap_or(&event.metadata.response_id)
                .to_owned()
        })
        .collect::<BTreeSet<_>>();
    let payload = json!({
        "ok": true,
        "updates": updates,
        "state": {
            "acceptedEvents": accepted_events,
            "cutoffResponses": cutoff_responses,
        },
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize application event stream evaluation: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_application_protocol_stream_json(
    state_json: &str,
    operations_json: &str,
) -> PyResult<String> {
    let state_value = parse_json_argument(state_json, "application protocol stream state")?;
    let operations_value =
        parse_json_argument(operations_json, "application protocol stream operations")?;
    let state_object = json_object(&state_value, "application protocol stream state")?;
    let mut stream = ApplicationProtocolStreamState::default();
    if let Some(events) = state_object
        .get("acceptedEvents")
        .or_else(|| state_object.get("accepted_events"))
    {
        let Some(events) = events.as_array() else {
            return Err(PyValueError::new_err(
                "application protocol stream state.acceptedEvents must be an array",
            ));
        };
        for (event_index, event) in events.iter().enumerate() {
            let event = parse_application_protocol_event(
                event,
                &format!("application protocol stream state.acceptedEvents[{event_index}]"),
            )?;
            stream.accept(event).ok_or_else(|| {
                PyValueError::new_err(format!(
                    "invalid application protocol stream state event {event_index}"
                ))
            })?;
        }
    }

    let operations = operations_value.as_array().ok_or_else(|| {
        PyValueError::new_err("application protocol stream operations JSON must be an array")
    })?;
    let mut updates = Vec::new();
    for (operation_index, operation) in operations.iter().enumerate() {
        let label = format!("operations[{operation_index}]");
        let operation = json_object(operation, &label)?;
        match required_string(operation, "kind", &label)? {
            "event" => {
                let event_value = operation
                    .get("event")
                    .ok_or_else(|| PyValueError::new_err(format!("{label}.event is required")))?;
                let event =
                    parse_application_protocol_event(event_value, &format!("{label}.event"))?;
                let event_payload = serialize_application_protocol_event(&event);
                if let Some(accepted) = stream.accept(event) {
                    updates.push(json!({
                        "operationIndex": operation_index,
                        "kind": "accepted",
                        "event": serialize_application_protocol_event(&accepted),
                    }));
                } else {
                    updates.push(json!({
                        "operationIndex": operation_index,
                        "kind": "dropped",
                        "event": event_payload,
                    }));
                }
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.kind has unknown kind {value:?}"
                )));
            }
        }
    }

    let accepted_events = stream
        .accepted_events()
        .iter()
        .map(serialize_application_protocol_event)
        .collect::<Vec<_>>();
    let cutoff_responses = stream
        .accepted_events()
        .iter()
        .filter(|event| event.kind == ApplicationProtocolEventKind::OutputCutoff)
        .filter_map(|event| {
            event
                .payload
                .get("response_id")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .collect::<BTreeSet<_>>();
    let payload = json!({
        "ok": true,
        "updates": updates,
        "state": {
            "acceptedEvents": accepted_events,
            "cutoffResponses": cutoff_responses,
        },
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize application protocol stream evaluation: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_application_protocol_log_json(
    state_json: &str,
    operations_json: &str,
) -> PyResult<String> {
    let state_value = parse_json_argument(state_json, "application protocol log state")?;
    let operations_value =
        parse_json_argument(operations_json, "application protocol log operations")?;
    let state_object = json_object(&state_value, "application protocol log state")?;
    let mut log = ApplicationProtocolLog::new();
    if let Some(events) = state_object
        .get("events")
        .or_else(|| state_object.get("acceptedEvents"))
        .or_else(|| state_object.get("accepted_events"))
    {
        let Some(events) = events.as_array() else {
            return Err(PyValueError::new_err(
                "application protocol log state.events must be an array",
            ));
        };
        for (event_index, event) in events.iter().enumerate() {
            let event = parse_application_protocol_event(
                event,
                &format!("application protocol log state.events[{event_index}]"),
            )?;
            log.append(event).map_err(|error| {
                PyValueError::new_err(format!(
                    "invalid application protocol log state event {event_index}: {error}"
                ))
            })?;
        }
    }

    let operations = operations_value.as_array().ok_or_else(|| {
        PyValueError::new_err("application protocol log operations JSON must be an array")
    })?;
    let mut updates = Vec::new();
    for (operation_index, operation) in operations.iter().enumerate() {
        let label = format!("operations[{operation_index}]");
        let operation = json_object(operation, &label)?;
        match required_string(operation, "kind", &label)? {
            "append" => {
                let event_value = operation
                    .get("event")
                    .ok_or_else(|| PyValueError::new_err(format!("{label}.event is required")))?;
                let event =
                    parse_application_protocol_event(event_value, &format!("{label}.event"))?;
                let event_payload = serialize_application_protocol_event(&event);
                match log.append(event) {
                    Ok(true) => updates.push(json!({
                        "operationIndex": operation_index,
                        "kind": "appended",
                        "event": event_payload,
                    })),
                    Ok(false) => updates.push(json!({
                        "operationIndex": operation_index,
                        "kind": "duplicate",
                        "event": event_payload,
                    })),
                    Err(error) => updates.push(json!({
                        "operationIndex": operation_index,
                        "kind": "error",
                        "error": serialize_application_protocol_log_error(&error),
                        "event": event_payload,
                    })),
                }
            }
            "replay_after" | "replayAfter" => {
                let cursor = optional_nullable_alias_string(operation, "cursor", "cursor", &label)?;
                let limit = optional_alias_u64(operation, "limit", "limit", &label)?.unwrap_or(100);
                let replay = log
                    .replay_after(cursor, limit as usize)
                    .iter()
                    .map(serialize_application_protocol_event)
                    .collect::<Vec<_>>();
                updates.push(json!({
                    "operationIndex": operation_index,
                    "kind": "replay",
                    "events": replay,
                }));
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.kind has unknown kind {value:?}"
                )));
            }
        }
    }

    let events = log
        .replay_after(None, usize::MAX)
        .iter()
        .map(serialize_application_protocol_event)
        .collect::<Vec<_>>();
    let payload = json!({
        "ok": updates.iter().all(|update| update.get("kind").and_then(Value::as_str) != Some("error")),
        "updates": updates,
        "state": {
            "events": events,
            "length": log.len(),
        },
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize application protocol log evaluation: {error}"
        ))
    })
}

#[pyfunction]
fn negotiate_application_protocol_capabilities_json(
    server_json: &str,
    client_json: &str,
) -> PyResult<String> {
    let server_value =
        parse_json_argument(server_json, "server application protocol capabilities")?;
    let client_value =
        parse_json_argument(client_json, "client application protocol capabilities")?;
    let server = parse_application_protocol_capabilities(&server_value, "server capabilities")?;
    let client = parse_application_protocol_capabilities(&client_value, "client capabilities")?;
    let negotiated = server.negotiate(&client).map_err(|error| {
        PyValueError::new_err(format!(
            "application protocol capability negotiation failed: {error}"
        ))
    })?;
    let mut commands = negotiated
        .commands
        .iter()
        .map(ApplicationCommandKind::as_str)
        .collect::<Vec<_>>();
    commands.sort_unstable();
    let mut events = negotiated
        .events
        .iter()
        .map(ApplicationProtocolEventKind::as_str)
        .collect::<Vec<_>>();
    events.sort_unstable();
    let payload = json!({
        "ok": true,
        "protocolVersion": negotiated.protocol_version,
        "commands": commands,
        "events": events,
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize application protocol capability negotiation: {error}"
        ))
    })
}

struct JsonNodeExecutor {
    outputs_by_node: BTreeMap<String, Value>,
}

impl NodeExecutor for JsonNodeExecutor {
    fn execute(&mut self, node: StartedNode) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        let Some(outputs) = self
            .outputs_by_node
            .get(&node.node_id)
            .and_then(Value::as_object)
        else {
            return Err(BlockError::new(
                format!("{}.missing_fixture", node.node_id),
                ErrorCategory::Configuration,
                "node output fixture must be an object",
                false,
            ));
        };

        Ok(outputs
            .iter()
            .map(|(port, value)| {
                (
                    PortRef::new(node.node_id.clone(), port.clone()),
                    Outcome::Value(value.clone()),
                )
            })
            .collect())
    }
}

struct RuntimeBridgePlan {
    graph_hash: String,
    edges: Vec<Value>,
    scheduled_nodes: Vec<ScheduledNode>,
}

fn parse_json_argument(text: &str, label: &str) -> PyResult<Value> {
    serde_json::from_str::<Value>(text)
        .map_err(|error| PyValueError::new_err(format!("invalid {label} JSON: {error}")))
}

fn json_object<'a>(value: &'a Value, label: &str) -> PyResult<&'a serde_json::Map<String, Value>> {
    value
        .as_object()
        .ok_or_else(|| PyValueError::new_err(format!("{label} must be an object")))
}

fn required_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    field: &str,
    label: &str,
) -> PyResult<&'a str> {
    object
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| PyValueError::new_err(format!("{label}.{field} must be a string")))
}

fn required_alias_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    label: &str,
) -> PyResult<&'a str> {
    object
        .get(primary)
        .or_else(|| object.get(alternate))
        .and_then(Value::as_str)
        .ok_or_else(|| PyValueError::new_err(format!("{label}.{primary} must be a string")))
}

fn required_u64(
    object: &serde_json::Map<String, Value>,
    field: &str,
    label: &str,
) -> PyResult<u64> {
    object.get(field).and_then(Value::as_u64).ok_or_else(|| {
        PyValueError::new_err(format!("{label}.{field} must be an unsigned integer"))
    })
}

fn required_alias_u64(
    object: &serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    label: &str,
) -> PyResult<u64> {
    object
        .get(primary)
        .or_else(|| object.get(alternate))
        .and_then(Value::as_u64)
        .ok_or_else(|| {
            PyValueError::new_err(format!("{label}.{primary} must be an unsigned integer"))
        })
}

fn optional_alias_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    label: &str,
) -> PyResult<Option<&'a str>> {
    object
        .get(primary)
        .or_else(|| object.get(alternate))
        .map(|value| {
            value
                .as_str()
                .ok_or_else(|| PyValueError::new_err(format!("{label}.{primary} must be a string")))
        })
        .transpose()
}

fn optional_alias_u64(
    object: &serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    label: &str,
) -> PyResult<Option<u64>> {
    object
        .get(primary)
        .or_else(|| object.get(alternate))
        .map(|value| {
            value.as_u64().ok_or_else(|| {
                PyValueError::new_err(format!("{label}.{primary} must be an unsigned integer"))
            })
        })
        .transpose()
}

fn alias_value<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
) -> Option<&'a Value> {
    object.get(primary).or_else(|| object.get(alternate))
}

fn optional_nullable_alias_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    label: &str,
) -> PyResult<Option<&'a str>> {
    alias_value(object, primary, alternate)
        .filter(|value| !value.is_null())
        .map(|value| {
            value
                .as_str()
                .ok_or_else(|| PyValueError::new_err(format!("{label}.{primary} must be a string")))
        })
        .transpose()
}

fn optional_nullable_alias_u64(
    object: &serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    label: &str,
) -> PyResult<Option<u64>> {
    alias_value(object, primary, alternate)
        .filter(|value| !value.is_null())
        .map(|value| {
            value.as_u64().ok_or_else(|| {
                PyValueError::new_err(format!("{label}.{primary} must be an unsigned integer"))
            })
        })
        .transpose()
}

fn optional_nullable_alias_bool(
    object: &serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    label: &str,
) -> PyResult<Option<bool>> {
    alias_value(object, primary, alternate)
        .filter(|value| !value.is_null())
        .map(|value| {
            value.as_bool().ok_or_else(|| {
                PyValueError::new_err(format!("{label}.{primary} must be a boolean"))
            })
        })
        .transpose()
}

fn u64_to_usize(value: u64, label: &str) -> PyResult<usize> {
    usize::try_from(value).map_err(|_| PyValueError::new_err(format!("{label} exceeds usize")))
}

fn u64_to_u32(value: u64, label: &str) -> PyResult<u32> {
    u32::try_from(value).map_err(|_| PyValueError::new_err(format!("{label} exceeds u32")))
}

fn parse_daemon_config(value: Option<&Value>) -> PyResult<DaemonConfig> {
    let mut config = DaemonConfig::new("daemon-1", "127.0.0.1:0");
    let Some(value) = value else {
        return Ok(config);
    };
    let object = json_object(value, "daemon config")?;
    let daemon_id =
        optional_nullable_alias_string(object, "daemonId", "daemon_id", "daemon config")?
            .unwrap_or("daemon-1");
    let bind_address =
        optional_nullable_alias_string(object, "bindAddress", "bind_address", "daemon config")?
            .unwrap_or("127.0.0.1:0");
    config = DaemonConfig::new(daemon_id, bind_address);
    if let Some(max_workers) =
        optional_nullable_alias_u64(object, "maxWorkers", "max_workers", "daemon config")?
    {
        config = config.with_max_workers(u64_to_usize(max_workers, "daemon config.maxWorkers")?);
    }
    if let Some(package_lock_hash) = optional_nullable_alias_string(
        object,
        "packageLockHash",
        "package_lock_hash",
        "daemon config",
    )? {
        config = config.require_package_lock_hash(package_lock_hash);
    }
    Ok(config)
}

fn daemon_status_json(status: &DaemonStatus) -> Value {
    json!({
        "daemonId": status.daemon_id,
        "bindAddress": status.bind_address,
        "protocolVersion": status.protocol_version,
        "readyWorkers": status.ready_workers,
        "saturatedWorkers": status.saturated_workers,
        "drainingWorkers": status.draining_workers,
        "admittedWorkers": status.admitted_workers,
        "rejectedWorkers": status.rejected_workers,
    })
}

fn worker_registry_error_json(error: &WorkerRegistryError) -> Value {
    match error {
        WorkerRegistryError::UnknownWorker { worker_id } => {
            json!({"code": "daemon.unknown_worker", "workerId": worker_id})
        }
        WorkerRegistryError::DrainPlan { source } => {
            json!({"code": "daemon.invalid_drain_plan", "message": format!("{source:?}")})
        }
        WorkerRegistryError::IncompatibleMessageProtocolVersion { expected, actual } => json!({
            "code": "daemon.incompatible_message_protocol_version",
            "expected": expected,
            "actual": actual,
        }),
        WorkerRegistryError::EmptyMessageId => json!({"code": "daemon.empty_message_id"}),
        WorkerRegistryError::EmptyCorrelationId => json!({"code": "daemon.empty_correlation_id"}),
        WorkerRegistryError::EmptyCausationId => json!({"code": "daemon.empty_causation_id"}),
        WorkerRegistryError::KindPayloadMismatch { kind, payload_kind } => json!({
            "code": "daemon.kind_payload_mismatch",
            "kind": worker_message_kind_name(*kind),
            "payloadKind": worker_message_kind_name(*payload_kind),
        }),
        WorkerRegistryError::UnexpectedWorkerMessageKind { kind } => json!({
            "code": "daemon.unexpected_worker_message_kind",
            "kind": worker_message_kind_name(*kind),
        }),
        WorkerRegistryError::InvalidWireMessage { field, expected } => json!({
            "code": "daemon.invalid_wire_message",
            "field": field,
            "expected": expected,
        }),
        WorkerRegistryError::WirePayloadDecode { kind, source } => json!({
            "code": "daemon.wire_payload_decode_failed",
            "kind": worker_message_kind_name(*kind),
            "message": source,
        }),
    }
}

fn worker_message_kind_name(kind: WorkerProtocolMessageKind) -> &'static str {
    match kind {
        WorkerProtocolMessageKind::Advertisement => "advertisement",
        WorkerProtocolMessageKind::AdmissionDecision => "admission_decision",
        WorkerProtocolMessageKind::InvokeRequest => "invoke_request",
        WorkerProtocolMessageKind::InvokeResult => "invoke_result",
        WorkerProtocolMessageKind::DrainPlan => "drain_plan",
        WorkerProtocolMessageKind::Error => "error",
    }
}

fn parse_json_value_map(value: Option<&Value>, label: &str) -> PyResult<BTreeMap<String, Value>> {
    let Some(value) = value else {
        return Ok(BTreeMap::new());
    };
    let object = json_object(value, label)?;
    Ok(object
        .iter()
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect())
}

fn parse_string_map(value: Option<&Value>, label: &str) -> PyResult<BTreeMap<String, String>> {
    let Some(value) = value else {
        return Ok(BTreeMap::new());
    };
    let object = json_object(value, label)?;
    let mut output = BTreeMap::new();
    for (key, value) in object {
        let Some(value) = value.as_str() else {
            return Err(PyValueError::new_err(format!(
                "{label}.{key} must be a string"
            )));
        };
        output.insert(key.clone(), value.to_owned());
    }
    Ok(output)
}

fn parse_string_set(value: Option<&Value>, label: &str) -> PyResult<BTreeSet<String>> {
    let Some(value) = value else {
        return Ok(BTreeSet::new());
    };
    let Some(values) = value.as_array() else {
        return Err(PyValueError::new_err(format!("{label} must be an array")));
    };
    values
        .iter()
        .enumerate()
        .map(|(index, value)| {
            value
                .as_str()
                .map(str::to_owned)
                .ok_or_else(|| PyValueError::new_err(format!("{label}[{index}] must be a string")))
        })
        .collect()
}

fn parse_string_vec(value: Option<&Value>, label: &str) -> PyResult<Vec<String>> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let Some(values) = value.as_array() else {
        return Err(PyValueError::new_err(format!("{label} must be an array")));
    };
    values
        .iter()
        .enumerate()
        .map(|(index, value)| {
            value
                .as_str()
                .map(str::to_owned)
                .ok_or_else(|| PyValueError::new_err(format!("{label}[{index}] must be a string")))
        })
        .collect()
}

fn parse_capability_list(value: Option<&Value>, label: &str) -> PyResult<Vec<String>> {
    Ok(parse_string_set(value, label)?.into_iter().collect())
}

fn parse_secret_ref(value: &Value, label: &str) -> PyResult<SecretRef> {
    if let Some(uri) = value.as_str() {
        return Ok(SecretRef::new(uri));
    }
    let object = json_object(value, label)?;
    let mut reference = SecretRef::new(required_string(object, "uri", label)?);
    if let Some(version) = optional_nullable_alias_string(object, "version", "version", label)? {
        reference = reference.with_version(version);
    }
    Ok(reference)
}

fn parse_connection_spec(value: &Value, label: &str) -> PyResult<ConnectionSpec> {
    let object = json_object(value, label)?;
    let mut connection = ConnectionSpec::new(
        required_alias_string(object, "connectionId", "connection_id", label)?,
        required_string(object, "kind", label)?,
        required_string(object, "provider", label)?,
    );
    for (key, value) in parse_json_value_map(
        alias_value(object, "config", "config"),
        &format!("{label}.config"),
    )? {
        connection = connection.with_config(key, value);
    }
    if let Some(credentials) =
        alias_value(object, "credentials", "credentials").filter(|value| !value.is_null())
    {
        connection = connection.with_credentials(parse_secret_ref(
            credentials,
            &format!("{label}.credentials"),
        )?);
    }
    Ok(connection)
}

fn parse_required_capabilities(value: &Value) -> PyResult<Vec<String>> {
    if value.is_array() {
        return parse_capability_list(Some(value), "required capabilities");
    }
    let object = json_object(value, "required capabilities")?;
    let required = object
        .get("requiredCapabilities")
        .or_else(|| object.get("required_capabilities"))
        .or_else(|| object.get("required"));
    parse_capability_list(required, "required capabilities.requiredCapabilities")
}

fn parse_connector_supported_capabilities(
    object: &serde_json::Map<String, Value>,
) -> PyResult<Vec<String>> {
    let supported = object
        .get("supportedCapabilities")
        .or_else(|| object.get("supported_capabilities"))
        .or_else(|| object.get("capabilities"))
        .or_else(|| {
            object
                .get("config")
                .and_then(Value::as_object)
                .and_then(|config| {
                    config
                        .get("supportedCapabilities")
                        .or_else(|| config.get("supported_capabilities"))
                        .or_else(|| config.get("capabilities"))
                })
        });
    parse_capability_list(supported, "connection.supportedCapabilities")
}

fn parse_principal_ref(value: &Value, label: &str) -> PyResult<PrincipalRef> {
    let object = json_object(value, label)?;
    let mut principal = PrincipalRef::new(required_alias_string(
        object,
        "principalId",
        "principal_id",
        label,
    )?);
    if let Some(tenant_id) = optional_nullable_alias_string(object, "tenantId", "tenant_id", label)?
    {
        principal = principal.with_tenant_id(tenant_id);
    }
    for group in parse_string_vec(
        alias_value(object, "groups", "groups"),
        &format!("{label}.groups"),
    )? {
        principal = principal.with_group(group);
    }
    for role in parse_string_vec(
        alias_value(object, "roles", "roles"),
        &format!("{label}.roles"),
    )? {
        principal = principal.with_role(role);
    }
    for (key, value) in parse_json_value_map(
        alias_value(object, "attributes", "attributes"),
        &format!("{label}.attributes"),
    )? {
        principal = principal.with_attribute(key, value);
    }
    Ok(principal)
}

fn parse_tool_effect(value: &Value, label: &str) -> PyResult<ToolEffect> {
    let Some(value) = value.as_str() else {
        return Err(PyValueError::new_err(format!("{label} must be a string")));
    };
    match value {
        "none" => Ok(ToolEffect::None),
        "external_read" => Ok(ToolEffect::ExternalRead),
        "external_write" => Ok(ToolEffect::ExternalWrite),
        "filesystem_read" => Ok(ToolEffect::FilesystemRead),
        "filesystem_write" => Ok(ToolEffect::FilesystemWrite),
        "process" => Ok(ToolEffect::Process),
        "network" => Ok(ToolEffect::Network),
        "destructive" => Ok(ToolEffect::Destructive),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown tool effect {value:?}"
        ))),
    }
}

fn parse_tool_effects(value: Option<&Value>, label: &str) -> PyResult<BTreeSet<ToolEffect>> {
    let Some(value) = value else {
        return Ok(BTreeSet::new());
    };
    let Some(values) = value.as_array() else {
        return Err(PyValueError::new_err(format!("{label} must be an array")));
    };
    values
        .iter()
        .enumerate()
        .map(|(index, value)| parse_tool_effect(value, &format!("{label}[{index}]")))
        .collect()
}

fn parse_tool_status(value: &str, label: &str) -> PyResult<ToolCallStatus> {
    match value {
        "validated" => Ok(ToolCallStatus::Validated),
        "policy_pending" => Ok(ToolCallStatus::PolicyPending),
        "approval_pending" => Ok(ToolCallStatus::ApprovalPending),
        "admitted" => Ok(ToolCallStatus::Admitted),
        "running" => Ok(ToolCallStatus::Running),
        "completed" => Ok(ToolCallStatus::Completed),
        "failed" => Ok(ToolCallStatus::Failed),
        "denied" => Ok(ToolCallStatus::Denied),
        "cancelled" => Ok(ToolCallStatus::Cancelled),
        "policy_stopped" => Ok(ToolCallStatus::PolicyStopped),
        "expired" => Ok(ToolCallStatus::Expired),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown tool call status {value:?}"
        ))),
    }
}

fn parse_tool_call(value: &Value, label: &str) -> PyResult<ToolCall> {
    let object = json_object(value, label)?;
    let arguments = object
        .get("arguments")
        .cloned()
        .ok_or_else(|| PyValueError::new_err(format!("{label}.arguments is required")))?;
    let depends_on = alias_value(object, "dependsOn", "depends_on")
        .map(|value| {
            let Some(values) = value.as_array() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.dependsOn must be an array"
                )));
            };
            values
                .iter()
                .enumerate()
                .map(|(index, value)| {
                    value.as_str().map(str::to_owned).ok_or_else(|| {
                        PyValueError::new_err(format!(
                            "{label}.dependsOn[{index}] must be a string"
                        ))
                    })
                })
                .collect::<PyResult<Vec<_>>>()
        })
        .transpose()?
        .unwrap_or_default();
    let revision = u64_to_u32(
        alias_value(object, "revision", "revision")
            .and_then(Value::as_u64)
            .unwrap_or(1),
        &format!("{label}.revision"),
    )?;
    let call = ToolCall {
        tool_call_id: required_alias_string(object, "toolCallId", "tool_call_id", label)?
            .to_owned(),
        response_id: required_alias_string(object, "responseId", "response_id", label)?.to_owned(),
        resolved_tool_id: required_alias_string(
            object,
            "resolvedToolId",
            "resolved_tool_id",
            label,
        )?
        .to_owned(),
        name: required_string(object, "name", label)?.to_owned(),
        arguments,
        arguments_digest: required_alias_string(
            object,
            "argumentsDigest",
            "arguments_digest",
            label,
        )?
        .to_owned(),
        revision,
        status: parse_tool_status(required_string(object, "status", label)?, label)?,
        depends_on,
        created_at_unix_ms: required_alias_u64(
            object,
            "createdAtUnixMs",
            "created_at_unix_ms",
            label,
        )?,
        admitted_at_unix_ms: optional_nullable_alias_u64(
            object,
            "admittedAtUnixMs",
            "admitted_at_unix_ms",
            label,
        )?,
        completed_at_unix_ms: optional_nullable_alias_u64(
            object,
            "completedAtUnixMs",
            "completed_at_unix_ms",
            label,
        )?,
    };
    call.validate()
        .map_err(|error| PyValueError::new_err(format!("invalid {label}: {error:?}")))?;
    Ok(call)
}

fn parse_tool_definition(value: &Value, label: &str) -> PyResult<ToolDefinition> {
    let object = json_object(value, label)?;
    let mut definition = ToolDefinition::new(
        required_string(object, "name", label)?,
        required_string(object, "description", label)?,
        required_alias_string(object, "inputSchema", "input_schema", label)?,
    );
    if let Some(output_schema) =
        optional_nullable_alias_string(object, "outputSchema", "output_schema", label)?
    {
        definition = definition.with_output_schema(output_schema);
    }
    definition = definition.with_tags(parse_string_set(
        alias_value(object, "tags", "tags"),
        &format!("{label}.tags"),
    )?);
    if let Some(version) = optional_nullable_alias_string(object, "version", "version", label)? {
        definition = definition.with_version(version);
    }
    Ok(definition)
}

fn parse_tool_implementation(value: &Value, label: &str) -> PyResult<ToolImplementation> {
    let object = json_object(value, label)?;
    let kind = required_string(object, "kind", label)?;
    match kind {
        "block" => {
            let mut implementation =
                BlockToolImplementation::new(required_string(object, "block", label)?);
            implementation.input_mapping = parse_string_map(
                alias_value(object, "inputMapping", "input_mapping"),
                &format!("{label}.inputMapping"),
            )?;
            implementation.output_mapping = parse_string_map(
                alias_value(object, "outputMapping", "output_mapping"),
                &format!("{label}.outputMapping"),
            )?;
            Ok(ToolImplementation::Block(implementation))
        }
        "graph" => {
            let mut implementation =
                GraphToolImplementation::new(required_string(object, "graph", label)?);
            implementation.input_mapping = parse_string_map(
                alias_value(object, "inputMapping", "input_mapping"),
                &format!("{label}.inputMapping"),
            )?;
            implementation.output_mapping = parse_string_map(
                alias_value(object, "outputMapping", "output_mapping"),
                &format!("{label}.outputMapping"),
            )?;
            Ok(ToolImplementation::Graph(implementation))
        }
        "remote" => Ok(ToolImplementation::Remote(RemoteToolImplementation::new(
            required_string(object, "connection", label)?,
            required_string(object, "operation", label)?,
        ))),
        "mcp" => Ok(ToolImplementation::Mcp(McpToolImplementation::new(
            required_string(object, "server", label)?,
            required_alias_string(object, "remoteName", "remote_name", label)?,
        ))),
        "openapi" => Ok(ToolImplementation::OpenApi(OpenApiToolImplementation::new(
            required_string(object, "connection", label)?,
            required_alias_string(object, "operationId", "operation_id", label)?,
        ))),
        value => Err(PyValueError::new_err(format!(
            "{label}.kind has unknown tool implementation kind {value:?}"
        ))),
    }
}

fn parse_tool_binding(value: &Value, label: &str) -> PyResult<ToolBinding> {
    let object = json_object(value, label)?;
    let implementation = object
        .get("implementation")
        .ok_or_else(|| PyValueError::new_err(format!("{label}.implementation is required")))?;
    let mut binding = ToolBinding::new(
        required_alias_string(object, "bindingId", "binding_id", label)?,
        required_alias_string(object, "toolName", "tool_name", label)?,
        parse_tool_implementation(implementation, &format!("{label}.implementation"))?,
    )
    .with_effects(parse_tool_effects(
        alias_value(object, "effects", "effects"),
        &format!("{label}.effects"),
    )?);

    if let Some(approval) = optional_nullable_alias_string(object, "approval", "approval", label)? {
        binding = binding.with_approval(match approval {
            "never" => ToolApproval::Never,
            "policy" => ToolApproval::Policy,
            "always" => ToolApproval::Always,
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.approval has unknown approval {value:?}"
                )));
            }
        });
    }
    if let Some(idempotency) =
        optional_nullable_alias_string(object, "idempotency", "idempotency", label)?
    {
        binding = binding.with_idempotency(match idempotency {
            "not_applicable" => ToolIdempotency::NotApplicable,
            "optional" => ToolIdempotency::Optional,
            "required" => ToolIdempotency::Required,
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.idempotency has unknown idempotency {value:?}"
                )));
            }
        });
    }
    if let Some(cancellation) =
        optional_nullable_alias_string(object, "cancellation", "cancellation", label)?
    {
        binding = binding.with_cancellation(parse_tool_cancellation(
            cancellation,
            &format!("{label}.cancellation"),
        )?);
    }
    if let Some(result_mode) =
        optional_nullable_alias_string(object, "resultMode", "result_mode", label)?
    {
        binding = binding.with_result_mode(match result_mode {
            "value" => ToolResultMode::Value,
            "incremental" => ToolResultMode::Incremental,
            "bounded_sequence" => ToolResultMode::BoundedSequence,
            "artifact_reference" => ToolResultMode::ArtifactReference,
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.resultMode has unknown result mode {value:?}"
                )));
            }
        });
    }
    if let Some(timeout_ms) = optional_nullable_alias_u64(object, "timeoutMs", "timeout_ms", label)?
    {
        binding = binding.with_timeout_ms(timeout_ms);
    }
    binding.retry_policy_ref =
        optional_nullable_alias_string(object, "retryPolicyRef", "retry_policy_ref", label)?
            .map(str::to_owned);
    binding.policy_profile_ref =
        optional_nullable_alias_string(object, "policyProfileRef", "policy_profile_ref", label)?
            .map(str::to_owned);
    binding.execution_class =
        optional_nullable_alias_string(object, "executionClass", "execution_class", label)?
            .map(str::to_owned);
    Ok(binding)
}

fn parse_resolved_tool(value: &Value, label: &str) -> PyResult<ResolvedTool> {
    let object = json_object(value, label)?;
    let definition_value = object
        .get("definition")
        .ok_or_else(|| PyValueError::new_err(format!("{label}.definition is required")))?;
    let binding_value = object
        .get("binding")
        .ok_or_else(|| PyValueError::new_err(format!("{label}.binding is required")))?;
    let valid_until_unix_ms =
        optional_nullable_alias_u64(object, "validUntil", "valid_until", label)?;
    let resolved_tool = ResolvedTool::from_definition_and_binding(
        required_alias_string(object, "resolvedToolId", "resolved_tool_id", label)?,
        parse_tool_definition(definition_value, &format!("{label}.definition"))?,
        parse_tool_binding(binding_value, &format!("{label}.binding"))?,
        required_alias_string(
            object,
            "effectivePolicySnapshotId",
            "effective_policy_snapshot_id",
            label,
        )?,
        optional_nullable_alias_bool(
            object,
            "allowedForPrincipal",
            "allowed_for_principal",
            label,
        )?
        .unwrap_or(true),
        valid_until_unix_ms,
    )
    .map_err(|error| PyValueError::new_err(format!("invalid {label}: {error:?}")))?;

    if let Some(definition_digest) =
        optional_nullable_alias_string(object, "definitionDigest", "definition_digest", label)?
        && definition_digest != resolved_tool.definition_digest
    {
        return Err(PyValueError::new_err(format!(
            "{label}.definitionDigest does not match definition"
        )));
    }
    if let Some(binding_digest) =
        optional_nullable_alias_string(object, "bindingDigest", "binding_digest", label)?
        && binding_digest != resolved_tool.binding_digest
    {
        return Err(PyValueError::new_err(format!(
            "{label}.bindingDigest does not match binding"
        )));
    }

    Ok(resolved_tool)
}

fn parse_tool_approval_status(value: &str, label: &str) -> PyResult<ToolApprovalStatus> {
    match value {
        "requested" => Ok(ToolApprovalStatus::Requested),
        "approved" => Ok(ToolApprovalStatus::Approved),
        "denied" => Ok(ToolApprovalStatus::Denied),
        "invalidated" => Ok(ToolApprovalStatus::Invalidated),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown tool approval status {value:?}"
        ))),
    }
}

fn parse_tool_approval_request(value: &Value, label: &str) -> PyResult<ToolApprovalRequest> {
    let object = json_object(value, label)?;
    let revision = u64_to_u32(
        required_alias_u64(object, "revision", "revision", label)?,
        &format!("{label}.revision"),
    )?;
    Ok(ToolApprovalRequest {
        approval_id: required_alias_string(object, "approvalId", "approval_id", label)?.to_owned(),
        tool_call_id: required_alias_string(object, "toolCallId", "tool_call_id", label)?
            .to_owned(),
        tool_name: required_alias_string(object, "toolName", "tool_name", label)?.to_owned(),
        revision,
        definition_digest: required_alias_string(
            object,
            "definitionDigest",
            "definition_digest",
            label,
        )?
        .to_owned(),
        binding_digest: required_alias_string(object, "bindingDigest", "binding_digest", label)?
            .to_owned(),
        arguments_digest: required_alias_string(
            object,
            "argumentsDigest",
            "arguments_digest",
            label,
        )?
        .to_owned(),
        policy_snapshot_id: required_alias_string(
            object,
            "policySnapshotId",
            "policy_snapshot_id",
            label,
        )?
        .to_owned(),
        principal_id: required_alias_string(object, "principalId", "principal_id", label)?
            .to_owned(),
        requested_at_unix_ms: required_alias_u64(
            object,
            "requestedAtUnixMs",
            "requested_at_unix_ms",
            label,
        )
        .or_else(|_| required_alias_u64(object, "requestedAt", "requested_at", label))?,
        expires_at_unix_ms: required_alias_u64(
            object,
            "expiresAtUnixMs",
            "expires_at_unix_ms",
            label,
        )
        .or_else(|_| required_alias_u64(object, "expiresAt", "expires_at", label))?,
    })
}

fn parse_tool_approval_record(value: &Value, label: &str) -> PyResult<ToolApprovalRecord> {
    let object = json_object(value, label)?;
    let request_value = object
        .get("request")
        .ok_or_else(|| PyValueError::new_err(format!("{label}.request is required")))?;
    Ok(ToolApprovalRecord {
        approval_id: required_alias_string(object, "approvalId", "approval_id", label)?.to_owned(),
        request: parse_tool_approval_request(request_value, &format!("{label}.request"))?,
        status: parse_tool_approval_status(required_string(object, "status", label)?, label)?,
        approver_id: optional_nullable_alias_string(object, "approverId", "approver_id", label)?
            .map(str::to_owned),
        decided_at_unix_ms: optional_nullable_alias_u64(
            object,
            "decidedAtUnixMs",
            "decided_at_unix_ms",
            label,
        )?
        .or(optional_nullable_alias_u64(
            object,
            "decidedAt",
            "decided_at",
            label,
        )?),
        invalidated_at_unix_ms: optional_nullable_alias_u64(
            object,
            "invalidatedAtUnixMs",
            "invalidated_at_unix_ms",
            label,
        )?
        .or(optional_nullable_alias_u64(
            object,
            "invalidatedAt",
            "invalidated_at",
            label,
        )?),
        reason: optional_nullable_alias_string(object, "reason", "reason", label)?
            .map(str::to_owned),
    })
}

fn tool_approval_status_name(status: ToolApprovalStatus) -> &'static str {
    match status {
        ToolApprovalStatus::Requested => "requested",
        ToolApprovalStatus::Approved => "approved",
        ToolApprovalStatus::Denied => "denied",
        ToolApprovalStatus::Invalidated => "invalidated",
    }
}

fn tool_approval_error_json(error: &ToolApprovalError) -> Value {
    match error {
        ToolApprovalError::EmptyField { field } => {
            json!({"code": "EmptyField", "field": field})
        }
        ToolApprovalError::MissingField { field } => {
            json!({"code": "MissingField", "field": field})
        }
        ToolApprovalError::ApprovalIdMismatch { expected, actual } => json!({
            "code": "ApprovalIdMismatch",
            "expected": expected,
            "actual": actual,
        }),
        ToolApprovalError::ResolvedToolMismatch { expected, actual } => json!({
            "code": "ResolvedToolMismatch",
            "expected": expected,
            "actual": actual,
        }),
        ToolApprovalError::ToolNameMismatch { expected, actual } => json!({
            "code": "ToolNameMismatch",
            "expected": expected,
            "actual": actual,
        }),
        ToolApprovalError::InvalidExpiration {
            requested_at_unix_ms,
            expires_at_unix_ms,
        } => json!({
            "code": "InvalidExpiration",
            "requestedAtUnixMs": requested_at_unix_ms,
            "expiresAtUnixMs": expires_at_unix_ms,
        }),
        ToolApprovalError::InvalidDecisionTime {
            requested_at_unix_ms,
            decided_at_unix_ms,
            expires_at_unix_ms,
        } => json!({
            "code": "InvalidDecisionTime",
            "requestedAtUnixMs": requested_at_unix_ms,
            "decidedAtUnixMs": decided_at_unix_ms,
            "expiresAtUnixMs": expires_at_unix_ms,
        }),
        ToolApprovalError::InvalidRevision { revision } => {
            json!({"code": "InvalidRevision", "revision": revision})
        }
        ToolApprovalError::InvalidToolCall { source } => json!({
            "code": "InvalidToolCall",
            "message": format!("{source:?}"),
        }),
    }
}

fn parse_json_schema_node(value: &Value, label: &str) -> PyResult<JsonSchemaNode> {
    let object = json_object(value, label)?;
    let expected_type = alias_value(object, "expectedType", "expected_type")
        .or_else(|| object.get("type"))
        .filter(|value| !value.is_null())
        .map(|value| {
            value.as_str().ok_or_else(|| {
                PyValueError::new_err(format!("{label}.expectedType must be a string"))
            })
        })
        .transpose()?;
    let required = parse_string_set(object.get("required"), &format!("{label}.required"))?;
    let mut node = match expected_type {
        None => JsonSchemaNode::any(),
        Some("null") => JsonSchemaNode::any(),
        Some("boolean") => JsonSchemaNode::boolean(),
        Some("integer") => JsonSchemaNode::integer(),
        Some("number") => JsonSchemaNode::number(),
        Some("string") => JsonSchemaNode::string(),
        Some("object") => JsonSchemaNode::object(),
        Some("array") => {
            let items = object
                .get("items")
                .map(|value| parse_json_schema_node(value, &format!("{label}.items")))
                .transpose()?
                .unwrap_or_else(JsonSchemaNode::any);
            JsonSchemaNode::array(items)
        }
        Some(value) => {
            return Err(PyValueError::new_err(format!(
                "{label}.expectedType has unknown JSON schema type {value:?}"
            )));
        }
    };
    if let Some(properties) = object.get("properties") {
        let properties = json_object(properties, &format!("{label}.properties"))?;
        for (property, schema_value) in properties {
            let property_schema =
                parse_json_schema_node(schema_value, &format!("{label}.properties.{property}"))?;
            if required.contains(property) {
                node = node.required_property(property.clone(), property_schema);
            } else {
                node = node.property(property.clone(), property_schema);
            }
        }
    }
    Ok(node)
}

fn parse_tool_schema_registry(value: &Value, label: &str) -> PyResult<ToolSchemaRegistry> {
    if value.is_null() {
        return ToolSchemaRegistry::new(Vec::<JsonSchema>::new())
            .map_err(|error| PyValueError::new_err(format!("invalid {label}: {error:?}")));
    }

    let mut schemas = Vec::new();
    let mut parse_schema = |schema_id_override: Option<&str>,
                            schema_value: &Value,
                            schema_label: &str|
     -> PyResult<()> {
        let schema_object = json_object(schema_value, schema_label)?;
        let schema_id = if let Some(schema_id) = schema_id_override {
            schema_id.to_owned()
        } else {
            optional_nullable_alias_string(schema_object, "schemaId", "schema_id", schema_label)?
                .ok_or_else(|| {
                    PyValueError::new_err(format!("{schema_label}.schemaId is required"))
                })?
                .to_owned()
        };
        let root_value = schema_object
            .get("root")
            .or_else(|| schema_object.get("schema"))
            .unwrap_or(schema_value);
        schemas.push(JsonSchema::new(
            schema_id,
            parse_json_schema_node(root_value, &format!("{schema_label}.root"))?,
        ));
        Ok(())
    };

    if let Some(array) = value.as_array() {
        for (index, schema_value) in array.iter().enumerate() {
            parse_schema(None, schema_value, &format!("{label}[{index}]"))?;
        }
    } else {
        let object = json_object(value, label)?;
        if object.contains_key("schemaId") || object.contains_key("schema_id") {
            parse_schema(None, value, label)?;
        } else if let Some(schema_values) = object.get("schemas") {
            let Some(schema_values) = schema_values.as_array() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.schemas must be an array"
                )));
            };
            for (index, schema_value) in schema_values.iter().enumerate() {
                parse_schema(None, schema_value, &format!("{label}.schemas[{index}]"))?;
            }
        } else {
            for (schema_id, schema_value) in object {
                parse_schema(
                    Some(schema_id),
                    schema_value,
                    &format!("{label}.{schema_id}"),
                )?;
            }
        }
    }

    ToolSchemaRegistry::new(schemas)
        .map_err(|error| PyValueError::new_err(format!("invalid {label}: {error:?}")))
}

fn parse_content_part(value: &Value, label: &str) -> PyResult<ContentPart> {
    let object = json_object(value, label)?;
    let kind = required_string(object, "kind", label)?;
    let metadata = parse_json_value_map(object.get("metadata"), &format!("{label}.metadata"))?;
    let mut part = match kind {
        "text" => ContentPart {
            kind: ContentPartKind::Text,
            text: object
                .get("text")
                .filter(|value| !value.is_null())
                .map(|value| {
                    value.as_str().map(str::to_owned).ok_or_else(|| {
                        PyValueError::new_err(format!("{label}.text must be a string"))
                    })
                })
                .transpose()?,
            data: object.get("data").filter(|value| !value.is_null()).cloned(),
            metadata,
        },
        "json" => ContentPart {
            kind: ContentPartKind::Json,
            text: object
                .get("text")
                .filter(|value| !value.is_null())
                .map(|value| {
                    value.as_str().map(str::to_owned).ok_or_else(|| {
                        PyValueError::new_err(format!("{label}.text must be a string"))
                    })
                })
                .transpose()?,
            data: object.get("data").filter(|value| !value.is_null()).cloned(),
            metadata,
        },
        "artifact_ref" => ContentPart {
            kind: ContentPartKind::ArtifactRef,
            text: object
                .get("text")
                .filter(|value| !value.is_null())
                .map(|value| {
                    value.as_str().map(str::to_owned).ok_or_else(|| {
                        PyValueError::new_err(format!("{label}.text must be a string"))
                    })
                })
                .transpose()?,
            data: object.get("data").filter(|value| !value.is_null()).cloned(),
            metadata,
        },
        value => {
            return Err(PyValueError::new_err(format!(
                "{label}.kind has unknown content part kind {value:?}"
            )));
        }
    };
    if kind == "artifact_ref" && part.data.is_none() {
        let artifact = parse_artifact_ref(value, label)?;
        part = ContentPart::artifact_ref(artifact);
    }
    part.validate()
        .map_err(|error| PyValueError::new_err(format!("invalid {label}: {error:?}")))?;
    Ok(part)
}

fn parse_artifact_ref(value: &Value, label: &str) -> PyResult<ArtifactRef> {
    let object = json_object(value, label)?;
    let mut artifact = ArtifactRef::new(
        required_alias_string(object, "artifactId", "artifact_id", label)?,
        required_string(object, "uri", label)?,
    );
    if let Some(checksum) = optional_nullable_alias_string(object, "checksum", "checksum", label)? {
        artifact = artifact.with_checksum(checksum);
    }
    if let Some(media_type) =
        optional_nullable_alias_string(object, "mediaType", "media_type", label)?
    {
        artifact = artifact.with_media_type(media_type);
    }
    Ok(artifact)
}

fn parse_diagnostic_severity(value: &str, label: &str) -> PyResult<DiagnosticSeverity> {
    match value {
        "info" => Ok(DiagnosticSeverity::Info),
        "warning" => Ok(DiagnosticSeverity::Warning),
        "error" => Ok(DiagnosticSeverity::Error),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown diagnostic severity {value:?}"
        ))),
    }
}

fn parse_diagnostic(value: &Value, label: &str) -> PyResult<Diagnostic> {
    let object = json_object(value, label)?;
    Ok(Diagnostic {
        code: required_string(object, "code", label)?.to_owned(),
        message: required_string(object, "message", label)?.to_owned(),
        severity: parse_diagnostic_severity(required_string(object, "severity", label)?, label)?,
        path: optional_nullable_alias_string(object, "path", "path", label)?.map(str::to_owned),
    })
}

fn parse_error_category(value: &str, label: &str) -> PyResult<ErrorCategory> {
    match value {
        "validation" => Ok(ErrorCategory::Validation),
        "configuration" => Ok(ErrorCategory::Configuration),
        "authentication" => Ok(ErrorCategory::Authentication),
        "authorization" => Ok(ErrorCategory::Authorization),
        "not_found" => Ok(ErrorCategory::NotFound),
        "rate_limit" => Ok(ErrorCategory::RateLimit),
        "quota" => Ok(ErrorCategory::Quota),
        "budget" => Ok(ErrorCategory::Budget),
        "capacity" => Ok(ErrorCategory::Capacity),
        "timeout" => Ok(ErrorCategory::Timeout),
        "transient" => Ok(ErrorCategory::Transient),
        "permanent" => Ok(ErrorCategory::Permanent),
        "provider" => Ok(ErrorCategory::Provider),
        "policy" => Ok(ErrorCategory::Policy),
        "cancelled" => Ok(ErrorCategory::Cancelled),
        "conflict" => Ok(ErrorCategory::Conflict),
        "internal" => Ok(ErrorCategory::Internal),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown error category {value:?}"
        ))),
    }
}

fn parse_block_error(value: &Value, label: &str) -> PyResult<BlockError> {
    let object = json_object(value, label)?;
    let mut error = BlockError::new(
        required_string(object, "code", label)?,
        parse_error_category(required_string(object, "category", label)?, label)?,
        required_string(object, "message", label)?,
        optional_nullable_alias_bool(object, "retryable", "retryable", label)?.unwrap_or(false),
    );
    error.details = parse_json_value_map(object.get("details"), &format!("{label}.details"))?;
    if let Some(cause_chain) = object
        .get("causeChain")
        .or_else(|| object.get("cause_chain"))
    {
        let Some(cause_chain) = cause_chain.as_array() else {
            return Err(PyValueError::new_err(format!(
                "{label}.causeChain must be an array"
            )));
        };
        error.cause_chain = cause_chain
            .iter()
            .enumerate()
            .map(|(index, value)| {
                value.as_str().map(str::to_owned).ok_or_else(|| {
                    PyValueError::new_err(format!("{label}.causeChain[{index}] must be a string"))
                })
            })
            .collect::<PyResult<Vec<_>>>()?;
    }
    Ok(error)
}

fn parse_cancel_code(value: &str, label: &str) -> PyResult<CancelCode> {
    match value {
        "client_disconnect" | "clientDisconnect" => Ok(CancelCode::ClientDisconnect),
        "user_cancel" | "userCancel" => Ok(CancelCode::UserCancel),
        "timeout" => Ok(CancelCode::Timeout),
        "superseded" => Ok(CancelCode::Superseded),
        "policy_denied" | "policyDenied" => Ok(CancelCode::PolicyDenied),
        "budget_exhausted" | "budgetExhausted" => Ok(CancelCode::BudgetExhausted),
        "provider_quota_exhausted" | "providerQuotaExhausted" => {
            Ok(CancelCode::ProviderQuotaExhausted)
        }
        "dependency_failed" | "dependencyFailed" => Ok(CancelCode::DependencyFailed),
        "shutdown" => Ok(CancelCode::Shutdown),
        "barge_in" | "bargeIn" => Ok(CancelCode::BargeIn),
        "rollout_drain" | "rolloutDrain" => Ok(CancelCode::RolloutDrain),
        "lease_lost" | "leaseLost" => Ok(CancelCode::LeaseLost),
        "entitlement_revoked" | "entitlementRevoked" => Ok(CancelCode::EntitlementRevoked),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown cancellation code {value:?}"
        ))),
    }
}

fn parse_cancel_reason(value: &Value, label: &str) -> PyResult<CancelReason> {
    let object = json_object(value, label)?;
    let mut reason = CancelReason::new(parse_cancel_code(
        required_alias_string(object, "code", "code", label)?,
        &format!("{label}.code"),
    )?);
    reason.message =
        optional_nullable_alias_string(object, "message", "message", label)?.map(ToOwned::to_owned);
    reason.requested_by =
        optional_nullable_alias_string(object, "requestedBy", "requested_by", label)?
            .map(ToOwned::to_owned);
    reason.policy_decision_ref =
        optional_nullable_alias_string(object, "policyDecisionRef", "policy_decision_ref", label)?
            .map(ToOwned::to_owned);
    Ok(reason)
}

fn parse_retry_backoff(value: Option<&Value>, label: &str) -> PyResult<Backoff> {
    let Some(value) = value.filter(|value| !value.is_null()) else {
        return Ok(Backoff::None);
    };
    if let Some(value) = value.as_str() {
        return match value {
            "none" => Ok(Backoff::None),
            value => Err(PyValueError::new_err(format!(
                "{label} has unknown backoff {value:?}"
            ))),
        };
    }
    let object = json_object(value, label)?;
    let kind = optional_nullable_alias_string(object, "kind", "mode", label)?.unwrap_or("fixed");
    match kind {
        "none" => Ok(Backoff::None),
        "fixed" => Ok(Backoff::Fixed {
            delay_ms: optional_nullable_alias_u64(object, "delayMs", "delay_ms", label)?
                .or(optional_nullable_alias_u64(
                    object,
                    "fixedDelayMs",
                    "fixed_delay_ms",
                    label,
                )?)
                .ok_or_else(|| {
                    PyValueError::new_err(format!("{label}.delayMs must be an unsigned integer"))
                })?,
        }),
        value => Err(PyValueError::new_err(format!(
            "{label}.kind has unknown backoff {value:?}"
        ))),
    }
}

fn parse_partial_output_policy(
    value: Option<&Value>,
    label: &str,
) -> PyResult<PartialOutputPolicy> {
    let Some(value) = value.filter(|value| !value.is_null()) else {
        return Ok(PartialOutputPolicy::Fail);
    };
    let Some(value) = value.as_str() else {
        return Err(PyValueError::new_err(format!("{label} must be a string")));
    };
    match value {
        "fail" => Ok(PartialOutputPolicy::Fail),
        "resume_with_cursor" => Ok(PartialOutputPolicy::ResumeWithCursor),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown partial output policy {value:?}"
        ))),
    }
}

fn parse_retry_policy(value: &Value, label: &str) -> PyResult<RetryPolicy> {
    let object = json_object(value, label)?;
    if optional_nullable_alias_string(object, "preset", "preset", label)?
        == Some("default_model_read")
    {
        return Ok(RetryPolicy::default_model_read());
    }
    let max_attempts = u64_to_u32(
        required_alias_u64(object, "maxAttempts", "max_attempts", label)?,
        &format!("{label}.maxAttempts"),
    )?;
    let retry_on = object
        .get("retryOn")
        .or_else(|| object.get("retry_on"))
        .map(|value| {
            let Some(values) = value.as_array() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.retryOn must be an array"
                )));
            };
            values
                .iter()
                .enumerate()
                .map(|(index, value)| {
                    value
                        .as_str()
                        .ok_or_else(|| {
                            PyValueError::new_err(format!(
                                "{label}.retryOn[{index}] must be a string"
                            ))
                        })
                        .and_then(|value| {
                            parse_error_category(value, &format!("{label}.retryOn[{index}]"))
                        })
                })
                .collect::<PyResult<Vec<_>>>()
        })
        .transpose()?
        .unwrap_or_default();
    let mut policy = RetryPolicy::new(max_attempts)
        .retry_on(retry_on)
        .with_backoff(parse_retry_backoff(
            object.get("backoff"),
            &format!("{label}.backoff"),
        )?);
    policy = policy.with_partial_output_policy(parse_partial_output_policy(
        object
            .get("partialOutputPolicy")
            .or_else(|| object.get("partial_output_policy")),
        &format!("{label}.partialOutputPolicy"),
    )?);
    Ok(policy)
}

fn parse_effect_kind(value: &str, label: &str) -> PyResult<EffectKind> {
    match value {
        "external_write" => Ok(EffectKind::ExternalWrite),
        "filesystem_write" => Ok(EffectKind::FilesystemWrite),
        "destructive" => Ok(EffectKind::Destructive),
        "process" => Ok(EffectKind::Process),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown effect kind {value:?}"
        ))),
    }
}

fn parse_retry_request(value: &Value, label: &str) -> PyResult<RetryRequest> {
    let object = json_object(value, label)?;
    let attempt = u64_to_u32(
        required_alias_u64(object, "attempt", "attempt", label)?,
        &format!("{label}.attempt"),
    )?;
    let error_value = object
        .get("error")
        .ok_or_else(|| PyValueError::new_err(format!("{label}.error is required")))?;
    let mut request = RetryRequest::new(
        attempt,
        parse_block_error(error_value, &format!("{label}.error"))?,
    );
    if optional_nullable_alias_bool(object, "hasPartialOutput", "has_partial_output", label)?
        .unwrap_or(false)
    {
        request = request.with_partial_output();
    }
    if let Some(cursor) =
        optional_nullable_alias_string(object, "resumeCursor", "resume_cursor", label)?
    {
        request = request.with_resume_cursor(cursor);
    }
    if let Some(effect) = optional_nullable_alias_string(object, "effect", "effect", label)? {
        request = request.with_effect(parse_effect_kind(effect, &format!("{label}.effect"))?);
    }
    if let Some(idempotency_key) =
        optional_nullable_alias_string(object, "idempotencyKey", "idempotency_key", label)?
    {
        request = request.with_idempotency_key(idempotency_key);
    }
    if let Some(retry_after_ms) =
        optional_nullable_alias_u64(object, "retryAfterMs", "retry_after_ms", label)?
    {
        request = request.with_retry_after_ms(retry_after_ms);
    }
    Ok(request)
}

fn parse_provider_limit_kind(value: &str, label: &str) -> PyResult<ProviderLimitKind> {
    match value {
        "graphblocks_quota_exceeded" => Ok(ProviderLimitKind::GraphBlocksQuotaExceeded),
        "provider_quota_exceeded" => Ok(ProviderLimitKind::ProviderQuotaExceeded),
        "capacity_unavailable" => Ok(ProviderLimitKind::CapacityUnavailable),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown provider limit kind {value:?}"
        ))),
    }
}

fn parse_provider_limit_policy(value: &Value, label: &str) -> PyResult<ProviderLimitPolicy> {
    let object = json_object(value, label)?;
    Ok(ProviderLimitPolicy::new()
        .with_fallback_enabled(
            optional_nullable_alias_bool(object, "fallbackEnabled", "fallback_enabled", label)?
                .unwrap_or(false),
        )
        .with_queue_enabled(
            optional_nullable_alias_bool(object, "queueEnabled", "queue_enabled", label)?
                .unwrap_or(false),
        )
        .with_credential_or_topup_enabled(
            optional_nullable_alias_bool(
                object,
                "credentialOrTopupEnabled",
                "credential_or_topup_enabled",
                label,
            )?
            .unwrap_or(false),
        ))
}

fn parse_provider_limit_incident(value: &Value, label: &str) -> PyResult<ProviderLimitIncident> {
    let object = json_object(value, label)?;
    let mut incident = ProviderLimitIncident::new(parse_provider_limit_kind(
        required_string(object, "kind", label)?,
        &format!("{label}.kind"),
    )?);
    if let Some(retry_after_ms) =
        optional_nullable_alias_u64(object, "retryAfterMs", "retry_after_ms", label)?
    {
        incident = incident.with_retry_after_ms(retry_after_ms);
    }
    for fallback in parse_string_vec(
        object
            .get("compatibleFallbacks")
            .or_else(|| object.get("compatible_fallbacks")),
        &format!("{label}.compatibleFallbacks"),
    )? {
        incident = incident.with_fallback(fallback);
    }
    if optional_nullable_alias_bool(
        object,
        "credentialOrTopupAvailable",
        "credential_or_topup_available",
        label,
    )?
    .unwrap_or(false)
    {
        incident = incident.with_credential_or_topup_available();
    }
    Ok(incident)
}

fn provider_limit_decision_json(decision: ProviderLimitDecision) -> Value {
    match decision {
        ProviderLimitDecision::RetryAfter { delay_ms } => json!({
            "ok": true,
            "decision": "retry_after",
            "delayMs": delay_ms,
            "target": Value::Null,
            "requiresPolicyRecheck": false,
            "reason": Value::Null,
        }),
        ProviderLimitDecision::Fallback {
            target,
            requires_policy_recheck,
        } => json!({
            "ok": true,
            "decision": "fallback",
            "delayMs": Value::Null,
            "target": target,
            "requiresPolicyRecheck": requires_policy_recheck,
            "reason": Value::Null,
        }),
        ProviderLimitDecision::Pause { reason } => json!({
            "ok": true,
            "decision": "pause",
            "delayMs": Value::Null,
            "target": Value::Null,
            "requiresPolicyRecheck": false,
            "reason": reason,
        }),
        ProviderLimitDecision::RequestCredentialOrTopup => json!({
            "ok": true,
            "decision": "request_credential_or_topup",
            "delayMs": Value::Null,
            "target": Value::Null,
            "requiresPolicyRecheck": false,
            "reason": Value::Null,
        }),
        ProviderLimitDecision::Fail { reason } => json!({
            "ok": true,
            "decision": "fail",
            "delayMs": Value::Null,
            "target": Value::Null,
            "requiresPolicyRecheck": false,
            "reason": reason,
        }),
    }
}

fn parse_tool_result_status(value: &str, label: &str) -> PyResult<ToolResultStatus> {
    match value {
        "completed" => Ok(ToolResultStatus::Completed),
        "failed" => Ok(ToolResultStatus::Failed),
        "denied" => Ok(ToolResultStatus::Denied),
        "cancelled" => Ok(ToolResultStatus::Cancelled),
        "policy_stopped" => Ok(ToolResultStatus::PolicyStopped),
        "incomplete" => Ok(ToolResultStatus::Incomplete),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown tool result status {value:?}"
        ))),
    }
}

fn parse_tool_effect_outcome(value: Option<&Value>, label: &str) -> PyResult<ToolEffectOutcome> {
    let Some(value) = value.filter(|value| !value.is_null()) else {
        return Ok(ToolEffectOutcome::Unknown);
    };
    let Some(value) = value.as_str() else {
        return Err(PyValueError::new_err(format!("{label} must be a string")));
    };
    match value {
        "no_external_effect" => Ok(ToolEffectOutcome::NoExternalEffect),
        "committed" => Ok(ToolEffectOutcome::Committed),
        "not_committed" => Ok(ToolEffectOutcome::NotCommitted),
        "unknown" => Ok(ToolEffectOutcome::Unknown),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown tool effect outcome {value:?}"
        ))),
    }
}

fn parse_tool_result(value: &Value, label: &str) -> PyResult<ToolResult> {
    let object = json_object(value, label)?;
    let output = alias_value(object, "output", "output")
        .map(|value| {
            let Some(values) = value.as_array() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.output must be an array"
                )));
            };
            values
                .iter()
                .enumerate()
                .map(|(index, value)| {
                    parse_content_part(value, &format!("{label}.output[{index}]"))
                })
                .collect::<PyResult<Vec<_>>>()
        })
        .transpose()?
        .unwrap_or_default();
    let artifacts = object
        .get("artifacts")
        .map(|value| {
            let Some(values) = value.as_array() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.artifacts must be an array"
                )));
            };
            values
                .iter()
                .enumerate()
                .map(|(index, value)| {
                    parse_artifact_ref(value, &format!("{label}.artifacts[{index}]"))
                })
                .collect::<PyResult<Vec<_>>>()
        })
        .transpose()?
        .unwrap_or_default();
    let diagnostics = object
        .get("diagnostics")
        .map(|value| {
            let Some(values) = value.as_array() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.diagnostics must be an array"
                )));
            };
            values
                .iter()
                .enumerate()
                .map(|(index, value)| {
                    parse_diagnostic(value, &format!("{label}.diagnostics[{index}]"))
                })
                .collect::<PyResult<Vec<_>>>()
        })
        .transpose()?
        .unwrap_or_default();
    let result = ToolResult {
        tool_call_id: required_alias_string(object, "toolCallId", "tool_call_id", label)?
            .to_owned(),
        status: parse_tool_result_status(required_string(object, "status", label)?, label)?,
        output,
        output_digest: optional_nullable_alias_string(
            object,
            "outputDigest",
            "output_digest",
            label,
        )?
        .map(str::to_owned),
        artifacts,
        diagnostics,
        error: object
            .get("error")
            .filter(|value| !value.is_null())
            .map(|value| parse_block_error(value, &format!("{label}.error")))
            .transpose()?,
        started_at_unix_ms: optional_nullable_alias_u64(
            object,
            "startedAtUnixMs",
            "started_at_unix_ms",
            label,
        )?,
        completed_at_unix_ms: optional_nullable_alias_u64(
            object,
            "completedAtUnixMs",
            "completed_at_unix_ms",
            label,
        )?,
        effect_outcome: parse_tool_effect_outcome(
            alias_value(object, "effectOutcome", "effect_outcome"),
            &format!("{label}.effectOutcome"),
        )?,
    };
    result
        .validate()
        .map_err(|error| PyValueError::new_err(format!("invalid {label}: {error:?}")))?;
    Ok(result)
}

fn parse_tool_result_event(value: &Value, label: &str) -> PyResult<ToolResultEvent> {
    let object = json_object(value, label)?;
    let kind = required_string(object, "kind", label)?;
    let tool_call_id = required_alias_string(object, "toolCallId", "tool_call_id", label)?;
    let sequence = required_u64(object, "sequence", label)?;
    match kind {
        "started" => Ok(ToolResultEvent::started(
            tool_call_id,
            sequence,
            required_alias_u64(object, "startedAtUnixMs", "started_at_unix_ms", label)?,
        )),
        "delta" => {
            let output = object
                .get("output")
                .ok_or_else(|| PyValueError::new_err(format!("{label}.output is required")))?;
            let Some(output) = output.as_array() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.output must be an array"
                )));
            };
            let output = output
                .iter()
                .enumerate()
                .map(|(index, value)| {
                    parse_content_part(value, &format!("{label}.output[{index}]"))
                })
                .collect::<PyResult<Vec<_>>>()?;
            Ok(ToolResultEvent::delta(tool_call_id, sequence, output))
        }
        "artifact_ready" => Ok(ToolResultEvent::artifact_ready(
            tool_call_id,
            sequence,
            parse_artifact_ref(
                object.get("artifact").ok_or_else(|| {
                    PyValueError::new_err(format!("{label}.artifact is required"))
                })?,
                &format!("{label}.artifact"),
            )?,
        )),
        "completed" => parse_final_tool_result_event(
            tool_call_id,
            sequence,
            object,
            label,
            |tool_call_id, sequence, result| {
                ToolResultEvent::completed(tool_call_id, sequence, result)
            },
        ),
        "failed" => parse_final_tool_result_event(
            tool_call_id,
            sequence,
            object,
            label,
            |tool_call_id, sequence, result| {
                ToolResultEvent::failed(tool_call_id, sequence, result)
            },
        ),
        "denied" => parse_final_tool_result_event(
            tool_call_id,
            sequence,
            object,
            label,
            |tool_call_id, sequence, result| {
                ToolResultEvent::denied(tool_call_id, sequence, result)
            },
        ),
        "cancelled" => parse_final_tool_result_event(
            tool_call_id,
            sequence,
            object,
            label,
            |tool_call_id, sequence, result| {
                ToolResultEvent::cancelled(tool_call_id, sequence, result)
            },
        ),
        "policy_stopped" => parse_final_tool_result_event(
            tool_call_id,
            sequence,
            object,
            label,
            |tool_call_id, sequence, result| {
                ToolResultEvent::policy_stopped(tool_call_id, sequence, result)
            },
        ),
        "incomplete" => parse_final_tool_result_event(
            tool_call_id,
            sequence,
            object,
            label,
            |tool_call_id, sequence, result| {
                ToolResultEvent::incomplete(tool_call_id, sequence, result)
            },
        ),
        value => Err(PyValueError::new_err(format!(
            "{label}.kind has unknown tool result event kind {value:?}"
        ))),
    }
}

fn parse_final_tool_result_event(
    tool_call_id: &str,
    sequence: u64,
    object: &serde_json::Map<String, Value>,
    label: &str,
    build: impl FnOnce(String, u64, ToolResult) -> ToolResultEvent,
) -> PyResult<ToolResultEvent> {
    let result = parse_tool_result(
        object
            .get("result")
            .ok_or_else(|| PyValueError::new_err(format!("{label}.result is required")))?,
        &format!("{label}.result"),
    )?;
    Ok(build(tool_call_id.to_owned(), sequence, result))
}

fn parse_capture_decision(value: &Value, label: &str) -> PyResult<CaptureDecision> {
    let object = json_object(value, label)?;
    let retention_policy =
        optional_nullable_alias_string(object, "retentionPolicy", "retention_policy", label)?
            .unwrap_or("");
    let mut decision = match optional_nullable_alias_string(object, "mode", "mode", label)?
        .unwrap_or("hash_only")
    {
        "none" => CaptureDecision::none(retention_policy),
        "hash_only" => CaptureDecision::hash_only(retention_policy),
        "reference_only" => CaptureDecision::reference_only(retention_policy),
        "redacted_preview" => CaptureDecision::redacted_preview(retention_policy),
        "full" => CaptureDecision::full(retention_policy),
        value => {
            return Err(PyValueError::new_err(format!(
                "{label}.mode has unknown capture mode {value:?}"
            )));
        }
    };
    if let Some(consent_ref) =
        optional_nullable_alias_string(object, "consentRef", "consent_ref", label)?
    {
        decision = decision.with_consent_ref(consent_ref);
    }
    Ok(decision)
}

fn parse_tool_result_content_policy(
    value: Option<&Value>,
    label: &str,
) -> PyResult<ToolResultContentPolicy> {
    let Some(value) = value.filter(|value| !value.is_null()) else {
        return Ok(ToolResultContentPolicy::new());
    };
    let object = json_object(value, label)?;
    let mut policy = ToolResultContentPolicy::new();
    if let Some(max_output_bytes) =
        optional_nullable_alias_u64(object, "maxOutputBytes", "max_output_bytes", label)?
    {
        policy = policy.with_max_output_bytes(u64_to_usize(
            max_output_bytes,
            &format!("{label}.maxOutputBytes"),
        )?);
    }
    if let Some(redactions) = object.get("redactions") {
        let Some(redactions) = redactions.as_array() else {
            return Err(PyValueError::new_err(format!(
                "{label}.redactions must be an array"
            )));
        };
        let mut parsed_redactions = Vec::new();
        for (index, redaction) in redactions.iter().enumerate() {
            let redaction_label = format!("{label}.redactions[{index}]");
            let redaction = json_object(redaction, &redaction_label)?;
            parsed_redactions.push(RedactionInstruction::text_range(
                required_string(redaction, "path", &redaction_label)?,
                required_u64(redaction, "start", &redaction_label)?,
                required_u64(redaction, "end", &redaction_label)?,
                required_string(redaction, "replacement", &redaction_label)?,
            ));
        }
        policy = policy.with_redactions(parsed_redactions);
    }
    if let Some(capture_policy) = object
        .get("capturePolicy")
        .or_else(|| object.get("capture_policy"))
        .or_else(|| object.get("captureDecision"))
        .or_else(|| object.get("capture_decision"))
        .filter(|value| !value.is_null())
    {
        policy =
            policy.with_capture_decision(parse_capture_decision(capture_policy, "capturePolicy")?);
    }
    let trust_designation =
        optional_nullable_alias_string(object, "trustDesignation", "trust_designation", label)?
            .unwrap_or("untrusted_external");
    let prompt_injection_label = optional_nullable_alias_string(
        object,
        "promptInjectionLabel",
        "prompt_injection_label",
        label,
    )?
    .unwrap_or("untrusted_tool_output");
    let content_classification = optional_nullable_alias_string(
        object,
        "contentClassification",
        "content_classification",
        label,
    )?
    .unwrap_or("external_tool_output");
    policy = policy.with_model_output_labels(
        trust_designation,
        prompt_injection_label,
        content_classification,
    );
    Ok(policy)
}

fn serialize_content_part(part: &ContentPart) -> Value {
    let kind = match part.kind {
        ContentPartKind::Text => "text",
        ContentPartKind::Json => "json",
        ContentPartKind::ArtifactRef => "artifact_ref",
    };
    json!({
        "kind": kind,
        "text": part.text,
        "data": part.data,
        "metadata": part.metadata,
    })
}

fn serialize_error_category(category: ErrorCategory) -> &'static str {
    match category {
        ErrorCategory::Validation => "validation",
        ErrorCategory::Configuration => "configuration",
        ErrorCategory::Authentication => "authentication",
        ErrorCategory::Authorization => "authorization",
        ErrorCategory::NotFound => "not_found",
        ErrorCategory::RateLimit => "rate_limit",
        ErrorCategory::Quota => "quota",
        ErrorCategory::Budget => "budget",
        ErrorCategory::Capacity => "capacity",
        ErrorCategory::Timeout => "timeout",
        ErrorCategory::Transient => "transient",
        ErrorCategory::Permanent => "permanent",
        ErrorCategory::Provider => "provider",
        ErrorCategory::Policy => "policy",
        ErrorCategory::Cancelled => "cancelled",
        ErrorCategory::Conflict => "conflict",
        ErrorCategory::Internal => "internal",
    }
}

fn serialize_cancel_code(code: CancelCode) -> &'static str {
    match code {
        CancelCode::ClientDisconnect => "client_disconnect",
        CancelCode::UserCancel => "user_cancel",
        CancelCode::Timeout => "timeout",
        CancelCode::Superseded => "superseded",
        CancelCode::PolicyDenied => "policy_denied",
        CancelCode::BudgetExhausted => "budget_exhausted",
        CancelCode::ProviderQuotaExhausted => "provider_quota_exhausted",
        CancelCode::DependencyFailed => "dependency_failed",
        CancelCode::Shutdown => "shutdown",
        CancelCode::BargeIn => "barge_in",
        CancelCode::RolloutDrain => "rollout_drain",
        CancelCode::LeaseLost => "lease_lost",
        CancelCode::EntitlementRevoked => "entitlement_revoked",
    }
}

fn serialize_cancel_reason(reason: &CancelReason) -> Value {
    json!({
        "code": serialize_cancel_code(reason.code),
        "message": reason.message.as_deref(),
        "requestedBy": reason.requested_by.as_deref(),
        "policyDecisionRef": reason.policy_decision_ref.as_deref(),
    })
}

fn serialize_tool_result_status(status: ToolResultStatus) -> &'static str {
    match status {
        ToolResultStatus::Completed => "completed",
        ToolResultStatus::Failed => "failed",
        ToolResultStatus::Denied => "denied",
        ToolResultStatus::Cancelled => "cancelled",
        ToolResultStatus::PolicyStopped => "policy_stopped",
        ToolResultStatus::Incomplete => "incomplete",
    }
}

fn serialize_tool_effect_outcome(outcome: ToolEffectOutcome) -> &'static str {
    match outcome {
        ToolEffectOutcome::NoExternalEffect => "no_external_effect",
        ToolEffectOutcome::Committed => "committed",
        ToolEffectOutcome::NotCommitted => "not_committed",
        ToolEffectOutcome::Unknown => "unknown",
    }
}

fn serialize_artifact_ref(artifact: &ArtifactRef) -> Value {
    json!({
        "artifactId": artifact.artifact_id.as_str(),
        "uri": artifact.uri.as_str(),
        "checksum": artifact.checksum.as_deref(),
        "mediaType": artifact.media_type.as_deref(),
    })
}

fn serialize_diagnostic_severity(severity: DiagnosticSeverity) -> &'static str {
    match severity {
        DiagnosticSeverity::Info => "info",
        DiagnosticSeverity::Warning => "warning",
        DiagnosticSeverity::Error => "error",
    }
}

fn serialize_diagnostic(diagnostic: &Diagnostic) -> Value {
    json!({
        "code": diagnostic.code.as_str(),
        "message": diagnostic.message.as_str(),
        "severity": serialize_diagnostic_severity(diagnostic.severity),
        "path": diagnostic.path.as_deref(),
    })
}

fn serialize_block_error(error: &BlockError) -> Value {
    json!({
        "code": error.code.as_str(),
        "category": serialize_error_category(error.category),
        "message": error.message.as_str(),
        "retryable": error.retryable,
        "details": &error.details,
        "causeChain": &error.cause_chain,
    })
}

fn serialize_tool_result(result: &ToolResult) -> Value {
    json!({
        "toolCallId": result.tool_call_id.as_str(),
        "status": serialize_tool_result_status(result.status),
        "output": result.output.iter().map(serialize_content_part).collect::<Vec<_>>(),
        "outputDigest": result.output_digest.as_deref(),
        "artifacts": result.artifacts.iter().map(serialize_artifact_ref).collect::<Vec<_>>(),
        "diagnostics": result.diagnostics.iter().map(serialize_diagnostic).collect::<Vec<_>>(),
        "error": result.error.as_ref().map(serialize_block_error),
        "startedAtUnixMs": result.started_at_unix_ms,
        "completedAtUnixMs": result.completed_at_unix_ms,
        "effectOutcome": serialize_tool_effect_outcome(result.effect_outcome),
    })
}

fn serialize_principal_ref(principal: &PrincipalRef) -> Value {
    json!({
        "principalId": principal.principal_id.as_str(),
        "tenantId": principal.tenant_id.as_deref(),
        "groups": &principal.groups,
        "roles": &principal.roles,
        "attributes": &principal.attributes,
    })
}

fn serialize_resource_ref(resource: &ResourceRef) -> Value {
    json!({
        "resourceId": resource.resource_id.as_str(),
        "resourceKind": resource.resource_kind.as_deref(),
        "tenantId": resource.tenant_id.as_deref(),
        "attributes": &resource.attributes,
    })
}

fn serialize_audit_event(event: &AuditEvent) -> Value {
    json!({
        "eventId": event.event_id.as_str(),
        "targetKind": event.target_kind.as_str(),
        "occurredAt": event.occurred_at.as_str(),
        "actor": event.actor.as_ref().map(serialize_principal_ref),
        "resource": event.resource.as_ref().map(serialize_resource_ref),
        "reasonCodes": &event.reason_codes,
        "payload": &event.payload,
        "metadata": &event.metadata,
        "payloadDigest": event.payload_digest(),
    })
}

fn serialize_capture_mode(mode: CaptureMode) -> &'static str {
    match mode {
        CaptureMode::None => "none",
        CaptureMode::HashOnly => "hash_only",
        CaptureMode::ReferenceOnly => "reference_only",
        CaptureMode::RedactedPreview => "redacted_preview",
        CaptureMode::Full => "full",
    }
}

fn serialize_captured_content(captured: &CapturedContent) -> Value {
    json!({
        "mode": serialize_capture_mode(captured.mode),
        "contentKind": captured.content_kind.as_str(),
        "contentDigest": captured.content_digest.as_str(),
        "preview": captured.preview.as_deref(),
        "contentRef": captured.content_ref.as_deref(),
        "retentionPolicy": captured.retention_policy.as_str(),
        "consentRef": captured.consent_ref.as_deref(),
        "redactionCount": captured.redaction_count,
        "originalBytes": captured.original_bytes,
    })
}

fn serialize_tool_result_event(event: &ToolResultEvent) -> Value {
    match event {
        ToolResultEvent::Started {
            tool_call_id,
            sequence,
            started_at_unix_ms,
        } => json!({
            "kind": "started",
            "toolCallId": tool_call_id,
            "sequence": sequence,
            "startedAtUnixMs": started_at_unix_ms,
        }),
        ToolResultEvent::Delta {
            tool_call_id,
            sequence,
            output,
        } => json!({
            "kind": "delta",
            "toolCallId": tool_call_id,
            "sequence": sequence,
            "output": output.iter().map(serialize_content_part).collect::<Vec<_>>(),
        }),
        ToolResultEvent::ArtifactReady {
            tool_call_id,
            sequence,
            artifact,
        } => json!({
            "kind": "artifact_ready",
            "toolCallId": tool_call_id,
            "sequence": sequence,
            "artifact": serialize_artifact_ref(artifact),
        }),
        ToolResultEvent::Completed {
            tool_call_id,
            sequence,
            result,
        } => json!({
            "kind": "completed",
            "toolCallId": tool_call_id,
            "sequence": sequence,
            "result": serialize_tool_result(result),
        }),
        ToolResultEvent::Failed {
            tool_call_id,
            sequence,
            result,
        } => json!({
            "kind": "failed",
            "toolCallId": tool_call_id,
            "sequence": sequence,
            "result": serialize_tool_result(result),
        }),
        ToolResultEvent::Denied {
            tool_call_id,
            sequence,
            result,
        } => json!({
            "kind": "denied",
            "toolCallId": tool_call_id,
            "sequence": sequence,
            "result": serialize_tool_result(result),
        }),
        ToolResultEvent::Cancelled {
            tool_call_id,
            sequence,
            result,
        } => json!({
            "kind": "cancelled",
            "toolCallId": tool_call_id,
            "sequence": sequence,
            "result": serialize_tool_result(result),
        }),
        ToolResultEvent::PolicyStopped {
            tool_call_id,
            sequence,
            result,
        } => json!({
            "kind": "policy_stopped",
            "toolCallId": tool_call_id,
            "sequence": sequence,
            "result": serialize_tool_result(result),
        }),
        ToolResultEvent::Incomplete {
            tool_call_id,
            sequence,
            result,
        } => json!({
            "kind": "incomplete",
            "toolCallId": tool_call_id,
            "sequence": sequence,
            "result": serialize_tool_result(result),
        }),
    }
}

fn serialize_tool_result_stream_error(error: &ToolResultStreamError) -> Value {
    match error {
        ToolResultStreamError::InvalidEvent { source } => json!({
            "code": "invalid_event",
            "source": format!("{source:?}"),
        }),
        ToolResultStreamError::NonMonotonicSequence {
            tool_call_id,
            last_sequence,
            sequence,
        } => json!({
            "code": "non_monotonic_sequence",
            "toolCallId": tool_call_id,
            "lastSequence": last_sequence,
            "sequence": sequence,
        }),
        ToolResultStreamError::EventAfterFinalResult {
            tool_call_id,
            final_status,
        } => json!({
            "code": "event_after_final_result",
            "toolCallId": tool_call_id,
            "finalStatus": serialize_tool_result_status(*final_status),
        }),
        ToolResultStreamError::DuplicateStarted {
            tool_call_id,
            last_sequence,
            sequence,
        } => json!({
            "code": "duplicate_started",
            "toolCallId": tool_call_id,
            "lastSequence": last_sequence,
            "sequence": sequence,
        }),
        ToolResultStreamError::EventBeforeStarted {
            tool_call_id,
            kind,
            sequence,
        } => json!({
            "code": "event_before_started",
            "toolCallId": tool_call_id,
            "kind": kind,
            "sequence": sequence,
        }),
    }
}

fn parse_application_event_kind(value: &str, label: &str) -> PyResult<ApplicationEventKind> {
    match value {
        "RunStarted" => Ok(ApplicationEventKind::RunStarted),
        "RunSucceeded" => Ok(ApplicationEventKind::RunSucceeded),
        "RunFailed" => Ok(ApplicationEventKind::RunFailed),
        "RunCancelled" => Ok(ApplicationEventKind::RunCancelled),
        "ToolCallProposed" => Ok(ApplicationEventKind::ToolCallProposed),
        "ToolCallArgumentsDelta" => Ok(ApplicationEventKind::ToolCallArgumentsDelta),
        "ToolCallArgumentsCompleted" => Ok(ApplicationEventKind::ToolCallArgumentsCompleted),
        "ToolCallValidated" => Ok(ApplicationEventKind::ToolCallValidated),
        "ToolCallPolicyEvaluated" => Ok(ApplicationEventKind::ToolCallPolicyEvaluated),
        "ToolCallApprovalRequested" => Ok(ApplicationEventKind::ToolCallApprovalRequested),
        "ToolCallAdmitted" => Ok(ApplicationEventKind::ToolCallAdmitted),
        "ToolCallStarted" => Ok(ApplicationEventKind::ToolCallStarted),
        "ToolCallCompleted" => Ok(ApplicationEventKind::ToolCallCompleted),
        "ToolCallFailed" => Ok(ApplicationEventKind::ToolCallFailed),
        "ToolCallDenied" => Ok(ApplicationEventKind::ToolCallDenied),
        "ToolCallCancelled" => Ok(ApplicationEventKind::ToolCallCancelled),
        "ToolCallPolicyStopped" => Ok(ApplicationEventKind::ToolCallPolicyStopped),
        "ToolCallIncomplete" => Ok(ApplicationEventKind::ToolCallIncomplete),
        "ToolResultStarted" => Ok(ApplicationEventKind::ToolResultStarted),
        "ToolResultDelta" => Ok(ApplicationEventKind::ToolResultDelta),
        "ToolResultArtifactReady" => Ok(ApplicationEventKind::ToolResultArtifactReady),
        "ToolResultCompleted" => Ok(ApplicationEventKind::ToolResultCompleted),
        "ToolResultFailed" => Ok(ApplicationEventKind::ToolResultFailed),
        "ToolResultDenied" => Ok(ApplicationEventKind::ToolResultDenied),
        "ToolResultCancelled" => Ok(ApplicationEventKind::ToolResultCancelled),
        "ToolResultPolicyStopped" => Ok(ApplicationEventKind::ToolResultPolicyStopped),
        "ToolResultIncomplete" => Ok(ApplicationEventKind::ToolResultIncomplete),
        "OutputPolicyEvaluationStarted" => Ok(ApplicationEventKind::OutputPolicyEvaluationStarted),
        "OutputPolicyAllowed" => Ok(ApplicationEventKind::OutputPolicyAllowed),
        "OutputPolicyHeld" => Ok(ApplicationEventKind::OutputPolicyHeld),
        "OutputPolicyRedacted" => Ok(ApplicationEventKind::OutputPolicyRedacted),
        "OutputPolicyReplaced" => Ok(ApplicationEventKind::OutputPolicyReplaced),
        "OutputPolicyViolationDetected" => Ok(ApplicationEventKind::OutputPolicyViolationDetected),
        "OutputCutoff" => Ok(ApplicationEventKind::OutputCutoff),
        "AssistantIncomplete" => Ok(ApplicationEventKind::AssistantIncomplete),
        "AssistantRetracted" => Ok(ApplicationEventKind::AssistantRetracted),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown application event kind {value:?}"
        ))),
    }
}

fn parse_application_event_metadata(
    value: &Value,
    label: &str,
) -> PyResult<ApplicationEventMetadata> {
    let object = json_object(value, label)?;
    Ok(ApplicationEventMetadata {
        event_id: required_alias_string(object, "eventId", "event_id", label)?.to_owned(),
        run_id: required_alias_string(object, "runId", "run_id", label)?.to_owned(),
        response_id: required_alias_string(object, "responseId", "response_id", label)?.to_owned(),
        turn_id: optional_nullable_alias_string(object, "turnId", "turn_id", label)?
            .map(str::to_owned),
        sequence: required_u64(object, "sequence", label)?,
        release_id: required_alias_string(object, "releaseId", "release_id", label)?.to_owned(),
        policy_snapshot_id: required_alias_string(
            object,
            "policySnapshotId",
            "policy_snapshot_id",
            label,
        )?
        .to_owned(),
        occurred_at_unix_ms: required_alias_u64(
            object,
            "occurredAtUnixMs",
            "occurred_at_unix_ms",
            label,
        )?,
    })
}

fn parse_application_event(value: &Value, label: &str) -> PyResult<ApplicationEvent> {
    let object = json_object(value, label)?;
    let kind = parse_application_event_kind(required_string(object, "kind", label)?, label)?;
    let metadata = parse_application_event_metadata(
        object
            .get("metadata")
            .ok_or_else(|| PyValueError::new_err(format!("{label}.metadata is required")))?,
        &format!("{label}.metadata"),
    )?;
    let payload = object.get("payload").cloned().unwrap_or_else(|| json!({}));
    let event = if kind.is_tool_event() {
        ApplicationEvent::tool(
            kind,
            metadata,
            required_alias_string(object, "toolCallId", "tool_call_id", label)?,
            payload,
        )
    } else {
        ApplicationEvent::new(kind, metadata, payload)
    }
    .map_err(|error| PyValueError::new_err(format!("invalid {label}: {error:?}")))?;
    Ok(event)
}

fn serialize_application_event_metadata(metadata: &ApplicationEventMetadata) -> Value {
    json!({
        "eventId": metadata.event_id.as_str(),
        "runId": metadata.run_id.as_str(),
        "responseId": metadata.response_id.as_str(),
        "turnId": metadata.turn_id.as_deref(),
        "sequence": metadata.sequence,
        "releaseId": metadata.release_id.as_str(),
        "policySnapshotId": metadata.policy_snapshot_id.as_str(),
        "occurredAtUnixMs": metadata.occurred_at_unix_ms,
    })
}

fn serialize_application_event(event: &ApplicationEvent) -> Value {
    json!({
        "kind": event.kind.as_str(),
        "metadata": serialize_application_event_metadata(&event.metadata),
        "toolCallId": event.tool_call_id.as_deref(),
        "payload": &event.payload,
    })
}

fn parse_application_protocol_event_kind(
    value: &str,
    label: &str,
) -> PyResult<ApplicationProtocolEventKind> {
    match value {
        "RunStarted" => Ok(ApplicationProtocolEventKind::RunStarted),
        "TurnStarted" => Ok(ApplicationProtocolEventKind::TurnStarted),
        "ContextReady" => Ok(ApplicationProtocolEventKind::ContextReady),
        "AssistantDraftStarted" => Ok(ApplicationProtocolEventKind::AssistantDraftStarted),
        "AssistantDraftDelta" => Ok(ApplicationProtocolEventKind::AssistantDraftDelta),
        "AssistantCommitted" => Ok(ApplicationProtocolEventKind::AssistantCommitted),
        "AssistantIncomplete" => Ok(ApplicationProtocolEventKind::AssistantIncomplete),
        "AssistantRetracted" => Ok(ApplicationProtocolEventKind::AssistantRetracted),
        "ToolStarted" => Ok(ApplicationProtocolEventKind::ToolStarted),
        "ToolCompleted" => Ok(ApplicationProtocolEventKind::ToolCompleted),
        "ToolCallApprovalRequested" => Ok(ApplicationProtocolEventKind::ToolCallApprovalRequested),
        "ApprovalRequested" => Ok(ApplicationProtocolEventKind::ApprovalRequested),
        "ReviewRequested" => Ok(ApplicationProtocolEventKind::ReviewRequested),
        "BudgetConstrained" => Ok(ApplicationProtocolEventKind::BudgetConstrained),
        "BudgetExhausted" => Ok(ApplicationProtocolEventKind::BudgetExhausted),
        "BudgetExtensionRequested" => Ok(ApplicationProtocolEventKind::BudgetExtensionRequested),
        "BudgetExtensionGranted" => Ok(ApplicationProtocolEventKind::BudgetExtensionGranted),
        "PolicyDecisionRequired" => Ok(ApplicationProtocolEventKind::PolicyDecisionRequired),
        "ExecutionDegraded" => Ok(ApplicationProtocolEventKind::ExecutionDegraded),
        "OutputCutoff" => Ok(ApplicationProtocolEventKind::OutputCutoff),
        "FilePatchPreview" => Ok(ApplicationProtocolEventKind::FilePatchPreview),
        "JobProgress" => Ok(ApplicationProtocolEventKind::JobProgress),
        "ArtifactReady" => Ok(ApplicationProtocolEventKind::ArtifactReady),
        "StateSnapshot" => Ok(ApplicationProtocolEventKind::StateSnapshot),
        "RunCompleted" => Ok(ApplicationProtocolEventKind::RunCompleted),
        "RunFailed" => Ok(ApplicationProtocolEventKind::RunFailed),
        "RunCancelled" => Ok(ApplicationProtocolEventKind::RunCancelled),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown application protocol event kind {value:?}"
        ))),
    }
}

fn parse_application_protocol_event_metadata(
    value: &Value,
    label: &str,
) -> PyResult<ApplicationProtocolEventMetadata> {
    let object = json_object(value, label)?;
    Ok(ApplicationProtocolEventMetadata {
        event_id: required_alias_string(object, "eventId", "event_id", label)?.to_owned(),
        protocol_version: required_alias_string(
            object,
            "protocolVersion",
            "protocol_version",
            label,
        )?
        .to_owned(),
        run_id: required_alias_string(object, "runId", "run_id", label)?.to_owned(),
        turn_id: optional_nullable_alias_string(object, "turnId", "turn_id", label)?
            .map(str::to_owned),
        sequence: required_u64(object, "sequence", label)?,
        cursor: optional_nullable_alias_string(object, "cursor", "cursor", label)?
            .map(str::to_owned),
        occurred_at_unix_ms: required_alias_u64(
            object,
            "occurredAtUnixMs",
            "occurred_at_unix_ms",
            label,
        )?,
    })
}

fn parse_application_protocol_event(
    value: &Value,
    label: &str,
) -> PyResult<ApplicationProtocolEvent> {
    let object = json_object(value, label)?;
    let kind_value = object
        .get("kind")
        .or_else(|| object.get("eventKind"))
        .or_else(|| object.get("event_kind"))
        .and_then(Value::as_str)
        .ok_or_else(|| PyValueError::new_err(format!("{label}.kind must be a string")))?;
    let kind = parse_application_protocol_event_kind(kind_value, label)?;
    let metadata = parse_application_protocol_event_metadata(
        object
            .get("metadata")
            .ok_or_else(|| PyValueError::new_err(format!("{label}.metadata is required")))?,
        &format!("{label}.metadata"),
    )?;
    let payload = object.get("payload").cloned().unwrap_or_else(|| json!({}));
    ApplicationProtocolEvent::new(kind, metadata, payload)
        .map_err(|error| PyValueError::new_err(format!("invalid {label}: {error:?}")))
}

fn serialize_application_protocol_event_metadata(
    metadata: &ApplicationProtocolEventMetadata,
) -> Value {
    json!({
        "eventId": metadata.event_id.as_str(),
        "protocolVersion": metadata.protocol_version.as_str(),
        "runId": metadata.run_id.as_str(),
        "turnId": metadata.turn_id.as_deref(),
        "sequence": metadata.sequence,
        "cursor": metadata.cursor.as_deref(),
        "occurredAtUnixMs": metadata.occurred_at_unix_ms,
    })
}

fn serialize_application_protocol_event(event: &ApplicationProtocolEvent) -> Value {
    json!({
        "kind": event.kind.as_str(),
        "metadata": serialize_application_protocol_event_metadata(&event.metadata),
        "payload": &event.payload,
    })
}

fn serialize_application_protocol_log_error(error: &ApplicationProtocolError) -> Value {
    match error {
        ApplicationProtocolError::NonMonotonicSequence { previous, next } => json!({
            "code": "non_monotonic_sequence",
            "previous": previous,
            "next": next,
        }),
        ApplicationProtocolError::InvalidToolResultEvent { source } => json!({
            "code": "invalid_tool_result_event",
            "source": format!("{source:?}"),
        }),
        ApplicationProtocolError::ProtocolVersionMismatch { left, right } => json!({
            "code": "protocol_version_mismatch",
            "left": left,
            "right": right,
        }),
        ApplicationProtocolError::EmptyCommandId => json!({
            "code": "empty_command_id",
        }),
        ApplicationProtocolError::EmptyEventId => json!({
            "code": "empty_event_id",
        }),
        ApplicationProtocolError::EmptyMetadataField { field } => json!({
            "code": "empty_metadata_field",
            "field": field,
        }),
    }
}

fn parse_application_command_kind(value: &str, label: &str) -> PyResult<ApplicationCommandKind> {
    match value {
        "InvokeGraph" => Ok(ApplicationCommandKind::InvokeGraph),
        "CancelRun" => Ok(ApplicationCommandKind::CancelRun),
        "SubmitInput" => Ok(ApplicationCommandKind::SubmitInput),
        "ApproveEffect" => Ok(ApplicationCommandKind::ApproveEffect),
        "DenyEffect" => Ok(ApplicationCommandKind::DenyEffect),
        "SubmitReview" => Ok(ApplicationCommandKind::SubmitReview),
        "RequestBudgetExtension" => Ok(ApplicationCommandKind::RequestBudgetExtension),
        "ApplyPolicyOverride" => Ok(ApplicationCommandKind::ApplyPolicyOverride),
        "ResumeInterrupt" => Ok(ApplicationCommandKind::ResumeInterrupt),
        "SelectCandidate" => Ok(ApplicationCommandKind::SelectCandidate),
        "OpenArtifact" => Ok(ApplicationCommandKind::OpenArtifact),
        "SetBreakpoint" => Ok(ApplicationCommandKind::SetBreakpoint),
        "RequestSnapshot" => Ok(ApplicationCommandKind::RequestSnapshot),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown application command kind {value:?}"
        ))),
    }
}

fn parse_application_protocol_capabilities(
    value: &Value,
    label: &str,
) -> PyResult<ApplicationProtocolCapabilities> {
    let object = json_object(value, label)?;
    let mut capabilities = ApplicationProtocolCapabilities::new(required_alias_string(
        object,
        "protocolVersion",
        "protocol_version",
        label,
    )?);
    if let Some(commands) = object.get("commands") {
        let Some(commands) = commands.as_array() else {
            return Err(PyValueError::new_err(format!(
                "{label}.commands must be an array"
            )));
        };
        let parsed_commands = commands
            .iter()
            .enumerate()
            .map(|(index, command)| {
                let Some(command) = command.as_str() else {
                    return Err(PyValueError::new_err(format!(
                        "{label}.commands[{index}] must be a string"
                    )));
                };
                parse_application_command_kind(command, &format!("{label}.commands[{index}]"))
            })
            .collect::<PyResult<Vec<_>>>()?;
        capabilities = capabilities.with_commands(parsed_commands);
    }
    if let Some(events) = object.get("events") {
        let Some(events) = events.as_array() else {
            return Err(PyValueError::new_err(format!(
                "{label}.events must be an array"
            )));
        };
        let parsed_events = events
            .iter()
            .enumerate()
            .map(|(index, event)| {
                let Some(event) = event.as_str() else {
                    return Err(PyValueError::new_err(format!(
                        "{label}.events[{index}] must be a string"
                    )));
                };
                parse_application_protocol_event_kind(event, &format!("{label}.events[{index}]"))
            })
            .collect::<PyResult<Vec<_>>>()?;
        capabilities = capabilities.with_events(parsed_events);
    }
    Ok(capabilities)
}

fn parse_work_kind(value: &Value, label: &str) -> PyResult<WorkKind> {
    let Some(value) = value.as_str() else {
        return Err(PyValueError::new_err(format!("{label} must be a string")));
    };
    match value {
        "current_provider_call" => Ok(WorkKind::CurrentProviderCall),
        "already_admitted_child_work" => Ok(WorkKind::AlreadyAdmittedChildWork),
        "declared_finalization" => Ok(WorkKind::DeclaredFinalization),
        "checkpoint" => Ok(WorkKind::Checkpoint),
        "cleanup" => Ok(WorkKind::Cleanup),
        "read_only_tool" => Ok(WorkKind::ReadOnlyTool),
        "new_turn" => Ok(WorkKind::NewTurn),
        "plan_expansion" => Ok(WorkKind::PlanExpansion),
        "optional_task" => Ok(WorkKind::OptionalTask),
        "new_trial" => Ok(WorkKind::NewTrial),
        "state_changing_effect" => Ok(WorkKind::StateChangingEffect),
        "unreserved_provider_call" => Ok(WorkKind::UnreservedProviderCall),
        _ => Err(PyValueError::new_err(format!(
            "{label} has unknown work kind {value:?}"
        ))),
    }
}

fn parse_work_kind_list(
    object: &serde_json::Map<String, Value>,
    field: &str,
    label: &str,
) -> PyResult<Vec<WorkKind>> {
    let Some(value) = object.get(field) else {
        return Ok(Vec::new());
    };
    let Some(values) = value.as_array() else {
        return Err(PyValueError::new_err(format!(
            "{label}.{field} must be an array"
        )));
    };
    values
        .iter()
        .enumerate()
        .map(|(index, value)| parse_work_kind(value, &format!("{label}.{field}[{index}]")))
        .collect()
}

fn parse_usage_amount(value: &Value, label: &str) -> PyResult<UsageAmount> {
    let object = json_object(value, label)?;
    let kind = required_string(object, "kind", label)?;
    let amount = object
        .get("amount")
        .and_then(Value::as_i64)
        .ok_or_else(|| PyValueError::new_err(format!("{label}.amount must be an integer")))?;
    let unit = required_string(object, "unit", label)?;
    let mut usage = UsageAmount::new(kind, amount, unit);
    if let Some(dimensions) = object.get("dimensions") {
        let dimensions = json_object(dimensions, &format!("{label}.dimensions"))?;
        for (key, value) in dimensions {
            let Some(value) = value.as_str() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.dimensions.{key} must be a string"
                )));
            };
            usage = usage.with_dimension(key.clone(), value);
        }
    }
    Ok(usage)
}

fn parse_usage_amounts(
    object: &serde_json::Map<String, Value>,
    field: &str,
    label: &str,
) -> PyResult<Vec<UsageAmount>> {
    let Some(value) = object.get(field) else {
        return Ok(Vec::new());
    };
    let Some(values) = value.as_array() else {
        return Err(PyValueError::new_err(format!(
            "{label}.{field} must be an array"
        )));
    };
    values
        .iter()
        .enumerate()
        .map(|(index, value)| parse_usage_amount(value, &format!("{label}.{field}[{index}]")))
        .collect()
}

fn parse_budget_permit(value: &Value, label: &str) -> PyResult<BudgetPermit> {
    let object = json_object(value, label)?;
    let reservation_refs = object
        .get("reservationRefs")
        .and_then(Value::as_array)
        .ok_or_else(|| PyValueError::new_err(format!("{label}.reservationRefs must be an array")))?
        .iter()
        .enumerate()
        .map(|(index, value)| {
            value.as_str().map(str::to_owned).ok_or_else(|| {
                PyValueError::new_err(format!("{label}.reservationRefs[{index}] must be a string"))
            })
        })
        .collect::<PyResult<Vec<_>>>()?;
    let mut fencing_tokens = BTreeMap::new();
    if let Some(tokens) = object.get("fencingTokens") {
        let tokens = json_object(tokens, &format!("{label}.fencingTokens"))?;
        for (budget_id, token) in tokens {
            let Some(token) = token.as_u64() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.fencingTokens.{budget_id} must be an unsigned integer"
                )));
            };
            fencing_tokens.insert(budget_id.clone(), token);
        }
    }

    Ok(BudgetPermit {
        permit_id: required_string(object, "permitId", label)?.to_owned(),
        reservation_refs,
        owner: required_string(object, "owner", label)?.to_owned(),
        atomic_unit: required_string(object, "atomicUnit", label)?.to_owned(),
        admission_epoch: required_u64(object, "admissionEpoch", label)?,
        authorized_amounts: parse_usage_amounts(object, "authorizedAmounts", label)?,
        continuation_profile: required_string(object, "continuationProfile", label)?.to_owned(),
        policy_snapshot_digest: required_string(object, "policySnapshotDigest", label)?.to_owned(),
        expires_at: required_string(object, "expiresAt", label)?.to_owned(),
        low_watermark: parse_usage_amounts(object, "lowWatermark", label)?,
        fencing_tokens,
    })
}

fn parse_continuation_envelope(value: &Value, label: &str) -> PyResult<ContinuationEnvelope> {
    let object = json_object(value, label)?;
    let max_steps = if let Some(value) = object.get("maxAdditionalSteps") {
        let Some(value) = value.as_u64() else {
            return Err(PyValueError::new_err(format!(
                "{label}.maxAdditionalSteps must be an unsigned integer"
            )));
        };
        Some(u32::try_from(value).map_err(|_| {
            PyValueError::new_err(format!("{label}.maxAdditionalSteps exceeds u32"))
        })?)
    } else {
        None
    };
    let deadline = object
        .get("deadline")
        .map(|value| {
            value
                .as_str()
                .map(str::to_owned)
                .ok_or_else(|| PyValueError::new_err(format!("{label}.deadline must be a string")))
        })
        .transpose()?;
    let mut envelope = ContinuationEnvelope::new()
        .with_allowed_work(parse_work_kind_list(object, "allowedWork", label)?)
        .with_forbidden_work(parse_work_kind_list(object, "forbiddenWork", label)?)
        .with_max_additional_usage(parse_usage_amounts(object, "maxAdditionalUsage", label)?);
    if let Some(max_steps) = max_steps {
        envelope = envelope.with_max_additional_steps(max_steps);
    }
    if let Some(deadline) = deadline {
        envelope = envelope.with_deadline(deadline);
    }
    Ok(envelope)
}

fn parse_exhaustion_policy(value: &Value) -> PyResult<ExhaustionPolicy> {
    let object = json_object(value, "policy")?;
    let preset = match required_string(object, "preset", "policy")? {
        "finish_current_turn" => ExhaustionPreset::FinishCurrentTurn,
        "finish_current_call" => ExhaustionPreset::FinishCurrentCall,
        "finish_current_step" => ExhaustionPreset::FinishCurrentStep,
        "checkpoint_and_pause" => ExhaustionPreset::CheckpointAndPause,
        "hard_stop" => ExhaustionPreset::HardStop,
        "degrade_then_finalize" => ExhaustionPreset::DegradeThenFinalize,
        "request_extension" => ExhaustionPreset::RequestExtension,
        preset => {
            return Err(PyValueError::new_err(format!(
                "policy.preset has unknown preset {preset:?}"
            )));
        }
    };
    let unit = match required_string(object, "unit", "policy")? {
        "provider_call" => ExhaustionUnit::ProviderCall,
        "node" => ExhaustionUnit::Node,
        "agent_step" => ExhaustionUnit::AgentStep,
        "turn" => ExhaustionUnit::Turn,
        "map_item" => ExhaustionUnit::MapItem,
        "task" => ExhaustionUnit::Task,
        "trial" => ExhaustionUnit::Trial,
        "run" => ExhaustionUnit::Run,
        unit => {
            return Err(PyValueError::new_err(format!(
                "policy.unit has unknown unit {unit:?}"
            )));
        }
    };
    let continuation = object
        .get("continuation")
        .map(|value| parse_continuation_envelope(value, "policy.continuation"))
        .transpose()?;
    Ok(ExhaustionPolicy::from_preset(preset, unit, continuation))
}

fn build_runtime_bridge_plan(graph: &Value) -> PyResult<RuntimeBridgePlan> {
    let plan = compile_graph(graph);
    if !plan.ok() {
        let error_codes = plan
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        return Err(PyValueError::new_err(format!(
            "graph did not compile: {error_codes}"
        )));
    }

    let spec = plan
        .normalized
        .get("spec")
        .and_then(Value::as_object)
        .ok_or_else(|| PyValueError::new_err("normalized graph spec must be an object"))?;
    let nodes = spec
        .get("nodes")
        .and_then(Value::as_object)
        .ok_or_else(|| PyValueError::new_err("normalized graph nodes must be an object"))?;
    let edges = spec
        .get("edges")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut dependencies_by_node = nodes
        .keys()
        .map(|node_id| (node_id.clone(), Vec::new()))
        .collect::<BTreeMap<_, _>>();

    for edge in &edges {
        let Some(edge) = edge.as_object() else {
            continue;
        };
        let (Some(source), Some(target)) = (
            edge.get("from").and_then(Value::as_str),
            edge.get("to").and_then(Value::as_str),
        ) else {
            continue;
        };
        let (source_owner, source_path) = source.split_once('.').unwrap_or((source, ""));
        let (target_owner, target_path) = target.split_once('.').unwrap_or((target, ""));
        if target_owner.starts_with('$') {
            continue;
        }
        if source_owner.starts_with('$') && source_owner != "$input" {
            continue;
        }
        let Some(source_port) = source_path
            .split('.')
            .next()
            .filter(|port| !port.is_empty())
        else {
            return Err(PyValueError::new_err(format!(
                "edge source {source:?} must include a port"
            )));
        };
        let Some(target_input) = target_path
            .split('.')
            .next()
            .filter(|port| !port.is_empty())
        else {
            return Err(PyValueError::new_err(format!(
                "edge target {target:?} must include an input"
            )));
        };
        let Some(dependencies) = dependencies_by_node.get_mut(target_owner) else {
            return Err(PyValueError::new_err(format!(
                "edge target references unknown node {target_owner:?}"
            )));
        };
        dependencies.push(InputDependency::value(
            target_input,
            PortRef::new(source_owner, source_port),
        ));
    }

    let scheduled_nodes = dependencies_by_node
        .into_iter()
        .map(|(node_id, dependencies)| ScheduledNode::new(node_id, dependencies))
        .collect::<Vec<_>>();

    Ok(RuntimeBridgePlan {
        graph_hash: plan.graph_hash,
        edges,
        scheduled_nodes,
    })
}

fn runtime_with_inputs(
    scheduled_nodes: Vec<ScheduledNode>,
    inputs: &Value,
) -> PyResult<InProcessTestRuntime> {
    let mut runtime =
        InProcessTestRuntime::new("run-000001", scheduled_nodes).map_err(|error| {
            PyValueError::new_err(format!("failed to create test runtime: {error:?}"))
        })?;
    if let Some(input_object) = inputs.as_object() {
        for (input_name, value) in input_object {
            runtime = runtime.with_initial_value(PortRef::new("$input", input_name), value.clone());
        }
    }
    Ok(runtime)
}

fn collect_output_values(
    edges: &[Value],
    inputs: &Value,
    outputs_by_node: &BTreeMap<String, Value>,
    status: TestRunStatus,
) -> PyResult<Value> {
    let mut output_values = json!({});

    if status == TestRunStatus::Succeeded {
        for edge in edges {
            let Some(edge) = edge.as_object() else {
                continue;
            };
            let (Some(source), Some(target)) = (
                edge.get("from").and_then(Value::as_str),
                edge.get("to").and_then(Value::as_str),
            ) else {
                continue;
            };
            let (source_owner, source_path) = source.split_once('.').unwrap_or((source, ""));
            let (target_owner, target_path) = target.split_once('.').unwrap_or((target, ""));
            if target_owner != "$output" {
                continue;
            }
            let mut value = if source_owner == "$input" {
                inputs.clone()
            } else {
                outputs_by_node.get(source_owner).cloned().ok_or_else(|| {
                    PyRuntimeError::new_err(format!(
                        "output edge references missing node output {source_owner:?}"
                    ))
                })?
            };
            if !source_path.is_empty() {
                for part in source_path.split('.') {
                    value = value.get(part).cloned().ok_or_else(|| {
                        PyRuntimeError::new_err(format!(
                            "output edge source {source:?} is missing path segment {part:?}"
                        ))
                    })?;
                }
            }
            let target_parts = target_path.split('.').collect::<Vec<_>>();
            if target_parts.is_empty() || target_parts.iter().any(|part| part.is_empty()) {
                return Err(PyValueError::new_err(format!(
                    "output edge target {target:?} must include an output path"
                )));
            }
            let mut current = &mut output_values;
            for part in &target_parts[..target_parts.len() - 1] {
                let Some(current_object) = current.as_object_mut() else {
                    return Err(PyRuntimeError::new_err(format!(
                        "output path conflict at {target:?}"
                    )));
                };
                current = current_object
                    .entry((*part).to_owned())
                    .or_insert_with(|| json!({}));
            }
            let Some(current_object) = current.as_object_mut() else {
                return Err(PyRuntimeError::new_err(format!(
                    "output path conflict at {target:?}"
                )));
            };
            current_object.insert(target_parts[target_parts.len() - 1].to_owned(), value);
        }
    }

    Ok(output_values)
}

fn serialize_runtime_result(
    result: graphblocks_runtime_core::test_runtime::TestRunResult,
    graph_hash: String,
    output_values: Value,
) -> PyResult<String> {
    let status = match result.status {
        TestRunStatus::Succeeded => "succeeded",
        TestRunStatus::Failed => "failed",
        TestRunStatus::Cancelled => "cancelled",
    };
    let journal = result
        .journal
        .records()
        .iter()
        .map(|record| {
            json!({
                "recordId": record.record_id.as_str(),
                "runId": record.run_id.as_str(),
                "runSequence": record.run_sequence,
                "kind": record.kind.as_str(),
                "causationId": record.causation_id.as_deref(),
                "nodeId": record.node_id.as_deref(),
                "attemptId": record.attempt_id.as_deref(),
                "leaseEpoch": record.lease_epoch,
                "payload": record.payload.as_ref(),
                "terminal": record.terminal,
            })
        })
        .collect::<Vec<_>>();
    let payload = json!({
        "runId": result.run_id,
        "graphHash": graph_hash,
        "status": status,
        "outputs": output_values,
        "journal": journal,
    });

    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!("failed to serialize runtime result: {error}"))
    })
}

#[pyfunction]
fn run_test_graph_json(
    graph_json: &str,
    inputs_json: &str,
    node_outputs_json: &str,
) -> PyResult<String> {
    let graph = parse_json_argument(graph_json, "graph document")?;
    let inputs = parse_json_argument(inputs_json, "runtime inputs")?;
    let node_outputs = parse_json_argument(node_outputs_json, "node outputs")?;
    let Some(node_outputs) = node_outputs.as_object() else {
        return Err(PyValueError::new_err(
            "node outputs JSON must be an object keyed by node id",
        ));
    };
    let bridge_plan = build_runtime_bridge_plan(&graph)?;
    let mut runtime = runtime_with_inputs(bridge_plan.scheduled_nodes, &inputs)?;
    let mut executor = JsonNodeExecutor {
        outputs_by_node: node_outputs
            .iter()
            .map(|(node_id, outputs)| (node_id.clone(), outputs.clone()))
            .collect(),
    };
    let result = runtime.run(&mut executor).map_err(|error| {
        PyRuntimeError::new_err(format!("test runtime execution failed: {error:?}"))
    })?;
    let output_values = collect_output_values(
        &bridge_plan.edges,
        &inputs,
        &executor.outputs_by_node,
        result.status,
    )?;
    serialize_runtime_result(result, bridge_plan.graph_hash, output_values)
}

#[pyfunction]
fn run_stdlib_graph_json(graph_json: &str, inputs_json: &str) -> PyResult<String> {
    graphblocks_runtime_core::stdlib_runtime::run_stdlib_graph_json(graph_json, inputs_json)
        .map_err(|error| PyRuntimeError::new_err(error.to_string()))
}

#[pyfunction]
fn decide_agent_step_json(spec_json: &str, request_json: &str) -> PyResult<String> {
    let spec_value = parse_json_argument(spec_json, "agent spec")?;
    let request_value = parse_json_argument(request_json, "agent step request")?;
    let spec_object = json_object(&spec_value, "agent spec")?;
    let request_object = json_object(&request_value, "agent step request")?;

    let mut spec = AgentSpec::new(required_string(spec_object, "modelPool", "agent spec")?);
    if let Some(tools) = spec_object.get("tools") {
        let tools = tools
            .as_array()
            .ok_or_else(|| PyValueError::new_err("agent spec.tools must be an array"))?;
        let mut parsed_tools = Vec::new();
        for (index, tool) in tools.iter().enumerate() {
            let Some(tool) = tool.as_str() else {
                return Err(PyValueError::new_err(format!(
                    "agent spec.tools[{index}] must be a string"
                )));
            };
            parsed_tools.push(tool.to_owned());
        }
        spec = spec.with_tools(parsed_tools);
    }
    if let Some(max_steps) = spec_object.get("maxSteps") {
        let Some(max_steps) = max_steps.as_u64() else {
            return Err(PyValueError::new_err(
                "agent spec.maxSteps must be an unsigned integer",
            ));
        };
        spec = spec
            .with_max_steps(usize::try_from(max_steps).map_err(|_| {
                PyValueError::new_err("agent spec.maxSteps exceeds platform usize")
            })?);
    }
    if let Some(completion_reserve_units) = spec_object.get("completionReserveUnits") {
        let Some(completion_reserve_units) = completion_reserve_units.as_i64() else {
            return Err(PyValueError::new_err(
                "agent spec.completionReserveUnits must be an integer",
            ));
        };
        spec = spec.with_completion_reserve_units(completion_reserve_units);
    }

    let completed_steps = required_u64(request_object, "completedSteps", "agent step request")?;
    let completed_steps = usize::try_from(completed_steps).map_err(|_| {
        PyValueError::new_err("agent step request.completedSteps exceeds platform usize")
    })?;
    let remaining_budget_units = request_object
        .get("remainingBudgetUnits")
        .and_then(Value::as_i64)
        .ok_or_else(|| {
            PyValueError::new_err("agent step request.remainingBudgetUnits must be an integer")
        })?;

    let decision =
        AgentLoopController::new(spec).decide_next_step(completed_steps, remaining_budget_units);
    let (disposition, reason) = match decision {
        AgentLoopDecision::Continue { reason } => ("continue", reason),
        AgentLoopDecision::Finalize { reason } => ("finalize", reason),
        AgentLoopDecision::Stop { reason } => ("stop", reason),
    };
    let payload = json!({
        "disposition": disposition,
        "reason": reason,
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!("failed to serialize agent step decision: {error}"))
    })
}

#[pyfunction]
fn admit_exhaustion_work_json(policy_json: &str, request_json: &str) -> PyResult<String> {
    let policy_value = parse_json_argument(policy_json, "exhaustion policy")?;
    let request_value = parse_json_argument(request_json, "exhaustion admission request")?;
    let policy = parse_exhaustion_policy(&policy_value)?;
    let request = json_object(&request_value, "request")?;
    let atomic_unit_id = required_string(request, "atomicUnitId", "request")?;
    let admission_epoch = required_u64(request, "admissionEpoch", "request")?;
    let work_kind = parse_work_kind(
        request
            .get("workKind")
            .ok_or_else(|| PyValueError::new_err("request.workKind is required"))?,
        "request.workKind",
    )?;
    let work_epoch = required_u64(request, "workEpoch", "request")?;
    let permit = request
        .get("permit")
        .map(|value| parse_budget_permit(value, "request.permit"))
        .transpose()?;
    let requested_usage = parse_usage_amounts(request, "requestedUsage", "request")?;
    let continuation_permit = request
        .get("continuationPermit")
        .map(|value| parse_budget_permit(value, "request.continuationPermit"))
        .transpose()?;
    let validation_time = request
        .get("validationTime")
        .map(|value| {
            value
                .as_str()
                .ok_or_else(|| PyValueError::new_err("request.validationTime must be a string"))
        })
        .transpose()?;
    let mut controller = ExhaustionController::new(policy, atomic_unit_id, admission_epoch);
    if let Some(continuation_permit) = continuation_permit {
        controller = controller.with_continuation_permit(continuation_permit);
    }
    if let Some(validation_time) = validation_time {
        controller = controller.with_validation_time(validation_time);
    }

    let decision =
        controller.admit_with_usage(work_kind, work_epoch, permit.as_ref(), requested_usage);
    let payload = json!({
        "allowed": decision.allowed,
        "reason": decision.reason,
        "usedAdditionalSteps": controller.used_additional_steps,
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize exhaustion admission result: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_output_gate_json(gate_json: &str, operations_json: &str) -> PyResult<String> {
    let gate_value = parse_json_argument(gate_json, "output gate")?;
    let operations_value = parse_json_argument(operations_json, "output gate operations")?;
    let gate_object = json_object(&gate_value, "gate")?;
    let stream_id = required_alias_string(gate_object, "streamId", "stream_id", "gate")?;
    let response_id = required_alias_string(gate_object, "responseId", "response_id", "gate")?;
    let last_generated_sequence = optional_alias_u64(
        gate_object,
        "lastGeneratedSequence",
        "last_generated_sequence",
        "gate",
    )?
    .unwrap_or(0);
    let last_policy_accepted_sequence = optional_alias_u64(
        gate_object,
        "lastPolicyAcceptedSequence",
        "last_policy_accepted_sequence",
        "gate",
    )?
    .unwrap_or(0);
    let last_client_delivered_sequence = optional_alias_u64(
        gate_object,
        "lastClientDeliveredSequence",
        "last_client_delivered_sequence",
        "gate",
    )?
    .unwrap_or(0);
    let mut pending_chunks = Vec::new();
    if let Some(pending) = gate_object.get("pending") {
        let Some(pending) = pending.as_array() else {
            return Err(PyValueError::new_err("gate.pending must be an array"));
        };
        for (pending_index, pending_chunk) in pending.iter().enumerate() {
            let pending_label = format!("gate.pending[{pending_index}]");
            let pending_chunk = json_object(pending_chunk, &pending_label)?;
            pending_chunks.push(GenerationChunk::text(
                optional_alias_string(pending_chunk, "streamId", "stream_id", &pending_label)?
                    .unwrap_or(stream_id),
                optional_alias_string(pending_chunk, "responseId", "response_id", &pending_label)?
                    .unwrap_or(response_id),
                required_u64(pending_chunk, "sequence", &pending_label)?,
                required_string(pending_chunk, "text", &pending_label)?,
            ));
        }
    }
    let mut gate = if let Some(cutoff) = gate_object.get("cutoff") {
        let cutoff = json_object(cutoff, "gate.cutoff")?;
        let terminal_reason = match required_alias_string(
            cutoff,
            "terminalReason",
            "terminal_reason",
            "gate.cutoff",
        )? {
            "policy_denied" => TerminalReason::PolicyDenied,
            "budget_exhausted" => TerminalReason::BudgetExhausted,
            "cancelled" => TerminalReason::Cancelled,
            "client_disconnected" => TerminalReason::ClientDisconnected,
            value => {
                return Err(PyValueError::new_err(format!(
                    "gate.cutoff.terminalReason has unknown reason {value:?}"
                )));
            }
        };
        let draft_disposition = match required_alias_string(
            cutoff,
            "draftDisposition",
            "draft_disposition",
            "gate.cutoff",
        )? {
            "keep" => DraftDisposition::Keep,
            "mark_incomplete" => DraftDisposition::MarkIncomplete,
            "retract" => DraftDisposition::Retract,
            value => {
                return Err(PyValueError::new_err(format!(
                    "gate.cutoff.draftDisposition has unknown disposition {value:?}"
                )));
            }
        };
        let durable_result = match required_alias_string(
            cutoff,
            "durableResult",
            "durable_result",
            "gate.cutoff",
        )? {
            "none" => DurableResult::None,
            "incomplete" => DurableResult::Incomplete,
            "partial" => DurableResult::Partial,
            value => {
                return Err(PyValueError::new_err(format!(
                    "gate.cutoff.durableResult has unknown result {value:?}"
                )));
            }
        };
        let turn_id =
            optional_alias_string(cutoff, "turnId", "turn_id", "gate.cutoff")?.map(str::to_owned);
        let policy_decision_id = optional_alias_string(
            cutoff,
            "policyDecisionId",
            "policy_decision_id",
            "gate.cutoff",
        )?
        .map(str::to_owned);
        OutputDeliveryGate::from_cutoff(OutputCutoff {
            stream_id: optional_alias_string(cutoff, "streamId", "stream_id", "gate.cutoff")?
                .unwrap_or(stream_id)
                .to_owned(),
            response_id: optional_alias_string(cutoff, "responseId", "response_id", "gate.cutoff")?
                .unwrap_or(response_id)
                .to_owned(),
            turn_id,
            last_generated_sequence: required_alias_u64(
                cutoff,
                "lastGeneratedSequence",
                "last_generated_sequence",
                "gate.cutoff",
            )?,
            last_policy_accepted_sequence: required_alias_u64(
                cutoff,
                "lastPolicyAcceptedSequence",
                "last_policy_accepted_sequence",
                "gate.cutoff",
            )?,
            last_client_delivered_sequence: required_alias_u64(
                cutoff,
                "lastClientDeliveredSequence",
                "last_client_delivered_sequence",
                "gate.cutoff",
            )?,
            terminal_reason,
            draft_disposition,
            durable_result,
            policy_decision_id,
            occurred_at_unix_ms: required_alias_u64(
                cutoff,
                "occurredAtUnixMs",
                "occurred_at_unix_ms",
                "gate.cutoff",
            )?,
        })
    } else {
        OutputDeliveryGate::from_state(
            stream_id,
            response_id,
            pending_chunks,
            last_generated_sequence,
            last_policy_accepted_sequence,
            last_client_delivered_sequence,
        )
    }
    .map_err(|error| PyValueError::new_err(format!("invalid output gate state: {error:?}")))?;
    if let Some(turn_id) = optional_alias_string(gate_object, "turnId", "turn_id", "gate")? {
        gate = gate.with_turn_id(turn_id);
    }
    if let Some(delivery_policy) = gate_object
        .get("deliveryPolicy")
        .or_else(|| gate_object.get("delivery_policy"))
    {
        let delivery_policy = json_object(delivery_policy, "gate.deliveryPolicy")?;
        let on_violation = match optional_alias_string(
            delivery_policy,
            "onViolation",
            "on_violation",
            "gate.deliveryPolicy",
        )?
        .unwrap_or("abort_response")
        {
            "abort_response" => ViolationAction::AbortResponse,
            "abort_turn" => ViolationAction::AbortTurn,
            "redact" => ViolationAction::Redact,
            "replace" => ViolationAction::Replace,
            value => {
                return Err(PyValueError::new_err(format!(
                    "gate.deliveryPolicy.onViolation has unknown action {value:?}"
                )));
            }
        };
        let delivered_draft_disposition = match optional_alias_string(
            delivery_policy,
            "deliveredDraftDisposition",
            "delivered_draft_disposition",
            "gate.deliveryPolicy",
        )?
        .unwrap_or("retract")
        {
            "keep" => DraftDisposition::Keep,
            "mark_incomplete" => DraftDisposition::MarkIncomplete,
            "retract" => DraftDisposition::Retract,
            value => {
                return Err(PyValueError::new_err(format!(
                    "gate.deliveryPolicy.deliveredDraftDisposition has unknown disposition {value:?}"
                )));
            }
        };
        let mut policy = match required_string(delivery_policy, "mode", "gate.deliveryPolicy")? {
            "buffer_until_commit" => OutputDeliveryPolicy::buffer_until_commit(on_violation),
            "bounded_holdback" => {
                OutputDeliveryPolicy::bounded_holdback(on_violation, delivered_draft_disposition)
            }
            "immediate_draft" => {
                OutputDeliveryPolicy::immediate_draft(on_violation, delivered_draft_disposition)
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "gate.deliveryPolicy.mode has unknown mode {value:?}"
                )));
            }
        };
        if let Some(value) = optional_alias_u64(
            delivery_policy,
            "holdbackMaxTokens",
            "holdback_max_tokens",
            "gate.deliveryPolicy",
        )? {
            policy = policy.with_holdback_max_tokens(value);
        }
        if let Some(value) = optional_alias_u64(
            delivery_policy,
            "holdbackMaxBytes",
            "holdback_max_bytes",
            "gate.deliveryPolicy",
        )? {
            policy = policy.with_holdback_max_bytes(value);
        }
        if let Some(value) = optional_alias_u64(
            delivery_policy,
            "holdbackMaxDurationMs",
            "holdback_max_duration_ms",
            "gate.deliveryPolicy",
        )? {
            policy = policy.with_holdback_max_duration_ms(value);
        }
        if let Some(boundaries) = delivery_policy
            .get("flushBoundaries")
            .or_else(|| delivery_policy.get("flush_boundaries"))
        {
            let Some(boundaries) = boundaries.as_array() else {
                return Err(PyValueError::new_err(
                    "gate.deliveryPolicy.flushBoundaries must be an array",
                ));
            };
            let mut parsed_boundaries = Vec::new();
            for (index, boundary) in boundaries.iter().enumerate() {
                let Some(boundary) = boundary.as_str() else {
                    return Err(PyValueError::new_err(format!(
                        "gate.deliveryPolicy.flushBoundaries[{index}] must be a string"
                    )));
                };
                parsed_boundaries.push(match boundary {
                    "token" => FlushBoundary::Token,
                    "sentence" => FlushBoundary::Sentence,
                    "paragraph" => FlushBoundary::Paragraph,
                    "content_part" => FlushBoundary::ContentPart,
                    "tool_call" => FlushBoundary::ToolCall,
                    "response" => FlushBoundary::Response,
                    value => {
                        return Err(PyValueError::new_err(format!(
                            "gate.deliveryPolicy.flushBoundaries[{index}] has unknown boundary {value:?}"
                        )));
                    }
                });
            }
            policy = policy.flush_on(parsed_boundaries);
        }
        gate = gate.with_delivery_policy(policy).map_err(|error| {
            PyValueError::new_err(format!("invalid output gate policy: {error:?}"))
        })?;
    }

    let operations = operations_value
        .as_array()
        .ok_or_else(|| PyValueError::new_err("output gate operations JSON must be an array"))?;
    let mut deliveries = Vec::new();
    let mut decisions = Vec::new();
    for (operation_index, operation) in operations.iter().enumerate() {
        let operation = json_object(operation, &format!("operations[{operation_index}]"))?;
        match required_string(operation, "kind", &format!("operations[{operation_index}]"))? {
            "chunk" => {
                let sequence = required_u64(
                    operation,
                    "sequence",
                    &format!("operations[{operation_index}]"),
                )?;
                let text =
                    required_string(operation, "text", &format!("operations[{operation_index}]"))?;
                let operation_stream_id = optional_alias_string(
                    operation,
                    "streamId",
                    "stream_id",
                    &format!("operations[{operation_index}]"),
                )?
                .unwrap_or(stream_id);
                let operation_response_id = optional_alias_string(
                    operation,
                    "responseId",
                    "response_id",
                    &format!("operations[{operation_index}]"),
                )?
                .unwrap_or(response_id);
                let deliverable = gate
                    .record_chunk(GenerationChunk::text(
                        operation_stream_id,
                        operation_response_id,
                        sequence,
                        text,
                    ))
                    .map_err(|error| {
                        PyValueError::new_err(format!(
                            "output gate operation {operation_index} failed: {error:?}"
                        ))
                    })?;
                if !deliverable.is_empty() {
                    deliveries.push(json!({
                        "operationIndex": operation_index,
                        "draft": true,
                        "chunks": deliverable
                            .iter()
                            .map(|chunk| json!({
                                "streamId": chunk.stream_id,
                                "responseId": chunk.response_id,
                                "sequence": chunk.sequence,
                                "text": chunk.text,
                            }))
                            .collect::<Vec<_>>(),
                    }));
                }
            }
            "commit" => {
                let deliverable = gate.commit_accepted_output();
                if !deliverable.is_empty() {
                    deliveries.push(json!({
                        "operationIndex": operation_index,
                        "chunks": deliverable
                            .iter()
                            .map(|chunk| json!({
                                "streamId": chunk.stream_id,
                                "responseId": chunk.response_id,
                                "sequence": chunk.sequence,
                                "text": chunk.text,
                            }))
                            .collect::<Vec<_>>(),
                    }));
                }
            }
            "decision" => {
                let label = format!("operations[{operation_index}]");
                let decision_id =
                    required_alias_string(operation, "decisionId", "decision_id", &label)?;
                let input_digest =
                    required_alias_string(operation, "inputDigest", "input_digest", &label)?;
                let disposition = required_string(operation, "disposition", &label)?;
                let accepted_through_sequence = optional_alias_u64(
                    operation,
                    "acceptedThroughSequence",
                    "accepted_through_sequence",
                    &label,
                )?;
                let mut decision = match disposition {
                    "allow" => OutputPolicyDecision::allow(
                        decision_id,
                        accepted_through_sequence,
                        input_digest,
                    ),
                    "hold" => OutputPolicyDecision::hold(decision_id, input_digest),
                    "redact" | "replace" => {
                        let mut replacement_chunks = Vec::new();
                        let replacement_parts = operation
                            .get("replacementParts")
                            .or_else(|| operation.get("replacement_parts"));
                        let replacement_chunks_value = operation
                            .get("replacementChunks")
                            .or_else(|| operation.get("replacement_chunks"));
                        if replacement_parts.is_some() && replacement_chunks_value.is_some() {
                            return Err(PyValueError::new_err(format!(
                                "{label} must not specify both replacementParts and replacementChunks"
                            )));
                        }
                        if let Some(parts) = replacement_parts {
                            let Some(parts) = parts.as_array() else {
                                return Err(PyValueError::new_err(format!(
                                    "{label}.replacementParts must be an array"
                                )));
                            };
                            let base_sequence = accepted_through_sequence
                                .unwrap_or_else(|| gate.last_generated_sequence());
                            for (part_index, part) in parts.iter().enumerate() {
                                let part_label = format!("{label}.replacementParts[{part_index}]");
                                let part = json_object(part, &part_label)?;
                                let kind = required_string(part, "kind", &part_label)?;
                                if kind != "text" {
                                    return Err(PyValueError::new_err(format!(
                                        "{part_label}.kind must be \"text\""
                                    )));
                                }
                                let sequence = base_sequence
                                    .checked_add(part_index as u64)
                                    .ok_or_else(|| {
                                        PyValueError::new_err(format!(
                                            "{part_label} replacement sequence overflowed"
                                        ))
                                    })?;
                                replacement_chunks.push(GenerationChunk::text(
                                    stream_id,
                                    response_id,
                                    sequence,
                                    required_string(part, "text", &part_label)?,
                                ));
                            }
                        } else if let Some(chunks) = replacement_chunks_value {
                            let Some(chunks) = chunks.as_array() else {
                                return Err(PyValueError::new_err(format!(
                                    "{label}.replacementChunks must be an array"
                                )));
                            };
                            for (chunk_index, chunk) in chunks.iter().enumerate() {
                                let chunk = json_object(
                                    chunk,
                                    &format!("{label}.replacementChunks[{chunk_index}]"),
                                )?;
                                let chunk_label =
                                    format!("{label}.replacementChunks[{chunk_index}]");
                                replacement_chunks.push(GenerationChunk::text(
                                    optional_alias_string(
                                        chunk,
                                        "streamId",
                                        "stream_id",
                                        &chunk_label,
                                    )?
                                    .unwrap_or(stream_id),
                                    optional_alias_string(
                                        chunk,
                                        "responseId",
                                        "response_id",
                                        &chunk_label,
                                    )?
                                    .unwrap_or(response_id),
                                    required_u64(chunk, "sequence", &chunk_label)?,
                                    required_string(chunk, "text", &chunk_label)?,
                                ));
                            }
                        }
                        if disposition == "redact" {
                            OutputPolicyDecision::redact(
                                decision_id,
                                accepted_through_sequence,
                                replacement_chunks,
                                input_digest,
                            )
                        } else {
                            OutputPolicyDecision::replace(
                                decision_id,
                                accepted_through_sequence,
                                replacement_chunks,
                                input_digest,
                            )
                        }
                    }
                    "abort_response" => {
                        let decision =
                            OutputPolicyDecision::abort_response(decision_id, input_digest);
                        if let Some(sequence) = accepted_through_sequence {
                            decision.with_accepted_through_sequence(sequence)
                        } else {
                            decision
                        }
                    }
                    "abort_turn" => {
                        let decision = OutputPolicyDecision::abort_turn(decision_id, input_digest);
                        if let Some(sequence) = accepted_through_sequence {
                            decision.with_accepted_through_sequence(sequence)
                        } else {
                            decision
                        }
                    }
                    "deny_commit" => {
                        let decision = OutputPolicyDecision::deny_commit(decision_id, input_digest);
                        if let Some(sequence) = accepted_through_sequence {
                            decision.with_accepted_through_sequence(sequence)
                        } else {
                            decision
                        }
                    }
                    value => {
                        return Err(PyValueError::new_err(format!(
                            "{label}.disposition has unknown disposition {value:?}"
                        )));
                    }
                };
                if let Some(redactions) = operation.get("redactions") {
                    let Some(redactions) = redactions.as_array() else {
                        return Err(PyValueError::new_err(format!(
                            "{label}.redactions must be an array"
                        )));
                    };
                    let mut parsed_redactions = Vec::new();
                    for (redaction_index, redaction) in redactions.iter().enumerate() {
                        let redaction_label = format!("{label}.redactions[{redaction_index}]");
                        let redaction = json_object(redaction, &redaction_label)?;
                        parsed_redactions.push(RedactionInstruction::text_range(
                            required_string(redaction, "path", &redaction_label)?,
                            required_u64(redaction, "start", &redaction_label)?,
                            required_u64(redaction, "end", &redaction_label)?,
                            required_string(redaction, "replacement", &redaction_label)?,
                        ));
                    }
                    decision = decision.with_redactions(parsed_redactions);
                }
                if let Some(values) = operation
                    .get("reasonCodes")
                    .or_else(|| operation.get("reason_codes"))
                {
                    let Some(values) = values.as_array() else {
                        return Err(PyValueError::new_err(format!(
                            "{label}.reasonCodes must be an array"
                        )));
                    };
                    let mut parsed_reason_codes = Vec::new();
                    for (value_index, value) in values.iter().enumerate() {
                        let Some(value) = value.as_str() else {
                            return Err(PyValueError::new_err(format!(
                                "{label}.reasonCodes[{value_index}] must be a string"
                            )));
                        };
                        parsed_reason_codes.push(value.to_owned());
                    }
                    decision = decision.with_reason_codes(parsed_reason_codes);
                }
                if let Some(values) = operation
                    .get("policyRefs")
                    .or_else(|| operation.get("policy_refs"))
                {
                    let Some(values) = values.as_array() else {
                        return Err(PyValueError::new_err(format!(
                            "{label}.policyRefs must be an array"
                        )));
                    };
                    let mut parsed_policy_refs = Vec::new();
                    for (value_index, value) in values.iter().enumerate() {
                        let Some(value) = value.as_str() else {
                            return Err(PyValueError::new_err(format!(
                                "{label}.policyRefs[{value_index}] must be a string"
                            )));
                        };
                        parsed_policy_refs.push(value.to_owned());
                    }
                    decision = decision.with_policy_refs(parsed_policy_refs);
                }
                if let Some(value) = operation
                    .get("providerCancellation")
                    .or_else(|| operation.get("provider_cancellation"))
                {
                    let Some(value) = value.as_str() else {
                        return Err(PyValueError::new_err(format!(
                            "{label}.providerCancellation must be a string"
                        )));
                    };
                    decision = decision.with_provider_cancellation(match value {
                        "none" => ProviderCancellation::None,
                        "request" => ProviderCancellation::Request,
                        "required_if_supported" => ProviderCancellation::RequiredIfSupported,
                        value => {
                            return Err(PyValueError::new_err(format!(
                                "{label}.providerCancellation has unknown mode {value:?}"
                            )));
                        }
                    });
                }
                if let Some(value) = operation
                    .get("draftDisposition")
                    .or_else(|| operation.get("draft_disposition"))
                {
                    let Some(value) = value.as_str() else {
                        return Err(PyValueError::new_err(format!(
                            "{label}.draftDisposition must be a string"
                        )));
                    };
                    decision = decision.with_draft_disposition(match value {
                        "keep" => DraftDisposition::Keep,
                        "mark_incomplete" => DraftDisposition::MarkIncomplete,
                        "retract" => DraftDisposition::Retract,
                        value => {
                            return Err(PyValueError::new_err(format!(
                                "{label}.draftDisposition has unknown disposition {value:?}"
                            )));
                        }
                    });
                }
                if let Some(value) = operation
                    .get("pendingToolCalls")
                    .or_else(|| operation.get("pending_tool_calls"))
                {
                    let Some(value) = value.as_str() else {
                        return Err(PyValueError::new_err(format!(
                            "{label}.pendingToolCalls must be a string"
                        )));
                    };
                    decision = decision.with_pending_tool_calls(match value {
                        "keep" => PendingToolCallsDisposition::Keep,
                        "deny" => PendingToolCallsDisposition::Deny,
                        "cancel_admitted" => PendingToolCallsDisposition::CancelAdmitted,
                        value => {
                            return Err(PyValueError::new_err(format!(
                                "{label}.pendingToolCalls has unknown disposition {value:?}"
                            )));
                        }
                    });
                }
                if let Some(value) = optional_alias_u64(
                    operation,
                    "evaluatedAtUnixMs",
                    "evaluated_at_unix_ms",
                    &label,
                )? {
                    decision = decision.evaluated_at_unix_ms(value);
                }
                let occurred_at_unix_ms = required_alias_u64(
                    operation,
                    "occurredAtUnixMs",
                    "occurred_at_unix_ms",
                    &label,
                )?;
                let decision_trace = json!({
                    "operationIndex": operation_index,
                    "decisionId": decision.decision_id.as_str(),
                    "disposition": disposition,
                    "acceptedThroughSequence": decision.accepted_through_sequence,
                    "reasonCodes": &decision.reason_codes,
                    "policyRefs": &decision.policy_refs,
                    "providerCancellation": match decision.provider_cancellation {
                        ProviderCancellation::None => "none",
                        ProviderCancellation::Request => "request",
                        ProviderCancellation::RequiredIfSupported => "required_if_supported",
                    },
                    "draftDisposition": match decision.draft_disposition {
                        DraftDisposition::Keep => "keep",
                        DraftDisposition::MarkIncomplete => "mark_incomplete",
                        DraftDisposition::Retract => "retract",
                    },
                    "pendingToolCalls": match decision.pending_tool_calls {
                        PendingToolCallsDisposition::Keep => "keep",
                        PendingToolCallsDisposition::Deny => "deny",
                        PendingToolCallsDisposition::CancelAdmitted => "cancel_admitted",
                    },
                    "evaluatedAtUnixMs": decision.evaluated_at_unix_ms,
                    "occurredAtUnixMs": occurred_at_unix_ms,
                    "inputDigest": decision.input_digest.as_str(),
                });
                let update =
                    gate.apply_decision(decision, occurred_at_unix_ms)
                        .map_err(|error| {
                            PyValueError::new_err(format!(
                                "output gate operation {operation_index} failed: {error:?}"
                            ))
                        })?;
                decisions.push(decision_trace);
                if !update.deliverable.is_empty() || update.cutoff.is_some() {
                    let cutoff = update.cutoff.as_ref().map(|cutoff| {
                        json!({
                            "streamId": cutoff.stream_id,
                            "responseId": cutoff.response_id,
                            "turnId": cutoff.turn_id,
                            "lastGeneratedSequence": cutoff.last_generated_sequence,
                            "lastPolicyAcceptedSequence": cutoff.last_policy_accepted_sequence,
                            "lastClientDeliveredSequence": cutoff.last_client_delivered_sequence,
                            "terminalReason": match cutoff.terminal_reason {
                                graphblocks_runtime_core::output_policy::TerminalReason::PolicyDenied => "policy_denied",
                                graphblocks_runtime_core::output_policy::TerminalReason::BudgetExhausted => "budget_exhausted",
                                graphblocks_runtime_core::output_policy::TerminalReason::Cancelled => "cancelled",
                                graphblocks_runtime_core::output_policy::TerminalReason::ClientDisconnected => "client_disconnected",
                            },
                            "draftDisposition": match cutoff.draft_disposition {
                                DraftDisposition::Keep => "keep",
                                DraftDisposition::MarkIncomplete => "mark_incomplete",
                                DraftDisposition::Retract => "retract",
                            },
                            "durableResult": match cutoff.durable_result {
                                graphblocks_runtime_core::output_policy::DurableResult::None => "none",
                                graphblocks_runtime_core::output_policy::DurableResult::Incomplete => "incomplete",
                                graphblocks_runtime_core::output_policy::DurableResult::Partial => "partial",
                            },
                            "policyDecisionId": cutoff.policy_decision_id,
                            "occurredAtUnixMs": cutoff.occurred_at_unix_ms,
                        })
                    });
                    deliveries.push(json!({
                        "operationIndex": operation_index,
                        "chunks": update
                            .deliverable
                            .iter()
                            .map(|chunk| json!({
                                "streamId": chunk.stream_id,
                                "responseId": chunk.response_id,
                                "sequence": chunk.sequence,
                                "text": chunk.text,
                            }))
                            .collect::<Vec<_>>(),
                        "cutoff": cutoff,
                        "pendingToolCalls": update.pending_tool_calls.map(|disposition| match disposition {
                            PendingToolCallsDisposition::Keep => "keep",
                            PendingToolCallsDisposition::Deny => "deny",
                            PendingToolCallsDisposition::CancelAdmitted => "cancel_admitted",
                        }),
                        "providerCancellation": update.provider_cancellation.map(|cancellation| match cancellation {
                            ProviderCancellation::None => "none",
                            ProviderCancellation::Request => "request",
                            ProviderCancellation::RequiredIfSupported => "required_if_supported",
                        }),
                    }));
                }
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "operations[{operation_index}].kind has unknown kind {value:?}"
                )));
            }
        }
    }

    let cutoff = gate.cutoff().map(|cutoff| {
        json!({
            "streamId": cutoff.stream_id,
            "responseId": cutoff.response_id,
            "turnId": cutoff.turn_id,
            "lastGeneratedSequence": cutoff.last_generated_sequence,
            "lastPolicyAcceptedSequence": cutoff.last_policy_accepted_sequence,
            "lastClientDeliveredSequence": cutoff.last_client_delivered_sequence,
            "terminalReason": match cutoff.terminal_reason {
                graphblocks_runtime_core::output_policy::TerminalReason::PolicyDenied => "policy_denied",
                graphblocks_runtime_core::output_policy::TerminalReason::BudgetExhausted => "budget_exhausted",
                graphblocks_runtime_core::output_policy::TerminalReason::Cancelled => "cancelled",
                graphblocks_runtime_core::output_policy::TerminalReason::ClientDisconnected => "client_disconnected",
            },
            "draftDisposition": match cutoff.draft_disposition {
                DraftDisposition::Keep => "keep",
                DraftDisposition::MarkIncomplete => "mark_incomplete",
                DraftDisposition::Retract => "retract",
            },
            "durableResult": match cutoff.durable_result {
                graphblocks_runtime_core::output_policy::DurableResult::None => "none",
                graphblocks_runtime_core::output_policy::DurableResult::Incomplete => "incomplete",
                graphblocks_runtime_core::output_policy::DurableResult::Partial => "partial",
            },
            "policyDecisionId": cutoff.policy_decision_id,
            "occurredAtUnixMs": cutoff.occurred_at_unix_ms,
        })
    });
    let payload = json!({
        "deliveries": deliveries,
        "decisions": decisions,
        "cutoff": cutoff,
        "pending": gate.pending_chunks()
            .map(|chunk| json!({
                "streamId": chunk.stream_id,
                "responseId": chunk.response_id,
                "sequence": chunk.sequence,
                "text": chunk.text,
            }))
            .collect::<Vec<_>>(),
        "lastGeneratedSequence": gate.last_generated_sequence(),
        "lastPolicyAcceptedSequence": gate.last_policy_accepted_sequence(),
        "lastClientDeliveredSequence": gate.last_client_delivered_sequence(),
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!("failed to serialize output gate result: {error}"))
    })
}

#[pyfunction]
fn evaluate_declarative_output_policy_json(
    rules_json: &str,
    chunk_json: &str,
    evaluated_at_unix_ms: u64,
) -> PyResult<String> {
    let rules_value = parse_json_argument(rules_json, "declarative output policy rules")?;
    let chunk_value = parse_json_argument(chunk_json, "generation chunk")?;
    let rules_array = rules_value.as_array().ok_or_else(|| {
        PyValueError::new_err("declarative output policy rules JSON must be an array")
    })?;
    let parse_string_list = |value: Option<&Value>, label: &str| -> PyResult<Vec<String>> {
        let Some(value) = value else {
            return Ok(Vec::new());
        };
        let Some(values) = value.as_array() else {
            return Err(PyValueError::new_err(format!("{label} must be an array")));
        };
        let mut parsed = Vec::new();
        for (index, value) in values.iter().enumerate() {
            let Some(value) = value.as_str() else {
                return Err(PyValueError::new_err(format!(
                    "{label}[{index}] must be a string"
                )));
            };
            parsed.push(value.to_owned());
        }
        Ok(parsed)
    };

    let mut rules = Vec::new();
    for (rule_index, rule) in rules_array.iter().enumerate() {
        let label = format!("rules[{rule_index}]");
        let rule = json_object(rule, &label)?;
        let rule_id = rule
            .get("ruleId")
            .or_else(|| rule.get("rule_id"))
            .and_then(Value::as_str)
            .ok_or_else(|| PyValueError::new_err(format!("{label}.ruleId is required")))?;
        let literal = required_string(rule, "literal", &label)?;
        let disposition = match required_string(rule, "disposition", &label)? {
            "allow" => OutputDisposition::Allow,
            "hold" => OutputDisposition::Hold,
            "redact" => OutputDisposition::Redact,
            "replace" => OutputDisposition::Replace,
            "abort_response" => OutputDisposition::AbortResponse,
            "abort_turn" => OutputDisposition::AbortTurn,
            "deny_commit" => OutputDisposition::DenyCommit,
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.disposition has unknown disposition {value:?}"
                )));
            }
        };
        let mut parsed_rule = DeclarativeOutputPolicyRule::new(rule_id, literal, disposition);
        if let Some(replacement) = rule.get("replacement") {
            let Some(replacement) = replacement.as_str() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.replacement must be a string"
                )));
            };
            parsed_rule = parsed_rule.with_replacement(replacement);
        }
        parsed_rule = parsed_rule.with_reason_codes(parse_string_list(
            rule.get("reasonCodes"),
            &format!("{label}.reasonCodes"),
        )?);
        parsed_rule = parsed_rule.with_policy_refs(parse_string_list(
            rule.get("policyRefs"),
            &format!("{label}.policyRefs"),
        )?);
        if let Some(priority) = rule.get("priority") {
            let Some(priority) = priority.as_i64() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.priority must be an integer"
                )));
            };
            parsed_rule = parsed_rule.with_priority(priority);
        }
        rules.push(parsed_rule);
    }

    let chunk = json_object(&chunk_value, "chunk")?;
    let chunk = GenerationChunk::text(
        required_string(chunk, "streamId", "chunk")?,
        required_string(chunk, "responseId", "chunk")?,
        required_u64(chunk, "sequence", "chunk")?,
        required_string(chunk, "text", "chunk")?,
    );
    let decision =
        DeclarativeOutputPolicyEvaluator::new(rules).evaluate_chunk(&chunk, evaluated_at_unix_ms);
    let disposition = match decision.disposition {
        OutputDisposition::Allow => "allow",
        OutputDisposition::Hold => "hold",
        OutputDisposition::Redact => "redact",
        OutputDisposition::Replace => "replace",
        OutputDisposition::AbortResponse => "abort_response",
        OutputDisposition::AbortTurn => "abort_turn",
        OutputDisposition::DenyCommit => "deny_commit",
    };
    let provider_cancellation = match decision.provider_cancellation {
        ProviderCancellation::None => "none",
        ProviderCancellation::Request => "request",
        ProviderCancellation::RequiredIfSupported => "required_if_supported",
    };
    let draft_disposition = match decision.draft_disposition {
        DraftDisposition::Keep => "keep",
        DraftDisposition::MarkIncomplete => "mark_incomplete",
        DraftDisposition::Retract => "retract",
    };
    let pending_tool_calls = match decision.pending_tool_calls {
        PendingToolCallsDisposition::Keep => "keep",
        PendingToolCallsDisposition::Deny => "deny",
        PendingToolCallsDisposition::CancelAdmitted => "cancel_admitted",
    };
    let payload = json!({
        "decisionId": decision.decision_id,
        "disposition": disposition,
        "acceptedThroughSequence": decision.accepted_through_sequence,
        "replacementParts": decision
            .replacement_chunks
            .iter()
            .map(|chunk| json!({
                "kind": "text",
                "text": chunk.text,
                "metadata": {},
            }))
            .collect::<Vec<_>>(),
        "replacementChunks": decision
            .replacement_chunks
            .iter()
            .map(|chunk| json!({
                "streamId": chunk.stream_id,
                "responseId": chunk.response_id,
                "sequence": chunk.sequence,
                "text": chunk.text,
            }))
            .collect::<Vec<_>>(),
        "redactions": decision
            .redactions
            .iter()
            .map(|redaction| json!({
                "path": redaction.path,
                "start": redaction.start,
                "end": redaction.end,
                "replacement": redaction.replacement,
            }))
            .collect::<Vec<_>>(),
        "reasonCodes": decision.reason_codes,
        "policyRefs": decision.policy_refs,
        "providerCancellation": provider_cancellation,
        "draftDisposition": draft_disposition,
        "pendingToolCalls": pending_tool_calls,
        "evaluatedAtUnixMs": decision.evaluated_at_unix_ms,
        "inputDigest": decision.input_digest,
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize declarative output policy decision: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_tool_execution_plan_json(plan_json: &str, operations_json: &str) -> PyResult<String> {
    let tool_execution_error_code = |error: &ToolExecutionPlanError| -> &'static str {
        match error {
            ToolExecutionPlanError::UnsafeParallelEffects { .. } => "unsafe_parallel_effects",
            ToolExecutionPlanError::EffectConflict { .. } => "effect_conflict",
            ToolExecutionPlanError::ParallelismExhausted => "parallelism_exhausted",
            ToolExecutionPlanError::DependenciesNotReady { .. } => "dependencies_not_ready",
            ToolExecutionPlanError::ToolCallNotPending { .. } => "tool_call_not_pending",
            ToolExecutionPlanError::ToolCallNotRunning { .. } => "tool_call_not_running",
            _ => "tool_execution_plan_error",
        }
    };
    let tool_execution_plan_error = |error: ToolExecutionPlanError| {
        PyValueError::new_err(format!(
            "tool execution plan error {}: {error:?}",
            tool_execution_error_code(&error)
        ))
    };
    let operation_result =
        |index: usize, op: &str, result: Result<(), ToolExecutionPlanError>| -> Value {
            match result {
                Ok(()) => json!({"index": index, "op": op, "error": Value::Null}),
                Err(error) => json!({
                    "index": index,
                    "op": op,
                    "error": tool_execution_error_code(&error),
                    "errorDebug": format!("{error:?}"),
                }),
            }
        };

    let plan_value = parse_json_argument(plan_json, "tool execution plan")?;
    let operations_value = parse_json_argument(operations_json, "tool execution operations")?;
    let plan_object = json_object(&plan_value, "plan")?;
    let plan_id = required_alias_string(plan_object, "planId", "plan_id", "plan")?;
    let response_id = required_alias_string(plan_object, "responseId", "response_id", "plan")?;
    let maximum_parallelism = u64_to_usize(
        required_alias_u64(
            plan_object,
            "maximumParallelism",
            "maximum_parallelism",
            "plan",
        )?,
        "plan.maximumParallelism",
    )?;
    let effect_key_template = optional_nullable_alias_string(
        plan_object,
        "effectKeyTemplate",
        "effect_key_template",
        "plan",
    )?;
    let raw_calls = alias_value(plan_object, "calls", "calls")
        .and_then(Value::as_array)
        .ok_or_else(|| PyValueError::new_err("plan.calls must be an array"))?;
    let planned_calls = raw_calls
        .iter()
        .enumerate()
        .map(|(index, raw_call)| {
            let label = format!("plan.calls[{index}]");
            let object = json_object(raw_call, &label)?;
            let arguments = object
                .get("arguments")
                .cloned()
                .ok_or_else(|| PyValueError::new_err(format!("{label}.arguments is required")))?;
            let mut draft = ToolCallDraft::proposed(
                response_id,
                required_alias_string(object, "toolCallId", "tool_call_id", &label)?,
                required_alias_string(object, "toolName", "tool_name", &label)?,
            );
            draft
                .append_argument_fragment(arguments.to_string())
                .map_err(|error| {
                    PyValueError::new_err(format!("{label}.arguments is invalid: {error:?}"))
                })?;
            let mut call = draft
                .into_completed_tool_call("resolved-tool-1", 1_000)
                .map_err(|error| {
                    PyValueError::new_err(format!("{label} could not be finalized: {error:?}"))
                })?;
            if let Some(depends_on) = alias_value(object, "dependsOn", "depends_on") {
                let Some(depends_on) = depends_on.as_array() else {
                    return Err(PyValueError::new_err(format!(
                        "{label}.dependsOn must be an array"
                    )));
                };
                call.depends_on = depends_on
                    .iter()
                    .enumerate()
                    .map(|(index, dependency)| {
                        dependency.as_str().map(str::to_owned).ok_or_else(|| {
                            PyValueError::new_err(format!(
                                "{label}.dependsOn[{index}] must be a string"
                            ))
                        })
                    })
                    .collect::<PyResult<Vec<_>>>()?;
            }

            let mut planned_call = ToolPlanCall::new(call).with_effects(parse_tool_effects(
                alias_value(object, "effects", "effects"),
                &format!("{label}.effects"),
            )?);
            if let Some(cancellation) =
                optional_nullable_alias_string(object, "cancellation", "cancellation", &label)?
            {
                planned_call = planned_call.with_cancellation(parse_tool_cancellation(
                    cancellation,
                    &format!("{label}.cancellation"),
                )?);
            }
            if let Some(effect_key) =
                optional_nullable_alias_string(object, "effectKey", "effect_key", &label)?
            {
                planned_call = planned_call.with_effect_key(effect_key);
            } else if let Some(template) = effect_key_template {
                planned_call = planned_call
                    .with_effect_key_template(template)
                    .map_err(&tool_execution_plan_error)?;
            }
            Ok(planned_call)
        })
        .collect::<PyResult<Vec<_>>>()?;
    let mut plan = ToolExecutionPlan::new(plan_id, response_id, planned_calls, maximum_parallelism)
        .map_err(&tool_execution_plan_error)?;
    if let Some(failure_policy) =
        optional_nullable_alias_string(plan_object, "failurePolicy", "failure_policy", "plan")?
    {
        plan = plan.with_failure_policy(match failure_policy {
            "fail_fast" => ToolExecutionFailurePolicy::FailFast,
            "collect" => ToolExecutionFailurePolicy::Collect,
            "return_failures_to_model" => ToolExecutionFailurePolicy::ReturnFailuresToModel,
            value => {
                return Err(PyValueError::new_err(format!(
                    "plan.failurePolicy has unknown failure policy {value:?}"
                )));
            }
        });
    }
    if let Some(cancellation_policy) = optional_nullable_alias_string(
        plan_object,
        "cancellationPolicy",
        "cancellation_policy",
        "plan",
    )? {
        plan = plan.with_cancellation_policy(match cancellation_policy {
            "cancel_dependents" => ToolExecutionCancellationPolicy::CancelDependents,
            "cancel_all" => ToolExecutionCancellationPolicy::CancelAll,
            "allow_independent_calls" => ToolExecutionCancellationPolicy::AllowIndependentCalls,
            value => {
                return Err(PyValueError::new_err(format!(
                    "plan.cancellationPolicy has unknown cancellation policy {value:?}"
                )));
            }
        });
    }

    let operations = operations_value
        .as_array()
        .ok_or_else(|| PyValueError::new_err("tool execution operations must be an array"))?;
    let mut operation_results = Vec::new();
    for (index, operation) in operations.iter().enumerate() {
        let label = format!("operations[{index}]");
        let operation = json_object(operation, &label)?;
        let op = required_string(operation, "op", &label)?;
        let result = match op {
            "ready" => json!({
                "index": index,
                "op": op,
                "ready": plan.ready_call_ids(),
            }),
            "start" => operation_result(
                index,
                op,
                plan.record_started(required_alias_string(
                    operation,
                    "toolCallId",
                    "tool_call_id",
                    &label,
                )?),
            ),
            "complete" => operation_result(
                index,
                op,
                plan.record_completed(required_alias_string(
                    operation,
                    "toolCallId",
                    "tool_call_id",
                    &label,
                )?),
            ),
            "fail" => operation_result(
                index,
                op,
                plan.record_failed(required_alias_string(
                    operation,
                    "toolCallId",
                    "tool_call_id",
                    &label,
                )?),
            ),
            "deny" => operation_result(
                index,
                op,
                plan.record_denied(required_alias_string(
                    operation,
                    "toolCallId",
                    "tool_call_id",
                    &label,
                )?),
            ),
            "expire" => operation_result(
                index,
                op,
                plan.record_expired(required_alias_string(
                    operation,
                    "toolCallId",
                    "tool_call_id",
                    &label,
                )?),
            ),
            "cancel" => operation_result(
                index,
                op,
                plan.record_cancelled(required_alias_string(
                    operation,
                    "toolCallId",
                    "tool_call_id",
                    &label,
                )?),
            ),
            "policy_stopped" => operation_result(
                index,
                op,
                plan.record_policy_stopped(required_alias_string(
                    operation,
                    "toolCallId",
                    "tool_call_id",
                    &label,
                )?),
            ),
            "policy_stop" => {
                let pending_tool_calls = required_alias_string(
                    operation,
                    "pendingToolCalls",
                    "pending_tool_calls",
                    &label,
                )?;
                let affected = plan.apply_policy_stop(match pending_tool_calls {
                    "keep" => PendingToolCallsDisposition::Keep,
                    "deny" => PendingToolCallsDisposition::Deny,
                    "cancel_admitted" => PendingToolCallsDisposition::CancelAdmitted,
                    value => {
                        return Err(PyValueError::new_err(format!(
                            "{label}.pendingToolCalls has unknown pending tool calls disposition {value:?}"
                        )));
                    }
                });
                json!({
                    "index": index,
                    "op": op,
                    "affected": affected,
                })
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.op has unknown tool execution operation {value:?}"
                )));
            }
        };
        operation_results.push(result);
    }

    let states = raw_calls
        .iter()
        .enumerate()
        .map(|(index, raw_call)| {
            let label = format!("plan.calls[{index}]");
            let call = json_object(raw_call, &label)?;
            let tool_call_id = required_alias_string(call, "toolCallId", "tool_call_id", &label)?;
            Ok((
                tool_call_id.to_owned(),
                plan.state(tool_call_id).map(|state| match state {
                    ToolExecutionState::Pending => "pending",
                    ToolExecutionState::Running => "running",
                    ToolExecutionState::Completed => "completed",
                    ToolExecutionState::Failed => "failed",
                    ToolExecutionState::Denied => "denied",
                    ToolExecutionState::Cancelled => "cancelled",
                    ToolExecutionState::PolicyStopped => "policy_stopped",
                    ToolExecutionState::Expired => "expired",
                    ToolExecutionState::Skipped => "skipped",
                }),
            ))
        })
        .collect::<PyResult<BTreeMap<_, _>>>()?;
    let failure_policy = match plan.failure_policy {
        ToolExecutionFailurePolicy::FailFast => "fail_fast",
        ToolExecutionFailurePolicy::Collect => "collect",
        ToolExecutionFailurePolicy::ReturnFailuresToModel => "return_failures_to_model",
    };
    let cancellation_policy = match plan.cancellation_policy {
        ToolExecutionCancellationPolicy::CancelDependents => "cancel_dependents",
        ToolExecutionCancellationPolicy::CancelAll => "cancel_all",
        ToolExecutionCancellationPolicy::AllowIndependentCalls => "allow_independent_calls",
    };
    serde_json::to_string(&json!({
        "planId": plan.plan_id,
        "responseId": plan.response_id,
        "maximumParallelism": plan.maximum_parallelism,
        "failurePolicy": failure_policy,
        "cancellationPolicy": cancellation_policy,
        "ready": plan.ready_call_ids(),
        "operations": operation_results,
        "states": states,
    }))
    .map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize tool execution plan evaluation: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_sequential_tool_queue_json(
    queue_json: &str,
    operations_json: &str,
) -> PyResult<String> {
    let tool_execution_error_code = |error: &ToolExecutionPlanError| -> &'static str {
        match error {
            ToolExecutionPlanError::UnsafeParallelEffects { .. } => "unsafe_parallel_effects",
            ToolExecutionPlanError::EffectConflict { .. } => "effect_conflict",
            ToolExecutionPlanError::ParallelismExhausted => "parallelism_exhausted",
            ToolExecutionPlanError::DependenciesNotReady { .. } => "dependencies_not_ready",
            ToolExecutionPlanError::ToolCallNotPending { .. } => "tool_call_not_pending",
            ToolExecutionPlanError::ToolCallNotRunning { .. } => "tool_call_not_running",
            _ => "tool_execution_plan_error",
        }
    };
    let queue_error_code = |error: &SequentialToolQueueError| -> &'static str {
        match error {
            SequentialToolQueueError::ToolCallNotAdmitted { .. } => "tool_call_not_admitted",
            SequentialToolQueueError::ToolCallMissingAdmissionTimestamp { .. } => {
                "tool_call_missing_admission_timestamp"
            }
            SequentialToolQueueError::RunningCallMismatch { .. } => "running_call_mismatch",
            SequentialToolQueueError::Plan(error) => tool_execution_error_code(error),
        }
    };
    let queue_error_to_py = |error: SequentialToolQueueError| {
        PyValueError::new_err(format!(
            "sequential tool queue error {}: {error:?}",
            queue_error_code(&error)
        ))
    };
    let operation_result =
        |index: usize, op: &str, result: Result<(), SequentialToolQueueError>| -> Value {
            match result {
                Ok(()) => json!({"index": index, "op": op, "error": Value::Null}),
                Err(error) => json!({
                    "index": index,
                    "op": op,
                    "error": queue_error_code(&error),
                    "errorDebug": format!("{error:?}"),
                }),
            }
        };
    let start_result = |index: usize,
                        op: &str,
                        result: Result<Option<String>, SequentialToolQueueError>|
     -> Value {
        match result {
            Ok(started) => {
                json!({"index": index, "op": op, "started": started, "error": Value::Null})
            }
            Err(error) => json!({
                "index": index,
                "op": op,
                "started": Value::Null,
                "error": queue_error_code(&error),
                "errorDebug": format!("{error:?}"),
            }),
        }
    };

    let queue_value = parse_json_argument(queue_json, "sequential tool queue")?;
    let operations_value =
        parse_json_argument(operations_json, "sequential tool queue operations")?;
    let queue_object = json_object(&queue_value, "queue")?;
    let plan_id = required_alias_string(queue_object, "planId", "plan_id", "queue")?;
    let response_id = required_alias_string(queue_object, "responseId", "response_id", "queue")?;
    let raw_calls = alias_value(queue_object, "calls", "calls")
        .and_then(Value::as_array)
        .ok_or_else(|| PyValueError::new_err("queue.calls must be an array"))?;
    let planned_calls = raw_calls
        .iter()
        .enumerate()
        .map(|(index, raw_call)| {
            let label = format!("queue.calls[{index}]");
            let object = json_object(raw_call, &label)?;
            let (call_value, call_label) = if let Some(call_value) = object.get("call") {
                (call_value, format!("{label}.call"))
            } else {
                (raw_call, label.clone())
            };
            let mut planned_call = ToolPlanCall::new(parse_tool_call(call_value, &call_label)?)
                .with_effects(parse_tool_effects(
                    alias_value(object, "effects", "effects"),
                    &format!("{label}.effects"),
                )?);
            if let Some(cancellation) =
                optional_nullable_alias_string(object, "cancellation", "cancellation", &label)?
            {
                planned_call = planned_call.with_cancellation(parse_tool_cancellation(
                    cancellation,
                    &format!("{label}.cancellation"),
                )?);
            }
            if let Some(effect_key) =
                optional_nullable_alias_string(object, "effectKey", "effect_key", &label)?
            {
                planned_call = planned_call.with_effect_key(effect_key);
            }
            Ok(planned_call)
        })
        .collect::<PyResult<Vec<_>>>()?;
    let mut queue = SequentialToolQueue::new(plan_id, response_id, planned_calls)
        .map_err(|error| queue_error_to_py(error))?;
    if let Some(failure_policy) =
        optional_nullable_alias_string(queue_object, "failurePolicy", "failure_policy", "queue")?
    {
        queue = queue.with_failure_policy(match failure_policy {
            "fail_fast" => ToolExecutionFailurePolicy::FailFast,
            "collect" => ToolExecutionFailurePolicy::Collect,
            "return_failures_to_model" => ToolExecutionFailurePolicy::ReturnFailuresToModel,
            value => {
                return Err(PyValueError::new_err(format!(
                    "queue.failurePolicy has unknown failure policy {value:?}"
                )));
            }
        });
    }
    if let Some(cancellation_policy) = optional_nullable_alias_string(
        queue_object,
        "cancellationPolicy",
        "cancellation_policy",
        "queue",
    )? {
        queue = queue.with_cancellation_policy(match cancellation_policy {
            "cancel_dependents" => ToolExecutionCancellationPolicy::CancelDependents,
            "cancel_all" => ToolExecutionCancellationPolicy::CancelAll,
            "allow_independent_calls" => ToolExecutionCancellationPolicy::AllowIndependentCalls,
            value => {
                return Err(PyValueError::new_err(format!(
                    "queue.cancellationPolicy has unknown cancellation policy {value:?}"
                )));
            }
        });
    }

    let operations = operations_value.as_array().ok_or_else(|| {
        PyValueError::new_err("sequential tool queue operations must be an array")
    })?;
    let mut operation_results = Vec::new();
    for (index, operation) in operations.iter().enumerate() {
        let label = format!("operations[{index}]");
        let operation = json_object(operation, &label)?;
        let op = required_string(operation, "op", &label)?;
        let result = match op {
            "start_next_ready" => start_result(index, op, queue.start_next_ready()),
            "complete" => operation_result(
                index,
                op,
                queue.record_completed(required_alias_string(
                    operation,
                    "toolCallId",
                    "tool_call_id",
                    &label,
                )?),
            ),
            "fail" => operation_result(
                index,
                op,
                queue.record_failed(required_alias_string(
                    operation,
                    "toolCallId",
                    "tool_call_id",
                    &label,
                )?),
            ),
            "deny" => operation_result(
                index,
                op,
                queue.record_denied(required_alias_string(
                    operation,
                    "toolCallId",
                    "tool_call_id",
                    &label,
                )?),
            ),
            "expire" => operation_result(
                index,
                op,
                queue.record_expired(required_alias_string(
                    operation,
                    "toolCallId",
                    "tool_call_id",
                    &label,
                )?),
            ),
            "cancel" => operation_result(
                index,
                op,
                queue.record_cancelled(required_alias_string(
                    operation,
                    "toolCallId",
                    "tool_call_id",
                    &label,
                )?),
            ),
            "policy_stop" => {
                let pending_tool_calls = required_alias_string(
                    operation,
                    "pendingToolCalls",
                    "pending_tool_calls",
                    &label,
                )?;
                let affected = queue.apply_policy_stop(match pending_tool_calls {
                    "keep" => PendingToolCallsDisposition::Keep,
                    "deny" => PendingToolCallsDisposition::Deny,
                    "cancel_admitted" => PendingToolCallsDisposition::CancelAdmitted,
                    value => {
                        return Err(PyValueError::new_err(format!(
                            "{label}.pendingToolCalls has unknown pending tool calls disposition {value:?}"
                        )));
                    }
                });
                json!({
                    "index": index,
                    "op": op,
                    "affected": affected,
                })
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.op has unknown sequential tool queue operation {value:?}"
                )));
            }
        };
        operation_results.push(result);
    }

    let states = raw_calls
        .iter()
        .enumerate()
        .map(|(index, raw_call)| {
            let label = format!("queue.calls[{index}]");
            let object = json_object(raw_call, &label)?;
            let call_value = object.get("call").unwrap_or(raw_call);
            let call = json_object(call_value, &label)?;
            let tool_call_id = required_alias_string(call, "toolCallId", "tool_call_id", &label)?;
            Ok((
                tool_call_id.to_owned(),
                queue.state(tool_call_id).map(|state| match state {
                    ToolExecutionState::Pending => "pending",
                    ToolExecutionState::Running => "running",
                    ToolExecutionState::Completed => "completed",
                    ToolExecutionState::Failed => "failed",
                    ToolExecutionState::Denied => "denied",
                    ToolExecutionState::Cancelled => "cancelled",
                    ToolExecutionState::PolicyStopped => "policy_stopped",
                    ToolExecutionState::Expired => "expired",
                    ToolExecutionState::Skipped => "skipped",
                }),
            ))
        })
        .collect::<PyResult<BTreeMap<_, _>>>()?;
    serde_json::to_string(&json!({
        "planId": plan_id,
        "responseId": response_id,
        "runningCallId": queue.running_call_id(),
        "operations": operation_results,
        "states": states,
    }))
    .map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize sequential tool queue evaluation: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_usage_ledger_json(operations_json: &str, run_id: Option<&str>) -> PyResult<String> {
    let usage_source = |value: &str, label: &str| -> PyResult<UsageSource> {
        match value {
            "provider_reported" => Ok(UsageSource::ProviderReported),
            "runtime_measured" => Ok(UsageSource::RuntimeMeasured),
            "tokenizer_estimated" => Ok(UsageSource::TokenizerEstimated),
            "pricing_estimated" => Ok(UsageSource::PricingEstimated),
            "reconciled" => Ok(UsageSource::Reconciled),
            value => Err(PyValueError::new_err(format!(
                "{label}.source has unknown usage source {value:?}"
            ))),
        }
    };
    let usage_confidence = |value: &str, label: &str| -> PyResult<UsageConfidence> {
        match value {
            "exact" => Ok(UsageConfidence::Exact),
            "provider_exact" => Ok(UsageConfidence::ProviderExact),
            "estimated" => Ok(UsageConfidence::Estimated),
            "unknown" => Ok(UsageConfidence::Unknown),
            value => Err(PyValueError::new_err(format!(
                "{label}.confidence has unknown usage confidence {value:?}"
            ))),
        }
    };
    let usage_amounts = |value: &Value, label: &str| -> PyResult<Vec<LedgerUsageAmount>> {
        let amounts = value
            .as_array()
            .ok_or_else(|| PyValueError::new_err(format!("{label} must be an array")))?;
        let mut parsed = Vec::new();
        for (index, amount) in amounts.iter().enumerate() {
            let amount_label = format!("{label}[{index}]");
            let amount_object = json_object(amount, &amount_label)?;
            let kind = required_string(amount_object, "kind", &amount_label)?;
            let amount_value = amount_object
                .get("amount")
                .and_then(Value::as_i64)
                .ok_or_else(|| {
                    PyValueError::new_err(format!("{amount_label}.amount must be an integer"))
                })?;
            let unit = required_string(amount_object, "unit", &amount_label)?;
            let mut usage_amount = LedgerUsageAmount::new(kind, amount_value, unit);
            if let Some(dimensions) = amount_object.get("dimensions") {
                let dimensions = dimensions.as_object().ok_or_else(|| {
                    PyValueError::new_err(format!("{amount_label}.dimensions must be an object"))
                })?;
                for (key, value) in dimensions {
                    let value = value.as_str().ok_or_else(|| {
                        PyValueError::new_err(format!(
                            "{amount_label}.dimensions.{key} must be a string"
                        ))
                    })?;
                    usage_amount = usage_amount.with_dimension(key, value);
                }
            }
            parsed.push(usage_amount);
        }
        Ok(parsed)
    };
    let usage_record = |value: &Value, label: &str| -> PyResult<UsageRecord> {
        let record = json_object(value, label)?;
        let mut usage_record = UsageRecord::new(
            required_alias_string(record, "recordId", "record_id", label)?,
            usage_source(required_string(record, "source", label)?, label)?,
            usage_confidence(required_string(record, "confidence", label)?, label)?,
            usage_amounts(
                record
                    .get("amounts")
                    .ok_or_else(|| PyValueError::new_err(format!("{label}.amounts is required")))?,
                &format!("{label}.amounts"),
            )?,
            required_alias_u64(record, "occurredAtUnixMs", "occurred_at_unix_ms", label)?,
        );
        if let Some(value) = optional_alias_string(record, "runId", "run_id", label)? {
            usage_record = usage_record.with_run_id(value);
        }
        if let Some(value) = optional_alias_string(record, "attemptId", "attempt_id", label)? {
            usage_record = usage_record.with_attempt_id(value);
        }
        if let Some(value) =
            optional_alias_string(record, "providerResponseId", "provider_response_id", label)?
        {
            usage_record = usage_record.with_provider_response_id(value);
        }
        if let Some(value) = optional_alias_string(record, "pricingRef", "pricing_ref", label)? {
            usage_record = usage_record.with_pricing_ref(value);
        }
        if let Some(value) =
            optional_alias_string(record, "quotaWindowId", "quota_window_id", label)?
        {
            usage_record = usage_record.with_quota_window_id(value);
        }
        if let Some(value) =
            optional_alias_string(record, "executionScope", "execution_scope", label)?
        {
            usage_record = usage_record.with_execution_scope(value);
        }
        if let Some(value) =
            optional_alias_string(record, "reconciliationOf", "reconciliation_of", label)?
        {
            usage_record.reconciliation_of = Some(value.to_owned());
        }
        if let Some(metadata) = record.get("metadata") {
            let metadata = metadata.as_object().ok_or_else(|| {
                PyValueError::new_err(format!("{label}.metadata must be an object"))
            })?;
            for (key, value) in metadata {
                let value = value.as_str().ok_or_else(|| {
                    PyValueError::new_err(format!("{label}.metadata.{key} must be a string"))
                })?;
                usage_record = usage_record.with_metadata(key, value);
            }
        }
        Ok(usage_record)
    };
    let usage_amount_json = |amount: &LedgerUsageAmount| {
        json!({
            "kind": amount.kind,
            "amount": amount.amount,
            "unit": amount.unit,
            "dimensions": amount.dimensions,
        })
    };
    let usage_error_json = |index: usize, op: &str, error: UsageLedgerError| {
        let (error_code, error_message) = match &error {
            UsageLedgerError::RecordNotFound { record_id } => (
                "record_not_found",
                format!("usage record {record_id:?} was not found"),
            ),
            UsageLedgerError::RecordConflict { record_id } => (
                "record_conflict",
                format!("usage record {record_id:?} conflicts with an existing record"),
            ),
            UsageLedgerError::InvalidRecord { message } => ("invalid_record", message.clone()),
            UsageLedgerError::Storage { message } => ("storage", message.clone()),
        };
        json!({
            "index": index,
            "op": op,
            "error": error_code,
            "errorMessage": error_message,
            "errorDebug": format!("{error:?}"),
        })
    };

    let operations_value = parse_json_argument(operations_json, "usage ledger operations")?;
    let operations = operations_value
        .as_array()
        .ok_or_else(|| PyValueError::new_err("usage ledger operations must be an array"))?;
    let mut ledger = InMemoryUsageLedger::new();
    let mut operation_results = Vec::new();
    let mut append_results = Vec::new();

    for (index, operation) in operations.iter().enumerate() {
        let label = format!("operations[{index}]");
        let operation = json_object(operation, &label)?;
        let op = required_string(operation, "op", &label)?;
        let result = match op {
            "append" => {
                let record = usage_record(
                    operation.get("record").ok_or_else(|| {
                        PyValueError::new_err(format!("{label}.record is required"))
                    })?,
                    &format!("{label}.record"),
                )?;
                match ledger.append(record) {
                    Ok(record) => {
                        append_results.push(record.record_id.clone());
                        json!({
                            "index": index,
                            "op": op,
                            "error": Value::Null,
                            "recordId": record.record_id,
                        })
                    }
                    Err(error) => usage_error_json(index, op, error),
                }
            }
            "reconcile" => {
                let amounts = usage_amounts(
                    operation.get("amounts").ok_or_else(|| {
                        PyValueError::new_err(format!("{label}.amounts is required"))
                    })?,
                    &format!("{label}.amounts"),
                )?;
                match ledger.reconcile(
                    required_alias_string(operation, "sourceRecordId", "source_record_id", &label)?,
                    amounts,
                    required_alias_u64(
                        operation,
                        "occurredAtUnixMs",
                        "occurred_at_unix_ms",
                        &label,
                    )?,
                    optional_alias_string(operation, "recordId", "record_id", &label)?
                        .map(str::to_owned),
                ) {
                    Ok(record) => {
                        append_results.push(record.record_id.clone());
                        json!({
                            "index": index,
                            "op": op,
                            "error": Value::Null,
                            "recordId": record.record_id,
                        })
                    }
                    Err(error) => usage_error_json(index, op, error),
                }
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.op has unknown usage ledger operation {value:?}"
                )));
            }
        };
        operation_results.push(result);
    }

    let (record_ids, totals) = if let Some(run_id) = run_id {
        (
            ledger
                .records_for_run(run_id)
                .iter()
                .map(|record| record.record_id.clone())
                .collect::<Vec<_>>(),
            ledger
                .totals_for_run(run_id)
                .iter()
                .map(usage_amount_json)
                .collect::<Vec<_>>(),
        )
    } else {
        (Vec::new(), Vec::new())
    };
    let ok = operation_results
        .iter()
        .all(|result| result.get("error").is_some_and(Value::is_null));

    serde_json::to_string(&json!({
        "ok": ok,
        "operations": operation_results,
        "appendResults": append_results,
        "runId": run_id,
        "recordIds": record_ids,
        "totals": totals,
    }))
    .map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize usage ledger evaluation: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_durable_tool_terminal_store_json(operations_json: &str) -> PyResult<String> {
    let terminal_state_name = |state: DurableToolTerminalState| -> &'static str {
        match state {
            DurableToolTerminalState::Completed => "completed",
            DurableToolTerminalState::Failed => "failed",
            DurableToolTerminalState::Denied => "denied",
            DurableToolTerminalState::Cancelled => "cancelled",
            DurableToolTerminalState::PolicyStopped => "policy_stopped",
            DurableToolTerminalState::Incomplete => "incomplete",
            DurableToolTerminalState::Expired => "expired",
        }
    };
    let terminal_reason_name = |reason: DurableOutputCutoffTerminalReason| -> &'static str {
        match reason {
            DurableOutputCutoffTerminalReason::PolicyDenied => "policy_denied",
            DurableOutputCutoffTerminalReason::BudgetExhausted => "budget_exhausted",
            DurableOutputCutoffTerminalReason::Cancelled => "cancelled",
            DurableOutputCutoffTerminalReason::ClientDisconnected => "client_disconnected",
        }
    };
    let draft_disposition_name =
        |disposition: DurableOutputCutoffDraftDisposition| -> &'static str {
            match disposition {
                DurableOutputCutoffDraftDisposition::Keep => "keep",
                DurableOutputCutoffDraftDisposition::MarkIncomplete => "mark_incomplete",
                DurableOutputCutoffDraftDisposition::Retract => "retract",
            }
        };
    let durable_result_name = |result: DurableOutputCutoffDurableResult| -> &'static str {
        match result {
            DurableOutputCutoffDurableResult::None => "none",
            DurableOutputCutoffDurableResult::Incomplete => "incomplete",
            DurableOutputCutoffDurableResult::Partial => "partial",
        }
    };
    let store_error_code = |error: &ToolTerminalStoreError| -> &'static str {
        match error {
            ToolTerminalStoreError::MissingRunId => "missing_run_id",
            ToolTerminalStoreError::MissingResponseId => "missing_response_id",
            ToolTerminalStoreError::MissingToolCallId => "missing_tool_call_id",
            ToolTerminalStoreError::MissingArgumentsDigest => "missing_arguments_digest",
            ToolTerminalStoreError::MissingOutputDigest => "missing_output_digest",
            ToolTerminalStoreError::MissingIdempotencyKey => "missing_idempotency_key",
            ToolTerminalStoreError::MissingPolicyDecisionId => "missing_policy_decision_id",
            ToolTerminalStoreError::MissingStreamId => "missing_stream_id",
            ToolTerminalStoreError::MissingTurnId => "missing_turn_id",
            ToolTerminalStoreError::InvalidRevision => "invalid_revision",
            ToolTerminalStoreError::InvalidCompletedAt => "invalid_completed_at",
            ToolTerminalStoreError::PolicyAcceptedSequenceBeyondGenerated { .. } => {
                "policy_accepted_sequence_beyond_generated"
            }
            ToolTerminalStoreError::ClientDeliveredSequenceBeyondGenerated { .. } => {
                "client_delivered_sequence_beyond_generated"
            }
            ToolTerminalStoreError::TerminalStateConflict { .. } => "terminal_state_conflict",
            ToolTerminalStoreError::ResponsePolicyStopConflict { .. } => {
                "response_policy_stop_conflict"
            }
            ToolTerminalStoreError::DurableResultAlreadyCommitted { .. } => {
                "durable_result_already_committed"
            }
            ToolTerminalStoreError::ResponsePolicyStopped { .. } => "response_policy_stopped",
        }
    };
    let terminal_record_json = |record: &DurableToolTerminalRecord| {
        json!({
            "runId": record.run_id,
            "responseId": record.response_id,
            "toolCallId": record.tool_call_id,
            "revision": record.revision,
            "terminalState": terminal_state_name(record.terminal_state),
            "argumentsDigest": record.arguments_digest,
            "outputDigest": record.output_digest,
            "idempotencyKey": record.idempotency_key,
            "effectCommitted": record.effect_committed,
            "durableResultCommitted": record.durable_result_committed,
            "completedAtUnixMs": record.completed_at_unix_ms,
        })
    };
    let policy_stop_record_json = |record: &DurableResponsePolicyStopRecord| {
        json!({
            "responseId": record.response_id,
            "streamId": record.stream_id,
            "turnId": record.turn_id,
            "policyDecisionId": record.policy_decision_id,
            "lastGeneratedSequence": record.last_generated_sequence,
            "lastPolicyAcceptedSequence": record.last_policy_accepted_sequence,
            "lastClientDeliveredSequence": record.last_client_delivered_sequence,
            "terminalReason": terminal_reason_name(record.terminal_reason),
            "draftDisposition": draft_disposition_name(record.draft_disposition),
            "durableResult": durable_result_name(record.durable_result),
            "occurredAtUnixMs": record.occurred_at_unix_ms,
        })
    };
    let store_error_json = |index: usize, op: &str, error: ToolTerminalStoreError| {
        json!({
            "index": index,
            "op": op,
            "error": store_error_code(&error),
            "errorDebug": format!("{error:?}"),
        })
    };

    let operations_value =
        parse_json_argument(operations_json, "durable tool terminal store operations")?;
    let operations = operations_value.as_array().ok_or_else(|| {
        PyValueError::new_err("durable tool terminal store operations must be an array")
    })?;
    let mut store = InMemoryDurableToolTerminalStore::new();
    let mut operation_results = Vec::new();
    for (index, operation) in operations.iter().enumerate() {
        let label = format!("operations[{index}]");
        let operation = json_object(operation, &label)?;
        let op = required_string(operation, "op", &label)?;
        let result = match op {
            "record_tool_terminal" => {
                let record_value = operation
                    .get("record")
                    .ok_or_else(|| PyValueError::new_err(format!("{label}.record is required")))?;
                let record_label = format!("{label}.record");
                let record_object = json_object(record_value, &record_label)?;
                let terminal_state = match required_alias_string(
                    record_object,
                    "terminalState",
                    "terminal_state",
                    &record_label,
                )? {
                    "completed" => DurableToolTerminalState::Completed,
                    "failed" => DurableToolTerminalState::Failed,
                    "denied" => DurableToolTerminalState::Denied,
                    "cancelled" => DurableToolTerminalState::Cancelled,
                    "policy_stopped" => DurableToolTerminalState::PolicyStopped,
                    "incomplete" => DurableToolTerminalState::Incomplete,
                    "expired" => DurableToolTerminalState::Expired,
                    value => {
                        return Err(PyValueError::new_err(format!(
                            "{record_label}.terminalState has unknown terminal state {value:?}"
                        )));
                    }
                };
                let mut record = DurableToolTerminalRecord::new(
                    required_alias_string(record_object, "runId", "run_id", &record_label)?,
                    required_alias_string(
                        record_object,
                        "responseId",
                        "response_id",
                        &record_label,
                    )?,
                    required_alias_string(
                        record_object,
                        "toolCallId",
                        "tool_call_id",
                        &record_label,
                    )?,
                    u64_to_u32(
                        required_alias_u64(record_object, "revision", "revision", &record_label)?,
                        &format!("{record_label}.revision"),
                    )?,
                    terminal_state,
                    required_alias_string(
                        record_object,
                        "argumentsDigest",
                        "arguments_digest",
                        &record_label,
                    )?,
                    required_alias_u64(
                        record_object,
                        "completedAtUnixMs",
                        "completed_at_unix_ms",
                        &record_label,
                    )?,
                );
                if let Some(output_digest) = optional_nullable_alias_string(
                    record_object,
                    "outputDigest",
                    "output_digest",
                    &record_label,
                )? {
                    record = record.with_output_digest(output_digest);
                }
                if let Some(idempotency_key) = optional_nullable_alias_string(
                    record_object,
                    "idempotencyKey",
                    "idempotency_key",
                    &record_label,
                )? {
                    record = record.with_idempotency_key(idempotency_key);
                }
                if optional_nullable_alias_bool(
                    record_object,
                    "effectCommitted",
                    "effect_committed",
                    &record_label,
                )?
                .unwrap_or(false)
                {
                    record = record.with_effect_committed();
                }
                if optional_nullable_alias_bool(
                    record_object,
                    "durableResultCommitted",
                    "durable_result_committed",
                    &record_label,
                )?
                .unwrap_or(false)
                {
                    record = record.with_durable_result_committed();
                }
                match store.record_tool_terminal(record) {
                    Ok(commit) => json!({
                        "index": index,
                        "op": op,
                        "error": Value::Null,
                        "commit": {
                            "sequence": commit.sequence,
                            "replayed": commit.replayed,
                            "record": terminal_record_json(&commit.record),
                        },
                    }),
                    Err(error) => store_error_json(index, op, error),
                }
            }
            "record_response_policy_stop" => {
                let record_value = operation
                    .get("record")
                    .ok_or_else(|| PyValueError::new_err(format!("{label}.record is required")))?;
                let record_label = format!("{label}.record");
                let record_object = json_object(record_value, &record_label)?;
                let mut record = DurableResponsePolicyStopRecord::new(
                    required_alias_string(
                        record_object,
                        "responseId",
                        "response_id",
                        &record_label,
                    )?,
                    required_alias_string(
                        record_object,
                        "policyDecisionId",
                        "policy_decision_id",
                        &record_label,
                    )?,
                    required_alias_u64(
                        record_object,
                        "lastPolicyAcceptedSequence",
                        "last_policy_accepted_sequence",
                        &record_label,
                    )?,
                    required_alias_u64(
                        record_object,
                        "occurredAtUnixMs",
                        "occurred_at_unix_ms",
                        &record_label,
                    )?,
                );
                if let Some(stream_id) = optional_nullable_alias_string(
                    record_object,
                    "streamId",
                    "stream_id",
                    &record_label,
                )? {
                    record = record.with_stream_id(stream_id);
                }
                if let Some(turn_id) = optional_nullable_alias_string(
                    record_object,
                    "turnId",
                    "turn_id",
                    &record_label,
                )? {
                    record = record.with_turn_id(turn_id);
                }
                if let Some(sequence) = optional_nullable_alias_u64(
                    record_object,
                    "lastGeneratedSequence",
                    "last_generated_sequence",
                    &record_label,
                )? {
                    record = record.with_last_generated_sequence(sequence);
                }
                if let Some(sequence) = optional_nullable_alias_u64(
                    record_object,
                    "lastClientDeliveredSequence",
                    "last_client_delivered_sequence",
                    &record_label,
                )? {
                    record = record.with_last_client_delivered_sequence(sequence);
                }
                if let Some(reason) = optional_nullable_alias_string(
                    record_object,
                    "terminalReason",
                    "terminal_reason",
                    &record_label,
                )? {
                    record = record.with_terminal_reason(match reason {
                        "policy_denied" => DurableOutputCutoffTerminalReason::PolicyDenied,
                        "budget_exhausted" => DurableOutputCutoffTerminalReason::BudgetExhausted,
                        "cancelled" => DurableOutputCutoffTerminalReason::Cancelled,
                        "client_disconnected" => {
                            DurableOutputCutoffTerminalReason::ClientDisconnected
                        }
                        value => {
                            return Err(PyValueError::new_err(format!(
                                "{record_label}.terminalReason has unknown terminal reason {value:?}"
                            )));
                        }
                    });
                }
                if let Some(disposition) = optional_nullable_alias_string(
                    record_object,
                    "draftDisposition",
                    "draft_disposition",
                    &record_label,
                )? {
                    record = record.with_draft_disposition(match disposition {
                        "keep" => DurableOutputCutoffDraftDisposition::Keep,
                        "mark_incomplete" => DurableOutputCutoffDraftDisposition::MarkIncomplete,
                        "retract" => DurableOutputCutoffDraftDisposition::Retract,
                        value => {
                            return Err(PyValueError::new_err(format!(
                                "{record_label}.draftDisposition has unknown draft disposition {value:?}"
                            )));
                        }
                    });
                }
                if let Some(durable_result) = optional_nullable_alias_string(
                    record_object,
                    "durableResult",
                    "durable_result",
                    &record_label,
                )? {
                    record = record.with_durable_result(match durable_result {
                        "none" => DurableOutputCutoffDurableResult::None,
                        "incomplete" => DurableOutputCutoffDurableResult::Incomplete,
                        "partial" => DurableOutputCutoffDurableResult::Partial,
                        value => {
                            return Err(PyValueError::new_err(format!(
                                "{record_label}.durableResult has unknown durable result {value:?}"
                            )));
                        }
                    });
                }
                match store.record_response_policy_stop(record) {
                    Ok(commit) => {
                        let cutoff = commit.record.to_output_cutoff().map_err(|error| {
                            PyRuntimeError::new_err(format!(
                                "committed response policy stop did not convert to output cutoff: {error:?}"
                            ))
                        })?;
                        json!({
                            "index": index,
                            "op": op,
                            "error": Value::Null,
                            "commit": {
                                "sequence": commit.sequence,
                                "replayed": commit.replayed,
                                "record": policy_stop_record_json(&commit.record),
                                "outputCutoff": {
                                    "streamId": cutoff.stream_id,
                                    "responseId": cutoff.response_id,
                                    "turnId": cutoff.turn_id,
                                    "lastGeneratedSequence": cutoff.last_generated_sequence,
                                    "lastPolicyAcceptedSequence": cutoff.last_policy_accepted_sequence,
                                    "lastClientDeliveredSequence": cutoff.last_client_delivered_sequence,
                                    "terminalReason": terminal_reason_name(commit.record.terminal_reason),
                                    "draftDisposition": draft_disposition_name(commit.record.draft_disposition),
                                    "durableResult": durable_result_name(commit.record.durable_result),
                                    "policyDecisionId": cutoff.policy_decision_id,
                                    "occurredAtUnixMs": cutoff.occurred_at_unix_ms,
                                },
                            },
                        })
                    }
                    Err(error) => store_error_json(index, op, error),
                }
            }
            "tool_terminal_count" => json!({
                "index": index,
                "op": op,
                "count": store.tool_terminal_count(),
                "error": Value::Null,
            }),
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.op has unknown durable tool terminal store operation {value:?}"
                )));
            }
        };
        operation_results.push(result);
    }

    serde_json::to_string(&json!({
        "operations": operation_results,
        "toolTerminalCount": store.tool_terminal_count(),
    }))
    .map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize durable tool terminal store evaluation: {error}"
        ))
    })
}

fn parse_tool_cancellation(value: &str, label: &str) -> PyResult<ToolCancellation> {
    match value {
        "unsupported" => Ok(ToolCancellation::Unsupported),
        "cooperative" => Ok(ToolCancellation::Cooperative),
        "force_terminable" => Ok(ToolCancellation::ForceTerminable),
        value => Err(PyValueError::new_err(format!(
            "{label} has unknown cancellation {value:?}"
        ))),
    }
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add_function(wrap_pyfunction!(binding_version, module)?)?;
    module.add_function(wrap_pyfunction!(finalize_tool_call_json, module)?)?;
    module.add_function(wrap_pyfunction!(compile_graph_json, module)?)?;
    module.add_function(wrap_pyfunction!(
        validate_worker_advertisement_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        validate_worker_protocol_message_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(admit_worker_message_json, module)?)?;
    module.add_function(wrap_pyfunction!(validate_remote_payload_json, module)?)?;
    module.add_function(wrap_pyfunction!(
        evaluate_connector_capabilities_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        prepare_tool_result_for_model_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(evaluate_tool_approval_json, module)?)?;
    module.add_function(wrap_pyfunction!(evaluate_retry_policy_json, module)?)?;
    module.add_function(wrap_pyfunction!(
        evaluate_provider_limit_policy_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(evaluate_cancellation_scope_json, module)?)?;
    module.add_function(wrap_pyfunction!(evaluate_task_group_json, module)?)?;
    module.add_function(wrap_pyfunction!(evaluate_node_lifecycle_json, module)?)?;
    module.add_function(wrap_pyfunction!(
        record_tool_effect_precondition_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        record_tool_effect_audit_event_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(capture_telemetry_content_json, module)?)?;
    module.add_function(wrap_pyfunction!(evaluate_tool_result_stream_json, module)?)?;
    module.add_function(wrap_pyfunction!(run_test_graph_json, module)?)?;
    module.add_function(wrap_pyfunction!(run_stdlib_graph_json, module)?)?;
    module.add_function(wrap_pyfunction!(decide_agent_step_json, module)?)?;
    module.add_function(wrap_pyfunction!(admit_exhaustion_work_json, module)?)?;
    module.add_function(wrap_pyfunction!(
        evaluate_application_event_stream_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        evaluate_application_protocol_stream_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        evaluate_application_protocol_log_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        negotiate_application_protocol_capabilities_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(evaluate_output_gate_json, module)?)?;
    module.add_function(wrap_pyfunction!(
        evaluate_declarative_output_policy_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(evaluate_tool_execution_plan_json, module)?)?;
    module.add_function(wrap_pyfunction!(
        evaluate_sequential_tool_queue_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(evaluate_usage_ledger_json, module)?)?;
    module.add_function(wrap_pyfunction!(
        evaluate_durable_tool_terminal_store_json,
        module
    )?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use graphblocks_runtime_core::tool_approval::{ToolApprovalRecord, ToolApprovalRequest};
    use graphblocks_runtime_core::tool_result::{ContentPart, ToolResult};
    use serde_json::{Value, json};

    use super::{
        admit_exhaustion_work_json, admit_worker_message_json, capture_telemetry_content_json,
        compile_graph_json, decide_agent_step_json, evaluate_application_event_stream_json,
        evaluate_application_protocol_log_json, evaluate_application_protocol_stream_json,
        evaluate_cancellation_scope_json, evaluate_connector_capabilities_json,
        evaluate_declarative_output_policy_json, evaluate_durable_tool_terminal_store_json,
        evaluate_node_lifecycle_json, evaluate_output_gate_json,
        evaluate_provider_limit_policy_json, evaluate_retry_policy_json,
        evaluate_sequential_tool_queue_json, evaluate_task_group_json, evaluate_tool_approval_json,
        evaluate_tool_execution_plan_json, evaluate_tool_result_stream_json,
        evaluate_usage_ledger_json, finalize_tool_call_json,
        negotiate_application_protocol_capabilities_json, parse_resolved_tool, parse_tool_call,
        prepare_tool_result_for_model_json, record_tool_effect_audit_event_json,
        record_tool_effect_precondition_json, run_stdlib_graph_json, run_test_graph_json,
        validate_remote_payload_json, validate_worker_advertisement_json,
        validate_worker_protocol_message_json,
    };

    fn native_audit_fixture() -> Result<(Value, Value, Value), String> {
        let resolved_tool = json!({
            "resolvedToolId": "resolved-ticket-create",
            "definition": {
                "name": "ticket.create",
                "description": "Create a support ticket.",
                "inputSchema": "schemas/TicketCreate@1"
            },
            "binding": {
                "bindingId": "binding-ticket-create",
                "toolName": "ticket.create",
                "implementation": {
                    "kind": "block",
                    "block": "blocks.ticket_create"
                },
                "effects": ["destructive", "external_write", "network"]
            },
            "effectivePolicySnapshotId": "policy-snapshot-1",
            "allowedForPrincipal": true
        });
        let draft = json!({
            "responseId": "response-1",
            "toolCallId": "call-1",
            "toolName": "ticket.create",
            "argumentFragments": ["{\"customer_id\":\"cust-1\",\"title\":\"Help\"}"],
            "sequence": 1,
            "status": "arguments_complete"
        });
        let call_json = finalize_tool_call_json(
            &serde_json::to_string(&draft).map_err(|error| error.to_string())?,
            "resolved-ticket-create",
            1_000,
        )
        .map_err(|error| error.to_string())?;
        let mut call =
            serde_json::from_str::<Value>(&call_json).map_err(|error| error.to_string())?;
        let call_object = call
            .as_object_mut()
            .ok_or_else(|| "finalized call must be an object".to_owned())?;
        call_object.insert("status".to_owned(), json!("admitted"));
        call_object.insert("admittedAtUnixMs".to_owned(), json!(1_050));
        let result = json!({
            "toolCallId": "call-1",
            "status": "completed",
            "output": [
                {
                    "kind": "json",
                    "data": {"ticket_id": "T-1"}
                }
            ],
            "startedAtUnixMs": 1_100,
            "completedAtUnixMs": 1_250,
            "effectOutcome": "committed"
        });
        Ok((resolved_tool, call, result))
    }

    #[test]
    fn finalize_tool_call_json_assembles_validated_call_with_canonical_digest() -> Result<(), String>
    {
        let draft = json!({
            "responseId": "response-1",
            "toolCallId": "call-1",
            "toolName": "knowledge.search",
            "argumentFragments": ["{\"b\":2,", "\"a\":1}"],
            "sequence": 2,
            "status": "arguments_complete"
        });
        let reversed_draft = json!({
            "response_id": "response-1",
            "tool_call_id": "call-2",
            "tool_name": "knowledge.search",
            "argument_fragments": ["{\"a\":1,", "\"b\":2}"],
            "sequence": 2,
            "status": "arguments_complete"
        });
        let draft_json = serde_json::to_string(&draft).map_err(|error| error.to_string())?;
        let reversed_draft_json =
            serde_json::to_string(&reversed_draft).map_err(|error| error.to_string())?;

        let result_json = finalize_tool_call_json(&draft_json, "resolved-tool-1", 1_000)
            .map_err(|error| error.to_string())?;
        let reversed_result_json =
            finalize_tool_call_json(&reversed_draft_json, "resolved-tool-1", 1_001)
                .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let reversed_result = serde_json::from_str::<Value>(&reversed_result_json)
            .map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("toolCallId").and_then(Value::as_str),
            Some("call-1")
        );
        assert_eq!(
            result.get("responseId").and_then(Value::as_str),
            Some("response-1")
        );
        assert_eq!(
            result.get("resolvedToolId").and_then(Value::as_str),
            Some("resolved-tool-1")
        );
        assert_eq!(
            result.get("name").and_then(Value::as_str),
            Some("knowledge.search")
        );
        assert_eq!(result.get("arguments"), Some(&json!({"a": 1, "b": 2})));
        assert_eq!(result.get("revision").and_then(Value::as_u64), Some(1));
        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("validated")
        );
        assert_eq!(result.get("dependsOn"), Some(&json!([])));
        assert_eq!(
            result.get("createdAtUnixMs").and_then(Value::as_u64),
            Some(1_000)
        );
        assert_eq!(
            result.get("argumentsDigest").and_then(Value::as_str),
            reversed_result
                .get("argumentsDigest")
                .and_then(Value::as_str)
        );

        Ok(())
    }

    #[test]
    fn finalize_tool_call_json_rejects_incomplete_arguments() -> Result<(), String> {
        pyo3::Python::initialize();

        let draft = json!({
            "responseId": "response-1",
            "toolCallId": "call-1",
            "toolName": "knowledge.search",
            "argumentFragments": ["{\"query\":\"runtime\"}"],
            "sequence": 1,
            "status": "arguments_streaming"
        });
        let draft_json = serde_json::to_string(&draft).map_err(|error| error.to_string())?;

        let error = finalize_tool_call_json(&draft_json, "resolved-tool-1", 1_000)
            .expect_err("streaming arguments must not finalize")
            .to_string();

        assert!(error.contains("ArgumentsNotComplete"));
        Ok(())
    }

    #[test]
    fn prepare_tool_result_for_model_json_delegates_to_runtime_validation() -> Result<(), String> {
        let draft = json!({
            "responseId": "response-1",
            "toolCallId": "call-1",
            "toolName": "knowledge.search",
            "argumentFragments": ["{}"],
            "sequence": 1,
            "status": "arguments_complete"
        });
        let call_json = finalize_tool_call_json(
            &serde_json::to_string(&draft).map_err(|error| error.to_string())?,
            "resolved-tool-1",
            1_000,
        )
        .map_err(|error| error.to_string())?;
        let result = ToolResult::completed(
            "call-1",
            [ContentPart::text("safe secret suffix")],
            1_001,
            1_002,
        );
        let result_json = serde_json::to_string(&json!({
            "toolCallId": "call-1",
            "status": "completed",
            "output": [
                {"kind": "text", "text": "safe secret suffix", "metadata": {}}
            ],
            "outputDigest": result.output_digest,
            "startedAtUnixMs": 1_001,
            "completedAtUnixMs": 1_002
        }))
        .map_err(|error| error.to_string())?;
        let resolved_tool_json = serde_json::to_string(&json!({
            "resolvedToolId": "resolved-tool-1",
            "definition": {
                "name": "knowledge.search",
                "description": "Search documentation.",
                "inputSchema": "schemas/SearchRequest@1"
            },
            "binding": {
                "bindingId": "binding-search",
                "toolName": "knowledge.search",
                "implementation": {"kind": "block", "block": "blocks.search"},
                "effects": ["external_read"],
                "approval": "never",
                "idempotency": "not_applicable"
            },
            "effectivePolicySnapshotId": "policy-snapshot-1",
            "allowedForPrincipal": true
        }))
        .map_err(|error| error.to_string())?;
        let content_policy_json = serde_json::to_string(&json!({
            "redactions": [
                {
                    "path": "/parts/0/text",
                    "start": 5,
                    "end": 11,
                    "replacement": "[redacted]"
                }
            ],
            "capturePolicy": {
                "mode": "redacted_preview",
                "retentionPolicy": "records-30d"
            },
            "trustDesignation": "policy_quarantined",
            "promptInjectionLabel": "classifier_flagged_tool_output",
            "contentClassification": "classified_external_tool_output"
        }))
        .map_err(|error| error.to_string())?;

        let output_json = prepare_tool_result_for_model_json(
            &call_json,
            &result_json,
            &resolved_tool_json,
            "[]",
            Some(&content_policy_json),
        )
        .map_err(|error| error.to_string())?;
        let output =
            serde_json::from_str::<Value>(&output_json).map_err(|error| error.to_string())?;

        assert_eq!(output.get("ok"), Some(&json!(true)));
        assert_eq!(
            output.pointer("/output/0/text").and_then(Value::as_str),
            Some("safe [redacted] suffix")
        );
        assert_eq!(
            output
                .pointer("/output/0/metadata/trust_designation")
                .and_then(Value::as_str),
            Some("policy_quarantined")
        );
        assert_eq!(
            output
                .pointer("/output/0/metadata/prompt_injection_label")
                .and_then(Value::as_str),
            Some("classifier_flagged_tool_output")
        );
        assert_eq!(
            output
                .pointer("/output/0/metadata/content_classification")
                .and_then(Value::as_str),
            Some("classified_external_tool_output")
        );
        assert_eq!(
            output
                .pointer("/output/0/metadata/capture/mode")
                .and_then(Value::as_str),
            Some("redacted_preview")
        );
        assert_eq!(
            output
                .pointer("/output/0/metadata/capture/redaction_count")
                .and_then(Value::as_u64),
            Some(1)
        );
        Ok(())
    }

    #[test]
    fn record_tool_effect_precondition_json_delegates_to_runtime_audit() -> Result<(), String> {
        let (resolved_tool, call, _) = native_audit_fixture()?;
        let precondition_json = record_tool_effect_precondition_json(
            &serde_json::to_string(&resolved_tool).map_err(|error| error.to_string())?,
            &serde_json::to_string(&call).map_err(|error| error.to_string())?,
            Some("ticket.create:cust-1"),
            Some("idem-ticket-1"),
            Some("decision-tool-1"),
            Some("worker:local"),
            Some("sandbox-1"),
        )
        .map_err(|error| error.to_string())?;
        let precondition =
            serde_json::from_str::<Value>(&precondition_json).map_err(|error| error.to_string())?;

        assert!(
            precondition
                .get("digest")
                .and_then(Value::as_str)
                .is_some_and(|digest| digest.starts_with("sha256:"))
        );
        assert_eq!(
            precondition
                .pointer("/payload/tool_name")
                .and_then(Value::as_str),
            Some("ticket.create")
        );
        assert_eq!(
            precondition
                .pointer("/payload/effects")
                .and_then(Value::as_array),
            Some(&vec![
                json!("destructive"),
                json!("external_write"),
                json!("network")
            ])
        );
        assert_eq!(
            precondition
                .pointer("/payload/admitted_at_unix_ms")
                .and_then(Value::as_u64),
            Some(1_050)
        );
        assert_eq!(
            precondition
                .pointer("/payload/idempotency_key")
                .and_then(Value::as_str),
            Some("idem-ticket-1")
        );
        Ok(())
    }

    #[test]
    fn record_tool_effect_audit_event_json_returns_canonical_audit_event() -> Result<(), String> {
        let (resolved_tool, call, result) = native_audit_fixture()?;
        let precondition_json = record_tool_effect_precondition_json(
            &serde_json::to_string(&resolved_tool).map_err(|error| error.to_string())?,
            &serde_json::to_string(&call).map_err(|error| error.to_string())?,
            Some("ticket.create:cust-1"),
            Some("idem-ticket-1"),
            Some("decision-tool-1"),
            Some("worker:local"),
            Some("sandbox-1"),
        )
        .map_err(|error| error.to_string())?;
        let precondition =
            serde_json::from_str::<Value>(&precondition_json).map_err(|error| error.to_string())?;
        let precondition_digest = precondition
            .get("digest")
            .and_then(Value::as_str)
            .ok_or_else(|| "native precondition is missing digest".to_owned())?;
        let event_json = record_tool_effect_audit_event_json(
            "audit-effect-1",
            "2026-06-23T00:00:02Z",
            &serde_json::to_string(&json!({
                "principalId": "user-1",
                "tenantId": "tenant-a",
                "roles": ["support"],
                "groups": ["tier-2"],
                "attributes": {"region": "us"}
            }))
            .map_err(|error| error.to_string())?,
            &serde_json::to_string(&resolved_tool).map_err(|error| error.to_string())?,
            &serde_json::to_string(&call).map_err(|error| error.to_string())?,
            &serde_json::to_string(&result).map_err(|error| error.to_string())?,
            Some("ticket.create:cust-1"),
            Some(precondition_digest),
            Some("idem-ticket-1"),
            Some("decision-tool-1"),
        )
        .map_err(|error| error.to_string())?;
        let event =
            serde_json::from_str::<Value>(&event_json).map_err(|error| error.to_string())?;

        assert_eq!(
            event.get("eventId").and_then(Value::as_str),
            Some("audit-effect-1")
        );
        assert_eq!(
            event.get("targetKind").and_then(Value::as_str),
            Some("destructive_effect")
        );
        assert_eq!(
            event.pointer("/actor/principalId").and_then(Value::as_str),
            Some("user-1")
        );
        assert_eq!(
            event
                .pointer("/resource/resourceId")
                .and_then(Value::as_str),
            Some("tool:ticket.create")
        );
        assert_eq!(
            event
                .pointer("/payload/result_status")
                .and_then(Value::as_str),
            Some("completed")
        );
        assert_eq!(
            event
                .pointer("/payload/effect_outcome")
                .and_then(Value::as_str),
            Some("committed")
        );
        assert_eq!(
            event
                .pointer("/payload/precondition_digest")
                .and_then(Value::as_str),
            Some(precondition_digest)
        );
        assert!(
            event
                .get("payloadDigest")
                .and_then(Value::as_str)
                .is_some_and(|digest| digest.starts_with("sha256:"))
        );
        Ok(())
    }

    #[test]
    fn capture_telemetry_content_json_uses_runtime_capture_policy() -> Result<(), String> {
        let captured_json = capture_telemetry_content_json(
            &serde_json::to_string(&json!({
                "mode": "redacted_preview",
                "retentionPolicy": "debug-7d",
                "consentRef": "consent-1"
            }))
            .map_err(|error| error.to_string())?,
            &serde_json::to_string(&json!({
                "contentKind": "tool_result",
                "text": "safe prefix secret suffix",
                "redactions": [
                    {"pattern": "secret", "replacement": "[redacted]"}
                ]
            }))
            .map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let captured =
            serde_json::from_str::<Value>(&captured_json).map_err(|error| error.to_string())?;

        assert_eq!(
            captured.get("mode").and_then(Value::as_str),
            Some("redacted_preview")
        );
        assert_eq!(
            captured.get("preview").and_then(Value::as_str),
            Some("safe prefix [redacted] suffix")
        );
        assert_eq!(
            captured.get("retentionPolicy").and_then(Value::as_str),
            Some("debug-7d")
        );
        assert_eq!(
            captured.get("consentRef").and_then(Value::as_str),
            Some("consent-1")
        );
        assert_eq!(
            captured.get("redactionCount").and_then(Value::as_u64),
            Some(1)
        );
        assert!(
            captured
                .get("contentDigest")
                .and_then(Value::as_str)
                .is_some_and(|digest| digest.starts_with("sha256:"))
        );

        let referenced_json = capture_telemetry_content_json(
            &serde_json::to_string(&json!({
                "mode": "reference_only",
                "retentionPolicy": "records-90d"
            }))
            .map_err(|error| error.to_string())?,
            &serde_json::to_string(&json!({
                "contentKind": "document",
                "text": "document body",
                "contentRef": "artifact://doc-1"
            }))
            .map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let referenced =
            serde_json::from_str::<Value>(&referenced_json).map_err(|error| error.to_string())?;

        assert_eq!(
            referenced.get("mode").and_then(Value::as_str),
            Some("reference_only")
        );
        assert_eq!(
            referenced.get("contentRef").and_then(Value::as_str),
            Some("artifact://doc-1")
        );
        assert_eq!(referenced.get("preview"), Some(&Value::Null));
        Ok(())
    }

    #[test]
    fn evaluate_connector_capabilities_json_returns_safe_connection_and_missing_capabilities()
    -> Result<(), String> {
        let accepted_json = evaluate_connector_capabilities_json(
            &serde_json::to_string(&json!({
                "connectionId": "ticket-system",
                "kind": "openapi",
                "provider": "zendesk",
                "config": {
                    "baseUrl": "https://tickets.example.invalid",
                    "capabilities": ["http_json", "oauth2"]
                },
                "credentials": {
                    "uri": "secret://env/TICKET_TOKEN",
                    "version": "2026-06"
                }
            }))
            .map_err(|error| error.to_string())?,
            &serde_json::to_string(&json!(["http_json"])).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let accepted =
            serde_json::from_str::<Value>(&accepted_json).map_err(|error| error.to_string())?;

        assert_eq!(accepted.get("ok"), Some(&json!(true)));
        assert_eq!(
            accepted
                .pointer("/connection/credentials/uri")
                .and_then(Value::as_str),
            Some("secret://env/TICKET_TOKEN")
        );
        assert!(accepted.pointer("/connection/credentials/value").is_none());

        let rejected_json = evaluate_connector_capabilities_json(
            &serde_json::to_string(&json!({
                "connection_id": "ticket-system",
                "kind": "openapi",
                "provider": "zendesk",
                "supported_capabilities": ["http_json"]
            }))
            .map_err(|error| error.to_string())?,
            &serde_json::to_string(&json!({"required": ["atomic_alias_swap", "http_json"]}))
                .map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let rejected =
            serde_json::from_str::<Value>(&rejected_json).map_err(|error| error.to_string())?;

        assert_eq!(rejected.get("ok"), Some(&json!(false)));
        assert_eq!(
            rejected
                .get("missingCapabilities")
                .and_then(Value::as_array),
            Some(&vec![json!("atomic_alias_swap")])
        );
        assert_eq!(
            rejected.pointer("/error/code").and_then(Value::as_str),
            Some("ConnectorCapabilityMissing")
        );
        Ok(())
    }

    fn approval_record_json(record: &ToolApprovalRecord) -> Value {
        json!({
            "approvalId": &record.approval_id,
            "request": {
                "approvalId": &record.request.approval_id,
                "toolCallId": &record.request.tool_call_id,
                "toolName": &record.request.tool_name,
                "revision": record.request.revision,
                "definitionDigest": &record.request.definition_digest,
                "bindingDigest": &record.request.binding_digest,
                "argumentsDigest": &record.request.arguments_digest,
                "policySnapshotId": &record.request.policy_snapshot_id,
                "principalId": &record.request.principal_id,
                "requestedAtUnixMs": record.request.requested_at_unix_ms,
                "expiresAtUnixMs": record.request.expires_at_unix_ms,
            },
            "status": "approved",
            "approverId": &record.approver_id,
            "decidedAtUnixMs": record.decided_at_unix_ms,
            "invalidatedAtUnixMs": record.invalidated_at_unix_ms,
            "reason": &record.reason,
        })
    }

    #[test]
    fn evaluate_tool_approval_json_reports_validity_against_call_revision() -> Result<(), String> {
        let (resolved_tool_value, mut call_value, _) = native_audit_fixture()?;
        let call_object = call_value
            .as_object_mut()
            .ok_or_else(|| "call fixture must be an object".to_owned())?;
        call_object.insert("status".to_owned(), json!("validated"));
        call_object.insert("admittedAtUnixMs".to_owned(), Value::Null);

        let resolved_tool =
            parse_resolved_tool(&resolved_tool_value, "resolved tool").map_err(|error| {
                format!("resolved tool fixture should parse for approval test: {error}")
            })?;
        let call = parse_tool_call(&call_value, "tool call").map_err(|error| {
            format!("tool call fixture should parse for approval test: {error}")
        })?;
        let request = ToolApprovalRequest::for_call(
            "approval-1",
            &resolved_tool,
            &call,
            "user-1",
            1_000,
            2_000,
        )
        .map_err(|error| format!("approval request should be valid: {error:?}"))?;
        let record = ToolApprovalRecord::approve(request, "admin-1", 1_100);
        let record_value = approval_record_json(&record);

        let accepted_json = evaluate_tool_approval_json(
            &serde_json::to_string(&record_value).map_err(|error| error.to_string())?,
            &serde_json::to_string(&resolved_tool_value).map_err(|error| error.to_string())?,
            &serde_json::to_string(&call_value).map_err(|error| error.to_string())?,
            "user-1",
            1_500,
        )
        .map_err(|error| error.to_string())?;
        let accepted =
            serde_json::from_str::<Value>(&accepted_json).map_err(|error| error.to_string())?;
        assert_eq!(accepted.get("recordValid"), Some(&json!(true)));
        assert_eq!(accepted.get("validForCall"), Some(&json!(true)));
        assert_eq!(accepted.get("validationError"), Some(&Value::Null));

        let revised = call
            .revise_arguments(json!({"customer_id": "cust-1", "title": "Changed"}))
            .map_err(|error| format!("call revision should be valid: {error:?}"))?;
        let mut revised_value = call_value.clone();
        let revised_object = revised_value
            .as_object_mut()
            .ok_or_else(|| "revised call fixture must be an object".to_owned())?;
        revised_object.insert("arguments".to_owned(), revised.arguments);
        revised_object.insert(
            "argumentsDigest".to_owned(),
            json!(revised.arguments_digest),
        );
        revised_object.insert("revision".to_owned(), json!(revised.revision));
        let revised_json = evaluate_tool_approval_json(
            &serde_json::to_string(&record_value).map_err(|error| error.to_string())?,
            &serde_json::to_string(&resolved_tool_value).map_err(|error| error.to_string())?,
            &serde_json::to_string(&revised_value).map_err(|error| error.to_string())?,
            "user-1",
            1_500,
        )
        .map_err(|error| error.to_string())?;
        let revised_result =
            serde_json::from_str::<Value>(&revised_json).map_err(|error| error.to_string())?;
        assert_eq!(revised_result.get("recordValid"), Some(&json!(true)));
        assert_eq!(revised_result.get("validForCall"), Some(&json!(false)));

        let mut mismatched_record = record_value.clone();
        mismatched_record
            .as_object_mut()
            .ok_or_else(|| "approval record must be an object".to_owned())?
            .insert("approvalId".to_owned(), json!("approval-other"));
        let mismatched_json = evaluate_tool_approval_json(
            &serde_json::to_string(&mismatched_record).map_err(|error| error.to_string())?,
            &serde_json::to_string(&resolved_tool_value).map_err(|error| error.to_string())?,
            &serde_json::to_string(&call_value).map_err(|error| error.to_string())?,
            "user-1",
            1_500,
        )
        .map_err(|error| error.to_string())?;
        let mismatched =
            serde_json::from_str::<Value>(&mismatched_json).map_err(|error| error.to_string())?;
        assert_eq!(mismatched.get("recordValid"), Some(&json!(false)));
        assert_eq!(mismatched.get("validForCall"), Some(&json!(false)));
        assert_eq!(
            mismatched
                .pointer("/validationError/code")
                .and_then(Value::as_str),
            Some("ApprovalIdMismatch")
        );
        Ok(())
    }

    #[test]
    fn evaluate_retry_policy_json_enforces_idempotency_and_retry_after() -> Result<(), String> {
        let policy = json!({
            "maxAttempts": 3,
            "retryOn": ["transient", "timeout"],
            "backoff": {
                "kind": "fixed",
                "delayMs": 250
            },
        });
        let missing_idempotency = json!({
            "attempt": 1,
            "error": {
                "code": "provider.transient",
                "category": "transient",
                "message": "transient provider error",
                "retryable": true
            },
            "effect": "external_write"
        });
        let stopped_json = evaluate_retry_policy_json(
            &serde_json::to_string(&policy).map_err(|error| error.to_string())?,
            &serde_json::to_string(&missing_idempotency).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let stopped =
            serde_json::from_str::<Value>(&stopped_json).map_err(|error| error.to_string())?;
        assert_eq!(stopped.get("decision"), Some(&json!("stop")));
        assert_eq!(
            stopped.get("reason").and_then(Value::as_str),
            Some("missing_idempotency_key")
        );

        let retry_after = json!({
            "attempt": 1,
            "error": {
                "code": "provider.timeout",
                "category": "timeout",
                "message": "provider timed out",
                "retryable": true
            },
            "retryAfterMs": 1_500
        });
        let retry_json = evaluate_retry_policy_json(
            &serde_json::to_string(&policy).map_err(|error| error.to_string())?,
            &serde_json::to_string(&retry_after).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let retry =
            serde_json::from_str::<Value>(&retry_json).map_err(|error| error.to_string())?;
        assert_eq!(retry.get("decision"), Some(&json!("retry")));
        assert_eq!(retry.get("delayMs").and_then(Value::as_u64), Some(1_500));
        assert_eq!(retry.get("reason"), Some(&Value::Null));
        Ok(())
    }

    #[test]
    fn evaluate_provider_limit_policy_json_selects_retry_after_and_fallback() -> Result<(), String>
    {
        let policy = json!({
            "fallbackEnabled": true,
            "queueEnabled": true,
            "credentialOrTopupEnabled": true
        });
        let retry_after_incident = json!({
            "kind": "provider_quota_exceeded",
            "retryAfterMs": 2_500,
            "compatibleFallbacks": ["openai-compatible:gpt-economy"]
        });
        let retry_after_json = evaluate_provider_limit_policy_json(
            &serde_json::to_string(&policy).map_err(|error| error.to_string())?,
            &serde_json::to_string(&retry_after_incident).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let retry_after =
            serde_json::from_str::<Value>(&retry_after_json).map_err(|error| error.to_string())?;
        assert_eq!(retry_after.get("decision"), Some(&json!("retry_after")));
        assert_eq!(
            retry_after.get("delayMs").and_then(Value::as_u64),
            Some(2_500)
        );

        let fallback_incident = json!({
            "kind": "provider_quota_exceeded",
            "compatibleFallbacks": ["openai-compatible:gpt-economy"]
        });
        let fallback_json = evaluate_provider_limit_policy_json(
            &serde_json::to_string(&policy).map_err(|error| error.to_string())?,
            &serde_json::to_string(&fallback_incident).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let fallback =
            serde_json::from_str::<Value>(&fallback_json).map_err(|error| error.to_string())?;
        assert_eq!(fallback.get("decision"), Some(&json!("fallback")));
        assert_eq!(
            fallback.get("target").and_then(Value::as_str),
            Some("openai-compatible:gpt-economy")
        );
        assert_eq!(fallback.get("requiresPolicyRecheck"), Some(&json!(true)));
        Ok(())
    }

    #[test]
    fn evaluate_cancellation_scope_json_propagates_and_preserves_first_reason() -> Result<(), String>
    {
        let root = json!({
            "tokenId": "run",
            "scope": "run",
            "guarantee": "cooperative"
        });
        let operations = json!([
            {
                "op": "child",
                "parentId": "run",
                "tokenId": "provider",
                "scope": "provider_call",
                "guarantee": "best_effort_remote"
            },
            {
                "op": "cancel",
                "tokenId": "run",
                "reason": {
                    "code": "policy_denied",
                    "message": "blocked by output policy",
                    "requestedBy": "policy",
                    "policyDecisionRef": "decision-1"
                }
            },
            {
                "op": "child",
                "parentId": "run",
                "tokenId": "late-task",
                "scope": "task",
                "guarantee": "immediate_local"
            },
            {
                "op": "cancel",
                "tokenId": "run",
                "reason": {"code": "timeout"}
            },
            {
                "op": "effective_guarantee",
                "requested": "immediate_local",
                "capability": "best_effort_remote"
            }
        ]);
        let result_json = evaluate_cancellation_scope_json(
            &serde_json::to_string(&root).map_err(|error| error.to_string())?,
            &serde_json::to_string(&operations).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("ok"), Some(&json!(true)));
        assert_eq!(result.pointer("/operations/1/accepted"), Some(&json!(true)));
        assert_eq!(
            result.pointer("/operations/2/cancelled"),
            Some(&json!(true))
        );
        assert_eq!(
            result.pointer("/operations/3/accepted"),
            Some(&json!(false))
        );
        assert_eq!(
            result
                .pointer("/operations/4/effective")
                .and_then(Value::as_str),
            Some("best_effort_remote")
        );
        assert_eq!(
            result
                .pointer("/stateByTokenId/provider/reason/code")
                .and_then(Value::as_str),
            Some("policy_denied")
        );
        assert_eq!(
            result
                .pointer("/stateByTokenId/late-task/reason/policyDecisionRef")
                .and_then(Value::as_str),
            Some("decision-1")
        );
        assert_eq!(
            result
                .pointer("/stateByTokenId/run/reason/code")
                .and_then(Value::as_str),
            Some("policy_denied")
        );
        Ok(())
    }

    #[test]
    fn evaluate_task_group_json_returns_fail_fast_sibling_cancellations() -> Result<(), String> {
        let group = json!({
            "children": ["dense", "keyword", "tickets"],
            "policy": {
                "minimumSuccesses": 2,
                "failure": "fail_fast",
                "cancellation": "cancel_siblings_on_fatal"
            }
        });
        let operations = json!([
            {"op": "start", "childId": "dense"},
            {"op": "start", "childId": "keyword"},
            {
                "op": "fail",
                "childId": "dense",
                "error": {
                    "code": "provider.timeout",
                    "category": "timeout",
                    "message": "provider timed out",
                    "retryable": true
                }
            }
        ]);
        let result_json = evaluate_task_group_json(
            &serde_json::to_string(&group).map_err(|error| error.to_string())?,
            &serde_json::to_string(&operations).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.pointer("/decision/status").and_then(Value::as_str),
            Some("failed")
        );
        assert_eq!(
            result
                .pointer("/decision/failure/kind")
                .and_then(Value::as_str),
            Some("child_failed")
        );
        assert_eq!(
            result
                .pointer("/decision/failure/error/category")
                .and_then(Value::as_str),
            Some("timeout")
        );
        assert_eq!(
            result
                .pointer("/decision/cancelSiblings")
                .and_then(Value::as_array),
            Some(&vec![json!("keyword"), json!("tickets")])
        );
        assert_eq!(
            result
                .pointer("/children/dense/status")
                .and_then(Value::as_str),
            Some("failed")
        );
        assert_eq!(
            result
                .pointer("/children/keyword/status")
                .and_then(Value::as_str),
            Some("running")
        );
        assert_eq!(
            result
                .pointer("/children/tickets/status")
                .and_then(Value::as_str),
            Some("pending")
        );
        Ok(())
    }

    #[test]
    fn evaluate_node_lifecycle_json_blocks_late_output_after_terminal() -> Result<(), String> {
        let state = json!({"initialStatus": "running"});
        let operations = json!([
            {"op": "output", "port": "value", "value": "before-terminal"},
            {"op": "patch", "patch": {"seen": true}},
            {"op": "complete"},
            {"op": "output", "port": "value", "value": "after-terminal"},
            {"op": "patch", "patch": {"late": true}},
            {
                "op": "fail",
                "error": {
                    "code": "provider.late",
                    "category": "provider",
                    "message": "late provider failure",
                    "retryable": false
                }
            }
        ]);
        let result_json = evaluate_node_lifecycle_json(
            &serde_json::to_string(&state).map_err(|error| error.to_string())?,
            &serde_json::to_string(&operations).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.pointer("/state/status").and_then(Value::as_str),
            Some("completed")
        );
        assert_eq!(result.pointer("/state/terminal"), Some(&json!(true)));
        assert_eq!(
            result
                .pointer("/state/outputs/0/value")
                .and_then(Value::as_str),
            Some("before-terminal")
        );
        assert_eq!(
            result.pointer("/state/statePatches/0"),
            Some(&json!({"seen": true}))
        );
        assert_eq!(
            result
                .pointer("/operations/3/error/code")
                .and_then(Value::as_str),
            Some("OutputAfterTerminal")
        );
        assert_eq!(
            result
                .pointer("/operations/4/error/code")
                .and_then(Value::as_str),
            Some("PatchAfterTerminal")
        );
        assert_eq!(
            result
                .pointer("/operations/5/error/code")
                .and_then(Value::as_str),
            Some("AlreadyTerminal")
        );
        Ok(())
    }

    #[test]
    fn compile_graph_json_matches_shared_tck_cases() -> Result<(), String> {
        let cases = serde_json::from_str::<Value>(include_str!("../../../tck/compiler/cases.json"))
            .map_err(|error| error.to_string())?;
        let cases = cases
            .as_array()
            .ok_or_else(|| "compiler TCK root must be an array".to_owned())?;

        for case in cases {
            let name = case
                .get("name")
                .and_then(Value::as_str)
                .ok_or_else(|| "compiler TCK case is missing name".to_owned())?;
            let document = case
                .get("document")
                .ok_or_else(|| format!("compiler TCK case {name} is missing document"))?;
            let expected = case
                .get("expected")
                .ok_or_else(|| format!("compiler TCK case {name} is missing expected result"))?;
            let expected_hash = expected
                .get("graph_hash")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    format!("compiler TCK case {name} is missing expected graph_hash")
                })?;
            let expected_error_codes = expected
                .get("error_codes")
                .and_then(Value::as_array)
                .ok_or_else(|| format!("compiler TCK case {name} is missing expected error_codes"))?
                .iter()
                .map(|code| {
                    code.as_str().ok_or_else(|| {
                        format!("compiler TCK case {name} has a non-string error code")
                    })
                })
                .collect::<Result<Vec<_>, _>>()?;

            let document_json =
                serde_json::to_string(document).map_err(|error| error.to_string())?;
            let block_catalog_json = case
                .get("block_catalog")
                .map(serde_json::to_string)
                .transpose()
                .map_err(|error| error.to_string())?;
            let compiled_json = compile_graph_json(&document_json, block_catalog_json.as_deref())
                .map_err(|error| error.to_string())?;
            let compiled =
                serde_json::from_str::<Value>(&compiled_json).map_err(|error| error.to_string())?;
            let diagnostics = compiled
                .get("diagnostics")
                .and_then(Value::as_array)
                .ok_or_else(|| {
                    format!("compiler bridge result for {name} is missing diagnostics")
                })?;
            let actual_error_codes = diagnostics
                .iter()
                .filter(|diagnostic| {
                    diagnostic.get("severity").and_then(Value::as_str) == Some("error")
                })
                .map(|diagnostic| {
                    diagnostic
                        .get("code")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            format!("compiler bridge result for {name} has an invalid code")
                        })
                })
                .collect::<Result<Vec<_>, _>>()?;

            assert_eq!(
                compiled.get("hash").and_then(Value::as_str),
                Some(expected_hash),
                "{name}"
            );
            assert_eq!(actual_error_codes, expected_error_codes, "{name}");
        }

        Ok(())
    }

    #[test]
    fn validate_worker_advertisement_json_reports_package_lock_mismatch() -> Result<(), String> {
        let advertisement = json!({
            "protocolVersion": 1,
            "workerId": "worker-local-1",
            "targetId": "doc-cpu",
            "packageLockHash": "sha256:actual",
            "imageDigest": "sha256:image",
            "supportedBlocks": [{"block": "prompt.render@1"}],
            "state": "ready"
        });
        let advertisement_json =
            serde_json::to_string(&advertisement).map_err(|error| error.to_string())?;
        let result_json =
            validate_worker_advertisement_json(&advertisement_json, Some("sha256:expected"))
                .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("ok"), Some(&json!(false)));
        assert_eq!(
            result.pointer("/error/code").and_then(Value::as_str),
            Some("worker.incompatible_package_lock"),
        );
        assert_eq!(
            result.pointer("/error/expected").and_then(Value::as_str),
            Some("sha256:expected"),
        );
        Ok(())
    }

    #[test]
    fn validate_worker_advertisement_json_reports_blank_block_capability() -> Result<(), String> {
        let advertisement = json!({
            "protocolVersion": 1,
            "workerId": "worker-local-1",
            "targetId": "doc-cpu",
            "packageLockHash": "sha256:package-lock",
            "imageDigest": "sha256:image",
            "supportedBlocks": [{"block": " "}],
            "state": "ready"
        });
        let advertisement_json =
            serde_json::to_string(&advertisement).map_err(|error| error.to_string())?;
        let result_json = validate_worker_advertisement_json(&advertisement_json, None)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("ok"), Some(&json!(false)));
        assert_eq!(
            result.pointer("/error/code").and_then(Value::as_str),
            Some("worker.empty_block_capability"),
        );
        Ok(())
    }

    #[test]
    fn validate_worker_protocol_message_json_returns_digest_for_valid_envelope()
    -> Result<(), String> {
        let message = json!({
            "protocolVersion": 1,
            "messageId": "message-000001",
            "kind": "invoke_request",
            "sequence": 7,
            "correlationId": null,
            "causationId": null,
            "payload": {
                "invocationId": "invoke-000001",
                "runId": "run-000001",
                "nodeId": "render",
                "nodeAttemptId": "render-attempt-1",
                "leaseEpoch": 3,
                "block": "prompt.render@1",
                "context": {
                    "releaseId": "release-1",
                    "deploymentRevisionId": "rev-1",
                    "attributes": {}
                },
                "inputs": {"message": {"text": "hi"}},
                "config": {"template": "Echo {message.text}"}
            }
        });
        let result_json = validate_worker_protocol_message_json(
            &serde_json::to_string(&message).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("ok"), Some(&json!(true)));
        assert_eq!(
            result.pointer("/kind").and_then(Value::as_str),
            Some("invoke_request")
        );
        assert_eq!(
            result.pointer("/messageId").and_then(Value::as_str),
            Some("message-000001"),
        );
        assert!(
            result
                .pointer("/contentDigest")
                .and_then(Value::as_str)
                .is_some_and(|digest| digest.starts_with("sha256:")),
        );
        Ok(())
    }

    #[test]
    fn validate_worker_protocol_message_json_reports_invalid_payload() -> Result<(), String> {
        let message = json!({
            "protocolVersion": 1,
            "messageId": "message-000001",
            "kind": "invoke_request",
            "sequence": 7,
            "payload": {
                "invocationId": " ",
                "runId": "run-000001",
                "nodeId": "render",
                "nodeAttemptId": "render-attempt-1",
                "leaseEpoch": 3,
                "block": "prompt.render@1",
                "context": {
                    "releaseId": "release-1",
                    "deploymentRevisionId": "rev-1",
                    "attributes": {}
                },
                "inputs": {},
                "config": {}
            }
        });
        let result_json = validate_worker_protocol_message_json(
            &serde_json::to_string(&message).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("ok"), Some(&json!(false)));
        assert_eq!(
            result.pointer("/error/code").and_then(Value::as_str),
            Some("worker_protocol_message.invalid"),
        );
        assert!(
            result
                .pointer("/error/message")
                .and_then(Value::as_str)
                .is_some_and(|message| message.contains("InvalidInvokeRequest")),
        );
        Ok(())
    }

    #[test]
    fn admit_worker_message_json_returns_daemon_admission_decision() -> Result<(), String> {
        let message = json!({
            "protocolVersion": 1,
            "messageId": "message-worker-1",
            "kind": "advertisement",
            "sequence": 1,
            "correlationId": "worker-1",
            "payload": {
                "protocolVersion": 2,
                "workerId": "worker-1",
                "targetId": "doc-cpu",
                "packageLockHash": "sha256:package-lock",
                "imageDigest": "sha256:image",
                "supportedBlocks": [{"block": "document.parse@1"}],
                "state": "ready"
            }
        });
        let config = json!({
            "daemonId": "daemon-1",
            "bindAddress": "127.0.0.1:8080"
        });
        let result_json = admit_worker_message_json(
            &serde_json::to_string(&message).map_err(|error| error.to_string())?,
            Some(&serde_json::to_string(&config).map_err(|error| error.to_string())?),
            "message-daemon-1",
            2,
        )
        .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("ok"), Some(&json!(true)));
        assert_eq!(
            result.pointer("/response/kind").and_then(Value::as_str),
            Some("admission_decision"),
        );
        assert_eq!(
            result.pointer("/response/payload/admitted"),
            Some(&json!(false)),
        );
        assert_eq!(
            result
                .pointer("/response/payload/reasonCodes/0")
                .and_then(Value::as_str),
            Some("worker.incompatible_protocol_version"),
        );
        assert_eq!(result.pointer("/status/rejectedWorkers"), Some(&json!(1)),);
        Ok(())
    }

    #[test]
    fn validate_remote_payload_json_rejects_oversized_inline_payload() -> Result<(), String> {
        let payload = json!({
            "mode": "inline",
            "schema": "graphblocks.ai/Message@1",
            "value": {"body": "this inline payload is too large"}
        });
        let payload_json = serde_json::to_string(&payload).map_err(|error| error.to_string())?;
        let result_json =
            validate_remote_payload_json(&payload_json, 8).map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("ok"), Some(&json!(false)));
        assert_eq!(
            result.pointer("/error/code").and_then(Value::as_str),
            Some("remote_payload.oversized_inline"),
        );
        assert_eq!(
            result
                .pointer("/error/maxInlineBytes")
                .and_then(Value::as_u64),
            Some(8),
        );
        Ok(())
    }

    #[test]
    fn validate_remote_payload_json_rejects_blank_schema() -> Result<(), String> {
        let payload = json!({
            "mode": "inline",
            "schema": " ",
            "value": {"body": "hello"}
        });
        let payload_json = serde_json::to_string(&payload).map_err(|error| error.to_string())?;
        let result_json =
            validate_remote_payload_json(&payload_json, 128).map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("ok"), Some(&json!(false)));
        assert_eq!(
            result.pointer("/error/code").and_then(Value::as_str),
            Some("remote_payload.invalid_schema"),
        );
        Ok(())
    }

    #[test]
    fn admit_exhaustion_work_json_allows_bounded_finalization_with_matching_permit()
    -> Result<(), String> {
        let policy = json!({
            "preset": "finish_current_turn",
            "unit": "turn",
            "continuation": {
                "maxAdditionalUsage": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "maxAdditionalSteps": 1
            }
        });
        let request = json!({
            "atomicUnitId": "turn:1",
            "admissionEpoch": 7,
            "workKind": "declared_finalization",
            "workEpoch": 8,
            "permit": {
                "permitId": "permit-1",
                "reservationRefs": ["reservation-1"],
                "owner": "worker:1",
                "atomicUnit": "turn:1",
                "admissionEpoch": 7,
                "authorizedAmounts": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "continuationProfile": "finish_current_turn",
                "policySnapshotDigest": "sha256:policy",
                "expiresAt": "2026-06-22T01:00:00Z",
                "fencingTokens": {"budget-1": 1}
            }
        });
        let policy_json = serde_json::to_string(&policy).map_err(|error| error.to_string())?;
        let request_json = serde_json::to_string(&request).map_err(|error| error.to_string())?;
        let result_json = admit_exhaustion_work_json(&policy_json, &request_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("allowed"), Some(&json!(true)));
        assert_eq!(
            result.get("reason").and_then(Value::as_str),
            Some("allowed")
        );
        assert_eq!(
            result.get("usedAdditionalSteps").and_then(Value::as_u64),
            Some(1),
        );
        Ok(())
    }

    #[test]
    fn decide_agent_step_json_uses_rust_agent_loop_boundaries() -> Result<(), String> {
        let spec = json!({
            "modelPool": "support-models",
            "maxSteps": 4,
            "completionReserveUnits": 100
        });
        let spec_json = serde_json::to_string(&spec).map_err(|error| error.to_string())?;

        let stop_request = json!({"completedSteps": 4, "remainingBudgetUnits": 1_000});
        let stop_json = serde_json::to_string(&stop_request).map_err(|error| error.to_string())?;
        let stop = serde_json::from_str::<Value>(
            &decide_agent_step_json(&spec_json, &stop_json).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;

        assert_eq!(
            stop.get("disposition").and_then(Value::as_str),
            Some("stop")
        );
        assert_eq!(
            stop.get("reason").and_then(Value::as_str),
            Some("max_steps_reached")
        );

        let finalize_request = json!({"completedSteps": 3, "remainingBudgetUnits": 100});
        let finalize_json =
            serde_json::to_string(&finalize_request).map_err(|error| error.to_string())?;
        let finalize = serde_json::from_str::<Value>(
            &decide_agent_step_json(&spec_json, &finalize_json)
                .map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;

        assert_eq!(
            finalize.get("disposition").and_then(Value::as_str),
            Some("finalize")
        );
        assert_eq!(
            finalize.get("reason").and_then(Value::as_str),
            Some("completion_reserve_reached")
        );

        let continue_request = json!({"completedSteps": 3, "remainingBudgetUnits": 101});
        let continue_json =
            serde_json::to_string(&continue_request).map_err(|error| error.to_string())?;
        let continued = serde_json::from_str::<Value>(
            &decide_agent_step_json(&spec_json, &continue_json)
                .map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;

        assert_eq!(
            continued.get("disposition").and_then(Value::as_str),
            Some("continue")
        );
        assert_eq!(
            continued.get("reason").and_then(Value::as_str),
            Some("admitted")
        );
        Ok(())
    }

    #[test]
    fn admit_exhaustion_work_json_rejects_mismatched_permit() -> Result<(), String> {
        let policy = json!({
            "preset": "finish_current_turn",
            "unit": "turn",
            "continuation": {
                "maxAdditionalUsage": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "maxAdditionalSteps": 1
            }
        });
        let request = json!({
            "atomicUnitId": "turn:1",
            "admissionEpoch": 7,
            "workKind": "declared_finalization",
            "workEpoch": 8,
            "permit": {
                "permitId": "permit-2",
                "reservationRefs": ["reservation-1"],
                "owner": "worker:1",
                "atomicUnit": "turn:other",
                "admissionEpoch": 7,
                "authorizedAmounts": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "continuationProfile": "finish_current_turn",
                "policySnapshotDigest": "sha256:policy",
                "expiresAt": "2026-06-22T01:00:00Z",
                "fencingTokens": {"budget-1": 1}
            }
        });
        let policy_json = serde_json::to_string(&policy).map_err(|error| error.to_string())?;
        let request_json = serde_json::to_string(&request).map_err(|error| error.to_string())?;
        let result_json = admit_exhaustion_work_json(&policy_json, &request_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("allowed"), Some(&json!(false)));
        assert_eq!(
            result.get("reason").and_then(Value::as_str),
            Some("invalid_permit"),
        );
        Ok(())
    }

    #[test]
    fn admit_exhaustion_work_json_rejects_expired_permit_at_validation_time() -> Result<(), String>
    {
        let policy = json!({
            "preset": "finish_current_turn",
            "unit": "turn",
            "continuation": {
                "maxAdditionalUsage": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "maxAdditionalSteps": 1
            }
        });
        let request = json!({
            "atomicUnitId": "turn:1",
            "admissionEpoch": 7,
            "workKind": "declared_finalization",
            "workEpoch": 8,
            "validationTime": "2026-06-22T01:00:00Z",
            "permit": {
                "permitId": "permit-1",
                "reservationRefs": ["reservation-1"],
                "owner": "worker:1",
                "atomicUnit": "turn:1",
                "admissionEpoch": 7,
                "authorizedAmounts": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "continuationProfile": "finish_current_turn",
                "policySnapshotDigest": "sha256:policy",
                "expiresAt": "2026-06-22T01:00:00Z",
                "fencingTokens": {"budget-1": 1}
            }
        });
        let policy_json = serde_json::to_string(&policy).map_err(|error| error.to_string())?;
        let request_json = serde_json::to_string(&request).map_err(|error| error.to_string())?;
        let result_json = admit_exhaustion_work_json(&policy_json, &request_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("allowed"), Some(&json!(false)));
        assert_eq!(
            result.get("reason").and_then(Value::as_str),
            Some("invalid_permit"),
        );
        Ok(())
    }

    #[test]
    fn admit_exhaustion_work_json_rejects_requested_usage_above_permit() -> Result<(), String> {
        let policy = json!({
            "preset": "finish_current_turn",
            "unit": "turn",
            "continuation": {
                "maxAdditionalUsage": [
                    {"kind": "model_output_tokens", "amount": 200, "unit": "tokens"}
                ],
                "maxAdditionalSteps": 1
            }
        });
        let request = json!({
            "atomicUnitId": "turn:1",
            "admissionEpoch": 7,
            "workKind": "declared_finalization",
            "workEpoch": 8,
            "requestedUsage": [
                {"kind": "model_output_tokens", "amount": 101, "unit": "tokens"}
            ],
            "permit": {
                "permitId": "permit-1",
                "reservationRefs": ["reservation-1"],
                "owner": "worker:1",
                "atomicUnit": "turn:1",
                "admissionEpoch": 7,
                "authorizedAmounts": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "continuationProfile": "finish_current_turn",
                "policySnapshotDigest": "sha256:policy",
                "expiresAt": "2026-06-22T01:00:00Z",
                "fencingTokens": {"budget-1": 1}
            }
        });
        let policy_json = serde_json::to_string(&policy).map_err(|error| error.to_string())?;
        let request_json = serde_json::to_string(&request).map_err(|error| error.to_string())?;
        let result_json = admit_exhaustion_work_json(&policy_json, &request_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("allowed"), Some(&json!(false)));
        assert_eq!(
            result.get("reason").and_then(Value::as_str),
            Some("usage_exceeds_permit"),
        );
        Ok(())
    }

    #[test]
    fn run_test_graph_json_executes_compiled_graph_with_fixture_outputs() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-runtime-bridge"},
            "spec": {
                "nodes": {
                    "model": {
                        "block": "model.generate@1",
                        "inputs": {"prompt": "render.prompt"},
                        "outputs": {"response": "$output.answer"}
                    },
                    "render": {
                        "block": "prompt.render@1",
                        "inputs": {"message": "$input.message"}
                    }
                }
            }
        });
        let node_outputs = json!({
            "render": {"prompt": "rendered"},
            "model": {"response": "generated"}
        });

        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let node_outputs_json =
            serde_json::to_string(&node_outputs).map_err(|error| error.to_string())?;
        let result_json =
            run_test_graph_json(&graph_json, r#"{"message":"hello"}"#, &node_outputs_json)
                .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let journal = result
            .get("journal")
            .and_then(Value::as_array)
            .ok_or_else(|| "runtime bridge result is missing journal".to_owned())?;
        let completed_nodes = journal
            .iter()
            .filter(|record| record.get("kind").and_then(Value::as_str) == Some("node_completed"))
            .map(|record| {
                record
                    .get("nodeId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "node_completed record is missing nodeId".to_owned())
            })
            .collect::<Result<Vec<_>, _>>()?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            result
                .get("outputs")
                .and_then(|outputs| outputs.get("answer"))
                .and_then(Value::as_str),
            Some("generated")
        );
        assert_eq!(completed_nodes, vec!["render", "model"]);

        Ok(())
    }

    #[test]
    fn evaluate_tool_result_stream_json_rejects_late_delta_after_policy_stop() -> Result<(), String>
    {
        let operations = json!([
            {
                "kind": "event",
                "event": {
                    "kind": "started",
                    "toolCallId": "call-1",
                    "sequence": 1,
                    "startedAtUnixMs": 1_000
                }
            },
            {
                "kind": "event",
                "event": {
                    "kind": "delta",
                    "toolCallId": "call-1",
                    "sequence": 2,
                    "output": [{"kind": "text", "text": "draft"}]
                }
            },
            {
                "kind": "event",
                "event": {
                    "kind": "policy_stopped",
                    "toolCallId": "call-1",
                    "sequence": 3,
                    "result": {
                        "toolCallId": "call-1",
                        "status": "policy_stopped",
                        "error": {
                            "code": "policy.denied",
                            "category": "policy",
                            "message": "blocked",
                            "retryable": false
                        },
                        "startedAtUnixMs": 1_000,
                        "completedAtUnixMs": 1_050
                    }
                }
            },
            {
                "kind": "event",
                "event": {
                    "kind": "delta",
                    "toolCallId": "call-1",
                    "sequence": 4,
                    "output": [{"kind": "text", "text": "late"}]
                }
            }
        ]);

        let output_json = evaluate_tool_result_stream_json(
            "{}",
            &serde_json::to_string(&operations).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let output =
            serde_json::from_str::<Value>(&output_json).map_err(|error| error.to_string())?;

        assert_eq!(output.get("ok"), Some(&json!(false)));
        assert_eq!(
            output.pointer("/state/lastSequences/call-1"),
            Some(&json!(3))
        );
        assert_eq!(
            output.pointer("/state/finalResults/call-1/status"),
            Some(&json!("policy_stopped"))
        );
        assert_eq!(
            output.pointer("/updates/3/error/code"),
            Some(&json!("event_after_final_result"))
        );
        assert_eq!(
            output.pointer("/updates/3/error/finalStatus"),
            Some(&json!("policy_stopped"))
        );
        assert_eq!(
            output
                .pointer("/state/acceptedEvents")
                .and_then(Value::as_array)
                .map(Vec::len),
            Some(3)
        );
        Ok(())
    }

    #[test]
    fn evaluate_tool_result_stream_json_reports_started_ordering_errors() -> Result<(), String> {
        let operations = json!([
            {
                "kind": "event",
                "event": {
                    "kind": "delta",
                    "toolCallId": "call-1",
                    "sequence": 1,
                    "output": [{"kind": "text", "text": "draft"}]
                }
            },
            {
                "kind": "event",
                "event": {
                    "kind": "started",
                    "toolCallId": "call-2",
                    "sequence": 1,
                    "startedAtUnixMs": 1_000
                }
            },
            {
                "kind": "event",
                "event": {
                    "kind": "started",
                    "toolCallId": "call-2",
                    "sequence": 2,
                    "startedAtUnixMs": 1_050
                }
            }
        ]);

        let output_json = evaluate_tool_result_stream_json(
            "{}",
            &serde_json::to_string(&operations).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let output =
            serde_json::from_str::<Value>(&output_json).map_err(|error| error.to_string())?;

        assert_eq!(output.get("ok"), Some(&json!(false)));
        assert_eq!(
            output.pointer("/updates/0/error/code"),
            Some(&json!("event_before_started"))
        );
        assert_eq!(
            output.pointer("/updates/0/error/kind"),
            Some(&json!("delta"))
        );
        assert_eq!(
            output.pointer("/updates/2/error/code"),
            Some(&json!("duplicate_started"))
        );
        assert_eq!(
            output.pointer("/updates/2/error/lastSequence"),
            Some(&json!(1))
        );
        assert_eq!(
            output.pointer("/state/lastSequences/call-2"),
            Some(&json!(1))
        );
        assert_eq!(
            output
                .pointer("/state/acceptedEvents")
                .and_then(Value::as_array)
                .map(Vec::len),
            Some(1)
        );
        Ok(())
    }

    #[test]
    fn evaluate_application_event_stream_json_drops_late_events_after_cutoff() -> Result<(), String>
    {
        let metadata = |event_id: &str, sequence: u64| {
            json!({
                "eventId": event_id,
                "runId": "run-1",
                "responseId": "response-1",
                "turnId": "turn-1",
                "sequence": sequence,
                "releaseId": "release-1",
                "policySnapshotId": "policy-1",
                "occurredAtUnixMs": 1_000 + sequence,
            })
        };
        let operations = json!([
            {
                "kind": "event",
                "event": {
                    "kind": "OutputCutoff",
                    "metadata": metadata("event-cutoff", 1),
                    "payload": {
                        "stream_id": "stream-1",
                        "response_id": "response-1",
                        "turn_id": "turn-1",
                        "last_generated_sequence": 4,
                        "last_policy_accepted_sequence": 2,
                        "last_client_delivered_sequence": 2,
                        "terminal_reason": "policy_denied",
                        "draft_disposition": "retract",
                        "durable_result": "none",
                        "policy_decision_id": "decision-1",
                        "occurred_at_unix_ms": 1_010
                    }
                }
            },
            {
                "kind": "event",
                "event": {
                    "kind": "OutputPolicyEvaluationStarted",
                    "metadata": metadata("event-late", 2),
                    "payload": {
                        "response_id": "response-1",
                        "chunk_sequence": 3,
                        "input_digest": "sha256:late"
                    }
                }
            },
            {
                "kind": "event",
                "event": {
                    "kind": "AssistantRetracted",
                    "metadata": metadata("event-retract", 3),
                    "payload": {
                        "response_id": "response-1",
                        "last_client_delivered_sequence": 2,
                        "terminal_reason": "policy_denied"
                    }
                }
            }
        ]);

        let output_json = evaluate_application_event_stream_json(
            "{}",
            &serde_json::to_string(&operations).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let output =
            serde_json::from_str::<Value>(&output_json).map_err(|error| error.to_string())?;

        assert_eq!(output.get("ok"), Some(&json!(true)));
        assert_eq!(output.pointer("/updates/0/kind"), Some(&json!("accepted")));
        assert_eq!(output.pointer("/updates/1/kind"), Some(&json!("dropped")));
        assert_eq!(output.pointer("/updates/2/kind"), Some(&json!("accepted")));
        assert_eq!(
            output.pointer("/state/cutoffResponses/0"),
            Some(&json!("response-1"))
        );
        assert_eq!(
            output
                .pointer("/state/acceptedEvents")
                .and_then(Value::as_array)
                .map(Vec::len),
            Some(2)
        );
        Ok(())
    }

    #[test]
    fn evaluate_application_protocol_stream_json_drops_late_events_after_cutoff()
    -> Result<(), String> {
        let metadata = |event_id: &str, sequence: u64| {
            json!({
                "eventId": event_id,
                "protocolVersion": "graphblocks.app.v1",
                "runId": "run-1",
                "turnId": "turn-1",
                "sequence": sequence,
                "cursor": format!("cursor-{sequence}"),
                "occurredAtUnixMs": 1_000 + sequence,
            })
        };
        let operations = json!([
            {
                "kind": "event",
                "event": {
                    "kind": "AssistantDraftDelta",
                    "metadata": metadata("event-delta-1", 1),
                    "payload": {
                        "response_id": "response-1",
                        "chunk_sequence": 1,
                        "delta": "allowed"
                    }
                }
            },
            {
                "kind": "event",
                "event": {
                    "kind": "OutputCutoff",
                    "metadata": metadata("event-cutoff", 2),
                    "payload": {
                        "response_id": "response-1",
                        "last_client_delivered_sequence": 1,
                        "terminal_reason": "policy_denied"
                    }
                }
            },
            {
                "kind": "event",
                "event": {
                    "kind": "AssistantDraftDelta",
                    "metadata": metadata("event-delta-2", 3),
                    "payload": {
                        "response_id": "response-1",
                        "chunk_sequence": 2,
                        "delta": "blocked"
                    }
                }
            },
            {
                "kind": "event",
                "event": {
                    "kind": "AssistantIncomplete",
                    "metadata": metadata("event-incomplete", 4),
                    "payload": {
                        "response_id": "response-1",
                        "terminal_reason": "policy_denied"
                    }
                }
            }
        ]);

        let output_json = evaluate_application_protocol_stream_json(
            "{}",
            &serde_json::to_string(&operations).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let output =
            serde_json::from_str::<Value>(&output_json).map_err(|error| error.to_string())?;

        assert_eq!(output.get("ok"), Some(&json!(true)));
        assert_eq!(output.pointer("/updates/0/kind"), Some(&json!("accepted")));
        assert_eq!(output.pointer("/updates/1/kind"), Some(&json!("accepted")));
        assert_eq!(output.pointer("/updates/2/kind"), Some(&json!("dropped")));
        assert_eq!(output.pointer("/updates/3/kind"), Some(&json!("accepted")));
        assert_eq!(
            output.pointer("/state/cutoffResponses/0"),
            Some(&json!("response-1"))
        );
        assert_eq!(
            output
                .pointer("/state/acceptedEvents")
                .and_then(Value::as_array)
                .map(Vec::len),
            Some(3)
        );
        Ok(())
    }

    #[test]
    fn evaluate_application_protocol_log_json_appends_duplicates_and_replays() -> Result<(), String>
    {
        let event = |event_id: &str, sequence: u64, cursor: &str| {
            json!({
                "kind": "JobProgress",
                "metadata": {
                    "eventId": event_id,
                    "protocolVersion": "graphblocks.app.v1",
                    "runId": "run-1",
                    "turnId": "turn-1",
                    "sequence": sequence,
                    "cursor": cursor,
                    "occurredAtUnixMs": 1_000 + sequence
                },
                "payload": {"done": sequence, "total": 2}
            })
        };
        let first = event("event-1", 1, "cursor-1");
        let second = event("event-2", 2, "cursor-2");
        let operations = json!([
            {"kind": "append", "event": first.clone()},
            {"kind": "append", "event": first},
            {"kind": "append", "event": second},
            {"kind": "replay_after", "cursor": "cursor-1", "limit": 10}
        ]);

        let output_json = evaluate_application_protocol_log_json(
            "{}",
            &serde_json::to_string(&operations).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let output =
            serde_json::from_str::<Value>(&output_json).map_err(|error| error.to_string())?;

        assert_eq!(output.get("ok"), Some(&json!(true)));
        assert_eq!(output.pointer("/updates/0/kind"), Some(&json!("appended")));
        assert_eq!(output.pointer("/updates/1/kind"), Some(&json!("duplicate")));
        assert_eq!(output.pointer("/updates/2/kind"), Some(&json!("appended")));
        assert_eq!(output.pointer("/updates/3/kind"), Some(&json!("replay")));
        assert_eq!(
            output.pointer("/updates/3/events/0/metadata/eventId"),
            Some(&json!("event-2"))
        );
        assert_eq!(output.pointer("/state/length"), Some(&json!(2)));
        assert_eq!(
            output
                .pointer("/state/events")
                .and_then(Value::as_array)
                .map(Vec::len),
            Some(2)
        );
        Ok(())
    }

    #[test]
    fn evaluate_application_protocol_log_json_reports_non_monotonic_append() -> Result<(), String> {
        let event = |event_id: &str, sequence: u64| {
            json!({
                "kind": "JobProgress",
                "metadata": {
                    "eventId": event_id,
                    "protocolVersion": "graphblocks.app.v1",
                    "runId": "run-1",
                    "turnId": "turn-1",
                    "sequence": sequence,
                    "cursor": format!("cursor-{sequence}"),
                    "occurredAtUnixMs": 1_000 + sequence
                },
                "payload": {"done": sequence, "total": 2}
            })
        };
        let operations = json!([
            {"kind": "append", "event": event("event-2", 2)},
            {"kind": "append", "event": event("event-1", 1)}
        ]);

        let output_json = evaluate_application_protocol_log_json(
            "{}",
            &serde_json::to_string(&operations).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let output =
            serde_json::from_str::<Value>(&output_json).map_err(|error| error.to_string())?;

        assert_eq!(output.get("ok"), Some(&json!(false)));
        assert_eq!(output.pointer("/updates/0/kind"), Some(&json!("appended")));
        assert_eq!(output.pointer("/updates/1/kind"), Some(&json!("error")));
        assert_eq!(
            output.pointer("/updates/1/error/code"),
            Some(&json!("non_monotonic_sequence"))
        );
        assert_eq!(output.pointer("/state/length"), Some(&json!(1)));
        Ok(())
    }

    #[test]
    fn negotiate_application_protocol_capabilities_json_intersects_sets() -> Result<(), String> {
        let server = json!({
            "protocolVersion": "graphblocks.app.v1",
            "commands": ["InvokeGraph", "CancelRun"],
            "events": ["RunStarted", "RunCompleted"]
        });
        let client = json!({
            "protocol_version": "graphblocks.app.v1",
            "commands": ["CancelRun", "OpenArtifact"],
            "events": ["RunCompleted", "ArtifactReady"]
        });

        let output_json = negotiate_application_protocol_capabilities_json(
            &serde_json::to_string(&server).map_err(|error| error.to_string())?,
            &serde_json::to_string(&client).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let output =
            serde_json::from_str::<Value>(&output_json).map_err(|error| error.to_string())?;

        assert_eq!(output.get("ok"), Some(&json!(true)));
        assert_eq!(
            output.get("protocolVersion"),
            Some(&json!("graphblocks.app.v1"))
        );
        assert_eq!(output.get("commands"), Some(&json!(["CancelRun"])));
        assert_eq!(output.get("events"), Some(&json!(["RunCompleted"])));
        Ok(())
    }

    #[test]
    fn negotiate_application_protocol_capabilities_json_rejects_version_mismatch()
    -> Result<(), String> {
        let server = json!({
            "protocolVersion": "graphblocks.app.v1",
            "commands": ["InvokeGraph"],
            "events": ["RunStarted"]
        });
        let client = json!({
            "protocolVersion": "graphblocks.app.v2",
            "commands": ["InvokeGraph"],
            "events": ["RunStarted"]
        });

        let result = negotiate_application_protocol_capabilities_json(
            &serde_json::to_string(&server).map_err(|error| error.to_string())?,
            &serde_json::to_string(&client).map_err(|error| error.to_string())?,
        );

        assert!(result.is_err());
        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_delivers_accepted_chunks_and_records_cutoff() -> Result<(), String>
    {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "turnId": "turn-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 2,
                "onViolation": "abort_response",
                "deliveredDraftDisposition": "retract"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe "
            },
            {
                "kind": "chunk",
                "sequence": 2,
                "text": "blocked"
            },
            {
                "kind": "decision",
                "decisionId": "decision-allow",
                "disposition": "allow",
                "acceptedThroughSequence": 1,
                "inputDigest": "sha256:allow",
                "occurredAtUnixMs": 1_000
            },
            {
                "kind": "decision",
                "decisionId": "decision-abort",
                "disposition": "abort_response",
                "inputDigest": "sha256:abort",
                "providerCancellation": "required_if_supported",
                "occurredAtUnixMs": 1_010
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.first())
                .and_then(|delivery| delivery.get("chunks"))
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("safe ")
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastGeneratedSequence"))
                .and_then(Value::as_u64),
            Some(2)
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastPolicyAcceptedSequence"))
                .and_then(Value::as_u64),
            Some(1)
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastClientDeliveredSequence"))
                .and_then(Value::as_u64),
            Some(1)
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("draftDisposition"))
                .and_then(Value::as_str),
            Some("retract")
        );
        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.last())
                .and_then(|delivery| delivery.get("pendingToolCalls"))
                .and_then(Value::as_str),
            Some("deny")
        );
        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.last())
                .and_then(|delivery| delivery.get("providerCancellation"))
                .and_then(Value::as_str),
            Some("required_if_supported")
        );
        assert_eq!(
            result
                .get("lastClientDeliveredSequence")
                .and_then(Value::as_u64),
            Some(1)
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_records_terminal_accepted_prefix() -> Result<(), String> {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 2,
                "onViolation": "abort_response"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe "
            },
            {
                "kind": "chunk",
                "sequence": 2,
                "text": "blocked"
            },
            {
                "kind": "decision",
                "decisionId": "decision-abort",
                "disposition": "abort_response",
                "acceptedThroughSequence": 1,
                "inputDigest": "sha256:abort",
                "occurredAtUnixMs": 1_010
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastPolicyAcceptedSequence"))
                .and_then(Value::as_u64),
            Some(1)
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastClientDeliveredSequence"))
                .and_then(Value::as_u64),
            Some(0)
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_resumes_pending_holdback_state() -> Result<(), String> {
        pyo3::Python::initialize();
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "turnId": "turn-1",
            "lastGeneratedSequence": 2,
            "lastPolicyAcceptedSequence": 1,
            "lastClientDeliveredSequence": 1,
            "pending": [
                {
                    "sequence": 2,
                    "text": "held"
                }
            ],
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 4,
                "onViolation": "abort_response",
                "deliveredDraftDisposition": "retract"
            }
        });
        let operations = json!([
            {
                "kind": "decision",
                "decisionId": "decision-2",
                "disposition": "allow",
                "acceptedThroughSequence": 2,
                "inputDigest": "sha256:second",
                "occurredAtUnixMs": 1_010
            },
            {
                "kind": "chunk",
                "sequence": 3,
                "text": " next"
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.first())
                .and_then(|delivery| delivery.get("chunks"))
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("held")
        );
        assert_eq!(
            result.get("lastGeneratedSequence").and_then(Value::as_u64),
            Some(3)
        );
        assert_eq!(
            result
                .get("lastClientDeliveredSequence")
                .and_then(Value::as_u64),
            Some(2)
        );
        assert_eq!(
            result
                .get("pending")
                .and_then(Value::as_array)
                .and_then(|pending| pending.first())
                .and_then(|chunk| chunk.get("sequence"))
                .and_then(Value::as_u64),
            Some(3)
        );
        assert_eq!(
            result
                .get("pending")
                .and_then(Value::as_array)
                .and_then(|pending| pending.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some(" next")
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_resumes_terminal_cutoff_state() -> Result<(), String> {
        pyo3::Python::initialize();
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "cutoff": {
                "streamId": "stream-1",
                "responseId": "response-1",
                "turnId": "turn-1",
                "lastGeneratedSequence": 2,
                "lastPolicyAcceptedSequence": 1,
                "lastClientDeliveredSequence": 1,
                "terminalReason": "policy_denied",
                "draftDisposition": "retract",
                "durableResult": "none",
                "policyDecisionId": "decision-abort",
                "occurredAtUnixMs": 1_100
            }
        });
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let empty_operations =
            serde_json::to_string(&json!([])).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &empty_operations)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("policyDecisionId"))
                .and_then(Value::as_str),
            Some("decision-abort")
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastClientDeliveredSequence"))
                .and_then(Value::as_u64),
            Some(1)
        );

        let late_operations = serde_json::to_string(&json!([
            {
                "kind": "chunk",
                "sequence": 3,
                "text": "late"
            }
        ]))
        .map_err(|error| error.to_string())?;
        let error = evaluate_output_gate_json(&gate_json, &late_operations)
            .expect_err("late chunks must remain blocked after a resumed cutoff");
        assert!(error.to_string().contains("PolicyStopped"));

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_accepts_snake_case_cutoff_state() -> Result<(), String> {
        pyo3::Python::initialize();
        let gate = json!({
            "stream_id": "stream-1",
            "response_id": "response-1",
            "cutoff": {
                "stream_id": "stream-1",
                "response_id": "response-1",
                "turn_id": "turn-1",
                "last_generated_sequence": 2,
                "last_policy_accepted_sequence": 1,
                "last_client_delivered_sequence": 1,
                "terminal_reason": "policy_denied",
                "draft_disposition": "retract",
                "durable_result": "none",
                "policy_decision_id": "decision-abort",
                "occurred_at_unix_ms": 1_100
            }
        });
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let empty_operations =
            serde_json::to_string(&json!([])).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &empty_operations)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("policyDecisionId"))
                .and_then(Value::as_str),
            Some("decision-abort")
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastClientDeliveredSequence"))
                .and_then(Value::as_u64),
            Some(1)
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_accepts_snake_case_decision_operation() -> Result<(), String> {
        pyo3::Python::initialize();
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "onViolation": "abort_response",
                "holdbackMaxTokens": 16
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe draft"
            },
            {
                "kind": "decision",
                "decision_id": "decision-allow",
                "disposition": "allow",
                "accepted_through_sequence": 1,
                "input_digest": "sha256:accepted",
                "reason_codes": ["safe"],
                "policy_refs": ["policy/output"],
                "provider_cancellation": "none",
                "draft_disposition": "keep",
                "pending_tool_calls": "keep",
                "evaluated_at_unix_ms": 1_000,
                "occurred_at_unix_ms": 1_010
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.first())
                .and_then(|delivery| delivery.get("chunks"))
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("safe draft")
        );
        assert_eq!(
            result
                .get("decisions")
                .and_then(Value::as_array)
                .and_then(|decisions| decisions.first())
                .and_then(|decision| decision.get("decisionId"))
                .and_then(Value::as_str),
            Some("decision-allow")
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_accepts_snake_case_delivery_policy() -> Result<(), String> {
        pyo3::Python::initialize();
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "delivery_policy": {
                "mode": "immediate_draft",
                "on_violation": "abort_response",
                "delivered_draft_disposition": "retract",
                "flush_boundaries": ["sentence"]
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe draft"
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.first())
                .and_then(|delivery| delivery.get("draft"))
                .and_then(Value::as_bool),
            Some(true)
        );
        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.first())
                .and_then(|delivery| delivery.get("chunks"))
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("safe draft")
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_honors_snake_case_chunk_identity() -> Result<(), String> {
        pyo3::Python::initialize();
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1"
        });
        let operations = json!([
            {
                "kind": "chunk",
                "stream_id": "stream-other",
                "response_id": "response-1",
                "sequence": 1,
                "text": "misrouted"
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let error = evaluate_output_gate_json(&gate_json, &operations_json)
            .expect_err("snake_case chunk stream mismatch should be rejected");

        assert!(error.to_string().contains("StreamMismatch"));

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_emits_immediate_draft_before_retraction() -> Result<(), String> {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "immediate_draft",
                "onViolation": "abort_response",
                "deliveredDraftDisposition": "retract"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "provisional draft"
            },
            {
                "kind": "decision",
                "decisionId": "decision-abort",
                "disposition": "abort_response",
                "inputDigest": "sha256:abort",
                "occurredAtUnixMs": 1_010
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let deliveries = result
            .get("deliveries")
            .and_then(Value::as_array)
            .ok_or_else(|| "output gate result is missing deliveries".to_owned())?;

        assert_eq!(
            deliveries
                .first()
                .and_then(|delivery| delivery.get("operationIndex"))
                .and_then(Value::as_u64),
            Some(0)
        );
        assert_eq!(
            deliveries
                .first()
                .and_then(|delivery| delivery.get("draft"))
                .and_then(Value::as_bool),
            Some(true)
        );
        assert_eq!(
            deliveries
                .first()
                .and_then(|delivery| delivery.get("chunks"))
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("provisional draft")
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastPolicyAcceptedSequence"))
                .and_then(Value::as_u64),
            Some(0)
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastClientDeliveredSequence"))
                .and_then(Value::as_u64),
            Some(1)
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("draftDisposition"))
                .and_then(Value::as_str),
            Some("retract")
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_applies_redaction_instructions() -> Result<(), String> {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 8,
                "onViolation": "abort_response"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "hello secret world"
            },
            {
                "kind": "decision",
                "decisionId": "decision-redact",
                "disposition": "redact",
                "acceptedThroughSequence": 1,
                "inputDigest": "sha256:redact",
                "redactions": [
                    {
                        "path": "/chunks/1/text",
                        "start": 6,
                        "end": 12,
                        "replacement": "[redacted]"
                    }
                ],
                "occurredAtUnixMs": 1_000
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.first())
                .and_then(|delivery| delivery.get("chunks"))
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("hello [redacted] world")
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_accepts_replacement_parts() -> Result<(), String> {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 8,
                "onViolation": "abort_response"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "blocked draft"
            },
            {
                "kind": "decision",
                "decisionId": "decision-replace",
                "disposition": "replace",
                "acceptedThroughSequence": 1,
                "inputDigest": "sha256:replace",
                "replacementParts": [
                    {"kind": "text", "text": "policy-approved "},
                    {"kind": "text", "text": "replacement"}
                ],
                "occurredAtUnixMs": 1_000
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let chunks = result
            .get("deliveries")
            .and_then(Value::as_array)
            .and_then(|deliveries| deliveries.first())
            .and_then(|delivery| delivery.get("chunks"))
            .and_then(Value::as_array)
            .ok_or_else(|| "missing replacement delivery chunks".to_owned())?;

        assert_eq!(
            chunks
                .iter()
                .map(|chunk| (
                    chunk.get("sequence").and_then(Value::as_u64),
                    chunk.get("text").and_then(Value::as_str),
                ))
                .collect::<Vec<_>>(),
            vec![
                (Some(1), Some("policy-approved ")),
                (Some(2), Some("replacement")),
            ],
        );
        assert_eq!(
            result
                .get("lastPolicyAcceptedSequence")
                .and_then(Value::as_u64),
            Some(2)
        );
        assert_eq!(
            result
                .get("lastClientDeliveredSequence")
                .and_then(Value::as_u64),
            Some(2)
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_preserves_pending_prefix_on_replacement_parts()
    -> Result<(), String> {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 8,
                "onViolation": "abort_response"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe "
            },
            {
                "kind": "chunk",
                "sequence": 2,
                "text": "context "
            },
            {
                "kind": "chunk",
                "sequence": 3,
                "text": "secret"
            },
            {
                "kind": "decision",
                "decisionId": "decision-replace",
                "disposition": "replace",
                "acceptedThroughSequence": 3,
                "inputDigest": "sha256:replace",
                "replacementParts": [
                    {"kind": "text", "text": "[redacted]"}
                ],
                "occurredAtUnixMs": 1_000
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let chunks = result
            .get("deliveries")
            .and_then(Value::as_array)
            .and_then(|deliveries| deliveries.first())
            .and_then(|delivery| delivery.get("chunks"))
            .and_then(Value::as_array)
            .ok_or_else(|| "missing replacement delivery chunks".to_owned())?;

        assert_eq!(
            chunks
                .iter()
                .map(|chunk| (
                    chunk.get("sequence").and_then(Value::as_u64),
                    chunk.get("text").and_then(Value::as_str),
                ))
                .collect::<Vec<_>>(),
            vec![
                (Some(1), Some("safe ")),
                (Some(2), Some("context ")),
                (Some(3), Some("[redacted]")),
            ],
        );
        assert_eq!(
            result
                .get("lastPolicyAcceptedSequence")
                .and_then(Value::as_u64),
            Some(3)
        );
        assert_eq!(
            result
                .get("lastClientDeliveredSequence")
                .and_then(Value::as_u64),
            Some(3)
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_enforces_token_holdback_limit() -> Result<(), String> {
        pyo3::Python::initialize();

        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 3,
                "onViolation": "abort_response"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe text"
            },
            {
                "kind": "chunk",
                "sequence": 2,
                "text": "still"
            },
            {
                "kind": "chunk",
                "sequence": 3,
                "text": "blocked"
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let error = evaluate_output_gate_json(&gate_json, &operations_json)
            .expect_err("token holdback overflow should be rejected")
            .to_string();

        assert!(error.contains("BoundedHoldbackTokensExceeded"));
        assert!(error.contains("max_tokens: 3"));

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_preserves_decision_metadata() -> Result<(), String> {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 8,
                "onViolation": "abort_response"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe"
            },
            {
                "kind": "decision",
                "decisionId": "decision-allow",
                "disposition": "allow",
                "acceptedThroughSequence": 1,
                "inputDigest": "sha256:allow",
                "reasonCodes": ["pii.clear"],
                "policyRefs": ["policy/output-standard"],
                "providerCancellation": "required_if_supported",
                "draftDisposition": "mark_incomplete",
                "pendingToolCalls": "cancel_admitted",
                "evaluatedAtUnixMs": 995,
                "occurredAtUnixMs": 1_000
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let decision = result
            .get("decisions")
            .and_then(Value::as_array)
            .and_then(|decisions| decisions.first())
            .ok_or_else(|| "missing output decision trace".to_owned())?;

        assert_eq!(
            decision.get("decisionId").and_then(Value::as_str),
            Some("decision-allow")
        );
        assert_eq!(decision.get("reasonCodes"), Some(&json!(["pii.clear"])));
        assert_eq!(
            decision.get("policyRefs"),
            Some(&json!(["policy/output-standard"]))
        );
        assert_eq!(
            decision.get("evaluatedAtUnixMs").and_then(Value::as_u64),
            Some(995)
        );
        assert_eq!(
            decision.get("providerCancellation").and_then(Value::as_str),
            Some("required_if_supported")
        );
        assert_eq!(
            decision.get("draftDisposition").and_then(Value::as_str),
            Some("mark_incomplete")
        );
        assert_eq!(
            decision.get("pendingToolCalls").and_then(Value::as_str),
            Some("cancel_admitted")
        );

        Ok(())
    }

    #[test]
    fn evaluate_tool_execution_plan_json_runs_native_plan_operations() -> Result<(), String> {
        let plan_json = json!({
            "planId": "plan-1",
            "responseId": "response-1",
            "maximumParallelism": 2,
            "effectKeyTemplate": "{tool.name}:{arguments.resource_id}",
            "calls": [
                {
                    "toolCallId": "call-a",
                    "toolName": "ticket.create",
                    "arguments": {"resource_id": "ticket-1"},
                    "effects": ["external_write"],
                    "cancellation": "force_terminable"
                },
                {
                    "toolCallId": "call-b",
                    "toolName": "ticket.create",
                    "arguments": {"resource_id": "ticket-1"},
                    "effects": ["external_write"],
                    "cancellation": "force_terminable"
                },
                {
                    "toolCallId": "call-c",
                    "toolName": "knowledge.search",
                    "arguments": {"resource_id": "docs"},
                    "effects": ["external_read"]
                }
            ]
        })
        .to_string();
        let operations_json = json!([
            {"op": "ready"},
            {"op": "start", "toolCallId": "call-a"},
            {"op": "ready"},
            {"op": "policy_stop", "pendingToolCalls": "cancel_admitted"}
        ])
        .to_string();

        let payload = evaluate_tool_execution_plan_json(&plan_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let payload = serde_json::from_str::<Value>(&payload).map_err(|error| error.to_string())?;

        assert_eq!(
            payload["operations"][0]["ready"],
            json!(["call-a", "call-c"])
        );
        assert_eq!(payload["operations"][1]["error"], Value::Null);
        assert_eq!(payload["operations"][2]["ready"], json!(["call-c"]));
        assert_eq!(
            payload["operations"][3]["affected"],
            json!(["call-a", "call-b", "call-c"])
        );
        assert_eq!(payload["states"]["call-a"], json!("cancelled"));
        assert_eq!(payload["states"]["call-b"], json!("denied"));
        assert_eq!(payload["states"]["call-c"], json!("denied"));
        Ok(())
    }

    #[test]
    fn evaluate_tool_execution_plan_json_reports_native_creation_errors() -> Result<(), String> {
        pyo3::Python::initialize();
        let plan_json = json!({
            "planId": "plan-1",
            "responseId": "response-1",
            "maximumParallelism": 2,
            "calls": [
                {
                    "toolCallId": "call-a",
                    "toolName": "ticket.create",
                    "arguments": {"resource_id": "ticket-1"},
                    "effects": ["external_write"]
                },
                {
                    "toolCallId": "call-b",
                    "toolName": "ticket.create",
                    "arguments": {"resource_id": "ticket-2"},
                    "effects": ["external_write"]
                }
            ]
        })
        .to_string();

        let error = evaluate_tool_execution_plan_json(&plan_json, "[]")
            .expect_err("unsafe parallel effects should be rejected")
            .to_string();

        assert!(error.contains("unsafe_parallel_effects"), "{error}");
        Ok(())
    }

    #[test]
    fn evaluate_sequential_tool_queue_json_runs_admitted_calls_in_order() -> Result<(), String> {
        let admitted_call = |tool_call_id: &str| -> Result<Value, String> {
            let draft_json = json!({
                "toolCallId": tool_call_id,
                "responseId": "response-1",
                "toolName": "knowledge.search",
                "status": "arguments_complete",
                "argumentFragments": ["{\"resource_id\":\"docs\"}"],
                "sequence": 1
            })
            .to_string();
            let call_json = finalize_tool_call_json(&draft_json, "resolved-tool-1", 1_000)
                .map_err(|error| error.to_string())?;
            let mut call =
                serde_json::from_str::<Value>(&call_json).map_err(|error| error.to_string())?;
            call["status"] = json!("admitted");
            call["admittedAtUnixMs"] = json!(1_100);
            Ok(call)
        };
        let call_a = admitted_call("call-a")?;
        let mut call_b = admitted_call("call-b")?;
        call_b["dependsOn"] = json!(["call-a"]);
        let queue_json = json!({
            "planId": "plan-1",
            "responseId": "response-1",
            "calls": [
                {"call": call_a, "effects": ["external_read"]},
                {"call": call_b, "effects": ["external_read"]}
            ]
        })
        .to_string();
        let operations_json = json!([
            {"op": "start_next_ready"},
            {"op": "start_next_ready"},
            {"op": "complete", "toolCallId": "call-a"},
            {"op": "start_next_ready"},
            {"op": "complete", "toolCallId": "call-b"}
        ])
        .to_string();

        let payload = evaluate_sequential_tool_queue_json(&queue_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let payload = serde_json::from_str::<Value>(&payload).map_err(|error| error.to_string())?;

        assert_eq!(payload["operations"][0]["started"], json!("call-a"));
        assert_eq!(payload["operations"][1]["started"], Value::Null);
        assert_eq!(payload["operations"][2]["error"], Value::Null);
        assert_eq!(payload["operations"][3]["started"], json!("call-b"));
        assert_eq!(payload["operations"][4]["error"], Value::Null);
        assert_eq!(payload["runningCallId"], Value::Null);
        assert_eq!(payload["states"]["call-a"], json!("completed"));
        assert_eq!(payload["states"]["call-b"], json!("completed"));
        Ok(())
    }

    #[test]
    fn evaluate_sequential_tool_queue_json_rejects_non_admitted_calls() -> Result<(), String> {
        pyo3::Python::initialize();
        let draft_json = json!({
            "toolCallId": "call-a",
            "responseId": "response-1",
            "toolName": "knowledge.search",
            "status": "arguments_complete",
            "argumentFragments": ["{}"],
            "sequence": 1
        })
        .to_string();
        let call = serde_json::from_str::<Value>(
            &finalize_tool_call_json(&draft_json, "resolved-tool-1", 1_000)
                .map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let queue_json = json!({
            "planId": "plan-1",
            "responseId": "response-1",
            "calls": [{"call": call}]
        })
        .to_string();

        let error = evaluate_sequential_tool_queue_json(&queue_json, "[]")
            .expect_err("sequential queue should require admitted tool calls")
            .to_string();

        assert!(error.contains("tool_call_not_admitted"), "{error}");
        Ok(())
    }

    #[test]
    fn evaluate_usage_ledger_json_reconciles_and_reports_invalid_records() -> Result<(), String> {
        let operations_json = json!([
            {
                "op": "append",
                "record": {
                    "recordId": "usage-provisional",
                    "source": "tokenizer_estimated",
                    "confidence": "estimated",
                    "amounts": [
                        {
                            "kind": "model_output_tokens",
                            "amount": 18,
                            "unit": "tokens"
                        }
                    ],
                    "occurredAtUnixMs": 1_700_000,
                    "runId": "run-1",
                    "attemptId": "attempt-1",
                    "providerResponseId": "resp-1"
                }
            },
            {
                "op": "reconcile",
                "sourceRecordId": "usage-provisional",
                "recordId": "usage-reconciled",
                "amounts": [
                    {
                        "kind": "model_output_tokens",
                        "amount": 21,
                        "unit": "tokens"
                    }
                ],
                "occurredAtUnixMs": 1_700_300
            },
            {
                "op": "append",
                "record": {
                    "recordId": "usage-negative",
                    "source": "runtime_measured",
                    "confidence": "exact",
                    "amounts": [
                        {
                            "kind": "model_output_tokens",
                            "amount": -1,
                            "unit": "tokens"
                        }
                    ],
                    "occurredAtUnixMs": 1_700_400,
                    "runId": "run-1"
                }
            }
        ])
        .to_string();

        let payload = evaluate_usage_ledger_json(&operations_json, Some("run-1"))
            .map_err(|error| error.to_string())?;
        let payload = serde_json::from_str::<Value>(&payload).map_err(|error| error.to_string())?;

        assert_eq!(payload["ok"], json!(false));
        assert_eq!(
            payload["appendResults"],
            json!(["usage-provisional", "usage-reconciled"])
        );
        assert_eq!(
            payload["recordIds"],
            json!(["usage-provisional", "usage-reconciled"])
        );
        assert_eq!(
            payload["totals"],
            json!([
                {
                    "kind": "model_output_tokens",
                    "amount": 21,
                    "unit": "tokens",
                    "dimensions": {}
                }
            ])
        );
        assert_eq!(payload["operations"][0]["error"], Value::Null);
        assert_eq!(payload["operations"][1]["error"], Value::Null);
        assert_eq!(payload["operations"][2]["error"], json!("invalid_record"));
        assert_eq!(
            payload["operations"][2]["errorMessage"],
            json!("usage amount must be non-negative")
        );
        Ok(())
    }

    #[test]
    fn evaluate_durable_tool_terminal_store_json_replays_terminal_records() -> Result<(), String> {
        let record = json!({
            "runId": "run-000001",
            "responseId": "response-1",
            "toolCallId": "call-1",
            "revision": 1,
            "terminalState": "completed",
            "argumentsDigest": "sha256:arguments",
            "outputDigest": "sha256:output",
            "idempotencyKey": "ticket-create:call-1",
            "effectCommitted": true,
            "durableResultCommitted": true,
            "completedAtUnixMs": 1_820_000_000_000_u64
        });
        let operations_json = json!([
            {"op": "record_tool_terminal", "record": record},
            {"op": "record_tool_terminal", "record": record},
            {"op": "tool_terminal_count"}
        ])
        .to_string();

        let payload = evaluate_durable_tool_terminal_store_json(&operations_json)
            .map_err(|error| error.to_string())?;
        let payload = serde_json::from_str::<Value>(&payload).map_err(|error| error.to_string())?;

        assert_eq!(payload["operations"][0]["commit"]["sequence"], json!(1));
        assert_eq!(payload["operations"][0]["commit"]["replayed"], json!(false));
        assert_eq!(payload["operations"][1]["commit"]["sequence"], json!(1));
        assert_eq!(payload["operations"][1]["commit"]["replayed"], json!(true));
        assert_eq!(payload["operations"][2]["count"], json!(1));
        assert_eq!(payload["toolTerminalCount"], json!(1));
        Ok(())
    }

    #[test]
    fn evaluate_durable_tool_terminal_store_json_blocks_late_result_after_policy_stop()
    -> Result<(), String> {
        let operations_json = json!([
            {
                "op": "record_response_policy_stop",
                "record": {
                    "responseId": "response-1",
                    "policyDecisionId": "decision-abort",
                    "streamId": "stream-1",
                    "turnId": "turn-1",
                    "lastGeneratedSequence": 9,
                    "lastPolicyAcceptedSequence": 7,
                    "lastClientDeliveredSequence": 6,
                    "terminalReason": "policy_denied",
                    "draftDisposition": "retract",
                    "durableResult": "none",
                    "occurredAtUnixMs": 1_820_000_000_000_u64
                }
            },
            {
                "op": "record_tool_terminal",
                "record": {
                    "runId": "run-000001",
                    "responseId": "response-1",
                    "toolCallId": "call-1",
                    "revision": 1,
                    "terminalState": "completed",
                    "argumentsDigest": "sha256:arguments",
                    "outputDigest": "sha256:output",
                    "durableResultCommitted": true,
                    "completedAtUnixMs": 1_820_000_000_100_u64
                }
            },
            {
                "op": "record_tool_terminal",
                "record": {
                    "runId": "run-000001",
                    "responseId": "response-1",
                    "toolCallId": "call-2",
                    "revision": 1,
                    "terminalState": "cancelled",
                    "argumentsDigest": "sha256:arguments-late",
                    "effectCommitted": true,
                    "completedAtUnixMs": 1_820_000_000_200_u64
                }
            }
        ])
        .to_string();

        let payload = evaluate_durable_tool_terminal_store_json(&operations_json)
            .map_err(|error| error.to_string())?;
        let payload = serde_json::from_str::<Value>(&payload).map_err(|error| error.to_string())?;

        assert_eq!(payload["operations"][0]["commit"]["sequence"], json!(1));
        assert_eq!(
            payload["operations"][0]["commit"]["outputCutoff"]["lastClientDeliveredSequence"],
            json!(6)
        );
        assert_eq!(
            payload["operations"][1]["error"],
            json!("response_policy_stopped")
        );
        assert_eq!(payload["operations"][2]["commit"]["sequence"], json!(2));
        assert_eq!(
            payload["operations"][2]["commit"]["record"]["terminalState"],
            json!("cancelled")
        );
        assert_eq!(
            payload["operations"][2]["commit"]["record"]["effectCommitted"],
            json!(true)
        );
        assert_eq!(payload["toolTerminalCount"], json!(1));
        Ok(())
    }

    #[test]
    fn evaluate_declarative_output_policy_json_returns_redaction_decision() -> Result<(), String> {
        let rules = json!([
            {
                "ruleId": "redact-secret",
                "literal": "secret",
                "disposition": "redact",
                "replacement": "[redacted]",
                "reasonCodes": ["pii.secret"],
                "policyRefs": ["policy/output-standard#redact-secret"],
                "priority": 10
            }
        ]);
        let chunk = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "sequence": 7,
            "text": "hello secret"
        });
        let rules_json = serde_json::to_string(&rules).map_err(|error| error.to_string())?;
        let chunk_json = serde_json::to_string(&chunk).map_err(|error| error.to_string())?;

        let result_json = evaluate_declarative_output_policy_json(&rules_json, &chunk_json, 1_000)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("disposition").and_then(Value::as_str),
            Some("redact")
        );
        assert_eq!(
            result
                .get("acceptedThroughSequence")
                .and_then(Value::as_u64),
            Some(7)
        );
        assert_eq!(result.get("reasonCodes"), Some(&json!(["pii.secret"])));
        assert_eq!(
            result.get("policyRefs"),
            Some(&json!(["policy/output-standard#redact-secret"]))
        );
        assert_eq!(
            result
                .get("redactions")
                .and_then(Value::as_array)
                .and_then(|redactions| redactions.first()),
            Some(&json!({
                "path": "/chunks/7/text",
                "start": 6,
                "end": 12,
                "replacement": "[redacted]"
            }))
        );
        assert!(
            result
                .get("decisionId")
                .and_then(Value::as_str)
                .is_some_and(|decision_id| decision_id.starts_with("output-decision:sha256:"))
        );
        assert!(
            result
                .get("inputDigest")
                .and_then(Value::as_str)
                .is_some_and(|digest| digest.starts_with("sha256:"))
        );
        assert_eq!(
            result.get("evaluatedAtUnixMs").and_then(Value::as_u64),
            Some(1_000)
        );

        Ok(())
    }

    #[test]
    fn evaluate_declarative_output_policy_json_returns_replacement_parts() -> Result<(), String> {
        let rules = json!([
            {
                "ruleId": "blocked",
                "literal": "blocked",
                "disposition": "replace",
                "replacement": "policy-approved",
                "reasonCodes": ["content.replaced"]
            }
        ]);
        let chunk = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "sequence": 1,
            "text": "blocked"
        });
        let rules_json = serde_json::to_string(&rules).map_err(|error| error.to_string())?;
        let chunk_json = serde_json::to_string(&chunk).map_err(|error| error.to_string())?;

        let result_json = evaluate_declarative_output_policy_json(&rules_json, &chunk_json, 1_000)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("disposition").and_then(Value::as_str),
            Some("replace")
        );
        assert_eq!(
            result
                .get("replacementParts")
                .and_then(Value::as_array)
                .and_then(|parts| parts.first())
                .and_then(|part| part.get("kind"))
                .and_then(Value::as_str),
            Some("text")
        );
        assert_eq!(
            result
                .get("replacementParts")
                .and_then(Value::as_array)
                .and_then(|parts| parts.first())
                .and_then(|part| part.get("text"))
                .and_then(Value::as_str),
            Some("policy-approved")
        );
        assert_eq!(
            result
                .get("replacementChunks")
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("policy-approved")
        );

        Ok(())
    }

    #[test]
    fn run_test_graph_json_blocks_missing_external_inputs() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-runtime-missing-input"},
            "spec": {
                "nodes": {
                    "render": {
                        "block": "prompt.render@1",
                        "inputs": {"message": "$input.message"},
                        "outputs": {"prompt": "$output.prompt"}
                    }
                }
            }
        });
        let node_outputs = json!({"render": {"prompt": "rendered"}});
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let node_outputs_json =
            serde_json::to_string(&node_outputs).map_err(|error| error.to_string())?;

        let result_json = run_test_graph_json(&graph_json, "{}", &node_outputs_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("status").and_then(Value::as_str), Some("failed"));
        assert_eq!(
            result
                .get("journal")
                .and_then(Value::as_array)
                .and_then(|journal| journal.last())
                .and_then(|record| record.get("kind"))
                .and_then(Value::as_str),
            Some("run_failed")
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_executes_conversation_vertical_slice() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-conversation-slice"},
            "spec": {
                "interface": {
                    "inputs": {"message": "graphblocks.ai/Message@1"},
                    "outputs": {"answer": "graphblocks.ai/Answer@1"}
                },
                "nodes": {
                    "begin": {"block": "conversation.begin_turn@1"},
                    "render": {
                        "block": "prompt.render@1",
                        "config": {"template": "Answer: {message.text}"},
                        "inputs": {"message": "$input.message"}
                    },
                    "generate": {
                        "block": "model.generate@1",
                        "config": {"script": {"Answer: Hello": "Hello from Rust."}},
                        "inputs": {"prompt": "render.prompt"}
                    },
                    "commit": {
                        "block": "conversation.commit_turn@1",
                        "inputs": {
                            "transaction": "begin.transaction",
                            "candidate": "generate.response"
                        },
                        "outputs": {"answer": "$output.answer"}
                    }
                }
            }
        });
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let result_json = run_stdlib_graph_json(&graph_json, r#"{"message":{"text":"Hello"}}"#)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let journal = result
            .get("journal")
            .and_then(Value::as_array)
            .ok_or_else(|| "stdlib runtime result is missing journal".to_owned())?;
        let completed_nodes = journal
            .iter()
            .filter(|record| record.get("kind").and_then(Value::as_str) == Some("node_completed"))
            .map(|record| {
                record
                    .get("nodeId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "node_completed record is missing nodeId".to_owned())
            })
            .collect::<Result<Vec<_>, _>>()?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            result
                .get("outputs")
                .and_then(|outputs| outputs.get("answer")),
            Some(&json!({
                "conversationId": "conversation-default",
                "text": "Hello from Rust.",
                "turnId": "turn-000001"
            }))
        );
        assert_eq!(
            completed_nodes,
            vec!["begin", "render", "generate", "commit"]
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_executes_scripted_agent_run() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-scripted-agent"},
            "spec": {
                "interface": {
                    "inputs": {
                        "messages": "graphblocks.ai/Messages@1",
                        "tools": "graphblocks.ai/ResolvedTools@1"
                    },
                    "outputs": {"candidate": "graphblocks.ai/AssistantCandidate@1"}
                },
                "nodes": {
                    "agent": {
                        "block": "agent.run@1",
                        "config": {"response": "native scripted response"},
                        "inputs": {
                            "messages": "$input.messages",
                            "tools": "$input.tools"
                        },
                        "outputs": {"candidate": "$output.candidate"}
                    }
                }
            }
        });
        let inputs = json!({
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [{"definition": {"name": "knowledge.search"}}]
        });
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let inputs_json = serde_json::to_string(&inputs).map_err(|error| error.to_string())?;
        let result_json =
            run_stdlib_graph_json(&graph_json, &inputs_json).map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            result
                .get("outputs")
                .and_then(|outputs| outputs.get("candidate")),
            Some(&json!({
                "finishReason": "scripted",
                "modelVisibleTools": [
                    {
                        "allowedForPrincipal": false,
                        "bindingDigest": "",
                        "definitionDigest": "",
                        "effectivePolicySnapshotId": "",
                        "resolvedToolId": "",
                        "toolName": "knowledge.search",
                        "validUntil": null
                    }
                ],
                "text": "native scripted response",
                "toolCount": 1
            }))
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_resolves_tools_for_scripted_agent() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-agent-tools"},
            "spec": {
                "interface": {
                    "inputs": {"messages": "graphblocks.ai/Messages@1"},
                    "outputs": {
                        "candidate": "graphblocks.ai/AssistantCandidate@1",
                        "tools": "graphblocks.ai/ResolvedTools@1"
                    }
                },
                "nodes": {
                    "resolve": {
                        "block": "tools.resolve@1",
                        "config": {
                            "effectivePolicySnapshotId": "policy-snapshot-1",
                            "definitions": [
                                {
                                    "name": "knowledge.search",
                                    "description": "Search support documentation.",
                                    "inputSchema": "schemas/SearchRequest@1"
                                }
                            ],
                            "bindings": [
                                {
                                    "bindingId": "binding-search",
                                    "toolName": "knowledge.search",
                                    "implementation": {"kind": "block", "block": "knowledge.search@1"},
                                    "effects": ["external_read"],
                                    "approval": "never",
                                    "timeoutMs": 250
                                }
                            ],
                            "scope": {"principalTools": ["knowledge.search"]}
                        },
                        "outputs": {"tools": "$output.tools"}
                    },
                    "agent": {
                        "block": "agent.run@1",
                        "config": {"response": "Hello from native agent."},
                        "inputs": {
                            "messages": "$input.messages",
                            "tools": "resolve.tools"
                        },
                        "outputs": {"candidate": "$output.candidate"}
                    }
                }
            }
        });
        let inputs = json!({"messages": [{"role": "user", "content": "Hello"}]});
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let inputs_json = serde_json::to_string(&inputs).map_err(|error| error.to_string())?;
        let result_json =
            run_stdlib_graph_json(&graph_json, &inputs_json).map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        let candidate = result
            .get("outputs")
            .and_then(|outputs| outputs.get("candidate"))
            .ok_or_else(|| "candidate output missing".to_owned())?;
        assert_eq!(
            candidate.get("finishReason").and_then(Value::as_str),
            Some("scripted")
        );
        assert_eq!(
            candidate.get("text").and_then(Value::as_str),
            Some("Hello from native agent.")
        );
        assert_eq!(candidate.get("toolCount").and_then(Value::as_u64), Some(1));
        let resolved_tool = result
            .get("outputs")
            .and_then(|outputs| outputs.get("tools"))
            .and_then(Value::as_array)
            .and_then(|tools| tools.first())
            .ok_or_else(|| "resolved tools output missing first tool".to_owned())?;
        assert_eq!(
            resolved_tool
                .get("definition")
                .and_then(|definition| definition.get("name"))
                .and_then(Value::as_str),
            Some("knowledge.search")
        );
        assert_eq!(
            resolved_tool
                .get("allowed_for_principal")
                .and_then(Value::as_bool),
            Some(true)
        );
        assert_eq!(
            resolved_tool
                .get("binding")
                .and_then(|binding| binding.get("timeout_ms"))
                .and_then(Value::as_u64),
            Some(250)
        );
        assert_eq!(
            candidate.get("modelVisibleTools"),
            Some(&json!([
                {
                    "allowedForPrincipal": true,
                    "bindingDigest": resolved_tool["binding_digest"],
                    "definitionDigest": resolved_tool["definition_digest"],
                    "effectivePolicySnapshotId": resolved_tool["effective_policy_snapshot_id"],
                    "resolvedToolId": resolved_tool["resolved_tool_id"],
                    "toolName": "knowledge.search",
                    "validUntil": resolved_tool["valid_until"]
                }
            ]))
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_maps_items_with_native_block() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-map-prompts"},
            "spec": {
                "interface": {
                    "inputs": {"items": "graphblocks.ai/Items@1"},
                    "outputs": {"values": "graphblocks.ai/Values@1"}
                },
                "nodes": {
                    "map": {
                        "block": "control.map@2",
                        "inputs": {"items": "$input.items"},
                        "outputs": {"values": "$output.values"},
                        "config": {
                            "block": "prompt.render@1",
                            "inputName": "message",
                            "outputName": "prompt",
                            "config": {"template": "Item {message.index}: {message.text}"}
                        }
                    }
                }
            }
        });
        let inputs = json!({
            "items": [
                {"index": 1, "text": "alpha"},
                {"index": 2, "text": "beta"}
            ]
        });
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let inputs_json = serde_json::to_string(&inputs).map_err(|error| error.to_string())?;
        let result_json =
            run_stdlib_graph_json(&graph_json, &inputs_json).map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            result
                .get("outputs")
                .and_then(|outputs| outputs.get("values")),
            Some(&json!(["Item 1: alpha", "Item 2: beta"]))
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_collects_native_map_errors() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-map-collect-errors"},
            "spec": {
                "interface": {
                    "inputs": {"items": "graphblocks.ai/Items@1"},
                    "outputs": {"outcomes": "graphblocks.ai/MapOutcomes@1"}
                },
                "nodes": {
                    "map": {
                        "block": "control.map@2",
                        "inputs": {"items": "$input.items"},
                        "outputs": {"outcomes": "$output.outcomes"},
                        "config": {
                            "block": "prompt.render@1",
                            "inputName": "message",
                            "outputName": "prompt",
                            "onError": "collect",
                            "config": {"template": "Value {message.text}"}
                        }
                    }
                }
            }
        });
        let inputs = json!({"items": [{"text": "ok"}, {}]});
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let inputs_json = serde_json::to_string(&inputs).map_err(|error| error.to_string())?;
        let result_json =
            run_stdlib_graph_json(&graph_json, &inputs_json).map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let outcomes = result
            .get("outputs")
            .and_then(|outputs| outputs.get("outcomes"))
            .and_then(Value::as_array)
            .ok_or_else(|| "native map result is missing outcomes".to_owned())?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            outcomes.first(),
            Some(&json!({"status": "succeeded", "value": "Value ok"}))
        );
        assert_eq!(
            outcomes
                .get(1)
                .and_then(|outcome| outcome.get("status"))
                .and_then(Value::as_str),
            Some("failed")
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_selects_native_cases() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-select-first"},
            "spec": {
                "interface": {
                    "inputs": {"cases": "graphblocks.ai/Cases@1"},
                    "outputs": {
                        "value": "graphblocks.ai/SelectedValue@1",
                        "selected": "graphblocks.ai/SelectedKey@1"
                    }
                },
                "nodes": {
                    "select": {
                        "block": "control.select@1",
                        "inputs": {"cases": "$input.cases"},
                        "outputs": {
                            "value": "$output.value",
                            "selected": "$output.selected"
                        },
                        "config": {"order": ["preferred", "fallback"], "default": "unused"}
                    }
                }
            }
        });
        let inputs = json!({"cases": {"preferred": null, "fallback": "fallback-value"}});
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let inputs_json = serde_json::to_string(&inputs).map_err(|error| error.to_string())?;
        let result_json =
            run_stdlib_graph_json(&graph_json, &inputs_json).map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            result.get("outputs"),
            Some(&json!({"selected": "preferred", "value": null}))
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_blocks_commit_after_policy_stop() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-policy-stopped-turn"},
            "spec": {
                "interface": {
                    "inputs": {"message": "graphblocks.ai/Message@1"},
                    "outputs": {"answer": "graphblocks.ai/Answer@1"}
                },
                "nodes": {
                    "begin": {"block": "conversation.begin_turn@1"},
                    "render": {
                        "block": "prompt.render@1",
                        "config": {"template": "Answer: {message.text}"},
                        "inputs": {"message": "$input.message"}
                    },
                    "generate": {
                        "block": "model.generate@1",
                        "config": {"script": {"Answer: Hello": "blocked answer"}},
                        "inputs": {"prompt": "render.prompt"}
                    },
                    "stop": {
                        "block": "conversation.policy_stop_turn@1",
                        "inputs": {"transaction": "begin.transaction"}
                    },
                    "commit": {
                        "block": "conversation.commit_turn@1",
                        "inputs": {
                            "transaction": "stop.transaction",
                            "candidate": "generate.response"
                        },
                        "outputs": {"answer": "$output.answer"}
                    }
                }
            }
        });
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let result_json = run_stdlib_graph_json(&graph_json, r#"{"message":{"text":"Hello"}}"#)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let journal = result
            .get("journal")
            .and_then(Value::as_array)
            .ok_or_else(|| "stdlib runtime result is missing journal".to_owned())?;
        let failed = journal
            .iter()
            .find(|record| record.get("kind").and_then(Value::as_str) == Some("node_failed"))
            .ok_or_else(|| "stdlib runtime result is missing node_failed".to_owned())?;

        assert_eq!(result.get("status").and_then(Value::as_str), Some("failed"));
        assert_eq!(
            result
                .get("outputs")
                .and_then(|outputs| outputs.get("answer")),
            None
        );
        assert_eq!(failed.get("nodeId").and_then(Value::as_str), Some("commit"));
        assert_eq!(
            failed
                .get("payload")
                .and_then(|payload| payload.get("message"))
                .and_then(Value::as_str),
            Some("conversation.commit_turn@1 cannot commit policy-stopped turn")
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_passes_shared_runtime_tck_cases() -> Result<(), String> {
        let cases_path =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tck/runtime/cases.json");
        let cases_text = std::fs::read_to_string(&cases_path).map_err(|error| error.to_string())?;
        let cases =
            serde_json::from_str::<Value>(&cases_text).map_err(|error| error.to_string())?;
        let cases = cases
            .as_array()
            .ok_or_else(|| "runtime TCK root must be an array".to_owned())?;

        for case in cases {
            let case_name = case
                .get("name")
                .and_then(Value::as_str)
                .ok_or_else(|| "runtime TCK case missing name".to_owned())?;
            let document = case
                .get("document")
                .ok_or_else(|| format!("runtime TCK case {case_name} missing document"))?;
            let expected = case
                .get("expected")
                .and_then(Value::as_object)
                .ok_or_else(|| format!("runtime TCK case {case_name} missing expected object"))?;
            let inputs = case.get("inputs").cloned().unwrap_or_else(|| json!({}));
            let result_json = run_stdlib_graph_json(
                &serde_json::to_string(document).map_err(|error| error.to_string())?,
                &serde_json::to_string(&inputs).map_err(|error| error.to_string())?,
            )
            .map_err(|error| error.to_string())?;
            let result =
                serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
            let terminal_kind = result
                .get("journal")
                .and_then(Value::as_array)
                .and_then(|journal| {
                    journal.iter().rev().find(|record| {
                        record.get("terminal").and_then(Value::as_bool) == Some(true)
                    })
                })
                .and_then(|record| record.get("kind"))
                .and_then(Value::as_str);

            assert_eq!(
                result.get("status").and_then(Value::as_str),
                expected.get("status").and_then(Value::as_str),
                "runtime TCK case {case_name} status mismatch",
            );
            assert_eq!(
                terminal_kind,
                expected.get("terminal_kind").and_then(Value::as_str),
                "runtime TCK case {case_name} terminal kind mismatch",
            );
            assert_eq!(
                result.get("outputs"),
                expected.get("outputs"),
                "runtime TCK case {case_name} outputs mismatch",
            );
        }

        Ok(())
    }
}

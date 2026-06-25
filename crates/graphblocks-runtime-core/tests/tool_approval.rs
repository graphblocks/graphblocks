use graphblocks_runtime_core::tool::{
    BlockToolImplementation, ToolBinding, ToolCatalog, ToolDefinition, ToolEffect,
    ToolImplementation, ToolResolutionError, ToolResolutionScope,
};
use graphblocks_runtime_core::tool_approval::{
    ToolApprovalError, ToolApprovalRecord, ToolApprovalRequest, ToolApprovalStatus,
};
use graphblocks_runtime_core::tool_call::{ToolCall, ToolCallDraft, ToolCallError};
use serde_json::json;

fn resolved_search_tool()
-> Result<graphblocks_runtime_core::tool::ResolvedTool, ToolResolutionError> {
    let catalog = ToolCatalog::new(
        [ToolDefinition::new(
            "knowledge.search",
            "Search documentation.",
            "schemas/Search@1",
        )],
        [ToolBinding::new(
            "binding-search",
            "knowledge.search",
            ToolImplementation::Block(BlockToolImplementation::new("blocks.search")),
        )
        .with_effects([ToolEffect::ExternalRead])],
    )?;
    let mut resolved = catalog.resolve(ToolResolutionScope::new(), "policy-snapshot-1")?;
    Ok(resolved.remove(0))
}

fn search_call(tool_call_id: &str, query: &str) -> Result<ToolCall, ToolCallError> {
    let mut draft = ToolCallDraft::proposed("response-1", tool_call_id, "knowledge.search");
    draft.append_argument_fragment(format!("{{\"query\":\"{query}\"}}"))?;
    draft.into_completed_tool_call("resolved-tool-1", 1_000)
}

#[test]
fn approved_record_is_valid_only_for_same_tool_call_and_arguments() {
    let resolved = resolved_search_tool().expect("resolved tool is valid");
    let mut call = search_call("call-1", "runtime").expect("tool call is valid");
    call.resolved_tool_id = resolved.resolved_tool_id.clone();
    let request =
        ToolApprovalRequest::for_call("approval-1", &resolved, &call, "user-1", 1_000, 2_000)
            .expect("approval request is valid");

    let record = ToolApprovalRecord::approve(request.clone(), "admin-1", 1_100);
    let mismatched_record = ToolApprovalRecord {
        approval_id: "approval-other".to_owned(),
        request: request.clone(),
        status: ToolApprovalStatus::Approved,
        approver_id: Some("admin-1".to_owned()),
        decided_at_unix_ms: Some(1_100),
        invalidated_at_unix_ms: None,
        reason: None,
    };
    let invalidated_approved_record = ToolApprovalRecord {
        approval_id: request.approval_id.clone(),
        request: request.clone(),
        status: ToolApprovalStatus::Approved,
        approver_id: Some("admin-1".to_owned()),
        decided_at_unix_ms: Some(1_100),
        invalidated_at_unix_ms: Some(1_200),
        reason: None,
    };

    assert_eq!(record.status, ToolApprovalStatus::Approved);
    assert_eq!(record.request.revision, 1);
    assert!(record.is_valid_for(&resolved, &call, "user-1", 1_500));
    assert!(!mismatched_record.is_valid_for(&resolved, &call, "user-1", 1_500));
    assert!(!invalidated_approved_record.is_valid_for(&resolved, &call, "user-1", 1_500));

    let mut changed_args = search_call("call-1", "changed").expect("changed tool call is valid");
    changed_args.resolved_tool_id = resolved.resolved_tool_id.clone();
    assert!(!record.is_valid_for(&resolved, &changed_args, "user-1", 1_500));
    assert!(!record.is_valid_for(&resolved, &call, "user-2", 1_500));
    assert!(!record.is_valid_for(&resolved, &call, "user-1", 2_001));
}

#[test]
fn approved_record_is_invalid_after_argument_revision() {
    let resolved = resolved_search_tool().expect("resolved tool is valid");
    let mut call = search_call("call-1", "runtime").expect("tool call is valid");
    call.resolved_tool_id = resolved.resolved_tool_id.clone();
    let request =
        ToolApprovalRequest::for_call("approval-1", &resolved, &call, "user-1", 1_000, 2_000)
            .expect("approval request is valid");
    let record = ToolApprovalRecord::approve(request, "admin-1", 1_100);

    let revised = call
        .revise_arguments(json!({"query": "changed"}))
        .expect("validated call can be revised");

    assert_eq!(revised.revision, 2);
    assert!(!record.is_valid_for(&resolved, &revised, "user-1", 1_500));
}

#[test]
fn denied_record_never_authorizes_tool_execution() {
    let resolved = resolved_search_tool().expect("resolved tool is valid");
    let mut call = search_call("call-1", "runtime").expect("tool call is valid");
    call.resolved_tool_id = resolved.resolved_tool_id.clone();
    let request =
        ToolApprovalRequest::for_call("approval-1", &resolved, &call, "user-1", 1_000, 2_000)
            .expect("approval request is valid");

    let record = ToolApprovalRecord::deny(request, "admin-1", 1_100, "not approved");

    assert_eq!(record.status, ToolApprovalStatus::Denied);
    assert_eq!(record.reason.as_deref(), Some("not approved"));
    assert!(!record.is_valid_for(&resolved, &call, "user-1", 1_500));
}

#[test]
fn approval_request_rejects_mismatched_resolved_tool() {
    let resolved = resolved_search_tool().expect("resolved tool is valid");
    let call = search_call("call-1", "runtime").expect("tool call is valid");

    assert_eq!(
        ToolApprovalRequest::for_call("approval-1", &resolved, &call, "user-1", 1_000, 2_000),
        Err(
            graphblocks_runtime_core::tool_approval::ToolApprovalError::ResolvedToolMismatch {
                expected: resolved.resolved_tool_id,
                actual: "resolved-tool-1".to_owned()
            }
        ),
    );
}

#[test]
fn approval_request_rejects_empty_identity_fields() {
    let resolved = resolved_search_tool().expect("resolved tool is valid");
    let mut call = search_call("call-1", "runtime").expect("tool call is valid");
    call.resolved_tool_id = resolved.resolved_tool_id.clone();

    assert_eq!(
        ToolApprovalRequest::for_call(" ", &resolved, &call, "user-1", 1_000, 2_000),
        Err(ToolApprovalError::EmptyField {
            field: "approval_id",
        }),
    );
    assert_eq!(
        ToolApprovalRequest::for_call("approval-1", &resolved, &call, "", 1_000, 2_000),
        Err(ToolApprovalError::EmptyField {
            field: "principal_id",
        }),
    );
}

#[test]
fn approval_request_rejects_invalid_tool_call_model() {
    let resolved = resolved_search_tool().expect("resolved tool is valid");
    let mut call = search_call("call-1", "runtime").expect("tool call is valid");
    call.resolved_tool_id = resolved.resolved_tool_id.clone();
    call.revision = 0;

    assert_eq!(
        ToolApprovalRequest::for_call("approval-1", &resolved, &call, "user-1", 1_000, 2_000),
        Err(ToolApprovalError::InvalidToolCall {
            source: ToolCallError::InvalidRevision { revision: 0 },
        }),
    );
}

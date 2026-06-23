use graphblocks_runtime_core::tool_call::{
    ToolCallDraft, ToolCallDraftStatus, ToolCallError, ToolCallStatus,
};
use serde_json::json;

#[test]
fn incremental_arguments_do_not_create_final_tool_call() -> Result<(), ToolCallError> {
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", "knowledge.search");

    draft.append_argument_fragment("{\"query\":")?;
    draft.append_argument_fragment("\"runtime policy\"}")?;

    assert_eq!(draft.status, ToolCallDraftStatus::ArgumentsStreaming);
    assert_eq!(
        draft.clone().into_tool_call("resolved-tool-1", 1_000),
        Err(ToolCallError::ArgumentsNotComplete {
            status: ToolCallDraftStatus::ArgumentsStreaming
        }),
    );

    let call = draft.into_completed_tool_call("resolved-tool-1", 1_000)?;
    assert_eq!(call.tool_call_id, "call-1");
    assert_eq!(call.response_id, "response-1");
    assert_eq!(call.resolved_tool_id, "resolved-tool-1");
    assert_eq!(call.name, "knowledge.search");
    assert_eq!(call.arguments, json!({"query": "runtime policy"}));
    assert!(call.arguments_digest.starts_with("sha256:"));
    assert_eq!(call.revision, 1);
    assert_eq!(call.status, ToolCallStatus::Validated);
    assert_eq!(call.created_at_unix_ms, 1_000);
    Ok(())
}

#[test]
fn invalid_arguments_are_rejected_before_validated_call_exists() -> Result<(), ToolCallError> {
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", "knowledge.search");

    draft.append_argument_fragment("{\"query\":")?;
    draft.complete_arguments()?;

    assert_eq!(
        draft.into_tool_call("resolved-tool-1", 1_000),
        Err(ToolCallError::InvalidArgumentsJson),
    );
    Ok(())
}

#[test]
fn canonical_arguments_digest_is_stable_for_object_key_order() -> Result<(), ToolCallError> {
    let mut left = ToolCallDraft::proposed("response-1", "call-1", "ticket.create");
    left.append_argument_fragment("{\"b\":2,\"a\":1}")?;
    let left = left.into_completed_tool_call("resolved-tool-1", 1_000)?;

    let mut right = ToolCallDraft::proposed("response-1", "call-2", "ticket.create");
    right.append_argument_fragment("{\"a\":1,\"b\":2}")?;
    let right = right.into_completed_tool_call("resolved-tool-1", 1_001)?;

    assert_eq!(left.arguments, right.arguments);
    assert_eq!(left.arguments_digest, right.arguments_digest);
    Ok(())
}

#[test]
fn argument_revision_recomputes_digest_and_invalidates_admission_state() -> Result<(), ToolCallError>
{
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", "ticket.create");
    draft.append_argument_fragment("{\"title\":\"old\"}")?;
    let call = draft.into_completed_tool_call("resolved-tool-1", 1_000)?;

    let revised = call.revise_arguments(json!({"title": "new"}))?;

    assert_eq!(revised.tool_call_id, "call-1");
    assert_eq!(revised.revision, 2);
    assert_eq!(revised.status, ToolCallStatus::Validated);
    assert_ne!(revised.arguments_digest, call.arguments_digest);
    assert_eq!(revised.admitted_at_unix_ms, None);
    assert_eq!(revised.completed_at_unix_ms, None);
    Ok(())
}

#[test]
fn admitted_tool_call_arguments_cannot_be_revised() -> Result<(), ToolCallError> {
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", "ticket.create");
    draft.append_argument_fragment("{\"title\":\"old\"}")?;
    let mut call = draft.into_completed_tool_call("resolved-tool-1", 1_000)?;
    call.status = ToolCallStatus::Admitted;

    assert_eq!(
        call.revise_arguments(json!({"title": "new"})),
        Err(ToolCallError::CannotReviseArguments {
            status: ToolCallStatus::Admitted
        }),
    );
    Ok(())
}

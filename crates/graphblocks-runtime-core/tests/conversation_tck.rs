use graphblocks_runtime_core::conversation::{
    AttachmentIngestionStatus, AttachmentPurpose, AttachmentScope, BranchRequest, CompactionRecord,
    ContentPart, Conversation, ConversationError, DeletePolicy, FileAttachment,
    InMemoryConversationStore, Message, MessageRole, MessageStatus, RegenerateRequest, TurnError,
    TurnStatus,
};
use graphblocks_runtime_core::documents::ArtifactRef;
use serde_json::{Value, json};

fn required_str<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("conversation TCK case missing string {key}"))
}

fn role(raw: &Value) -> Result<MessageRole, String> {
    match raw
        .get("role")
        .and_then(Value::as_str)
        .unwrap_or("assistant")
    {
        "system" => Ok(MessageRole::System),
        "developer" => Ok(MessageRole::Developer),
        "user" => Ok(MessageRole::User),
        "assistant" => Ok(MessageRole::Assistant),
        "tool" => Ok(MessageRole::Tool),
        other => Err(format!("unsupported message role {other:?}")),
    }
}

fn message_from(
    raw: &Value,
    fallback_id: &str,
    fallback_role: MessageRole,
) -> Result<Message, String> {
    let message_id = raw
        .get("messageId")
        .or_else(|| raw.get("message_id"))
        .and_then(Value::as_str)
        .unwrap_or(fallback_id);
    let role = if raw.get("role").is_some() {
        role(raw)?
    } else {
        fallback_role
    };
    let mut message = Message::new(message_id, role).with_part(ContentPart::text(
        raw.get("text").and_then(Value::as_str).unwrap_or_default(),
    ));
    message.parent_message_id = raw
        .get("parentMessageId")
        .or_else(|| raw.get("parent_message_id"))
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    Ok(message)
}

fn message_status_name(status: MessageStatus) -> &'static str {
    match status {
        MessageStatus::Draft => "draft",
        MessageStatus::Committed => "committed",
        MessageStatus::Superseded => "superseded",
        MessageStatus::Retracted => "retracted",
    }
}

fn turn_status_name(status: TurnStatus) -> &'static str {
    match status {
        TurnStatus::Created => "created",
        TurnStatus::ContextBuilding => "context_building",
        TurnStatus::ModelRunning => "model_running",
        TurnStatus::ToolWaiting => "tool_waiting",
        TurnStatus::ApprovalWaiting => "approval_waiting",
        TurnStatus::Finalizing => "finalizing",
        TurnStatus::Completed => "completed",
        TurnStatus::Failed => "failed",
        TurnStatus::Cancelled => "cancelled",
        TurnStatus::PolicyStopped => "policy_stopped",
    }
}

fn attachment_scope(raw: &Value) -> Result<AttachmentScope, String> {
    match raw
        .get("scope")
        .and_then(Value::as_str)
        .unwrap_or("message")
    {
        "message" => Ok(AttachmentScope::Message),
        "conversation" => Ok(AttachmentScope::Conversation),
        "user" => Ok(AttachmentScope::User),
        "project" => Ok(AttachmentScope::Project),
        "tenant" => Ok(AttachmentScope::Tenant),
        other => Err(format!("unsupported attachment scope {other:?}")),
    }
}

fn attachment_purpose(raw: &Value) -> Result<AttachmentPurpose, String> {
    match raw
        .get("purpose")
        .and_then(Value::as_str)
        .unwrap_or("retrieval")
    {
        "direct_input" => Ok(AttachmentPurpose::DirectInput),
        "retrieval" => Ok(AttachmentPurpose::Retrieval),
        "code_analysis" => Ok(AttachmentPurpose::CodeAnalysis),
        "reference" => Ok(AttachmentPurpose::Reference),
        "output" => Ok(AttachmentPurpose::Output),
        other => Err(format!("unsupported attachment purpose {other:?}")),
    }
}

fn attachment_status(raw: &Value) -> Result<AttachmentIngestionStatus, String> {
    match raw
        .get("ingestionStatus")
        .or_else(|| raw.get("ingestion_status"))
        .and_then(Value::as_str)
        .unwrap_or("ready")
    {
        "pending" => Ok(AttachmentIngestionStatus::Pending),
        "processing" => Ok(AttachmentIngestionStatus::Processing),
        "ready" => Ok(AttachmentIngestionStatus::Ready),
        "failed" => Ok(AttachmentIngestionStatus::Failed),
        "expired" => Ok(AttachmentIngestionStatus::Expired),
        "deleted" => Ok(AttachmentIngestionStatus::Deleted),
        other => Err(format!("unsupported attachment ingestion status {other:?}")),
    }
}

fn attachment_from(raw: &Value) -> Result<FileAttachment, String> {
    let attachment_id = raw
        .get("attachmentId")
        .or_else(|| raw.get("attachment_id"))
        .and_then(Value::as_str)
        .unwrap_or("att");
    let artifact_id = raw
        .get("artifactId")
        .or_else(|| raw.get("artifact_id"))
        .and_then(Value::as_str)
        .unwrap_or("artifact");
    let uri = raw
        .get("uri")
        .and_then(Value::as_str)
        .unwrap_or("blob://attachments/file");
    let mut attachment = FileAttachment::new(
        attachment_id,
        ArtifactRef::new(artifact_id, uri),
        attachment_scope(raw)?,
        attachment_purpose(raw)?,
    )
    .with_ingestion_status(attachment_status(raw)?);
    if let Some(message_id) = raw
        .get("messageId")
        .or_else(|| raw.get("message_id"))
        .and_then(Value::as_str)
    {
        attachment = attachment.with_message_id(message_id);
    }
    Ok(attachment)
}

fn run_case(case: &Value) -> Result<Value, String> {
    let kind = required_str(case, "kind")?;
    let conversation_id = case
        .get("conversationId")
        .or_else(|| case.get("conversation_id"))
        .and_then(Value::as_str)
        .unwrap_or("conv-1");
    let mut store = InMemoryConversationStore::new();

    match kind {
        "turn_commit" => {
            let turn_id = case
                .get("turnId")
                .or_else(|| case.get("turn_id"))
                .and_then(Value::as_str)
                .unwrap_or("turn-1");
            store
                .create(Conversation::new(conversation_id))
                .map_err(|error| error.to_string())?;
            store
                .begin_turn(conversation_id, 0, turn_id)
                .map_err(|error| error.to_string())?;
            let draft_turn = store
                .append_turn_message(
                    turn_id,
                    message_from(
                        case.get("message").unwrap_or(&Value::Null),
                        "msg-1",
                        MessageRole::Assistant,
                    )?,
                )
                .map_err(|error| error.to_string())?;
            let before_commit = store
                .get(conversation_id)
                .map_err(|error| error.to_string())?;
            let completed_turn = store
                .commit_turn(turn_id)
                .map_err(|error| error.to_string())?;
            let after_commit = store
                .get(conversation_id)
                .map_err(|error| error.to_string())?;

            Ok(json!({
                "draftStatus": turn_status_name(draft_turn.status),
                "draftMessageStatuses": draft_turn.messages.iter().map(|message| message_status_name(message.status)).collect::<Vec<_>>(),
                "preCommitMessageCount": before_commit.conversation.messages.len(),
                "turnStatus": turn_status_name(completed_turn.status),
                "committedRevision": completed_turn.committed_revision,
                "committedMessageIds": completed_turn.committed_message_ids,
                "conversationRevision": after_commit.revision,
                "conversationMessageIds": after_commit.conversation.messages.iter().map(|message| message.message_id.as_str()).collect::<Vec<_>>(),
                "conversationMessageStatuses": after_commit.conversation.messages.iter().map(|message| message_status_name(message.status)).collect::<Vec<_>>(),
            }))
        }
        "abort_turn" | "policy_stop_turn" => {
            let turn_id = case
                .get("turnId")
                .or_else(|| case.get("turn_id"))
                .and_then(Value::as_str)
                .unwrap_or("turn-1");
            store
                .create(Conversation::new(conversation_id))
                .map_err(|error| error.to_string())?;
            store
                .begin_turn(conversation_id, 0, turn_id)
                .map_err(|error| error.to_string())?;
            store
                .append_turn_message(
                    turn_id,
                    message_from(
                        case.get("message").unwrap_or(&Value::Null),
                        "msg-1",
                        MessageRole::Assistant,
                    )?,
                )
                .map_err(|error| error.to_string())?;
            let terminal_turn = if kind == "abort_turn" {
                store.abort_turn(turn_id)
            } else {
                store.policy_stop_turn(turn_id)
            }
            .map_err(|error| error.to_string())?;
            let terminal_commit_denied =
                matches!(store.commit_turn(turn_id), Err(TurnError::Terminal { .. }));
            let snapshot = store
                .get(conversation_id)
                .map_err(|error| error.to_string())?;

            Ok(json!({
                "turnStatus": turn_status_name(terminal_turn.status),
                "turnMessageStatuses": terminal_turn.messages.iter().map(|message| message_status_name(message.status)).collect::<Vec<_>>(),
                "conversationMessageCount": snapshot.conversation.messages.len(),
                "terminalCommitDenied": terminal_commit_denied,
            }))
        }
        "commit_conflict" => {
            let turn_id = case
                .get("turnId")
                .or_else(|| case.get("turn_id"))
                .and_then(Value::as_str)
                .unwrap_or("turn-1");
            store
                .create(Conversation::new(conversation_id))
                .map_err(|error| error.to_string())?;
            store
                .begin_turn(conversation_id, 0, turn_id)
                .map_err(|error| error.to_string())?;
            store
                .append_turn_message(
                    turn_id,
                    message_from(
                        case.get("draftMessage")
                            .or_else(|| case.get("draft_message"))
                            .unwrap_or(&Value::Null),
                        "msg-draft",
                        MessageRole::Assistant,
                    )?,
                )
                .map_err(|error| error.to_string())?;
            store
                .append_messages(
                    conversation_id,
                    0,
                    [message_from(
                        case.get("conflictingMessage")
                            .or_else(|| case.get("conflicting_message"))
                            .unwrap_or(&Value::Null),
                        "msg-other",
                        MessageRole::User,
                    )?],
                )
                .map_err(|error| error.to_string())?;
            let commit_conflict = matches!(
                store.commit_turn(turn_id),
                Err(TurnError::Conversation(
                    ConversationError::RevisionConflict { .. }
                ))
            );
            let failed_turn = store.get_turn(turn_id).map_err(|error| error.to_string())?;
            let snapshot = store
                .get(conversation_id)
                .map_err(|error| error.to_string())?;

            Ok(json!({
                "commitConflict": commit_conflict,
                "turnStatus": turn_status_name(failed_turn.status),
                "conversationRevision": snapshot.revision,
                "conversationMessageIds": snapshot.conversation.messages.iter().map(|message| message.message_id.as_str()).collect::<Vec<_>>(),
                "committedMessageIds": failed_turn.committed_message_ids,
            }))
        }
        "branch_regenerate" => {
            let raw_messages = case
                .get("messages")
                .and_then(Value::as_array)
                .ok_or_else(|| "branch_regenerate case requires messages".to_owned())?;
            let mut messages = Vec::new();
            for raw_message in raw_messages {
                messages.push(message_from(raw_message, "msg", MessageRole::User)?);
            }
            let branch_from_message_id = case
                .get("branchFromMessageId")
                .or_else(|| case.get("branch_from_message_id"))
                .and_then(Value::as_str)
                .unwrap_or("msg-user");
            let branch_conversation_id = case
                .get("branchConversationId")
                .or_else(|| case.get("branch_conversation_id"))
                .and_then(Value::as_str)
                .unwrap_or("conv-branch");
            let regenerate_assistant_message_id = case
                .get("regenerateAssistantMessageId")
                .or_else(|| case.get("regenerate_assistant_message_id"))
                .and_then(Value::as_str)
                .unwrap_or("msg-assistant");
            let regenerate_conversation_id = case
                .get("regenerateConversationId")
                .or_else(|| case.get("regenerate_conversation_id"))
                .and_then(Value::as_str)
                .unwrap_or("conv-regenerated");

            store
                .create(Conversation::new(conversation_id))
                .map_err(|error| error.to_string())?;
            store
                .append_messages(conversation_id, 0, messages)
                .map_err(|error| error.to_string())?;
            let branch = store
                .branch(
                    BranchRequest::new(conversation_id, branch_from_message_id)
                        .with_new_conversation_id(branch_conversation_id),
                )
                .map_err(|error| error.to_string())?;
            let regenerated = store
                .regenerate(
                    RegenerateRequest::new(conversation_id, regenerate_assistant_message_id)
                        .with_new_conversation_id(regenerate_conversation_id),
                )
                .map_err(|error| error.to_string())?;
            let source = store
                .get(conversation_id)
                .map_err(|error| error.to_string())?;

            Ok(json!({
                "branchId": branch.conversation_id,
                "branchOf": branch.branch_of,
                "branchFrom": branch.branched_from_message_id,
                "branchMessageIds": branch.messages.iter().map(|message| message.message_id.as_str()).collect::<Vec<_>>(),
                "branchSourceRevision": branch.metadata.get("source_revision").cloned().unwrap_or(Value::Null),
                "regenerateId": regenerated.conversation_id,
                "regenerateBranchOf": regenerated.branch_of,
                "regenerateFrom": regenerated.branched_from_message_id,
                "regenerateMessageIds": regenerated.messages.iter().map(|message| message.message_id.as_str()).collect::<Vec<_>>(),
                "regeneratedFromMessageId": regenerated.metadata.get("regenerated_from_message_id").cloned().unwrap_or(Value::Null),
                "regenerateSourceRevision": regenerated.metadata.get("source_revision").cloned().unwrap_or(Value::Null),
                "sourceRevision": source.revision,
                "sourceMessageStatuses": source.conversation.messages.iter().map(|message| message_status_name(message.status)).collect::<Vec<_>>(),
            }))
        }
        "branch_attachments" => {
            let raw_messages = case
                .get("messages")
                .and_then(Value::as_array)
                .ok_or_else(|| "branch_attachments case requires messages".to_owned())?;
            let mut messages = Vec::new();
            for raw_message in raw_messages {
                messages.push(message_from(raw_message, "msg", MessageRole::User)?);
            }
            let raw_attachments = case
                .get("attachments")
                .and_then(Value::as_array)
                .ok_or_else(|| "branch_attachments case requires attachments".to_owned())?;
            let branch_from_message_id = case
                .get("branchFromMessageId")
                .or_else(|| case.get("branch_from_message_id"))
                .and_then(Value::as_str)
                .unwrap_or("msg-1");
            let branch_conversation_id = case
                .get("branchConversationId")
                .or_else(|| case.get("branch_conversation_id"))
                .and_then(Value::as_str)
                .unwrap_or("conv-branch");
            let branch_without_attachments_id = case
                .get("branchWithoutAttachmentsId")
                .or_else(|| case.get("branch_without_attachments_id"))
                .and_then(Value::as_str)
                .unwrap_or("conv-branch-without-attachments");

            store
                .create(Conversation::new(conversation_id))
                .map_err(|error| error.to_string())?;
            store
                .append_messages(conversation_id, 0, messages)
                .map_err(|error| error.to_string())?;
            for raw_attachment in raw_attachments {
                store
                    .add_attachment(conversation_id, attachment_from(raw_attachment)?)
                    .map_err(|error| error.to_string())?;
            }
            let branch = store
                .branch(
                    BranchRequest::new(conversation_id, branch_from_message_id)
                        .with_new_conversation_id(branch_conversation_id),
                )
                .map_err(|error| error.to_string())?;
            let mut request = BranchRequest::new(conversation_id, branch_from_message_id)
                .with_new_conversation_id(branch_without_attachments_id);
            request.include_attachments = false;
            let branch_without_attachments =
                store.branch(request).map_err(|error| error.to_string())?;
            let source = store
                .get(conversation_id)
                .map_err(|error| error.to_string())?;

            Ok(json!({
                "branchAttachmentIds": branch.attachments.iter().map(|attachment| attachment.attachment_id.as_str()).collect::<Vec<_>>(),
                "branchWithoutAttachmentIds": branch_without_attachments.attachments.iter().map(|attachment| attachment.attachment_id.as_str()).collect::<Vec<_>>(),
                "branchMessageIds": branch.messages.iter().map(|message| message.message_id.as_str()).collect::<Vec<_>>(),
                "sourceAttachmentIds": source.conversation.attachments.iter().map(|attachment| attachment.attachment_id.as_str()).collect::<Vec<_>>(),
            }))
        }
        "attachment_resolution" => {
            let raw_attachments = case
                .get("attachments")
                .and_then(Value::as_array)
                .ok_or_else(|| "attachment_resolution case requires attachments".to_owned())?;
            let message_ids = case
                .get("messageIds")
                .or_else(|| case.get("message_ids"))
                .and_then(Value::as_array)
                .ok_or_else(|| "attachment_resolution case requires messageIds".to_owned())?
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .ok_or_else(|| "messageIds entries must be strings".to_owned())
                        .map(ToOwned::to_owned)
                })
                .collect::<Result<Vec<_>, _>>()?;

            let attachments = raw_attachments
                .iter()
                .map(attachment_from)
                .collect::<Result<Vec<_>, _>>()?;
            let mut conversation = Conversation::new(conversation_id);
            for attachment in &attachments {
                if attachment.scope == AttachmentScope::Message
                    && let Some(message_id) = attachment.message_id.as_deref()
                    && !conversation
                        .messages
                        .iter()
                        .any(|message| message.message_id == message_id)
                {
                    conversation
                        .messages
                        .push(Message::new(message_id, MessageRole::User));
                }
            }
            store
                .create(conversation)
                .map_err(|error| error.to_string())?;
            for attachment in attachments {
                store
                    .add_attachment(conversation_id, attachment)
                    .map_err(|error| error.to_string())?;
            }
            let with_conversation_scope = store
                .resolve_attachments(conversation_id, &message_ids, true)
                .map_err(|error| error.to_string())?;
            let without_conversation_scope = store
                .resolve_attachments(conversation_id, &message_ids, false)
                .map_err(|error| error.to_string())?;

            Ok(json!({
                "withConversationScopeIds": with_conversation_scope.iter().map(|attachment| attachment.attachment_id.as_str()).collect::<Vec<_>>(),
                "withoutConversationScopeIds": without_conversation_scope.iter().map(|attachment| attachment.attachment_id.as_str()).collect::<Vec<_>>(),
            }))
        }
        "archive_conversation" => {
            let raw_message = case.get("message").unwrap_or(&Value::Null);
            store
                .create(Conversation::new(conversation_id))
                .map_err(|error| error.to_string())?;
            let archive_revision = store
                .archive(conversation_id)
                .map_err(|error| error.to_string())?;
            let append_rejected = matches!(
                store.append_messages(
                    conversation_id,
                    archive_revision,
                    [message_from(raw_message, "msg-1", MessageRole::User)?],
                ),
                Err(ConversationError::Archived { .. })
            );
            let snapshot = store
                .get(conversation_id)
                .map_err(|error| error.to_string())?;

            Ok(json!({
                "archiveRevision": archive_revision,
                "archived": snapshot.conversation.archived,
                "appendRejected": append_rejected,
                "messageCount": snapshot.conversation.messages.len(),
            }))
        }
        "compaction_record" => {
            let raw_messages = case
                .get("messages")
                .and_then(Value::as_array)
                .ok_or_else(|| "compaction_record case requires messages".to_owned())?;
            let mut messages = Vec::new();
            for raw_message in raw_messages {
                messages.push(message_from(raw_message, "msg", MessageRole::User)?);
            }
            let raw_compaction = case
                .get("compaction")
                .and_then(Value::as_object)
                .ok_or_else(|| "compaction_record case requires compaction".to_owned())?;
            let source_message_ids = raw_compaction
                .get("sourceMessageIds")
                .or_else(|| raw_compaction.get("source_message_ids"))
                .and_then(Value::as_array)
                .ok_or_else(|| "compaction_record case requires sourceMessageIds".to_owned())?
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .ok_or_else(|| "sourceMessageIds entries must be strings".to_owned())
                        .map(ToOwned::to_owned)
                })
                .collect::<Result<Vec<_>, _>>()?;
            let compaction_id = raw_compaction
                .get("compactionId")
                .or_else(|| raw_compaction.get("compaction_id"))
                .and_then(Value::as_str)
                .unwrap_or("compact-1");
            let output_message_id = raw_compaction
                .get("outputMessageId")
                .or_else(|| raw_compaction.get("output_message_id"))
                .and_then(Value::as_str)
                .unwrap_or("msg-summary");
            let method = raw_compaction
                .get("method")
                .and_then(Value::as_str)
                .unwrap_or("summary_memory");
            let token_before = raw_compaction
                .get("tokenBefore")
                .or_else(|| raw_compaction.get("token_before"))
                .and_then(Value::as_u64)
                .unwrap_or(0) as usize;
            let token_after = raw_compaction
                .get("tokenAfter")
                .or_else(|| raw_compaction.get("token_after"))
                .and_then(Value::as_u64)
                .unwrap_or(0) as usize;
            let mut record = CompactionRecord::new(
                compaction_id,
                source_message_ids,
                output_message_id,
                method,
                token_before,
                token_after,
            );
            if let Some(model) = raw_compaction.get("model").and_then(Value::as_str) {
                record = record.with_model(model);
            }

            store
                .create(Conversation::new(conversation_id))
                .map_err(|error| error.to_string())?;
            store
                .append_messages(conversation_id, 0, messages)
                .map_err(|error| error.to_string())?;
            let revision = store
                .record_compaction(conversation_id, record)
                .map_err(|error| error.to_string())?;
            let snapshot = store
                .get(conversation_id)
                .map_err(|error| error.to_string())?;
            let compaction = snapshot
                .conversation
                .compactions
                .first()
                .ok_or_else(|| "compaction was not recorded".to_owned())?;

            Ok(json!({
                "revision": revision,
                "compactionIds": snapshot.conversation.compactions.iter().map(|record| record.compaction_id.as_str()).collect::<Vec<_>>(),
                "sourceMessageIds": compaction.source_message_ids.iter().map(String::as_str).collect::<Vec<_>>(),
                "outputMessageId": compaction.output_message_id,
                "method": compaction.method,
                "tokenBefore": compaction.token_before,
                "tokenAfter": compaction.token_after,
                "model": compaction.model,
            }))
        }
        "delete_retention" => {
            let raw_messages = case
                .get("messages")
                .and_then(Value::as_array)
                .ok_or_else(|| "delete_retention case requires messages".to_owned())?;
            let mut messages = Vec::new();
            for raw_message in raw_messages {
                messages.push(message_from(raw_message, "msg", MessageRole::User)?);
            }
            let raw_attachments = case
                .get("attachments")
                .and_then(Value::as_array)
                .ok_or_else(|| "delete_retention case requires attachments".to_owned())?;
            let raw_compaction = case
                .get("compaction")
                .and_then(Value::as_object)
                .ok_or_else(|| "delete_retention case requires compaction".to_owned())?;
            let source_message_ids = raw_compaction
                .get("sourceMessageIds")
                .or_else(|| raw_compaction.get("source_message_ids"))
                .and_then(Value::as_array)
                .ok_or_else(|| "delete_retention case requires sourceMessageIds".to_owned())?
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .ok_or_else(|| "sourceMessageIds entries must be strings".to_owned())
                        .map(ToOwned::to_owned)
                })
                .collect::<Result<Vec<_>, _>>()?;
            let compaction_id = raw_compaction
                .get("compactionId")
                .or_else(|| raw_compaction.get("compaction_id"))
                .and_then(Value::as_str)
                .unwrap_or("compact-1");
            let output_message_id = raw_compaction
                .get("outputMessageId")
                .or_else(|| raw_compaction.get("output_message_id"))
                .and_then(Value::as_str)
                .unwrap_or("msg-summary");
            let method = raw_compaction
                .get("method")
                .and_then(Value::as_str)
                .unwrap_or("summary_memory");
            let token_before = raw_compaction
                .get("tokenBefore")
                .or_else(|| raw_compaction.get("token_before"))
                .and_then(Value::as_u64)
                .unwrap_or(0) as usize;
            let token_after = raw_compaction
                .get("tokenAfter")
                .or_else(|| raw_compaction.get("token_after"))
                .and_then(Value::as_u64)
                .unwrap_or(0) as usize;

            store
                .create(Conversation::new(conversation_id))
                .map_err(|error| error.to_string())?;
            store
                .append_messages(conversation_id, 0, messages.clone())
                .map_err(|error| error.to_string())?;
            for raw_attachment in raw_attachments {
                store
                    .add_attachment(conversation_id, attachment_from(raw_attachment)?)
                    .map_err(|error| error.to_string())?;
            }
            store
                .record_compaction(
                    conversation_id,
                    CompactionRecord::new(
                        compaction_id,
                        source_message_ids,
                        output_message_id,
                        method,
                        token_before,
                        token_after,
                    ),
                )
                .map_err(|error| error.to_string())?;
            let tombstone_revision = store
                .delete(conversation_id, DeletePolicy::Tombstone)
                .map_err(|error| error.to_string())?;
            let tombstone = store
                .get(conversation_id)
                .map_err(|error| error.to_string())?
                .conversation;

            let hard_delete_conversation_id = case
                .get("hardDeleteConversationId")
                .or_else(|| case.get("hard_delete_conversation_id"))
                .and_then(Value::as_str)
                .unwrap_or("conv-delete-hard");
            let mut hard_delete_conversation = Conversation::new(hard_delete_conversation_id);
            hard_delete_conversation.messages = messages;
            store
                .create(hard_delete_conversation)
                .map_err(|error| error.to_string())?;
            store
                .delete(hard_delete_conversation_id, DeletePolicy::Hard)
                .map_err(|error| error.to_string())?;
            let hard_deleted = matches!(
                store.get(hard_delete_conversation_id),
                Err(ConversationError::NotFound { .. })
            );

            Ok(json!({
                "tombstoneRevision": tombstone_revision,
                "tombstoneArchived": tombstone.archived,
                "tombstoneDeleted": tombstone.metadata.get("deleted").cloned().unwrap_or(Value::Null),
                "tombstoneMessageCount": tombstone.messages.len(),
                "tombstoneAttachmentCount": tombstone.attachments.len(),
                "tombstoneCompactionCount": tombstone.compactions.len(),
                "hardDeleted": hard_deleted,
            }))
        }
        other => Err(format!("unsupported conversation TCK kind {other:?}")),
    }
}

#[test]
fn rust_conversation_matches_shared_tck_cases() -> Result<(), String> {
    let cases: Value = serde_json::from_str(include_str!("fixtures/conversation-cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "conversation TCK root must be an array".to_owned())?;

    for case in cases {
        let case_name = required_str(case, "name")?;
        let observed = run_case(case).map_err(|error| format!("{case_name}: {error}"))?;
        let expected = case
            .get("expected")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("conversation TCK case {case_name} missing expected"))?;
        for (key, expected_value) in expected {
            assert_eq!(
                observed.get(key).unwrap_or(&Value::Null),
                expected_value,
                "conversation TCK case {case_name} expected {key}"
            );
        }
    }

    Ok(())
}

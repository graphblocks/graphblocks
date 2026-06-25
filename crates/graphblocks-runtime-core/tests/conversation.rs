use graphblocks_runtime_core::conversation::{
    AttachmentIngestionStatus, AttachmentPurpose, AttachmentScope, BranchRequest, CompactionRecord,
    ContentPart, Conversation, ConversationError, DeletePolicy, FileAttachment,
    InMemoryConversationStore, Message, MessageError, MessageRole, MessageStatus,
    RegenerateRequest, TurnError, TurnStatus,
};
use graphblocks_runtime_core::documents::ArtifactRef;
use serde_json::json;

fn assistant_message(message_id: &str, text: &str) -> Message {
    Message::new(message_id, MessageRole::Assistant).with_part(ContentPart::text(text))
}

#[test]
fn turn_draft_messages_commit_atomically_to_conversation() -> Result<(), Box<dyn std::error::Error>>
{
    let mut store = InMemoryConversationStore::new();
    store.create(Conversation::new("conv-1"))?;
    store.begin_turn("conv-1", 0, "turn-1")?;

    let draft_turn = store.append_turn_message("turn-1", assistant_message("msg-1", "hello"))?;

    assert_eq!(draft_turn.status, TurnStatus::ModelRunning);
    assert_eq!(draft_turn.messages[0].status, MessageStatus::Draft);
    assert!(store.get("conv-1")?.conversation.messages.is_empty());

    let completed_turn = store.commit_turn("turn-1")?;
    let snapshot = store.get("conv-1")?;

    assert_eq!(completed_turn.status, TurnStatus::Completed);
    assert_eq!(completed_turn.committed_revision, Some(1));
    assert_eq!(
        completed_turn.committed_message_ids,
        vec!["msg-1".to_owned()]
    );
    assert_eq!(
        snapshot.conversation.messages[0].status,
        MessageStatus::Committed
    );
    Ok(())
}

#[test]
fn abort_turn_retracts_drafts_without_appending_to_conversation()
-> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryConversationStore::new();
    store.create(Conversation::new("conv-1"))?;
    store.begin_turn("conv-1", 0, "turn-1")?;
    store.append_turn_message("turn-1", assistant_message("msg-1", "draft"))?;

    let cancelled_turn = store.abort_turn("turn-1")?;

    assert_eq!(cancelled_turn.status, TurnStatus::Cancelled);
    assert_eq!(cancelled_turn.messages[0].status, MessageStatus::Retracted);
    assert!(store.get("conv-1")?.conversation.messages.is_empty());
    assert_eq!(
        store.commit_turn("turn-1"),
        Err(TurnError::Terminal {
            turn_id: "turn-1".to_owned(),
            status: TurnStatus::Cancelled,
        }),
    );
    Ok(())
}

#[test]
fn policy_stop_turn_retracts_drafts_without_committing_assistant_message()
-> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryConversationStore::new();
    store.create(Conversation::new("conv-1"))?;
    store.begin_turn("conv-1", 0, "turn-1")?;
    store.append_turn_message("turn-1", assistant_message("msg-1", "blocked"))?;

    let stopped_turn = store.policy_stop_turn("turn-1")?;

    assert_eq!(stopped_turn.status, TurnStatus::PolicyStopped);
    assert_eq!(stopped_turn.messages[0].status, MessageStatus::Retracted);
    assert!(store.get("conv-1")?.conversation.messages.is_empty());
    assert_eq!(
        store.commit_turn("turn-1"),
        Err(TurnError::Terminal {
            turn_id: "turn-1".to_owned(),
            status: TurnStatus::PolicyStopped,
        }),
    );
    Ok(())
}

#[test]
fn turn_commit_conflict_marks_turn_failed() -> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryConversationStore::new();
    store.create(Conversation::new("conv-1"))?;
    store.begin_turn("conv-1", 0, "turn-1")?;
    store.append_turn_message("turn-1", assistant_message("msg-draft", "draft"))?;
    store.append_messages("conv-1", 0, [Message::new("msg-other", MessageRole::User)])?;

    assert_eq!(
        store.commit_turn("turn-1"),
        Err(TurnError::Conversation(
            ConversationError::RevisionConflict {
                conversation_id: "conv-1".to_owned(),
                expected: 0,
                actual: 1,
            }
        )),
    );
    assert_eq!(store.get_turn("turn-1")?.status, TurnStatus::Failed);
    Ok(())
}

#[test]
fn begin_turn_rejects_stale_revision_and_duplicate_turn_id()
-> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryConversationStore::new();
    store.create(Conversation::new("conv-1"))?;
    store.append_messages("conv-1", 0, [Message::new("msg-1", MessageRole::User)])?;

    assert_eq!(
        store.begin_turn("conv-1", 0, "turn-1"),
        Err(TurnError::Conversation(
            ConversationError::RevisionConflict {
                conversation_id: "conv-1".to_owned(),
                expected: 0,
                actual: 1,
            }
        )),
    );

    store.begin_turn("conv-1", 1, "turn-1")?;
    assert_eq!(
        store.begin_turn("conv-1", 1, "turn-1"),
        Err(TurnError::AlreadyExists {
            turn_id: "turn-1".to_owned()
        }),
    );
    Ok(())
}

#[test]
fn branch_preserves_lineage_and_copies_messages_through_source_message()
-> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryConversationStore::new();
    store.create(Conversation::new("conv-1"))?;
    store.append_messages(
        "conv-1",
        0,
        [
            Message::new("msg-user", MessageRole::User),
            assistant_message("msg-assistant", "policy summary"),
        ],
    )?;

    let branch = store
        .branch(BranchRequest::new("conv-1", "msg-user").with_new_conversation_id("conv-2"))?;

    assert_eq!(branch.conversation_id, "conv-2");
    assert_eq!(branch.branch_of.as_deref(), Some("conv-1"));
    assert_eq!(branch.branched_from_message_id.as_deref(), Some("msg-user"));
    assert_eq!(branch.revision, 0);
    assert_eq!(branch.messages.len(), 1);
    assert_eq!(branch.messages[0].message_id, "msg-user");
    assert_eq!(branch.metadata.get("source_revision"), Some(&json!(1)));

    assert_eq!(
        store.branch(BranchRequest::new("conv-1", "missing")),
        Err(MessageError::NotFound {
            message_id: "missing".to_owned()
        }),
    );
    Ok(())
}

#[test]
fn branch_rejects_archived_conversation() -> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryConversationStore::new();
    store.create(Conversation::new("conv-1"))?;
    store.append_messages("conv-1", 0, [Message::new("msg-user", MessageRole::User)])?;
    store.archive("conv-1")?;

    assert_eq!(
        store.branch(BranchRequest::new("conv-1", "msg-user")),
        Err(MessageError::Conversation(ConversationError::Archived {
            conversation_id: "conv-1".to_owned(),
        })),
    );
    Ok(())
}

#[test]
fn regenerate_supersedes_assistant_and_branches_from_parent_user()
-> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryConversationStore::new();
    let user =
        Message::new("msg-user", MessageRole::User).with_part(ContentPart::text("try again"));
    let mut assistant = assistant_message("msg-assistant", "first answer");
    assistant.parent_message_id = Some("msg-user".to_owned());
    let later = Message::new("msg-later", MessageRole::User).with_part(ContentPart::text("later"));
    store.create(Conversation::new("conv-1"))?;
    store.append_messages("conv-1", 0, [user.clone(), assistant, later])?;

    let branch = store.regenerate(
        RegenerateRequest::new("conv-1", "msg-assistant")
            .with_new_conversation_id("conv-regenerated"),
    )?;

    let snapshot = store.get("conv-1")?;
    assert_eq!(snapshot.revision, 2);
    assert_eq!(
        snapshot
            .conversation
            .messages
            .iter()
            .map(|message| message.status)
            .collect::<Vec<_>>(),
        vec![
            MessageStatus::Committed,
            MessageStatus::Superseded,
            MessageStatus::Committed
        ]
    );
    assert_eq!(branch.conversation_id, "conv-regenerated");
    assert_eq!(branch.branch_of.as_deref(), Some("conv-1"));
    assert_eq!(branch.branched_from_message_id.as_deref(), Some("msg-user"));
    assert_eq!(branch.messages, vec![user]);
    assert_eq!(branch.metadata.get("source_revision"), Some(&json!(1)));
    assert_eq!(
        branch.metadata.get("regenerated_from_message_id"),
        Some(&json!("msg-assistant"))
    );
    Ok(())
}

#[test]
fn regenerate_uses_previous_user_message_when_parent_is_not_recorded()
-> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryConversationStore::new();
    let user = Message::new("msg-1", MessageRole::User);
    let assistant = assistant_message("msg-2", "first answer");
    store.create(Conversation::new("conv-1"))?;
    store.append_messages("conv-1", 0, [user.clone(), assistant])?;

    let branch = store.regenerate(
        RegenerateRequest::new("conv-1", "msg-2").with_new_conversation_id("conv-regenerated"),
    )?;

    assert_eq!(branch.branched_from_message_id.as_deref(), Some("msg-1"));
    assert_eq!(branch.messages, vec![user]);
    Ok(())
}

#[test]
fn regenerate_branch_id_conflict_does_not_supersede_assistant()
-> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryConversationStore::new();
    let user = Message::new("msg-1", MessageRole::User);
    let mut assistant = assistant_message("msg-2", "first answer");
    assistant.parent_message_id = Some("msg-1".to_owned());
    store.create(Conversation::new("conv-1"))?;
    store.create(Conversation::new("conv-regenerated"))?;
    store.append_messages("conv-1", 0, [user, assistant])?;

    assert_eq!(
        store.regenerate(
            RegenerateRequest::new("conv-1", "msg-2").with_new_conversation_id("conv-regenerated"),
        ),
        Err(MessageError::Conversation(
            ConversationError::AlreadyExists {
                conversation_id: "conv-regenerated".to_owned(),
            }
        )),
    );

    let snapshot = store.get("conv-1")?;
    assert_eq!(snapshot.revision, 1);
    assert_eq!(
        snapshot.conversation.messages[1].status,
        MessageStatus::Committed
    );
    Ok(())
}

#[test]
fn scoped_attachments_resolve_for_context_without_promoting_to_knowledge()
-> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryConversationStore::new();
    store.create(Conversation::new("conv-1"))?;
    store.append_messages("conv-1", 0, [Message::new("msg-user", MessageRole::User)])?;
    store.add_attachment(
        "conv-1",
        FileAttachment::new(
            "att-message",
            ArtifactRef::new("artifact-message", "blob://attachments/message.pdf"),
            AttachmentScope::Message,
            AttachmentPurpose::Retrieval,
        )
        .with_message_id("msg-user")
        .with_ingestion_status(AttachmentIngestionStatus::Ready),
    )?;
    store.add_attachment(
        "conv-1",
        FileAttachment::new(
            "att-conversation",
            ArtifactRef::new(
                "artifact-conversation",
                "blob://attachments/conversation.pdf",
            ),
            AttachmentScope::Conversation,
            AttachmentPurpose::DirectInput,
        )
        .with_ingestion_status(AttachmentIngestionStatus::Ready),
    )?;

    let attachments = store.resolve_attachments("conv-1", &["msg-user".to_owned()], true)?;
    let snapshot = store.get("conv-1")?;

    assert_eq!(
        attachments
            .iter()
            .map(|attachment| attachment.attachment_id.as_str())
            .collect::<Vec<_>>(),
        vec!["att-message", "att-conversation"]
    );
    assert!(
        !snapshot
            .conversation
            .metadata
            .contains_key("knowledge_index_id")
    );
    Ok(())
}

#[test]
fn branch_respects_include_attachments_and_message_scope() -> Result<(), Box<dyn std::error::Error>>
{
    let mut store = InMemoryConversationStore::new();
    store.create(Conversation::new("conv-1"))?;
    store.append_messages(
        "conv-1",
        0,
        [
            Message::new("msg-1", MessageRole::User),
            Message::new("msg-2", MessageRole::User),
        ],
    )?;
    store.add_attachment(
        "conv-1",
        FileAttachment::new(
            "att-1",
            ArtifactRef::new("artifact-1", "blob://attachments/one.pdf"),
            AttachmentScope::Message,
            AttachmentPurpose::Retrieval,
        )
        .with_message_id("msg-1")
        .with_ingestion_status(AttachmentIngestionStatus::Ready),
    )?;
    store.add_attachment(
        "conv-1",
        FileAttachment::new(
            "att-2",
            ArtifactRef::new("artifact-2", "blob://attachments/two.pdf"),
            AttachmentScope::Message,
            AttachmentPurpose::Retrieval,
        )
        .with_message_id("msg-2")
        .with_ingestion_status(AttachmentIngestionStatus::Ready),
    )?;
    store.add_attachment(
        "conv-1",
        FileAttachment::new(
            "att-conversation",
            ArtifactRef::new(
                "artifact-conversation",
                "blob://attachments/conversation.pdf",
            ),
            AttachmentScope::Conversation,
            AttachmentPurpose::Reference,
        )
        .with_ingestion_status(AttachmentIngestionStatus::Ready),
    )?;

    let branch_with_attachments = store
        .branch(BranchRequest::new("conv-1", "msg-1").with_new_conversation_id("conv-branch-1"))?;
    let mut request =
        BranchRequest::new("conv-1", "msg-1").with_new_conversation_id("conv-branch-2");
    request.include_attachments = false;
    let branch_without_attachments = store.branch(request)?;

    assert_eq!(
        branch_with_attachments
            .attachments
            .iter()
            .map(|attachment| attachment.attachment_id.as_str())
            .collect::<Vec<_>>(),
        vec!["att-1", "att-conversation"]
    );
    assert!(branch_without_attachments.attachments.is_empty());
    Ok(())
}

#[test]
fn compaction_record_preserves_source_messages_and_records_token_delta()
-> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryConversationStore::new();
    store.create(Conversation::new("conv-1"))?;
    store.append_messages(
        "conv-1",
        0,
        [
            Message::new("msg-1", MessageRole::User),
            assistant_message("msg-2", "long answer"),
            assistant_message("msg-summary", "compact summary"),
        ],
    )?;

    let revision = store.record_compaction(
        "conv-1",
        CompactionRecord::new(
            "compact-1",
            ["msg-1", "msg-2"],
            "msg-summary",
            "summary_memory",
            1200,
            120,
        )
        .with_model("summary-model"),
    )?;
    let snapshot = store.get("conv-1")?;

    assert_eq!(revision, 2);
    assert_eq!(snapshot.conversation.messages.len(), 3);
    assert_eq!(
        snapshot.conversation.compactions[0].compaction_id,
        "compact-1"
    );
    assert_eq!(snapshot.conversation.compactions[0].token_before, 1200);
    assert_eq!(snapshot.conversation.compactions[0].token_after, 120);
    Ok(())
}

#[test]
fn archive_prevents_later_appends() -> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryConversationStore::new();
    store.create(Conversation::new("conv-1"))?;

    assert_eq!(store.archive("conv-1")?, 1);
    assert!(store.get("conv-1")?.conversation.archived);
    assert_eq!(
        store.append_messages("conv-1", 1, [Message::new("msg-1", MessageRole::User)]),
        Err(ConversationError::Archived {
            conversation_id: "conv-1".to_owned(),
        }),
    );
    Ok(())
}

#[test]
fn delete_hard_removes_conversation() -> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryConversationStore::new();
    store.create(Conversation::new("conv-1"))?;

    assert_eq!(store.delete("conv-1", DeletePolicy::Hard)?, None);
    assert_eq!(
        store.get("conv-1"),
        Err(ConversationError::NotFound {
            conversation_id: "conv-1".to_owned(),
        }),
    );
    Ok(())
}

#[test]
fn delete_tombstone_retains_empty_archived_conversation() -> Result<(), Box<dyn std::error::Error>>
{
    let mut store = InMemoryConversationStore::new();
    store.create(Conversation::new("conv-1"))?;

    assert_eq!(store.delete("conv-1", DeletePolicy::Tombstone)?, Some(1));
    let snapshot = store.get("conv-1")?;

    assert!(snapshot.conversation.archived);
    assert!(snapshot.conversation.messages.is_empty());
    assert_eq!(
        snapshot.conversation.metadata.get("deleted"),
        Some(&json!(true))
    );
    Ok(())
}

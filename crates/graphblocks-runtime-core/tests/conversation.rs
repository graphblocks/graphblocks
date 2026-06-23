use graphblocks_runtime_core::conversation::{
    BranchRequest, ContentPart, Conversation, ConversationError, DeletePolicy,
    InMemoryConversationStore, Message, MessageError, MessageRole, MessageStatus, TurnError,
    TurnStatus,
};
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

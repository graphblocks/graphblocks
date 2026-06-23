use graphblocks_runtime_core::conversation::{
    ContentPart, Conversation, ConversationError, InMemoryConversationStore, Message, MessageRole,
    MessageStatus, TurnError, TurnStatus,
};

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

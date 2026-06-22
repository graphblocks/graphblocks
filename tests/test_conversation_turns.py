from __future__ import annotations

import pytest

from graphblocks.conversation import (
    ContentPart,
    Conversation,
    ConversationConflictError,
    InMemoryConversationStore,
    Message,
    TurnConflictError,
)


def test_turn_draft_messages_commit_atomically_to_conversation() -> None:
    store = InMemoryConversationStore()
    store.create(Conversation(conversation_id="conv-1"))
    store.begin_turn("conv-1", expected_revision=0, turn_id="turn-1")
    message = Message(
        message_id="msg-1",
        role="assistant",
        parts=(ContentPart(kind="text", text="hello"),),
    )

    draft_turn = store.append_turn_message("turn-1", message)

    assert draft_turn.status == "model_running"
    assert draft_turn.messages[0].status == "draft"
    assert store.get("conv-1").conversation.messages == ()

    completed_turn = store.commit_turn("turn-1")

    snapshot = store.get("conv-1")
    assert completed_turn.status == "completed"
    assert completed_turn.committed_revision == 1
    assert completed_turn.committed_message_ids == ("msg-1",)
    assert snapshot.conversation.messages[0].status == "committed"


def test_abort_turn_retracts_drafts_without_appending_to_conversation() -> None:
    store = InMemoryConversationStore()
    store.create(Conversation(conversation_id="conv-1"))
    store.begin_turn("conv-1", expected_revision=0, turn_id="turn-1")
    store.append_turn_message(
        "turn-1",
        Message(message_id="msg-1", role="assistant", parts=(ContentPart(kind="text", text="draft"),)),
    )

    cancelled_turn = store.abort_turn("turn-1")

    assert cancelled_turn.status == "cancelled"
    assert cancelled_turn.messages[0].status == "retracted"
    assert store.get("conv-1").conversation.messages == ()
    with pytest.raises(TurnConflictError):
        store.commit_turn("turn-1")


def test_turn_commit_conflict_marks_turn_failed() -> None:
    store = InMemoryConversationStore()
    store.create(Conversation(conversation_id="conv-1"))
    store.begin_turn("conv-1", expected_revision=0, turn_id="turn-1")
    store.append_turn_message(
        "turn-1",
        Message(message_id="msg-draft", role="assistant", parts=(ContentPart(kind="text", text="draft"),)),
    )
    store.append_messages(
        "conv-1",
        expected_revision=0,
        messages=[Message(message_id="msg-other", role="user", parts=(ContentPart(kind="text", text="other"),))],
    )

    with pytest.raises(ConversationConflictError):
        store.commit_turn("turn-1")

    assert store.get_turn("turn-1").status == "failed"

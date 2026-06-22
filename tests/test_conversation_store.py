from __future__ import annotations

import pytest

from graphblocks.conversation import (
    BranchRequest,
    ContentPart,
    Conversation,
    ConversationArchivedError,
    ConversationConflictError,
    ConversationNotFoundError,
    InMemoryConversationStore,
    Message,
)


def test_append_messages_uses_expected_revision_cas() -> None:
    store = InMemoryConversationStore()
    store.create(Conversation(conversation_id="conv-1"))
    message = Message(
        message_id="msg-1",
        role="user",
        parts=(ContentPart(kind="text", text="hello"),),
    )

    new_revision = store.append_messages("conv-1", expected_revision=0, messages=[message])

    snapshot = store.get("conv-1")
    assert new_revision == 1
    assert snapshot.revision == 1
    assert snapshot.conversation.messages == (message,)
    with pytest.raises(ConversationConflictError):
        store.append_messages("conv-1", expected_revision=0, messages=[message])


def test_branch_preserves_lineage_and_copies_messages_through_source_message() -> None:
    store = InMemoryConversationStore()
    user_message = Message(
        message_id="msg-user",
        role="user",
        parts=(ContentPart(kind="text", text="explain policy"),),
    )
    assistant_message = Message(
        message_id="msg-assistant",
        role="assistant",
        parts=(ContentPart(kind="text", text="policy summary"),),
    )
    store.create(Conversation(conversation_id="conv-1"))
    store.append_messages("conv-1", expected_revision=0, messages=[user_message, assistant_message])

    branch = store.branch(BranchRequest(conversation_id="conv-1", from_message_id="msg-user", new_conversation_id="conv-2"))

    assert branch.conversation_id == "conv-2"
    assert branch.branch_of == "conv-1"
    assert branch.branched_from_message_id == "msg-user"
    assert branch.revision == 0
    assert branch.messages == (user_message,)
    assert branch.metadata["source_revision"] == 1


def test_archive_prevents_later_appends() -> None:
    store = InMemoryConversationStore()
    store.create(Conversation(conversation_id="conv-1"))

    revision = store.archive("conv-1")

    assert revision == 1
    assert store.get("conv-1").conversation.archived is True
    with pytest.raises(ConversationArchivedError):
        store.append_messages(
            "conv-1",
            expected_revision=1,
            messages=[Message(message_id="msg-1", role="user", parts=(ContentPart(kind="text", text="hello"),))],
        )


def test_delete_hard_removes_conversation() -> None:
    store = InMemoryConversationStore()
    store.create(Conversation(conversation_id="conv-1"))

    store.delete("conv-1", policy="hard")

    with pytest.raises(ConversationNotFoundError):
        store.get("conv-1")


def test_delete_tombstone_retains_empty_archived_conversation() -> None:
    store = InMemoryConversationStore()
    store.create(Conversation(conversation_id="conv-1"))

    revision = store.delete("conv-1", policy="tombstone")

    snapshot = store.get("conv-1")
    assert revision == 1
    assert snapshot.conversation.archived is True
    assert snapshot.conversation.messages == ()
    assert snapshot.conversation.metadata["deleted"] is True

from __future__ import annotations

import pytest

from graphblocks.documents import ArtifactRef
from graphblocks.conversation import (
    BranchRequest,
    ContentPart,
    Conversation,
    ConversationArchivedError,
    ConversationConflictError,
    ConversationNotFoundError,
    FileAttachment,
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


def test_scoped_attachments_resolve_for_context() -> None:
    store = InMemoryConversationStore()
    store.create(Conversation(conversation_id="conv-1"))
    store.add_attachment(
        "conv-1",
        FileAttachment(
            attachment_id="att-message",
            asset=ArtifactRef("artifact-message", "blob://attachments/message.pdf"),
            scope="message",
            purpose="retrieval",
            ingestion_status="ready",
            message_id="msg-user",
        ),
    )
    store.add_attachment(
        "conv-1",
        FileAttachment(
            attachment_id="att-conversation",
            asset=ArtifactRef("artifact-conversation", "blob://attachments/conversation.pdf"),
            scope="conversation",
            purpose="direct_input",
            ingestion_status="ready",
        ),
    )
    store.add_attachment(
        "conv-1",
        FileAttachment(
            attachment_id="att-pending",
            asset=ArtifactRef("artifact-pending", "blob://attachments/pending.pdf"),
            scope="message",
            purpose="retrieval",
            ingestion_status="pending",
            message_id="msg-user",
        ),
    )

    attachments = store.resolve_attachments("conv-1", ["msg-user"], include_conversation_scope=True)

    assert [attachment.attachment_id for attachment in attachments] == ["att-message", "att-conversation"]


def test_branch_respects_include_attachments_and_message_scope() -> None:
    store = InMemoryConversationStore()
    first = Message(message_id="msg-1", role="user")
    second = Message(message_id="msg-2", role="user")
    store.create(Conversation(conversation_id="conv-1"))
    store.append_messages("conv-1", expected_revision=0, messages=[first, second])
    store.add_attachment(
        "conv-1",
        FileAttachment(
            attachment_id="att-1",
            asset=ArtifactRef("artifact-1", "blob://attachments/one.pdf"),
            scope="message",
            purpose="retrieval",
            ingestion_status="ready",
            message_id="msg-1",
        ),
    )
    store.add_attachment(
        "conv-1",
        FileAttachment(
            attachment_id="att-2",
            asset=ArtifactRef("artifact-2", "blob://attachments/two.pdf"),
            scope="message",
            purpose="retrieval",
            ingestion_status="ready",
            message_id="msg-2",
        ),
    )
    store.add_attachment(
        "conv-1",
        FileAttachment(
            attachment_id="att-conversation",
            asset=ArtifactRef("artifact-conversation", "blob://attachments/conversation.pdf"),
            scope="conversation",
            purpose="reference",
            ingestion_status="ready",
        ),
    )

    with_attachments = store.branch(
        BranchRequest(conversation_id="conv-1", from_message_id="msg-1", new_conversation_id="conv-branch-1")
    )
    without_attachments = store.branch(
        BranchRequest(
            conversation_id="conv-1",
            from_message_id="msg-1",
            new_conversation_id="conv-branch-2",
            include_attachments=False,
        )
    )

    assert [attachment.attachment_id for attachment in with_attachments.attachments] == [
        "att-1",
        "att-conversation",
    ]
    assert without_attachments.attachments == ()


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

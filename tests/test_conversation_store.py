from __future__ import annotations

import pytest

from graphblocks.documents import ArtifactRef
from graphblocks.conversation import (
    BranchRequest,
    CompactionRecord,
    ContentPart,
    Conversation,
    ConversationArchivedError,
    ConversationConflictError,
    ConversationNotFoundError,
    FileAttachment,
    InMemoryConversationStore,
    Message,
    MessageNotFoundError,
    RegenerateRequest,
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


def test_regenerate_supersedes_assistant_and_branches_from_parent_user() -> None:
    store = InMemoryConversationStore()
    user = Message(message_id="msg-user", role="user", parts=(ContentPart(kind="text", text="try again"),))
    assistant = Message(
        message_id="msg-assistant",
        role="assistant",
        parent_message_id="msg-user",
        parts=(ContentPart(kind="text", text="first answer"),),
    )
    later = Message(message_id="msg-later", role="user", parts=(ContentPart(kind="text", text="later"),))
    store.create(Conversation(conversation_id="conv-1"))
    store.append_messages("conv-1", expected_revision=0, messages=[user, assistant, later])

    branch = store.regenerate(
        RegenerateRequest(
            conversation_id="conv-1",
            assistant_message_id="msg-assistant",
            new_conversation_id="conv-regenerated",
        )
    )

    snapshot = store.get("conv-1")
    assert snapshot.revision == 2
    assert [message.status for message in snapshot.conversation.messages] == [
        "committed",
        "superseded",
        "committed",
    ]
    assert branch.conversation_id == "conv-regenerated"
    assert branch.branch_of == "conv-1"
    assert branch.branched_from_message_id == "msg-user"
    assert branch.messages == (user,)
    assert branch.metadata["source_revision"] == 1
    assert branch.metadata["regenerated_from_message_id"] == "msg-assistant"


def test_regenerate_uses_previous_user_message_when_parent_is_not_recorded() -> None:
    store = InMemoryConversationStore()
    first = Message(message_id="msg-1", role="user")
    assistant = Message(message_id="msg-2", role="assistant")
    store.create(Conversation(conversation_id="conv-1"))
    store.append_messages("conv-1", expected_revision=0, messages=[first, assistant])

    branch = store.regenerate(
        RegenerateRequest(
            conversation_id="conv-1",
            assistant_message_id="msg-2",
            new_conversation_id="conv-regenerated",
        )
    )

    assert branch.branched_from_message_id == "msg-1"
    assert branch.messages == (first,)


def test_regenerate_branch_id_conflict_does_not_supersede_assistant() -> None:
    store = InMemoryConversationStore()
    user = Message(message_id="msg-1", role="user")
    assistant = Message(message_id="msg-2", role="assistant", parent_message_id="msg-1")
    store.create(Conversation(conversation_id="conv-1"))
    store.create(Conversation(conversation_id="conv-regenerated"))
    store.append_messages("conv-1", expected_revision=0, messages=[user, assistant])

    with pytest.raises(ConversationConflictError):
        store.regenerate(
            RegenerateRequest(
                conversation_id="conv-1",
                assistant_message_id="msg-2",
                new_conversation_id="conv-regenerated",
            )
        )

    snapshot = store.get("conv-1")
    assert snapshot.revision == 1
    assert snapshot.conversation.messages[1].status == "committed"


def test_compaction_record_preserves_source_messages_and_records_token_delta() -> None:
    store = InMemoryConversationStore()
    store.create(Conversation(conversation_id="conv-1"))
    store.append_messages(
        "conv-1",
        expected_revision=0,
        messages=[
            Message(message_id="msg-1", role="user"),
            Message(message_id="msg-2", role="assistant", parts=(ContentPart(kind="text", text="long answer"),)),
            Message(message_id="msg-summary", role="assistant", parts=(ContentPart(kind="text", text="compact"),)),
        ],
    )

    revision = store.record_compaction(
        "conv-1",
        CompactionRecord(
            compaction_id="compact-1",
            source_message_ids=("msg-1", "msg-2"),
            output_message_id="msg-summary",
            method="summary_memory",
            token_before=1200,
            token_after=120,
            model="summary-model",
        ),
    )

    snapshot = store.get("conv-1")
    assert revision == 2
    assert snapshot.conversation.messages[2].message_id == "msg-summary"
    assert snapshot.conversation.compactions[0].compaction_id == "compact-1"
    assert snapshot.conversation.compactions[0].source_message_ids == ("msg-1", "msg-2")
    assert snapshot.conversation.compactions[0].output_message_id == "msg-summary"
    assert snapshot.conversation.compactions[0].token_before == 1200
    assert snapshot.conversation.compactions[0].token_after == 120
    assert snapshot.conversation.compactions[0].model == "summary-model"


def test_compaction_rejects_missing_source_or_output_message() -> None:
    store = InMemoryConversationStore()
    store.create(Conversation(conversation_id="conv-1"))
    store.append_messages("conv-1", expected_revision=0, messages=[Message(message_id="msg-1", role="user")])

    with pytest.raises(MessageNotFoundError):
        store.record_compaction(
            "conv-1",
            CompactionRecord(
                compaction_id="compact-1",
                source_message_ids=("missing",),
                output_message_id="msg-1",
                method="summary_memory",
                token_before=10,
                token_after=5,
            ),
        )

    with pytest.raises(MessageNotFoundError):
        store.record_compaction(
            "conv-1",
            CompactionRecord(
                compaction_id="compact-2",
                source_message_ids=("msg-1",),
                output_message_id="missing-summary",
                method="summary_memory",
                token_before=10,
                token_after=5,
            ),
        )


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

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
from threading import Event, Lock

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
    ConversationSnapshot,
    FileAttachment,
    InMemoryConversationStore,
    Message,
    MessageNotFoundError,
    RegenerateRequest,
    Turn,
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


def test_append_messages_serializes_concurrent_revision_checks() -> None:
    store = InMemoryConversationStore()
    store.create(Conversation(conversation_id="conv-1"))
    first_read = Event()
    second_read = Event()
    call_lock = Lock()

    class CoordinatedConversations(dict[str, Conversation]):
        calls = 0

        def get(self, key: str, default: Conversation | None = None) -> Conversation | None:
            with call_lock:
                self.calls += 1
                call = self.calls
            if call == 1:
                first_read.set()
                second_read.wait(timeout=0.25)
            elif call == 2:
                second_read.set()
            return super().get(key, default)

    store._conversations = CoordinatedConversations(store._conversations)
    messages = (
        Message(message_id="msg-1", role="user"),
        Message(message_id="msg-2", role="user"),
    )

    def append(message: Message) -> str:
        first_read.wait(timeout=1)
        try:
            store.append_messages("conv-1", expected_revision=0, messages=[message])
        except ConversationConflictError:
            return "conflict"
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(append, messages))

    assert sorted(outcomes) == ["conflict", "success"]
    snapshot = store.get("conv-1")
    assert snapshot.revision == 1
    assert len(snapshot.conversation.messages) == 1


def test_conversation_store_copies_message_payloads_at_boundaries() -> None:
    store = InMemoryConversationStore()
    metadata = {"source": "initial"}
    data = {"answer": "original"}
    message = Message(
        message_id="msg-1",
        role="assistant",
        parts=(ContentPart(kind="json", data=data),),
        metadata=metadata,
    )

    store.create(Conversation(conversation_id="conv-1", messages=(message,)))
    metadata["source"] = "mutated"
    data["answer"] = "mutated"

    snapshot = store.get("conv-1")
    stored_message = snapshot.conversation.messages[0]
    assert stored_message.metadata == {"source": "initial"}
    assert stored_message.parts[0].data == {"answer": "original"}

    stored_message.metadata["source"] = "snapshot-mutated"
    assert stored_message.parts[0].data is not None
    stored_message.parts[0].data["answer"] = "snapshot-mutated"

    fresh = store.get("conv-1").conversation.messages[0]
    assert fresh.metadata == {"source": "initial"}
    assert fresh.parts[0].data == {"answer": "original"}


def test_conversation_records_validate_identity_literals_and_nested_types() -> None:
    with pytest.raises(ValueError, match="message message_id must not be empty"):
        Message(message_id=" ", role="user")
    with pytest.raises(ValueError, match="invalid message role"):
        Message(message_id="msg-1", role="critic")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="message parts must be ContentPart"):
        Message(message_id="msg-1", role="user", parts=(object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="message revision must be non-negative"):
        Message(message_id="msg-1", role="user", revision=-1)
    with pytest.raises(ValueError, match="content part metadata must be a mapping"):
        ContentPart(kind="text", text="hello", metadata=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="json content part data must be a mapping"):
        ContentPart(kind="json", data=[])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="json content part data.score must not contain non-finite numbers"):
        ContentPart(kind="json", data={"score": math.nan})
    with pytest.raises(ValueError, match="json content part data.payload must contain only JSON values"):
        ContentPart(kind="json", data={"payload": object()})
    with pytest.raises(ValueError, match="json content part data.items must contain only JSON values"):
        ContentPart(kind="json", data={"items": (1, 2)})
    with pytest.raises(ValueError, match="content part metadata.payload must contain only JSON values"):
        ContentPart(kind="text", text="hello", metadata={"payload": object()})

    with pytest.raises(ValueError, match="file attachment asset must be ArtifactRef"):
        FileAttachment("att-1", object(), "message", "retrieval", message_id="msg-1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid file attachment scope"):
        FileAttachment("att-1", ArtifactRef("artifact-1", "blob://a"), "workspace", "retrieval")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="message-scoped file attachment requires message_id"):
        FileAttachment("att-1", ArtifactRef("artifact-1", "blob://a"), "message", "retrieval")

    message = Message("msg-1", "user")
    with pytest.raises(ValueError, match="conversation message_id values must be unique"):
        Conversation("conv-1", messages=(message, message))
    with pytest.raises(ValueError, match="conversation archived must be a boolean"):
        Conversation("conv-1", archived="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="conversation snapshot revision must match conversation revision"):
        ConversationSnapshot(Conversation("conv-1", revision=1), revision=0)
    with pytest.raises(ValueError, match="surrounding whitespace"):
        Message(message_id=" msg-1", role="user")

    recursive: list[object] = []
    recursive.append(recursive)
    with pytest.raises(ValueError, match="strict canonical JSON"):
        ContentPart(kind="json", data={"recursive": recursive})
    nested: dict[str, object] = {}
    current = nested
    for _ in range(65):
        child: dict[str, object] = {}
        current["child"] = child
        current = child
    with pytest.raises(ValueError, match="at most 64 nesting levels"):
        ContentPart(kind="json", data=nested)
    with pytest.raises(ValueError, match="strict canonical JSON"):
        ContentPart(kind="text", text="hello", metadata={"value": "\ud800"})


def test_conversation_request_compaction_and_turn_records_validate_contracts() -> None:
    with pytest.raises(ValueError, match="branch request include_attachments must be a boolean"):
        BranchRequest("conv-1", "msg-1", include_attachments="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="regenerate request assistant_message_id must not be empty"):
        RegenerateRequest("conv-1", " ")
    with pytest.raises(ValueError, match="compaction record source_message_ids must not be empty"):
        CompactionRecord("compact-1", (), "msg-summary", "summary", 10, 5)
    with pytest.raises(ValueError, match="compaction record token_after must not exceed token_before"):
        CompactionRecord("compact-1", ("msg-1",), "msg-summary", "summary", 10, 11)
    with pytest.raises(ValueError, match="invalid turn status"):
        Turn("turn-1", "conv-1", 0, status="paused")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="turn messages must be Message"):
        Turn("turn-1", "conv-1", 0, messages=(object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="completed turn requires committed_revision"):
        Turn("turn-1", "conv-1", 0, status="completed", committed_message_ids=("msg-1",))
    with pytest.raises(ValueError, match="non-completed turn must not carry committed revision data"):
        Turn("turn-1", "conv-1", 0, committed_revision=1)
    duplicate = Message("msg-1", "assistant", status="draft")
    with pytest.raises(ValueError, match="turn message_id values must be unique"):
        Turn("turn-1", "conv-1", 0, messages=(duplicate, duplicate))
    committed = Message("msg-1", "assistant", status="committed")
    with pytest.raises(ValueError, match="base_revision plus one"):
        Turn(
            "turn-1",
            "conv-1",
            0,
            status="completed",
            messages=(committed,),
            committed_revision=2,
            committed_message_ids=("msg-1",),
        )


def test_conversation_store_validates_and_detaches_restored_state() -> None:
    conversation = Conversation("conv-1", metadata={"state": {"phase": "initial"}})
    restored = InMemoryConversationStore({"conv-1": conversation})
    conversation.metadata["state"]["phase"] = "mutated"  # type: ignore[index]

    assert restored.get("conv-1").conversation.metadata == {
        "state": {"phase": "initial"}
    }
    with pytest.raises(ValueError, match="conversation key must match"):
        InMemoryConversationStore({"wrong": conversation})
    with pytest.raises(ValueError, match="reference a stored conversation"):
        InMemoryConversationStore(
            {"conv-1": conversation},
            {"turn-1": Turn("turn-1", "missing", 0)},
        )


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


def test_archived_conversation_rejects_compaction_without_mutation() -> None:
    store = InMemoryConversationStore()
    message = Message(message_id="msg-1", role="user")
    store.create(Conversation(conversation_id="conv-1", messages=(message,)))
    archived_revision = store.archive("conv-1")

    with pytest.raises(ConversationArchivedError, match="is archived"):
        store.record_compaction(
            "conv-1",
            CompactionRecord(
                compaction_id="compaction-1",
                source_message_ids=("msg-1",),
                output_message_id="msg-1",
                method="summary",
                token_before=10,
                token_after=5,
            ),
        )

    snapshot = store.get("conv-1")
    assert snapshot.revision == archived_revision
    assert snapshot.conversation.compactions == ()


def test_scoped_attachments_resolve_for_context() -> None:
    store = InMemoryConversationStore()
    store.create(
        Conversation(
            conversation_id="conv-1",
            messages=(Message(message_id="msg-user", role="user"),),
        )
    )
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


def test_message_scoped_attachment_must_reference_a_conversation_message() -> None:
    message = Message(message_id="msg-1", role="user")
    orphan = FileAttachment(
        attachment_id="att-orphan",
        asset=ArtifactRef("artifact-orphan", "blob://attachments/orphan.pdf"),
        scope="message",
        purpose="retrieval",
        ingestion_status="ready",
        message_id="msg-other",
    )

    with pytest.raises(
        ValueError,
        match="message-scoped conversation attachment must reference a conversation message",
    ):
        Conversation("conv-invalid", messages=(message,), attachments=(orphan,))

    store = InMemoryConversationStore()
    store.create(Conversation("conv-1", messages=(message,)))
    with pytest.raises(
        ValueError,
        match="message-scoped conversation attachment must reference a conversation message",
    ):
        store.add_attachment("conv-1", orphan)

    snapshot = store.get("conv-1")
    assert snapshot.revision == 0
    assert snapshot.conversation.attachments == ()


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


def test_branch_and_regenerate_drop_compactions_with_excluded_messages() -> None:
    store = InMemoryConversationStore()
    user = Message(message_id="msg-user", role="user")
    assistant = Message(
        message_id="msg-assistant",
        role="assistant",
        parent_message_id="msg-user",
    )
    later = Message(message_id="msg-later", role="user")
    store.create(Conversation(conversation_id="conv-1"))
    store.append_messages(
        "conv-1",
        expected_revision=0,
        messages=[user, assistant, later],
    )
    store.record_compaction(
        "conv-1",
        CompactionRecord(
            compaction_id="compact-dangling",
            source_message_ids=("msg-user", "msg-later"),
            output_message_id="msg-assistant",
            method="summary_memory",
            token_before=20,
            token_after=10,
        ),
    )

    branch = store.branch(
        BranchRequest(
            conversation_id="conv-1",
            from_message_id="msg-assistant",
            new_conversation_id="conv-branch",
            include_memory=True,
        )
    )
    regenerated = store.regenerate(
        RegenerateRequest(
            conversation_id="conv-1",
            assistant_message_id="msg-assistant",
            new_conversation_id="conv-regenerated",
            include_memory=True,
        )
    )

    assert branch.compactions == ()
    assert regenerated.compactions == ()


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

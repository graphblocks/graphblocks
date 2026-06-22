from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


MessageRole = Literal["system", "developer", "user", "assistant", "tool"]
MessageStatus = Literal["draft", "committed", "superseded", "retracted"]
DeletePolicy = Literal["tombstone", "hard"]


class ConversationError(RuntimeError):
    pass


class ConversationNotFoundError(ConversationError):
    pass


class ConversationConflictError(ConversationError):
    pass


class ConversationArchivedError(ConversationError):
    pass


class MessageNotFoundError(ConversationError):
    pass


@dataclass(frozen=True, slots=True)
class ContentPart:
    kind: Literal["text", "json", "artifact_ref"]
    text: str | None = None
    data: dict[str, object] | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Message:
    message_id: str
    role: MessageRole
    parts: tuple[ContentPart, ...] = field(default_factory=tuple)
    parent_message_id: str | None = None
    revision: int = 0
    status: MessageStatus = "committed"
    created_at: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Conversation:
    conversation_id: str
    messages: tuple[Message, ...] = field(default_factory=tuple)
    revision: int = 0
    archived: bool = False
    branch_of: str | None = None
    branched_from_message_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    conversation: Conversation
    revision: int


@dataclass(frozen=True, slots=True)
class BranchRequest:
    conversation_id: str
    from_message_id: str
    new_conversation_id: str | None = None
    include_attachments: bool = True
    include_memory: bool = False


def _copy_conversation(conversation: Conversation) -> Conversation:
    return Conversation(
        conversation_id=conversation.conversation_id,
        messages=tuple(conversation.messages),
        revision=conversation.revision,
        archived=conversation.archived,
        branch_of=conversation.branch_of,
        branched_from_message_id=conversation.branched_from_message_id,
        metadata=dict(conversation.metadata),
    )


@dataclass(slots=True)
class InMemoryConversationStore:
    _conversations: dict[str, Conversation] = field(default_factory=dict)

    def create(self, conversation: Conversation) -> None:
        if conversation.conversation_id in self._conversations:
            raise ConversationConflictError(f"conversation {conversation.conversation_id!r} already exists")
        self._conversations[conversation.conversation_id] = _copy_conversation(conversation)

    def get(self, conversation_id: str) -> ConversationSnapshot:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        return ConversationSnapshot(conversation=_copy_conversation(conversation), revision=conversation.revision)

    def append_messages(self, conversation_id: str, expected_revision: int, messages: list[Message]) -> int:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        if conversation.archived:
            raise ConversationArchivedError(f"conversation {conversation_id!r} is archived")
        if conversation.revision != expected_revision:
            raise ConversationConflictError(
                f"conversation {conversation_id!r} is at revision {conversation.revision}, not {expected_revision}"
            )
        new_revision = conversation.revision + 1
        self._conversations[conversation_id] = Conversation(
            conversation_id=conversation.conversation_id,
            messages=(*conversation.messages, *messages),
            revision=new_revision,
            archived=conversation.archived,
            branch_of=conversation.branch_of,
            branched_from_message_id=conversation.branched_from_message_id,
            metadata=dict(conversation.metadata),
        )
        return new_revision

    def branch(self, request: BranchRequest) -> Conversation:
        conversation = self._conversations.get(request.conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {request.conversation_id!r} does not exist")
        branch_id = request.new_conversation_id or f"{request.conversation_id}:branch:{request.from_message_id}"
        if branch_id in self._conversations:
            raise ConversationConflictError(f"conversation {branch_id!r} already exists")
        source_index = None
        for index, message in enumerate(conversation.messages):
            if message.message_id == request.from_message_id:
                source_index = index
                break
        if source_index is None:
            raise MessageNotFoundError(f"message {request.from_message_id!r} does not exist")
        branch = Conversation(
            conversation_id=branch_id,
            messages=conversation.messages[: source_index + 1],
            revision=0,
            branch_of=conversation.conversation_id,
            branched_from_message_id=request.from_message_id,
            metadata={
                "source_revision": conversation.revision,
                "include_attachments": request.include_attachments,
                "include_memory": request.include_memory,
            },
        )
        self._conversations[branch_id] = branch
        return _copy_conversation(branch)

    def archive(self, conversation_id: str) -> int:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        new_revision = conversation.revision + 1
        self._conversations[conversation_id] = Conversation(
            conversation_id=conversation.conversation_id,
            messages=conversation.messages,
            revision=new_revision,
            archived=True,
            branch_of=conversation.branch_of,
            branched_from_message_id=conversation.branched_from_message_id,
            metadata=dict(conversation.metadata),
        )
        return new_revision

    def delete(self, conversation_id: str, policy: DeletePolicy = "tombstone") -> int | None:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        if policy == "hard":
            del self._conversations[conversation_id]
            return None
        if policy != "tombstone":
            raise ValueError("policy must be tombstone or hard")
        new_revision = conversation.revision + 1
        metadata = dict(conversation.metadata)
        metadata["deleted"] = True
        self._conversations[conversation_id] = Conversation(
            conversation_id=conversation.conversation_id,
            messages=(),
            revision=new_revision,
            archived=True,
            branch_of=conversation.branch_of,
            branched_from_message_id=conversation.branched_from_message_id,
            metadata=metadata,
        )
        return new_revision

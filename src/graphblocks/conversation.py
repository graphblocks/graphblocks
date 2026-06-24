from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from .documents import ArtifactRef

MessageRole = Literal["system", "developer", "user", "assistant", "tool"]
MessageStatus = Literal["draft", "committed", "superseded", "retracted"]
DeletePolicy = Literal["tombstone", "hard"]
AttachmentScope = Literal["message", "conversation", "user", "project", "tenant"]
AttachmentPurpose = Literal["direct_input", "retrieval", "code_analysis", "reference", "output"]
AttachmentIngestionStatus = Literal["pending", "processing", "ready", "failed", "expired", "deleted"]
TurnStatus = Literal[
    "created",
    "context_building",
    "model_running",
    "tool_waiting",
    "approval_waiting",
    "finalizing",
    "completed",
    "failed",
    "cancelled",
    "policy_stopped",
]


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


class TurnNotFoundError(ConversationError):
    pass


class TurnConflictError(ConversationError):
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
class FileAttachment:
    attachment_id: str
    asset: ArtifactRef
    scope: AttachmentScope
    purpose: AttachmentPurpose
    ingestion_status: AttachmentIngestionStatus = "pending"
    retention_policy: str | None = None
    message_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        return self.ingestion_status == "ready"


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    compaction_id: str
    source_message_ids: tuple[str, ...]
    output_message_id: str
    method: str
    token_before: int
    token_after: int
    model: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Conversation:
    conversation_id: str
    messages: tuple[Message, ...] = field(default_factory=tuple)
    attachments: tuple[FileAttachment, ...] = field(default_factory=tuple)
    compactions: tuple[CompactionRecord, ...] = field(default_factory=tuple)
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


@dataclass(frozen=True, slots=True)
class RegenerateRequest:
    conversation_id: str
    assistant_message_id: str
    new_conversation_id: str | None = None
    include_attachments: bool = True
    include_memory: bool = False


@dataclass(frozen=True, slots=True)
class Turn:
    turn_id: str
    conversation_id: str
    base_revision: int
    status: TurnStatus = "created"
    messages: tuple[Message, ...] = field(default_factory=tuple)
    committed_revision: int | None = None
    committed_message_ids: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, object] = field(default_factory=dict)


def _copy_conversation(conversation: Conversation) -> Conversation:
    return Conversation(
        conversation_id=conversation.conversation_id,
        messages=tuple(conversation.messages),
        attachments=tuple(_copy_attachment(attachment) for attachment in conversation.attachments),
        compactions=tuple(_copy_compaction(record) for record in conversation.compactions),
        revision=conversation.revision,
        archived=conversation.archived,
        branch_of=conversation.branch_of,
        branched_from_message_id=conversation.branched_from_message_id,
        metadata=dict(conversation.metadata),
    )


def _copy_attachment(attachment: FileAttachment) -> FileAttachment:
    return FileAttachment(
        attachment_id=attachment.attachment_id,
        asset=attachment.asset,
        scope=attachment.scope,
        purpose=attachment.purpose,
        ingestion_status=attachment.ingestion_status,
        retention_policy=attachment.retention_policy,
        message_id=attachment.message_id,
        metadata=dict(attachment.metadata),
    )


def _copy_compaction(record: CompactionRecord) -> CompactionRecord:
    return CompactionRecord(
        compaction_id=record.compaction_id,
        source_message_ids=tuple(record.source_message_ids),
        output_message_id=record.output_message_id,
        method=record.method,
        token_before=record.token_before,
        token_after=record.token_after,
        model=record.model,
        metadata=dict(record.metadata),
    )


def _copy_turn(turn: Turn) -> Turn:
    return Turn(
        turn_id=turn.turn_id,
        conversation_id=turn.conversation_id,
        base_revision=turn.base_revision,
        status=turn.status,
        messages=tuple(turn.messages),
        committed_revision=turn.committed_revision,
        committed_message_ids=tuple(turn.committed_message_ids),
        metadata=dict(turn.metadata),
    )


@dataclass(slots=True)
class InMemoryConversationStore:
    _conversations: dict[str, Conversation] = field(default_factory=dict)
    _turns: dict[str, Turn] = field(default_factory=dict)

    def create(self, conversation: Conversation) -> None:
        if conversation.conversation_id in self._conversations:
            raise ConversationConflictError(f"conversation {conversation.conversation_id!r} already exists")
        self._conversations[conversation.conversation_id] = _copy_conversation(conversation)

    def get(self, conversation_id: str) -> ConversationSnapshot:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        return ConversationSnapshot(conversation=_copy_conversation(conversation), revision=conversation.revision)

    def begin_turn(self, conversation_id: str, expected_revision: int, turn_id: str) -> Turn:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        if conversation.archived:
            raise ConversationArchivedError(f"conversation {conversation_id!r} is archived")
        if turn_id in self._turns:
            raise TurnConflictError(f"turn {turn_id!r} already exists")
        if conversation.revision != expected_revision:
            raise ConversationConflictError(
                f"conversation {conversation_id!r} is at revision {conversation.revision}, not {expected_revision}"
            )
        turn = Turn(turn_id=turn_id, conversation_id=conversation_id, base_revision=expected_revision)
        self._turns[turn_id] = turn
        return _copy_turn(turn)

    def get_turn(self, turn_id: str) -> Turn:
        turn = self._turns.get(turn_id)
        if turn is None:
            raise TurnNotFoundError(f"turn {turn_id!r} does not exist")
        return _copy_turn(turn)

    def append_turn_message(self, turn_id: str, message: Message) -> Turn:
        turn = self._turns.get(turn_id)
        if turn is None:
            raise TurnNotFoundError(f"turn {turn_id!r} does not exist")
        if turn.status in {"completed", "failed", "cancelled", "policy_stopped"}:
            raise TurnConflictError(f"turn {turn_id!r} is already terminal")
        draft_message = replace(message, status="draft")
        updated = Turn(
            turn_id=turn.turn_id,
            conversation_id=turn.conversation_id,
            base_revision=turn.base_revision,
            status="model_running" if turn.status == "created" else turn.status,
            messages=(*turn.messages, draft_message),
            committed_revision=turn.committed_revision,
            committed_message_ids=turn.committed_message_ids,
            metadata=dict(turn.metadata),
        )
        self._turns[turn_id] = updated
        return _copy_turn(updated)

    def commit_turn(self, turn_id: str) -> Turn:
        turn = self._turns.get(turn_id)
        if turn is None:
            raise TurnNotFoundError(f"turn {turn_id!r} does not exist")
        if turn.status in {"completed", "failed", "cancelled", "policy_stopped"}:
            raise TurnConflictError(f"turn {turn_id!r} is already terminal")
        committed_messages = tuple(replace(message, status="committed") for message in turn.messages)
        try:
            new_revision = self.append_messages(turn.conversation_id, turn.base_revision, list(committed_messages))
        except ConversationConflictError:
            failed = Turn(
                turn_id=turn.turn_id,
                conversation_id=turn.conversation_id,
                base_revision=turn.base_revision,
                status="failed",
                messages=turn.messages,
                committed_revision=None,
                committed_message_ids=(),
                metadata=dict(turn.metadata),
            )
            self._turns[turn_id] = failed
            raise
        completed = Turn(
            turn_id=turn.turn_id,
            conversation_id=turn.conversation_id,
            base_revision=turn.base_revision,
            status="completed",
            messages=committed_messages,
            committed_revision=new_revision,
            committed_message_ids=tuple(message.message_id for message in committed_messages),
            metadata=dict(turn.metadata),
        )
        self._turns[turn_id] = completed
        return _copy_turn(completed)

    def abort_turn(self, turn_id: str) -> Turn:
        turn = self._turns.get(turn_id)
        if turn is None:
            raise TurnNotFoundError(f"turn {turn_id!r} does not exist")
        if turn.status in {"completed", "failed", "cancelled", "policy_stopped"}:
            raise TurnConflictError(f"turn {turn_id!r} is already terminal")
        cancelled = Turn(
            turn_id=turn.turn_id,
            conversation_id=turn.conversation_id,
            base_revision=turn.base_revision,
            status="cancelled",
            messages=tuple(replace(message, status="retracted") for message in turn.messages),
            committed_revision=None,
            committed_message_ids=(),
            metadata=dict(turn.metadata),
        )
        self._turns[turn_id] = cancelled
        return _copy_turn(cancelled)

    def policy_stop_turn(self, turn_id: str) -> Turn:
        turn = self._turns.get(turn_id)
        if turn is None:
            raise TurnNotFoundError(f"turn {turn_id!r} does not exist")
        if turn.status in {"completed", "failed", "cancelled", "policy_stopped"}:
            raise TurnConflictError(f"turn {turn_id!r} is already terminal")
        stopped = Turn(
            turn_id=turn.turn_id,
            conversation_id=turn.conversation_id,
            base_revision=turn.base_revision,
            status="policy_stopped",
            messages=tuple(replace(message, status="retracted") for message in turn.messages),
            committed_revision=None,
            committed_message_ids=(),
            metadata=dict(turn.metadata),
        )
        self._turns[turn_id] = stopped
        return _copy_turn(stopped)

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
            attachments=conversation.attachments,
            compactions=conversation.compactions,
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
        if conversation.archived:
            raise ConversationArchivedError(f"conversation {request.conversation_id!r} is archived")
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
        branch_messages = conversation.messages[: source_index + 1]
        branch_message_ids = {message.message_id for message in branch_messages}
        branch = Conversation(
            conversation_id=branch_id,
            messages=branch_messages,
            attachments=tuple(
                _copy_attachment(attachment)
                for attachment in conversation.attachments
                if request.include_attachments
                and (
                    attachment.scope == "conversation"
                    or (attachment.scope == "message" and attachment.message_id in branch_message_ids)
                )
            ),
            compactions=tuple(_copy_compaction(record) for record in conversation.compactions) if request.include_memory else (),
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

    def regenerate(self, request: RegenerateRequest) -> Conversation:
        conversation = self._conversations.get(request.conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {request.conversation_id!r} does not exist")
        if conversation.archived:
            raise ConversationArchivedError(f"conversation {request.conversation_id!r} is archived")
        branch_id = request.new_conversation_id or f"{request.conversation_id}:regenerate:{request.assistant_message_id}"
        if branch_id in self._conversations:
            raise ConversationConflictError(f"conversation {branch_id!r} already exists")

        assistant_index = None
        for index, message in enumerate(conversation.messages):
            if message.message_id == request.assistant_message_id:
                assistant_index = index
                break
        if assistant_index is None:
            raise MessageNotFoundError(f"message {request.assistant_message_id!r} does not exist")

        assistant_message = conversation.messages[assistant_index]
        if assistant_message.role != "assistant":
            raise ConversationConflictError(f"message {request.assistant_message_id!r} is not an assistant message")
        if assistant_message.status == "superseded":
            raise ConversationConflictError(f"message {request.assistant_message_id!r} is already superseded")

        parent_index = None
        if assistant_message.parent_message_id is not None:
            for index, message in enumerate(conversation.messages):
                if message.message_id == assistant_message.parent_message_id:
                    parent_index = index
                    break
            if parent_index is None:
                raise MessageNotFoundError(f"message {assistant_message.parent_message_id!r} does not exist")
            if conversation.messages[parent_index].role != "user":
                raise ConversationConflictError(
                    f"message {assistant_message.parent_message_id!r} is not a user message"
                )
            if parent_index >= assistant_index:
                raise ConversationConflictError(
                    f"parent message {assistant_message.parent_message_id!r} must precede assistant message"
                )
        else:
            for index in range(assistant_index - 1, -1, -1):
                if conversation.messages[index].role == "user":
                    parent_index = index
                    break
            if parent_index is None:
                raise MessageNotFoundError(
                    f"parent user message for assistant message {request.assistant_message_id!r} does not exist"
                )

        branch_messages = conversation.messages[: parent_index + 1]
        branch_message_ids = {message.message_id for message in branch_messages}
        branch = Conversation(
            conversation_id=branch_id,
            messages=branch_messages,
            attachments=tuple(
                _copy_attachment(attachment)
                for attachment in conversation.attachments
                if request.include_attachments
                and (
                    attachment.scope == "conversation"
                    or (attachment.scope == "message" and attachment.message_id in branch_message_ids)
                )
            ),
            compactions=tuple(_copy_compaction(record) for record in conversation.compactions) if request.include_memory else (),
            revision=0,
            branch_of=conversation.conversation_id,
            branched_from_message_id=conversation.messages[parent_index].message_id,
            metadata={
                "source_revision": conversation.revision,
                "include_attachments": request.include_attachments,
                "include_memory": request.include_memory,
                "regenerated_from_message_id": request.assistant_message_id,
            },
        )
        superseded_messages = tuple(
            replace(message, status="superseded") if index == assistant_index else message
            for index, message in enumerate(conversation.messages)
        )
        self._conversations[request.conversation_id] = Conversation(
            conversation_id=conversation.conversation_id,
            messages=superseded_messages,
            attachments=conversation.attachments,
            compactions=conversation.compactions,
            revision=conversation.revision + 1,
            archived=conversation.archived,
            branch_of=conversation.branch_of,
            branched_from_message_id=conversation.branched_from_message_id,
            metadata=dict(conversation.metadata),
        )
        self._conversations[branch_id] = branch
        return _copy_conversation(branch)

    def add_attachment(self, conversation_id: str, attachment: FileAttachment) -> int:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        if conversation.archived:
            raise ConversationArchivedError(f"conversation {conversation_id!r} is archived")
        new_revision = conversation.revision + 1
        self._conversations[conversation_id] = Conversation(
            conversation_id=conversation.conversation_id,
            messages=conversation.messages,
            attachments=(*conversation.attachments, _copy_attachment(attachment)),
            compactions=conversation.compactions,
            revision=new_revision,
            archived=conversation.archived,
            branch_of=conversation.branch_of,
            branched_from_message_id=conversation.branched_from_message_id,
            metadata=dict(conversation.metadata),
        )
        return new_revision

    def resolve_attachments(
        self,
        conversation_id: str,
        message_ids: list[str],
        *,
        include_conversation_scope: bool,
    ) -> tuple[FileAttachment, ...]:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        message_id_set = set(message_ids)
        return tuple(
            _copy_attachment(attachment)
            for attachment in conversation.attachments
            if attachment.is_ready
            and (
                (include_conversation_scope and attachment.scope == "conversation")
                or (attachment.scope == "message" and attachment.message_id in message_id_set)
            )
        )

    def record_compaction(self, conversation_id: str, record: CompactionRecord) -> int:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        message_ids = {message.message_id for message in conversation.messages}
        for source_message_id in record.source_message_ids:
            if source_message_id not in message_ids:
                raise MessageNotFoundError(f"message {source_message_id!r} does not exist")
        if record.output_message_id not in message_ids:
            raise MessageNotFoundError(f"message {record.output_message_id!r} does not exist")
        new_revision = conversation.revision + 1
        self._conversations[conversation_id] = Conversation(
            conversation_id=conversation.conversation_id,
            messages=conversation.messages,
            attachments=conversation.attachments,
            compactions=(*conversation.compactions, _copy_compaction(record)),
            revision=new_revision,
            archived=conversation.archived,
            branch_of=conversation.branch_of,
            branched_from_message_id=conversation.branched_from_message_id,
            metadata=dict(conversation.metadata),
        )
        return new_revision

    def archive(self, conversation_id: str) -> int:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        new_revision = conversation.revision + 1
        self._conversations[conversation_id] = Conversation(
            conversation_id=conversation.conversation_id,
            messages=conversation.messages,
            attachments=conversation.attachments,
            compactions=conversation.compactions,
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
            attachments=(),
            compactions=(),
            revision=new_revision,
            archived=True,
            branch_of=conversation.branch_of,
            branched_from_message_id=conversation.branched_from_message_id,
            metadata=metadata,
        )
        return new_revision

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import wraps
import math
from threading import RLock
from typing import Literal, ParamSpec, TypeVar, cast

from .canonical import MAX_CANONICAL_JSON_DEPTH, _has_unicode_surrogate, canonical_dumps
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
VALID_MESSAGE_ROLES = frozenset(("system", "developer", "user", "assistant", "tool"))
VALID_MESSAGE_STATUSES = frozenset(("draft", "committed", "superseded", "retracted"))
VALID_DELETE_POLICIES = frozenset(("tombstone", "hard"))
VALID_ATTACHMENT_SCOPES = frozenset(("message", "conversation", "user", "project", "tenant"))
VALID_ATTACHMENT_PURPOSES = frozenset(("direct_input", "retrieval", "code_analysis", "reference", "output"))
VALID_ATTACHMENT_INGESTION_STATUSES = frozenset(("pending", "processing", "ready", "failed", "expired", "deleted"))
VALID_TURN_STATUSES = frozenset(
    (
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
    )
)
TERMINAL_TURN_STATUSES = frozenset(("completed", "failed", "cancelled", "policy_stopped"))
_MAX_CONVERSATION_REVISION = (1 << 64) - 1
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _with_conversation_store_lock(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def locked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        store = cast("InMemoryConversationStore", args[0])
        with store._lock:
            return method(*args, **kwargs)

    return locked


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{owner} {field_name} must not contain surrounding whitespace")
    if _has_unicode_surrogate(value):
        raise ValueError(
            f"{owner} {field_name} must contain only Unicode scalar values"
        )
    return value


def _validate_optional_non_empty_string(owner: str, field_name: str, value: object | None) -> str | None:
    if value is None:
        return None
    return _validate_non_empty_string(owner, field_name, value)


def _validate_non_negative_int(owner: str, field_name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{owner} {field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{owner} {field_name} must be non-negative")
    return value


def _validate_revision(owner: str, field_name: str, value: object) -> int:
    revision = _validate_non_negative_int(owner, field_name, value)
    if revision > _MAX_CONVERSATION_REVISION:
        raise ValueError(f"{owner} {field_name} exceeds storage range")
    return revision


def _next_revision(revision: int) -> int:
    if revision >= _MAX_CONVERSATION_REVISION:
        raise OverflowError("conversation revision exhausted")
    return revision + 1


def _require_expected_revision(
    conversation: Conversation,
    expected_revision: object,
) -> None:
    expected = _validate_revision(
        "conversation store",
        "expected_revision",
        expected_revision,
    )
    if conversation.revision != expected:
        raise ConversationConflictError(
            f"conversation {conversation.conversation_id!r} is at revision "
            f"{conversation.revision}, not {expected}"
        )


def _copy_mapping(owner: str, field_name: str, value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{owner} {field_name} must be a mapping")
    try:
        copied = _copy_mapping_value(
            owner,
            field_name,
            value,
            active_containers=set(),
            depth=0,
        )
    except ValueError:
        raise
    except (RuntimeError, TypeError, RecursionError) as error:
        raise ValueError(f"{owner} {field_name} must be a mapping") from error
    try:
        canonical_dumps(copied)
    except (TypeError, ValueError, RuntimeError, RecursionError) as error:
        raise ValueError(f"{owner} {field_name} must contain strict canonical JSON") from error
    return copied


def _copy_mapping_value(
    owner: str,
    path: str,
    value: Mapping[object, object],
    *,
    active_containers: set[int],
    depth: int,
) -> dict[str, object]:
    if depth > MAX_CANONICAL_JSON_DEPTH:
        raise ValueError(
            f"{owner} {path} must contain strict canonical JSON "
            f"with at most {MAX_CANONICAL_JSON_DEPTH} nesting levels"
        )
    identity = id(value)
    if identity in active_containers:
        raise ValueError(
            f"{owner} {path} must contain strict canonical JSON without cyclic values"
        )
    active_containers.add(identity)
    try:
        items = tuple(value.items())
        copied: dict[str, object] = {}
        for key, item in items:
            normalized_key = _validate_non_empty_string(
                owner,
                f"{path} key",
                key,
            )
            if normalized_key in copied:
                raise ValueError(
                    f"{owner} {path} keys must be unique"
                )
            copied[normalized_key] = _copy_json_value(
                owner,
                f"{path}.{normalized_key}",
                item,
                active_containers=active_containers,
                depth=depth + 1,
            )
        return copied
    finally:
        active_containers.remove(identity)


def _copy_json_value(
    owner: str,
    path: str,
    value: object,
    *,
    active_containers: set[int],
    depth: int,
) -> object:
    if depth > MAX_CANONICAL_JSON_DEPTH:
        raise ValueError(
            f"{owner} {path} must contain strict canonical JSON "
            f"with at most {MAX_CANONICAL_JSON_DEPTH} nesting levels"
        )
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{owner} {path} must not contain non-finite numbers")
        return value
    if isinstance(value, list):
        identity = id(value)
        if identity in active_containers:
            raise ValueError(
                f"{owner} {path} must contain strict canonical JSON without cyclic values"
            )
        active_containers.add(identity)
        try:
            return [
                _copy_json_value(
                    owner,
                    path,
                    item,
                    active_containers=active_containers,
                    depth=depth + 1,
                )
                for item in value
            ]
        finally:
            active_containers.remove(identity)
    if isinstance(value, Mapping):
        return _copy_mapping_value(
            owner,
            path,
            value,
            active_containers=active_containers,
            depth=depth,
        )
    raise ValueError(f"{owner} {path} must contain only JSON values")


def _validate_string_tuple(owner: str, field_name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise ValueError(f"{owner} {field_name} must be a collection of strings")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except (TypeError, RuntimeError) as error:
        raise ValueError(f"{owner} {field_name} must be a collection of strings") from error
    for item in items:
        _validate_non_empty_string(owner, f"{field_name} item", item)
    return items


def _snapshot_collection(
    owner: str,
    field_name: str,
    value: object,
) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise ValueError(f"{owner} {field_name} must be a collection")
    try:
        return tuple(value)  # type: ignore[arg-type]
    except (TypeError, RuntimeError) as error:
        raise ValueError(f"{owner} {field_name} must be a collection") from error


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

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {"text", "json", "artifact_ref"}:
            raise ValueError(f"invalid content part kind {self.kind}")
        if self.kind == "text":
            if self.text is None:
                raise ValueError("text content part requires text")
            if not isinstance(self.text, str):
                raise ValueError("text content part text must be a string")
            if _has_unicode_surrogate(self.text):
                raise ValueError(
                    "text content part text must contain only Unicode scalar values"
                )
            if self.data is not None:
                raise ValueError("text content part must not carry data")
        elif self.kind == "json":
            if self.data is None:
                raise ValueError("json content part requires data")
            object.__setattr__(self, "data", _copy_mapping("json content part", "data", self.data))
            if self.text is not None:
                raise ValueError("json content part must not carry text")
        elif self.kind == "artifact_ref":
            if self.data is None:
                raise ValueError("artifact_ref content part requires data")
            data = _copy_mapping("artifact_ref content part", "data", self.data)
            for field_name in ("artifact_id", "uri"):
                value = data.get(field_name)
                if not isinstance(value, str):
                    raise ValueError(f"artifact_ref content part {field_name} must be a string")
                if not value.strip():
                    raise ValueError(f"artifact_ref content part {field_name} must not be empty")
                if value != value.strip():
                    raise ValueError(
                        f"artifact_ref content part {field_name} must not contain surrounding whitespace"
                    )
            for field_name in ("media_type", "checksum", "etag", "version", "filename"):
                value = data.get(field_name)
                if value is not None:
                    if not isinstance(value, str):
                        raise ValueError(f"artifact_ref content part {field_name} must be a string")
                    if not value.strip():
                        raise ValueError(f"artifact_ref content part {field_name} must not be empty")
                    if value != value.strip():
                        raise ValueError(
                            f"artifact_ref content part {field_name} must not contain surrounding whitespace"
                        )
            size_bytes = data.get("size_bytes")
            if size_bytes is not None:
                if not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
                    raise ValueError("artifact_ref content part size_bytes must be an integer")
                if size_bytes < 0:
                    raise ValueError("artifact_ref content part size_bytes must be non-negative")
                if size_bytes > _MAX_CONVERSATION_REVISION:
                    raise ValueError(
                        "artifact_ref content part size_bytes exceeds storage range"
                    )
            object.__setattr__(self, "data", data)
            if self.text is not None:
                raise ValueError("artifact_ref content part must not carry text")
        object.__setattr__(self, "metadata", _copy_mapping("content part", "metadata", self.metadata))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "message_id", _validate_non_empty_string("message", "message_id", self.message_id).strip())
        if not isinstance(self.role, str) or self.role not in VALID_MESSAGE_ROLES:
            raise ValueError(f"invalid message role {self.role}")
        raw_parts = _snapshot_collection("message", "parts", self.parts)
        if any(not isinstance(part, ContentPart) for part in raw_parts):
            raise ValueError("message parts must be ContentPart")
        parts = tuple(_copy_content_part(part) for part in raw_parts)
        object.__setattr__(self, "parts", parts)
        object.__setattr__(self, "parent_message_id", _validate_optional_non_empty_string("message", "parent_message_id", self.parent_message_id))
        object.__setattr__(
            self,
            "revision",
            _validate_revision("message", "revision", self.revision),
        )
        if (
            not isinstance(self.status, str)
            or self.status not in VALID_MESSAGE_STATUSES
        ):
            raise ValueError(f"invalid message status {self.status}")
        object.__setattr__(self, "created_at", _validate_optional_non_empty_string("message", "created_at", self.created_at))
        object.__setattr__(self, "metadata", _copy_mapping("message", "metadata", self.metadata))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "attachment_id", _validate_non_empty_string("file attachment", "attachment_id", self.attachment_id).strip())
        if not isinstance(self.asset, ArtifactRef):
            raise ValueError("file attachment asset must be ArtifactRef")
        if (
            not isinstance(self.scope, str)
            or self.scope not in VALID_ATTACHMENT_SCOPES
        ):
            raise ValueError(f"invalid file attachment scope {self.scope}")
        if (
            not isinstance(self.purpose, str)
            or self.purpose not in VALID_ATTACHMENT_PURPOSES
        ):
            raise ValueError(f"invalid file attachment purpose {self.purpose}")
        if (
            not isinstance(self.ingestion_status, str)
            or self.ingestion_status not in VALID_ATTACHMENT_INGESTION_STATUSES
        ):
            raise ValueError(f"invalid file attachment ingestion_status {self.ingestion_status}")
        object.__setattr__(self, "retention_policy", _validate_optional_non_empty_string("file attachment", "retention_policy", self.retention_policy))
        object.__setattr__(self, "message_id", _validate_optional_non_empty_string("file attachment", "message_id", self.message_id))
        if self.scope == "message" and self.message_id is None:
            raise ValueError("message-scoped file attachment requires message_id")
        object.__setattr__(self, "metadata", _copy_mapping("file attachment", "metadata", self.metadata))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "compaction_id", _validate_non_empty_string("compaction record", "compaction_id", self.compaction_id).strip())
        object.__setattr__(self, "source_message_ids", _validate_string_tuple("compaction record", "source_message_ids", self.source_message_ids))
        if not self.source_message_ids:
            raise ValueError("compaction record source_message_ids must not be empty")
        if len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError(
                "compaction record source_message_ids must be unique"
            )
        object.__setattr__(self, "output_message_id", _validate_non_empty_string("compaction record", "output_message_id", self.output_message_id).strip())
        object.__setattr__(self, "method", _validate_non_empty_string("compaction record", "method", self.method).strip())
        object.__setattr__(
            self,
            "token_before",
            _validate_revision(
                "compaction record",
                "token_before",
                self.token_before,
            ),
        )
        object.__setattr__(
            self,
            "token_after",
            _validate_revision(
                "compaction record",
                "token_after",
                self.token_after,
            ),
        )
        if self.token_after > self.token_before:
            raise ValueError("compaction record token_after must not exceed token_before")
        object.__setattr__(self, "model", _validate_optional_non_empty_string("compaction record", "model", self.model))
        object.__setattr__(self, "metadata", _copy_mapping("compaction record", "metadata", self.metadata))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "conversation_id", _validate_non_empty_string("conversation", "conversation_id", self.conversation_id).strip())
        raw_messages = _snapshot_collection(
            "conversation",
            "messages",
            self.messages,
        )
        if any(not isinstance(message, Message) for message in raw_messages):
            raise ValueError("conversation messages must be Message")
        messages = tuple(_copy_message(message) for message in raw_messages)
        message_ids = [message.message_id for message in messages]
        if len(set(message_ids)) != len(message_ids):
            raise ValueError("conversation message_id values must be unique")
        object.__setattr__(self, "messages", messages)
        raw_attachments = _snapshot_collection(
            "conversation",
            "attachments",
            self.attachments,
        )
        if any(
            not isinstance(attachment, FileAttachment)
            for attachment in raw_attachments
        ):
            raise ValueError("conversation attachments must be FileAttachment")
        attachments = tuple(
            _copy_attachment(attachment) for attachment in raw_attachments
        )
        attachment_ids = [attachment.attachment_id for attachment in attachments]
        if len(set(attachment_ids)) != len(attachment_ids):
            raise ValueError("conversation attachment_id values must be unique")
        if any(
            attachment.scope == "message" and attachment.message_id not in message_ids
            for attachment in attachments
        ):
            raise ValueError(
                "message-scoped conversation attachment must reference a conversation message"
            )
        object.__setattr__(self, "attachments", attachments)
        raw_compactions = _snapshot_collection(
            "conversation",
            "compactions",
            self.compactions,
        )
        if any(
            not isinstance(record, CompactionRecord)
            for record in raw_compactions
        ):
            raise ValueError("conversation compactions must be CompactionRecord")
        compactions = tuple(
            _copy_compaction(record) for record in raw_compactions
        )
        compaction_ids = [record.compaction_id for record in compactions]
        if len(set(compaction_ids)) != len(compaction_ids):
            raise ValueError("conversation compaction_id values must be unique")
        if any(
            record.output_message_id not in message_ids
            or any(
                source_message_id not in message_ids
                for source_message_id in record.source_message_ids
            )
            for record in compactions
        ):
            raise ValueError(
                "conversation compactions must reference conversation messages"
            )
        object.__setattr__(self, "compactions", compactions)
        object.__setattr__(
            self,
            "revision",
            _validate_revision("conversation", "revision", self.revision),
        )
        if not isinstance(self.archived, bool):
            raise ValueError("conversation archived must be a boolean")
        object.__setattr__(self, "branch_of", _validate_optional_non_empty_string("conversation", "branch_of", self.branch_of))
        object.__setattr__(
            self,
            "branched_from_message_id",
            _validate_optional_non_empty_string("conversation", "branched_from_message_id", self.branched_from_message_id),
        )
        if (self.branch_of is None) != (self.branched_from_message_id is None):
            raise ValueError(
                "conversation branch lineage fields must be provided together"
            )
        if (
            self.branched_from_message_id is not None
            and self.branched_from_message_id not in message_ids
            and not (self.archived and not messages)
        ):
            raise ValueError(
                "conversation branched_from_message_id must reference a conversation message"
            )
        object.__setattr__(self, "metadata", _copy_mapping("conversation", "metadata", self.metadata))


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    conversation: Conversation
    revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.conversation, Conversation):
            raise ValueError("conversation snapshot conversation must be Conversation")
        object.__setattr__(
            self,
            "revision",
            _validate_revision(
                "conversation snapshot",
                "revision",
                self.revision,
            ),
        )
        if self.revision != self.conversation.revision:
            raise ValueError("conversation snapshot revision must match conversation revision")
        object.__setattr__(
            self,
            "conversation",
            _copy_conversation(self.conversation),
        )


@dataclass(frozen=True, slots=True)
class BranchRequest:
    conversation_id: str
    from_message_id: str
    expected_revision: int
    new_conversation_id: str | None = None
    include_attachments: bool = True
    include_memory: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "conversation_id", _validate_non_empty_string("branch request", "conversation_id", self.conversation_id).strip())
        object.__setattr__(self, "from_message_id", _validate_non_empty_string("branch request", "from_message_id", self.from_message_id).strip())
        object.__setattr__(
            self,
            "expected_revision",
            _validate_revision(
                "branch request",
                "expected_revision",
                self.expected_revision,
            ),
        )
        object.__setattr__(self, "new_conversation_id", _validate_optional_non_empty_string("branch request", "new_conversation_id", self.new_conversation_id))
        if not isinstance(self.include_attachments, bool):
            raise ValueError("branch request include_attachments must be a boolean")
        if not isinstance(self.include_memory, bool):
            raise ValueError("branch request include_memory must be a boolean")


@dataclass(frozen=True, slots=True)
class RegenerateRequest:
    conversation_id: str
    assistant_message_id: str
    expected_revision: int
    new_conversation_id: str | None = None
    include_attachments: bool = True
    include_memory: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "conversation_id", _validate_non_empty_string("regenerate request", "conversation_id", self.conversation_id).strip())
        object.__setattr__(self, "assistant_message_id", _validate_non_empty_string("regenerate request", "assistant_message_id", self.assistant_message_id).strip())
        object.__setattr__(
            self,
            "expected_revision",
            _validate_revision(
                "regenerate request",
                "expected_revision",
                self.expected_revision,
            ),
        )
        object.__setattr__(
            self,
            "new_conversation_id",
            _validate_optional_non_empty_string("regenerate request", "new_conversation_id", self.new_conversation_id),
        )
        if not isinstance(self.include_attachments, bool):
            raise ValueError("regenerate request include_attachments must be a boolean")
        if not isinstance(self.include_memory, bool):
            raise ValueError("regenerate request include_memory must be a boolean")


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "turn_id", _validate_non_empty_string("turn", "turn_id", self.turn_id).strip())
        object.__setattr__(self, "conversation_id", _validate_non_empty_string("turn", "conversation_id", self.conversation_id).strip())
        object.__setattr__(
            self,
            "base_revision",
            _validate_revision("turn", "base_revision", self.base_revision),
        )
        if not isinstance(self.status, str) or self.status not in VALID_TURN_STATUSES:
            raise ValueError(f"invalid turn status {self.status}")
        raw_messages = _snapshot_collection("turn", "messages", self.messages)
        if any(not isinstance(message, Message) for message in raw_messages):
            raise ValueError("turn messages must be Message")
        messages = tuple(_copy_message(message) for message in raw_messages)
        message_ids = [message.message_id for message in messages]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("turn message_id values must be unique")
        object.__setattr__(self, "messages", messages)
        if self.committed_revision is not None:
            object.__setattr__(
                self,
                "committed_revision",
                _validate_revision(
                    "turn",
                    "committed_revision",
                    self.committed_revision,
                ),
            )
        object.__setattr__(self, "committed_message_ids", _validate_string_tuple("turn", "committed_message_ids", self.committed_message_ids))
        if len(self.committed_message_ids) != len(set(self.committed_message_ids)):
            raise ValueError("turn committed_message_ids must be unique")
        if self.status == "completed":
            if self.committed_revision is None:
                raise ValueError("completed turn requires committed_revision")
            if self.committed_revision != self.base_revision + 1:
                raise ValueError(
                    "completed turn committed_revision must equal base_revision plus one"
                )
            if not self.committed_message_ids:
                raise ValueError("completed turn requires committed_message_ids")
            if self.committed_message_ids != tuple(message_ids):
                raise ValueError(
                    "completed turn committed_message_ids must match turn messages"
                )
            if any(message.status != "committed" for message in messages):
                raise ValueError("completed turn messages must be committed")
        elif self.committed_revision is not None or self.committed_message_ids:
            raise ValueError("non-completed turn must not carry committed revision data")
        if self.status == "created" and messages:
            raise ValueError("created turn must not carry messages")
        if self.status in {"cancelled", "policy_stopped"} and any(
            message.status != "retracted" for message in messages
        ):
            raise ValueError(
                f"{self.status} turn messages must be retracted"
            )
        if self.status in {
            "context_building",
            "model_running",
            "tool_waiting",
            "approval_waiting",
            "finalizing",
            "failed",
        } and any(message.status != "draft" for message in messages):
            raise ValueError(f"{self.status} turn messages must be drafts")
        object.__setattr__(self, "metadata", _copy_mapping("turn", "metadata", self.metadata))


def _copy_conversation(conversation: Conversation) -> Conversation:
    return Conversation(
        conversation_id=conversation.conversation_id,
        messages=tuple(_copy_message(message) for message in conversation.messages),
        attachments=tuple(_copy_attachment(attachment) for attachment in conversation.attachments),
        compactions=tuple(_copy_compaction(record) for record in conversation.compactions),
        revision=conversation.revision,
        archived=conversation.archived,
        branch_of=conversation.branch_of,
        branched_from_message_id=conversation.branched_from_message_id,
        metadata=dict(conversation.metadata),
    )


def _copy_content_part(part: ContentPart) -> ContentPart:
    return ContentPart(
        kind=part.kind,
        text=part.text,
        data=None if part.data is None else dict(part.data),
        metadata=dict(part.metadata),
    )


def _copy_message(message: Message) -> Message:
    return Message(
        message_id=message.message_id,
        role=message.role,
        parts=tuple(_copy_content_part(part) for part in message.parts),
        parent_message_id=message.parent_message_id,
        revision=message.revision,
        status=message.status,
        created_at=message.created_at,
        metadata=dict(message.metadata),
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
        messages=tuple(_copy_message(message) for message in turn.messages),
        committed_revision=turn.committed_revision,
        committed_message_ids=tuple(turn.committed_message_ids),
        metadata=dict(turn.metadata),
    )


@dataclass(slots=True)
class InMemoryConversationStore:
    _conversations: dict[str, Conversation] = field(default_factory=dict)
    _turns: dict[str, Turn] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self._conversations, Mapping):
            raise ValueError("conversation store conversations must be a mapping")
        conversations: dict[str, Conversation] = {}
        for conversation_id, conversation in self._conversations.items():
            _validate_non_empty_string(
                "conversation store", "conversation_id", conversation_id
            )
            if not isinstance(conversation, Conversation):
                raise ValueError(
                    "conversation store conversations must be Conversation records"
                )
            if conversation.conversation_id != conversation_id:
                raise ValueError(
                    "conversation store conversation key must match conversation_id"
                )
            conversations[conversation_id] = _copy_conversation(conversation)
        if not isinstance(self._turns, Mapping):
            raise ValueError("conversation store turns must be a mapping")
        turns: dict[str, Turn] = {}
        for turn_id, turn in self._turns.items():
            _validate_non_empty_string("conversation store", "turn_id", turn_id)
            if not isinstance(turn, Turn):
                raise ValueError("conversation store turns must be Turn records")
            if turn.turn_id != turn_id:
                raise ValueError("conversation store turn key must match turn_id")
            conversation = conversations.get(turn.conversation_id)
            if conversation is None:
                raise ValueError(
                    "conversation store turn must reference a stored conversation"
                )
            if turn.base_revision > conversation.revision:
                raise ValueError(
                    "conversation store turn base revision must not exceed "
                    "conversation revision"
                )
            if (
                turn.status == "completed"
                and turn.committed_revision is not None
                and turn.committed_revision > conversation.revision
            ):
                raise ValueError(
                    "conversation store completed turn revision must not exceed conversation revision"
                )
            if turn.status == "completed":
                stored_messages = {
                    message.message_id: message for message in conversation.messages
                }
                if any(
                    message_id not in stored_messages
                    for message_id in turn.committed_message_ids
                ):
                    raise ValueError(
                        "conversation store completed turn messages must exist in conversation"
                    )
                if any(
                    stored_messages[message.message_id] != message
                    for message in turn.messages
                ):
                    raise ValueError(
                        "conversation store completed turn messages must match "
                        "stored conversation messages"
                    )
            turns[turn_id] = _copy_turn(turn)
        self._conversations = conversations
        self._turns = turns

    @_with_conversation_store_lock
    def create(self, conversation: Conversation) -> None:
        if not isinstance(conversation, Conversation):
            raise ValueError(
                "conversation store conversation must be a Conversation"
            )
        if conversation.conversation_id in self._conversations:
            raise ConversationConflictError(f"conversation {conversation.conversation_id!r} already exists")
        self._conversations[conversation.conversation_id] = _copy_conversation(conversation)

    @_with_conversation_store_lock
    def get(self, conversation_id: str) -> ConversationSnapshot:
        conversation_id = _validate_non_empty_string(
            "conversation store",
            "conversation_id",
            conversation_id,
        )
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        return ConversationSnapshot(conversation=_copy_conversation(conversation), revision=conversation.revision)

    @_with_conversation_store_lock
    def begin_turn(self, conversation_id: str, expected_revision: int, turn_id: str) -> Turn:
        conversation_id = _validate_non_empty_string(
            "conversation store",
            "conversation_id",
            conversation_id,
        )
        turn_id = _validate_non_empty_string(
            "conversation store",
            "turn_id",
            turn_id,
        )
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        if conversation.archived:
            raise ConversationArchivedError(f"conversation {conversation_id!r} is archived")
        if turn_id in self._turns:
            raise TurnConflictError(f"turn {turn_id!r} already exists")
        _require_expected_revision(conversation, expected_revision)
        turn = Turn(
            turn_id=turn_id,
            conversation_id=conversation_id,
            base_revision=expected_revision,
        )
        self._turns[turn_id] = turn
        return _copy_turn(turn)

    @_with_conversation_store_lock
    def get_turn(self, turn_id: str) -> Turn:
        turn_id = _validate_non_empty_string(
            "conversation store",
            "turn_id",
            turn_id,
        )
        turn = self._turns.get(turn_id)
        if turn is None:
            raise TurnNotFoundError(f"turn {turn_id!r} does not exist")
        return _copy_turn(turn)

    @_with_conversation_store_lock
    def append_turn_message(self, turn_id: str, message: Message) -> Turn:
        turn_id = _validate_non_empty_string(
            "conversation store",
            "turn_id",
            turn_id,
        )
        if not isinstance(message, Message):
            raise ValueError("conversation store message must be a Message")
        turn = self._turns.get(turn_id)
        if turn is None:
            raise TurnNotFoundError(f"turn {turn_id!r} does not exist")
        if turn.status in {"completed", "failed", "cancelled", "policy_stopped"}:
            raise TurnConflictError(f"turn {turn_id!r} is already terminal")
        conversation = self._conversations.get(turn.conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(
                f"conversation {turn.conversation_id!r} does not exist"
            )
        if conversation.archived:
            raise ConversationArchivedError(
                f"conversation {turn.conversation_id!r} is archived"
            )
        draft_message = _copy_message(replace(message, status="draft"))
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

    @_with_conversation_store_lock
    def commit_turn(self, turn_id: str) -> Turn:
        turn_id = _validate_non_empty_string(
            "conversation store",
            "turn_id",
            turn_id,
        )
        turn = self._turns.get(turn_id)
        if turn is None:
            raise TurnNotFoundError(f"turn {turn_id!r} does not exist")
        if turn.status in {"completed", "failed", "cancelled", "policy_stopped"}:
            raise TurnConflictError(f"turn {turn_id!r} is already terminal")
        if not turn.messages:
            raise TurnConflictError(f"turn {turn_id!r} has no messages to commit")
        committed_messages = tuple(_copy_message(replace(message, status="committed")) for message in turn.messages)
        try:
            new_revision = self.append_messages(turn.conversation_id, turn.base_revision, list(committed_messages))
        except (
            ConversationArchivedError,
            ConversationConflictError,
            OverflowError,
        ):
            failed = Turn(
                turn_id=turn.turn_id,
                conversation_id=turn.conversation_id,
                base_revision=turn.base_revision,
                status="failed",
                messages=tuple(_copy_message(message) for message in turn.messages),
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

    @_with_conversation_store_lock
    def abort_turn(self, turn_id: str) -> Turn:
        turn_id = _validate_non_empty_string(
            "conversation store",
            "turn_id",
            turn_id,
        )
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
            messages=tuple(_copy_message(replace(message, status="retracted")) for message in turn.messages),
            committed_revision=None,
            committed_message_ids=(),
            metadata=dict(turn.metadata),
        )
        self._turns[turn_id] = cancelled
        return _copy_turn(cancelled)

    @_with_conversation_store_lock
    def policy_stop_turn(self, turn_id: str) -> Turn:
        turn_id = _validate_non_empty_string(
            "conversation store",
            "turn_id",
            turn_id,
        )
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
            messages=tuple(_copy_message(replace(message, status="retracted")) for message in turn.messages),
            committed_revision=None,
            committed_message_ids=(),
            metadata=dict(turn.metadata),
        )
        self._turns[turn_id] = stopped
        return _copy_turn(stopped)

    @_with_conversation_store_lock
    def append_messages(self, conversation_id: str, expected_revision: int, messages: list[Message]) -> int:
        conversation_id = _validate_non_empty_string(
            "conversation store",
            "conversation_id",
            conversation_id,
        )
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        if conversation.archived:
            raise ConversationArchivedError(f"conversation {conversation_id!r} is archived")
        _require_expected_revision(conversation, expected_revision)
        if not isinstance(messages, list):
            raise ValueError("conversation store messages must be a list")
        if not messages:
            raise ValueError("conversation store messages must not be empty")
        if any(not isinstance(message, Message) for message in messages):
            raise ValueError(
                "conversation store messages must contain Message records"
            )
        new_revision = _next_revision(conversation.revision)
        copied_messages = tuple(
            _copy_message(message)
            for message in messages
        )
        try:
            updated = Conversation(
                conversation_id=conversation.conversation_id,
                messages=(*conversation.messages, *copied_messages),
                attachments=conversation.attachments,
                compactions=conversation.compactions,
                revision=new_revision,
                archived=conversation.archived,
                branch_of=conversation.branch_of,
                branched_from_message_id=conversation.branched_from_message_id,
                metadata=dict(conversation.metadata),
            )
        except ValueError as error:
            raise ConversationConflictError(str(error)) from error
        self._conversations[conversation_id] = updated
        return new_revision

    @_with_conversation_store_lock
    def branch(self, request: BranchRequest) -> Conversation:
        if not isinstance(request, BranchRequest):
            raise ValueError(
                "conversation store branch request must be a BranchRequest"
            )
        conversation = self._conversations.get(request.conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {request.conversation_id!r} does not exist")
        if conversation.archived:
            raise ConversationArchivedError(f"conversation {request.conversation_id!r} is archived")
        _require_expected_revision(
            conversation,
            request.expected_revision,
        )
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
        branch_messages = tuple(_copy_message(message) for message in conversation.messages[: source_index + 1])
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
            compactions=tuple(
                _copy_compaction(record)
                for record in conversation.compactions
                if request.include_memory
                and record.output_message_id in branch_message_ids
                and all(
                    source_message_id in branch_message_ids
                    for source_message_id in record.source_message_ids
                )
            ),
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

    @_with_conversation_store_lock
    def regenerate(self, request: RegenerateRequest) -> Conversation:
        if not isinstance(request, RegenerateRequest):
            raise ValueError(
                "conversation store regenerate request must be a RegenerateRequest"
            )
        conversation = self._conversations.get(request.conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {request.conversation_id!r} does not exist")
        if conversation.archived:
            raise ConversationArchivedError(f"conversation {request.conversation_id!r} is archived")
        _require_expected_revision(
            conversation,
            request.expected_revision,
        )
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

        branch_messages = tuple(_copy_message(message) for message in conversation.messages[: parent_index + 1])
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
            compactions=tuple(
                _copy_compaction(record)
                for record in conversation.compactions
                if request.include_memory
                and record.output_message_id in branch_message_ids
                and all(
                    source_message_id in branch_message_ids
                    for source_message_id in record.source_message_ids
                )
            ),
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
            _copy_message(replace(message, status="superseded") if index == assistant_index else message)
            for index, message in enumerate(conversation.messages)
        )
        self._conversations[request.conversation_id] = Conversation(
            conversation_id=conversation.conversation_id,
            messages=superseded_messages,
            attachments=conversation.attachments,
            compactions=conversation.compactions,
            revision=_next_revision(conversation.revision),
            archived=conversation.archived,
            branch_of=conversation.branch_of,
            branched_from_message_id=conversation.branched_from_message_id,
            metadata=dict(conversation.metadata),
        )
        self._conversations[branch_id] = branch
        return _copy_conversation(branch)

    @_with_conversation_store_lock
    def add_attachment(
        self,
        conversation_id: str,
        attachment: FileAttachment,
        *,
        expected_revision: int,
    ) -> int:
        conversation_id = _validate_non_empty_string(
            "conversation store",
            "conversation_id",
            conversation_id,
        )
        if not isinstance(attachment, FileAttachment):
            raise ValueError(
                "conversation store attachment must be a FileAttachment"
            )
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        if conversation.archived:
            raise ConversationArchivedError(f"conversation {conversation_id!r} is archived")
        _require_expected_revision(conversation, expected_revision)
        new_revision = _next_revision(conversation.revision)
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

    @_with_conversation_store_lock
    def resolve_attachments(
        self,
        conversation_id: str,
        message_ids: list[str],
        *,
        include_conversation_scope: bool,
    ) -> tuple[FileAttachment, ...]:
        conversation_id = _validate_non_empty_string(
            "conversation store",
            "conversation_id",
            conversation_id,
        )
        if not isinstance(message_ids, list):
            raise ValueError(
                "conversation store message_ids must be a list"
            )
        for message_id in message_ids:
            _validate_non_empty_string(
                "conversation store",
                "message_id",
                message_id,
            )
        if not isinstance(include_conversation_scope, bool):
            raise ValueError(
                "conversation store include_conversation_scope must be a boolean"
            )
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

    @_with_conversation_store_lock
    def record_compaction(
        self,
        conversation_id: str,
        record: CompactionRecord,
        *,
        expected_revision: int,
    ) -> int:
        conversation_id = _validate_non_empty_string(
            "conversation store",
            "conversation_id",
            conversation_id,
        )
        if not isinstance(record, CompactionRecord):
            raise ValueError(
                "conversation store compaction must be a CompactionRecord"
            )
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        if conversation.archived:
            raise ConversationArchivedError(f"conversation {conversation_id!r} is archived")
        _require_expected_revision(conversation, expected_revision)
        message_ids = {message.message_id for message in conversation.messages}
        for source_message_id in record.source_message_ids:
            if source_message_id not in message_ids:
                raise MessageNotFoundError(f"message {source_message_id!r} does not exist")
        if record.output_message_id not in message_ids:
            raise MessageNotFoundError(f"message {record.output_message_id!r} does not exist")
        new_revision = _next_revision(conversation.revision)
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

    @_with_conversation_store_lock
    def archive(
        self,
        conversation_id: str,
        *,
        expected_revision: int,
    ) -> int:
        conversation_id = _validate_non_empty_string(
            "conversation store",
            "conversation_id",
            conversation_id,
        )
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        _require_expected_revision(conversation, expected_revision)
        new_revision = _next_revision(conversation.revision)
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

    @_with_conversation_store_lock
    def delete(
        self,
        conversation_id: str,
        policy: DeletePolicy = "tombstone",
        *,
        expected_revision: int,
    ) -> int | None:
        conversation_id = _validate_non_empty_string(
            "conversation store",
            "conversation_id",
            conversation_id,
        )
        if not isinstance(policy, str) or policy not in VALID_DELETE_POLICIES:
            raise ValueError("policy must be tombstone or hard")
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
        _require_expected_revision(conversation, expected_revision)
        if policy == "hard":
            del self._conversations[conversation_id]
            self._turns = {
                turn_id: turn
                for turn_id, turn in self._turns.items()
                if turn.conversation_id != conversation_id
            }
            return None
        new_revision = _next_revision(conversation.revision)
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
        self._turns = {
            turn_id: turn
            for turn_id, turn in self._turns.items()
            if turn.conversation_id != conversation_id
        }
        return new_revision

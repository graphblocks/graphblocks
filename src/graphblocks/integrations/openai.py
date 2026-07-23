from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import math
from types import MappingProxyType

from graphblocks import (
    ContentPart,
    GenerationChunk,
    Message,
    ToolCallDraft,
    ToolDefinition,
    UsageAmount,
    UsageRecord,
    canonical_dumps,
    canonical_loads,
)


class OpenAICompatibleAdapterError(ValueError):
    """Raised when an OpenAI-compatible adapter contract is invalid."""


def _strip_required_string(field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise OpenAICompatibleAdapterError(f"{field_name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise OpenAICompatibleAdapterError(f"{field_name} must not be empty")
    return stripped


def _strip_optional_string(field_name: str, value: object) -> str | None:
    if value is None:
        return None
    return _strip_required_string(field_name, value)


def _non_negative_integer(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OpenAICompatibleAdapterError(f"{field_name} must be a non-negative integer")
    if value < 0:
        raise OpenAICompatibleAdapterError(f"{field_name} must be a non-negative integer")
    return value


def _freeze_json_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _json_projection(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_projection(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_projection(item) for item in value]
    return value


def _strict_json_mapping(
    field_name: str,
    value: object,
) -> MappingProxyType[str, object]:
    if not isinstance(value, Mapping):
        raise OpenAICompatibleAdapterError(f"{field_name} must be a mapping")
    try:
        normalized = canonical_loads(canonical_dumps(value))
    except (TypeError, ValueError) as error:
        raise OpenAICompatibleAdapterError(
            f"{field_name} must be a strict JSON object"
        ) from error
    if not isinstance(normalized, dict):
        raise OpenAICompatibleAdapterError(f"{field_name} must be a strict JSON object")
    return _freeze_json_value(normalized)  # type: ignore[return-value]


def _validated_inline_json_schema(schema_ref: str, schema: Mapping[str, object]) -> dict[str, object]:
    try:
        normalized_schema = canonical_loads(canonical_dumps(schema))
    except (TypeError, ValueError) as error:
        raise OpenAICompatibleAdapterError(
            f"tool_schemas entry {schema_ref!r} must be a strict JSON object"
        ) from error
    if not isinstance(normalized_schema, dict):
        raise OpenAICompatibleAdapterError(
            f"tool_schemas entry {schema_ref!r} must be a strict JSON object"
        )
    pending: list[object] = [normalized_schema]
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise OpenAICompatibleAdapterError(
                        f"tool_schemas entry {schema_ref!r} contains non-local {key}"
                    )
                pending.append(child)
        elif isinstance(value, list):
            pending.extend(value)
    return normalized_schema


@dataclass(frozen=True, slots=True)
class OpenAIChatCompletionRequest:
    body: Mapping[str, object]
    metadata: Mapping[str, object] = field(default_factory=dict)
    endpoint: str = "/chat/completions"

    def __post_init__(self) -> None:
        body = _strict_json_mapping("body", self.body)
        if not body:
            raise OpenAICompatibleAdapterError("body must not be empty")
        object.__setattr__(self, "endpoint", _strip_required_string("endpoint", self.endpoint))
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "metadata", _strict_json_mapping("metadata", self.metadata))

    def request_contract(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "body": _json_projection(self.body),
            "metadata": _json_projection(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class OpenAIChatResponse:
    response_id: str
    model: str
    parts: tuple[ContentPart, ...] = field(default_factory=tuple)
    tool_calls: tuple[Mapping[str, object], ...] = field(default_factory=tuple)
    finish_reason: str | None = None
    usage: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "response_id", _strip_required_string("response_id", self.response_id))
        object.__setattr__(self, "model", _strip_required_string("model", self.model))
        object.__setattr__(self, "parts", tuple(self.parts))
        if (
            not isinstance(self.tool_calls, Sequence)
            or isinstance(self.tool_calls, (str, bytes))
        ):
            raise OpenAICompatibleAdapterError("tool_calls must be a sequence")
        normalized_tool_calls: list[Mapping[str, object]] = []
        seen_tool_call_ids: set[str] = set()
        for call in self.tool_calls:
            normalized_call = _strict_json_mapping("tool_call", call)
            call_id = _strip_required_string("tool_call id", normalized_call.get("id"))
            name = _strip_required_string("tool_call name", normalized_call.get("name"))
            arguments = normalized_call.get("arguments", "")
            if not isinstance(arguments, str):
                raise OpenAICompatibleAdapterError(
                    "tool_call arguments must be a string"
                )
            tool_type = _strip_required_string(
                "tool_call type",
                normalized_call.get("type", "function"),
            )
            if call_id in seen_tool_call_ids:
                raise OpenAICompatibleAdapterError(
                    f"duplicate provider response tool_call id {call_id!r}"
                )
            seen_tool_call_ids.add(call_id)
            normalized_tool_calls.append(
                _strict_json_mapping(
                    "tool_call",
                    {
                        **_json_projection(normalized_call),  # type: ignore[arg-type]
                        "id": call_id,
                        "name": name,
                        "arguments": arguments,
                        "type": tool_type,
                    },
                )
            )
        object.__setattr__(self, "tool_calls", tuple(normalized_tool_calls))
        object.__setattr__(self, "usage", _strict_json_mapping("usage", self.usage))

    def response_contract(self) -> dict[str, object]:
        parts: list[dict[str, object]] = []
        for part in self.parts:
            value: dict[str, object] = {
                "kind": part.kind,
                "metadata": deepcopy(dict(part.metadata)),
            }
            if part.text is not None:
                value["text"] = part.text
            if part.data is not None:
                value["data"] = deepcopy(dict(part.data))
            parts.append(value)
        return {
            "response_id": self.response_id,
            "model": self.model,
            "finish_reason": self.finish_reason,
            "parts": parts,
            "tool_calls": [_json_projection(call) for call in self.tool_calls],
            "usage": dict(sorted(_json_projection(self.usage).items())),  # type: ignore[union-attr]
        }


@dataclass(frozen=True, slots=True)
class OpenAIChatDelta:
    response_id: str
    sequence: int
    choice_index: int | None
    content_delta: str | None = None
    tool_call_deltas: tuple[Mapping[str, object], ...] = field(default_factory=tuple)
    finish_reason: str | None = None
    usage_delta: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "response_id", _strip_required_string("response_id", self.response_id))
        object.__setattr__(self, "sequence", _non_negative_integer("sequence", self.sequence))
        if self.sequence == 0:
            raise OpenAICompatibleAdapterError("sequence must be positive")
        if self.choice_index is not None:
            object.__setattr__(self, "choice_index", _non_negative_integer("choice_index", self.choice_index))
        if self.content_delta is not None and not isinstance(self.content_delta, str):
            raise OpenAICompatibleAdapterError("content_delta must be a string")
        if self.finish_reason is not None and not isinstance(self.finish_reason, str):
            raise OpenAICompatibleAdapterError("finish_reason must be a string")
        normalized_tool_call_deltas: list[dict[str, object]] = []
        for delta in self.tool_call_deltas:
            if not isinstance(delta, Mapping):
                raise OpenAICompatibleAdapterError("tool_call_delta must be a mapping")
            normalized_delta: dict[str, object] = {
                "index": _non_negative_integer("tool_call_delta index", delta.get("index", 0))
            }
            if "id" in delta:
                normalized_delta["id"] = _strip_optional_string("tool_call_delta id", delta.get("id"))
            if "type" in delta:
                normalized_delta["type"] = _strip_optional_string("tool_call_delta type", delta.get("type"))
            if "name" in delta:
                normalized_delta["name"] = _strip_optional_string("tool_call_delta name", delta.get("name"))
            if "arguments_delta" in delta:
                arguments_delta = delta.get("arguments_delta")
                if arguments_delta is not None and not isinstance(arguments_delta, str):
                    raise OpenAICompatibleAdapterError("tool_call_delta arguments_delta must be a string")
                normalized_delta["arguments_delta"] = arguments_delta
            normalized_tool_call_deltas.append(normalized_delta)
        object.__setattr__(
            self,
            "tool_call_deltas",
            tuple(
                _strict_json_mapping("tool_call_delta", delta)
                for delta in normalized_tool_call_deltas
            ),
        )
        object.__setattr__(
            self,
            "usage_delta",
            _strict_json_mapping("usage_delta", self.usage_delta),
        )

    def delta_contract(self) -> dict[str, object]:
        return {
            "response_id": self.response_id,
            "sequence": self.sequence,
            "choice_index": self.choice_index,
            "content_delta": self.content_delta,
            "tool_call_deltas": [
                _json_projection(delta) for delta in self.tool_call_deltas
            ],
            "finish_reason": self.finish_reason,
            "usage_delta": dict(
                sorted(_json_projection(self.usage_delta).items())  # type: ignore[union-attr]
            ),
        }


@dataclass(slots=True)
class OpenAIStreamingToolCallDraftAssembler:
    response_id: str | None = None
    _drafts_by_index: dict[int, ToolCallDraft] = field(default_factory=dict)
    _index_order: list[int] = field(default_factory=list)
    _applied_deltas: tuple[OpenAIChatDelta, ...] = field(
        default_factory=tuple,
        repr=False,
    )
    _completed: bool = field(default=False, repr=False)
    _applied_delta_results: dict[
        int,
        tuple[str, tuple[ToolCallDraft, ...]],
    ] = field(default_factory=dict, init=False, repr=False)
    _last_sequence: int | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.response_id is not None:
            self.response_id = _strip_required_string(
                "response_id",
                self.response_id,
            )
        if not isinstance(self._drafts_by_index, Mapping):
            raise OpenAICompatibleAdapterError(
                "drafts_by_index must be a mapping"
            )
        drafts_by_index: dict[int, ToolCallDraft] = {}
        for index, draft in self._drafts_by_index.items():
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index < 0
            ):
                raise OpenAICompatibleAdapterError(
                    "drafts_by_index keys must be non-negative integers"
                )
            if not isinstance(draft, ToolCallDraft):
                raise OpenAICompatibleAdapterError(
                    "drafts_by_index values must be ToolCallDraft records"
                )
            if self.response_id is None or draft.response_id != self.response_id:
                raise OpenAICompatibleAdapterError(
                    "restored draft response_id must match assembler response_id"
                )
            drafts_by_index[index] = draft
        if isinstance(self._index_order, (str, bytes, Mapping)):
            raise OpenAICompatibleAdapterError(
                "index_order must be a sequence of integers"
            )
        try:
            index_order = list(self._index_order)
        except TypeError as error:
            raise OpenAICompatibleAdapterError(
                "index_order must be a sequence of integers"
            ) from error
        if any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            for index in index_order
        ):
            raise OpenAICompatibleAdapterError(
                "index_order must be a sequence of non-negative integers"
            )
        if (
            len(set(index_order)) != len(index_order)
            or set(index_order) != set(drafts_by_index)
        ):
            raise OpenAICompatibleAdapterError(
                "index_order must contain every draft index exactly once"
            )
        tool_call_ids = [
            drafts_by_index[index].tool_call_id
            for index in index_order
        ]
        if len(set(tool_call_ids)) != len(tool_call_ids):
            raise OpenAICompatibleAdapterError(
                "restored drafts must have unique tool call ids"
            )
        if not isinstance(self._completed, bool):
            raise OpenAICompatibleAdapterError(
                "restored completion state must be a boolean"
            )
        if (
            not isinstance(self._applied_deltas, Sequence)
            or isinstance(self._applied_deltas, (str, bytes, Mapping))
        ):
            raise OpenAICompatibleAdapterError(
                "applied_deltas must be a sequence of OpenAIChatDelta records"
            )
        applied_deltas = tuple(self._applied_deltas)
        if any(not isinstance(delta, OpenAIChatDelta) for delta in applied_deltas):
            raise OpenAICompatibleAdapterError(
                "applied_deltas must be a sequence of OpenAIChatDelta records"
            )
        if not applied_deltas and (drafts_by_index or index_order or self._completed):
            raise OpenAICompatibleAdapterError(
                "restored drafts require complete applied delta history"
            )
        self._drafts_by_index = drafts_by_index
        self._index_order = index_order
        self._applied_deltas = applied_deltas
        if applied_deltas:
            replay = type(self)()
            previous_sequence: int | None = None
            for delta in applied_deltas:
                if (
                    previous_sequence is not None
                    and delta.sequence <= previous_sequence
                ):
                    raise OpenAICompatibleAdapterError(
                        "restored delta history sequences must strictly increase"
                    )
                replay.apply_delta(delta)
                previous_sequence = delta.sequence
            if self._completed:
                replay.complete_all()
            if (
                self.response_id != replay.response_id
                or self._index_order != replay._index_order
                or self._drafts_by_index != replay._drafts_by_index
            ):
                raise OpenAICompatibleAdapterError(
                    "restored assembler snapshot does not match applied delta history"
                )
            self._applied_delta_results = dict(replay._applied_delta_results)
            self._last_sequence = replay._last_sequence

    @classmethod
    def restore(
        cls,
        applied_deltas: Sequence[OpenAIChatDelta],
        *,
        completed: bool = False,
    ) -> OpenAIStreamingToolCallDraftAssembler:
        if not isinstance(completed, bool):
            raise OpenAICompatibleAdapterError("completed must be a boolean")
        if (
            not isinstance(applied_deltas, Sequence)
            or isinstance(applied_deltas, (str, bytes, Mapping))
        ):
            raise OpenAICompatibleAdapterError(
                "applied_deltas must be a sequence of OpenAIChatDelta records"
            )
        applied_deltas = tuple(applied_deltas)
        previous_sequence: int | None = None
        for delta in applied_deltas:
            if not isinstance(delta, OpenAIChatDelta):
                raise OpenAICompatibleAdapterError(
                    "applied_deltas must be a sequence of OpenAIChatDelta records"
                )
            if (
                previous_sequence is not None
                and delta.sequence <= previous_sequence
            ):
                raise OpenAICompatibleAdapterError(
                    "restored delta history sequences must strictly increase"
                )
            previous_sequence = delta.sequence
        replay = cls()
        for delta in applied_deltas:
            replay.apply_delta(delta)
        if not replay._applied_deltas:
            if completed:
                raise OpenAICompatibleAdapterError(
                    "completed restore requires applied delta history"
                )
            return replay
        if completed:
            replay.complete_all()
        return cls(
            response_id=replay.response_id,
            _drafts_by_index=dict(replay._drafts_by_index),
            _index_order=list(replay._index_order),
            _applied_deltas=replay._applied_deltas,
            _completed=completed,
        )

    def apply_delta(self, delta: OpenAIChatDelta) -> tuple[ToolCallDraft, ...]:
        if not isinstance(delta, OpenAIChatDelta):
            raise OpenAICompatibleAdapterError("delta must be an OpenAIChatDelta")
        delta_signature = canonical_dumps(delta.delta_contract())
        prior_result = self._applied_delta_results.get(delta.sequence)
        if prior_result is not None:
            prior_signature, drafts = prior_result
            if prior_signature == delta_signature:
                return drafts
            raise OpenAICompatibleAdapterError(
                f"streaming delta sequence {delta.sequence} was reused "
                "with different content"
            )
        if self._last_sequence is not None and delta.sequence < self._last_sequence:
            raise OpenAICompatibleAdapterError(
                "streaming delta sequence must increase"
            )
        response_id = self.response_id
        if response_id is None:
            response_id = delta.response_id
        elif response_id != delta.response_id:
            raise OpenAICompatibleAdapterError(
                f"streaming tool call delta changed response id from {response_id} to {delta.response_id}"
            )

        drafts_by_index = dict(self._drafts_by_index)
        index_order = list(self._index_order)
        updated: list[ToolCallDraft] = []
        for tool_call_delta in delta.tool_call_deltas:
            if not isinstance(tool_call_delta, Mapping):
                raise OpenAICompatibleAdapterError("streaming tool call delta must be a mapping")
            index = tool_call_delta.get("index", 0)
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise OpenAICompatibleAdapterError("streaming tool call delta index must be a non-negative integer")

            draft = drafts_by_index.get(index)
            call_id = tool_call_delta.get("id")
            name = tool_call_delta.get("name")
            if draft is None:
                if not isinstance(call_id, str) or not call_id.strip():
                    raise OpenAICompatibleAdapterError("streaming tool call delta requires an id for new index")
                if not isinstance(name, str) or not name.strip():
                    raise OpenAICompatibleAdapterError("streaming tool call delta requires a name for new index")
                if any(
                    existing.tool_call_id == call_id
                    for existing in drafts_by_index.values()
                ):
                    raise OpenAICompatibleAdapterError(
                        "streaming tool call id must identify one index"
                    )
                draft = ToolCallDraft.proposed(delta.response_id, call_id, name)
                drafts_by_index[index] = draft
                index_order.append(index)
            else:
                if call_id is not None and call_id != draft.tool_call_id:
                    raise OpenAICompatibleAdapterError(
                        f"streaming tool call delta for index {index} changed id from "
                        f"{draft.tool_call_id} to {call_id}"
                    )
                if name is not None and name != draft.tool_name:
                    raise OpenAICompatibleAdapterError(
                        f"streaming tool call delta for index {index} changed name from "
                        f"{draft.tool_name} to {name}"
                    )

            arguments_delta = tool_call_delta.get("arguments_delta")
            if arguments_delta is not None:
                if not isinstance(arguments_delta, str):
                    raise OpenAICompatibleAdapterError("streaming tool call arguments_delta must be a string")
                draft = draft.append_argument_fragment(arguments_delta)
                drafts_by_index[index] = draft
            updated.append(draft)
        self.response_id = response_id
        self._drafts_by_index = drafts_by_index
        self._index_order = index_order
        result = tuple(updated)
        self._applied_delta_results[delta.sequence] = (
            delta_signature,
            result,
        )
        self._last_sequence = delta.sequence
        self._applied_deltas = (*self._applied_deltas, delta)
        return result

    def drafts(self) -> tuple[ToolCallDraft, ...]:
        return tuple(self._drafts_by_index[index] for index in self._index_order)

    def applied_deltas(self) -> tuple[OpenAIChatDelta, ...]:
        return self._applied_deltas

    def complete_all(self) -> tuple[ToolCallDraft, ...]:
        drafts_by_index = dict(self._drafts_by_index)
        completed: list[ToolCallDraft] = []
        for index in self._index_order:
            draft = drafts_by_index[index]
            if draft.status != "arguments_complete":
                draft = draft.complete_arguments()
                drafts_by_index[index] = draft
            completed.append(draft)
        self._drafts_by_index = drafts_by_index
        self._completed = True
        return tuple(completed)


def _openai_usage_amounts(usage: Mapping[str, object], *, model: str) -> tuple[UsageAmount, ...]:
    dimensions = {"model": model, "provider": "openai-compatible"}
    usage_keys = (
        ("prompt_tokens", "model_input_tokens"),
        ("completion_tokens", "model_output_tokens"),
        ("total_tokens", "model_total_tokens"),
    )
    amounts: list[UsageAmount] = []
    for provider_key, amount_kind in usage_keys:
        value = usage.get(provider_key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise OpenAICompatibleAdapterError(f"provider usage {provider_key} must be an integer")
        amounts.append(UsageAmount(amount_kind, value, "tokens", dimensions=dimensions))
    if not amounts:
        raise OpenAICompatibleAdapterError("provider usage contains no recognized token counts")
    return tuple(amounts)


def openai_chat_completion_request(
    *,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[ToolDefinition] = (),
    tool_schemas: Mapping[str, Mapping[str, object]] | None = None,
    tool_choice: str | Mapping[str, object] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    stream: bool = False,
    metadata: Mapping[str, object] | None = None,
    extra_body: Mapping[str, object] | None = None,
) -> OpenAIChatCompletionRequest:
    """Build a request for the adapter's stable single-choice (``n=1``) contract."""
    if not isinstance(model, str) or not model.strip():
        raise OpenAICompatibleAdapterError("model must not be empty")
    if (
        not isinstance(messages, Sequence)
        or isinstance(messages, (str, bytes))
        or not messages
    ):
        raise OpenAICompatibleAdapterError("messages must contain at least one message")
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        raise OpenAICompatibleAdapterError("tools must be a sequence")
    if not isinstance(stream, bool):
        raise OpenAICompatibleAdapterError("stream must be a boolean")

    encoded_messages: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, Message):
            raise OpenAICompatibleAdapterError("messages must be graphblocks Message instances")
        if message.role not in {"system", "developer", "user", "assistant", "tool"}:
            raise OpenAICompatibleAdapterError(f"unsupported message role {message.role!r}")
        if len(message.parts) == 1 and message.parts[0].kind == "text":
            encoded_messages.append({"role": message.role, "content": message.parts[0].text or ""})
            continue
        content: list[dict[str, str]] = []
        for part in message.parts:
            if part.kind == "text":
                content.append({"type": "text", "text": part.text or ""})
            elif part.kind in {"json", "artifact_ref"}:
                content.append({"type": "text", "text": canonical_dumps(part.data or {})})
            else:
                raise OpenAICompatibleAdapterError(f"unsupported content part kind {part.kind!r}")
        encoded_messages.append({"role": message.role, "content": content})

    body: dict[str, object] = {
        "model": model.strip(),
        "messages": encoded_messages,
        "stream": stream,
    }
    if tools:
        if not isinstance(tool_schemas, Mapping):
            raise OpenAICompatibleAdapterError(
                "tool_schemas must map every tool input_schema ref to an inline JSON Schema object"
            )
        encoded_tools: list[dict[str, object]] = []
        for tool in tools:
            if not isinstance(tool, ToolDefinition):
                raise OpenAICompatibleAdapterError("tools must be ToolDefinition instances")
            parameters = tool_schemas.get(tool.input_schema)
            if not isinstance(parameters, Mapping):
                raise OpenAICompatibleAdapterError(
                    f"tool_schemas must provide an inline JSON Schema object for {tool.input_schema!r}"
                )
            inline_parameters = _validated_inline_json_schema(tool.input_schema, parameters)
            encoded_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": inline_parameters,
                    },
                }
            )
        body["tools"] = encoded_tools
    elif tool_schemas is not None:
        raise OpenAICompatibleAdapterError("tool_schemas requires at least one tool")
    if tool_choice is not None:
        if isinstance(tool_choice, str):
            if not tool_choice.strip():
                raise OpenAICompatibleAdapterError("tool_choice must not be empty")
            body["tool_choice"] = tool_choice
        elif isinstance(tool_choice, Mapping):
            body["tool_choice"] = deepcopy(dict(tool_choice))
        else:
            raise OpenAICompatibleAdapterError("tool_choice must be a string or mapping")
    if temperature is not None:
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise OpenAICompatibleAdapterError("temperature must be numeric")
        temperature_value = float(temperature)
        if not math.isfinite(temperature_value):
            raise OpenAICompatibleAdapterError("temperature must be finite")
        body["temperature"] = temperature_value
    if max_tokens is not None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise OpenAICompatibleAdapterError("max_tokens must be at least 1")
        body["max_tokens"] = max_tokens
    if extra_body is not None:
        if not isinstance(extra_body, Mapping):
            raise OpenAICompatibleAdapterError("extra_body must be a mapping")
        requested_choices = extra_body.get("n", 1)
        if (
            isinstance(requested_choices, bool)
            or not isinstance(requested_choices, int)
            or requested_choices != 1
        ):
            raise OpenAICompatibleAdapterError("extra_body n must be 1 for the single-choice adapter")
        conflicting_keys = set(extra_body).intersection(body)
        if conflicting_keys:
            raise OpenAICompatibleAdapterError(
                f"extra_body conflicts with canonical fields: {', '.join(sorted(conflicting_keys))}"
            )
        body.update(deepcopy(dict(extra_body)))

    return OpenAIChatCompletionRequest(
        body=body,
        metadata={} if metadata is None else metadata,
    )


def openai_tool_call_drafts_from_response(response: OpenAIChatResponse) -> tuple[ToolCallDraft, ...]:
    if not isinstance(response, OpenAIChatResponse):
        raise OpenAICompatibleAdapterError("response must be an OpenAIChatResponse")

    drafts: list[ToolCallDraft] = []
    for tool_call in response.tool_calls:
        if not isinstance(tool_call, Mapping):
            raise OpenAICompatibleAdapterError("provider response tool_call must be a mapping")
        call_id = tool_call.get("id")
        name = tool_call.get("name")
        arguments = tool_call.get("arguments", "")
        if not isinstance(call_id, str) or not call_id.strip():
            raise OpenAICompatibleAdapterError("provider response tool_call id must not be empty")
        if not isinstance(name, str) or not name.strip():
            raise OpenAICompatibleAdapterError("provider response tool_call name must not be empty")
        if not isinstance(arguments, str):
            raise OpenAICompatibleAdapterError("provider response tool_call arguments must be a string")
        drafts.append(
            ToolCallDraft.proposed(response.response_id, call_id, name)
            .append_argument_fragment(arguments)
            .complete_arguments()
        )
    return tuple(drafts)


def openai_generation_chunk_from_delta(
    delta: OpenAIChatDelta,
    *,
    stream_id: str | None = None,
    sequence: int | None = None,
) -> GenerationChunk | None:
    if not isinstance(delta, OpenAIChatDelta):
        raise OpenAICompatibleAdapterError("delta must be an OpenAIChatDelta")
    if delta.content_delta is None:
        return None
    generation_stream_id = delta.response_id if stream_id is None else stream_id
    if not isinstance(generation_stream_id, str) or not generation_stream_id.strip():
        raise OpenAICompatibleAdapterError("stream_id must not be empty")
    generation_sequence = delta.sequence if sequence is None else sequence
    if (
        isinstance(generation_sequence, bool)
        or not isinstance(generation_sequence, int)
        or generation_sequence <= 0
    ):
        raise OpenAICompatibleAdapterError("generation sequence must be positive")
    return GenerationChunk.text(
        generation_stream_id,
        delta.response_id,
        generation_sequence,
        delta.content_delta,
    )


def openai_usage_record_from_response(
    response: OpenAIChatResponse,
    *,
    record_id: str,
    occurred_at: str,
    run_id: str | None = None,
    attempt_id: str | None = None,
    pricing_ref: str | None = None,
    quota_window_id: str | None = None,
    execution_scope: str | None = None,
) -> UsageRecord:
    if not isinstance(response, OpenAIChatResponse):
        raise OpenAICompatibleAdapterError("response must be an OpenAIChatResponse")

    metadata: dict[str, object] = {
        "model": response.model,
        "provider": "openai-compatible",
    }
    if response.finish_reason is not None:
        metadata["finish_reason"] = response.finish_reason
    return UsageRecord(
        record_id=record_id,
        source="provider_reported",
        confidence="provider_exact",
        amounts=_openai_usage_amounts(response.usage, model=response.model),
        occurred_at=occurred_at,
        run_id=run_id,
        attempt_id=attempt_id,
        provider_response_id=response.response_id,
        pricing_ref=pricing_ref,
        quota_window_id=quota_window_id,
        execution_scope=execution_scope,
        metadata=metadata,
    )


def openai_usage_record_from_delta(
    delta: OpenAIChatDelta,
    *,
    record_id: str,
    model: str,
    occurred_at: str,
    run_id: str | None = None,
    attempt_id: str | None = None,
    pricing_ref: str | None = None,
    quota_window_id: str | None = None,
    execution_scope: str | None = None,
    reconciliation_of: str | None = None,
) -> UsageRecord:
    if not isinstance(delta, OpenAIChatDelta):
        raise OpenAICompatibleAdapterError("delta must be an OpenAIChatDelta")
    if not isinstance(model, str) or not model.strip():
        raise OpenAICompatibleAdapterError("model must not be empty")

    return UsageRecord(
        record_id=record_id,
        source="reconciled" if reconciliation_of is not None else "provider_reported",
        confidence="exact" if reconciliation_of is not None else "provider_exact",
        amounts=_openai_usage_amounts(delta.usage_delta, model=model.strip()),
        occurred_at=occurred_at,
        run_id=run_id,
        attempt_id=attempt_id,
        provider_response_id=delta.response_id,
        pricing_ref=pricing_ref,
        quota_window_id=quota_window_id,
        execution_scope=execution_scope,
        reconciliation_of=reconciliation_of,
        metadata={
            "model": model.strip(),
            "provider": "openai-compatible",
            "stream_sequence": delta.sequence,
        },
    )


def openai_chat_response_from_provider(data: Mapping[str, object]) -> OpenAIChatResponse:
    """Normalize one choice, rejecting multi-choice responses instead of flattening them."""
    if not isinstance(data, Mapping):
        raise OpenAICompatibleAdapterError("provider response must be a mapping")
    response_id = data.get("id")
    model = data.get("model")
    choices = data.get("choices")
    response_id = _strip_required_string("provider response id", response_id)
    model = _strip_required_string("provider response model", model)
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise OpenAICompatibleAdapterError("provider response choices must be a non-empty sequence")
    if len(choices) != 1:
        raise OpenAICompatibleAdapterError(
            "provider response contains multiple choices; this adapter requires n=1"
        )

    parts: list[ContentPart] = []
    tool_calls: list[dict[str, object]] = []
    finish_reason: str | None = None
    for choice in choices:
        if not isinstance(choice, Mapping):
            raise OpenAICompatibleAdapterError("provider response choice must be a mapping")
        choice_index = _non_negative_integer(
            "provider response choice index", choice.get("index", 0)
        )
        raw_finish_reason = choice.get("finish_reason")
        if raw_finish_reason is not None and not isinstance(raw_finish_reason, str):
            raise OpenAICompatibleAdapterError(
                "provider response finish_reason must be a string"
            )
        if finish_reason is None:
            finish_reason = raw_finish_reason
        message = choice.get("message", {})
        if not isinstance(message, Mapping):
            raise OpenAICompatibleAdapterError("provider response message must be a mapping")
        content = message.get("content")
        if isinstance(content, str) and content:
            parts.append(
                ContentPart(
                    kind="text",
                    text=content,
                    metadata={"choice_index": choice_index, "provider": "openai-compatible"},
                )
            )
        elif isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            for item in content:
                if not isinstance(item, Mapping):
                    raise OpenAICompatibleAdapterError("provider response content item must be a mapping")
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(
                        ContentPart(
                            kind="text",
                            text=item["text"],
                            metadata={"choice_index": choice_index, "provider": "openai-compatible"},
                        )
                    )
                else:
                    raise OpenAICompatibleAdapterError(
                        "provider response content item is unsupported"
                    )
        raw_tool_calls = message.get("tool_calls", [])
        if raw_tool_calls is None:
            raw_tool_calls = []
        if not isinstance(raw_tool_calls, Sequence) or isinstance(raw_tool_calls, (str, bytes)):
            raise OpenAICompatibleAdapterError("provider response tool_calls must be a sequence")
        for raw_tool_call in raw_tool_calls:
            if not isinstance(raw_tool_call, Mapping):
                raise OpenAICompatibleAdapterError("provider response tool_call must be a mapping")
            function = raw_tool_call.get("function", {})
            if not isinstance(function, Mapping):
                raise OpenAICompatibleAdapterError("provider response tool_call function must be a mapping")
            call_id = raw_tool_call.get("id")
            name = function.get("name")
            arguments = function.get("arguments", "")
            call_id = _strip_required_string("provider response tool_call id", call_id)
            name = _strip_required_string("provider response tool_call name", name)
            if not isinstance(arguments, str):
                raise OpenAICompatibleAdapterError("provider response tool_call arguments must be a string")
            tool_type = raw_tool_call.get("type")
            tool_type = (
                _strip_required_string("provider response tool_call type", tool_type)
                if tool_type is not None
                else "function"
            )
            tool_calls.append(
                {
                    "id": call_id,
                    "type": tool_type,
                    "name": name,
                    "arguments": arguments,
                }
            )

    usage = data.get("usage", {})
    if usage is None:
        usage = {}
    if not isinstance(usage, Mapping):
        raise OpenAICompatibleAdapterError("provider response usage must be a mapping")
    return OpenAIChatResponse(
        response_id=response_id,
        model=model,
        parts=tuple(parts),
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
    )


def openai_chat_delta_from_chunk(data: Mapping[str, object], *, sequence: int) -> OpenAIChatDelta:
    """Normalize one choice or an empty metadata/usage chunk from an ``n=1`` stream."""
    if not isinstance(data, Mapping):
        raise OpenAICompatibleAdapterError("provider chunk must be a mapping")
    response_id = data.get("id")
    choices = data.get("choices")
    usage = data.get("usage", {})
    if usage is None:
        usage = {}
    if not isinstance(usage, Mapping):
        raise OpenAICompatibleAdapterError("provider chunk usage must be a mapping")
    response_id = _strip_required_string("provider chunk id", response_id)
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)):
        raise OpenAICompatibleAdapterError("provider chunk choices must be a sequence")
    if not choices:
        return OpenAIChatDelta(
            response_id=response_id,
            sequence=sequence,
            choice_index=None,
            usage_delta=usage,
        )
    if len(choices) != 1:
        raise OpenAICompatibleAdapterError(
            "provider chunk contains multiple choices; this adapter requires n=1"
        )
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise OpenAICompatibleAdapterError("provider chunk choice must be a mapping")
    choice_index = choice.get("index", 0)
    choice_index = _non_negative_integer("provider chunk choice index", choice_index)
    delta = choice.get("delta", {})
    if not isinstance(delta, Mapping):
        raise OpenAICompatibleAdapterError("provider chunk delta must be a mapping")
    content_delta = delta.get("content")
    if content_delta is not None and not isinstance(content_delta, str):
        raise OpenAICompatibleAdapterError("provider chunk content delta must be a string")

    tool_call_deltas: list[dict[str, object]] = []
    raw_tool_call_deltas = delta.get("tool_calls", [])
    if raw_tool_call_deltas is None:
        raw_tool_call_deltas = []
    if not isinstance(raw_tool_call_deltas, Sequence) or isinstance(raw_tool_call_deltas, (str, bytes)):
        raise OpenAICompatibleAdapterError("provider chunk tool_calls must be a sequence")
    for raw_delta in raw_tool_call_deltas:
        if not isinstance(raw_delta, Mapping):
            raise OpenAICompatibleAdapterError("provider chunk tool_call delta must be a mapping")
        tool_call_index = _non_negative_integer("provider chunk tool_call index", raw_delta.get("index", 0))
        function = raw_delta.get("function", {})
        if function is None:
            function = {}
        if not isinstance(function, Mapping):
            raise OpenAICompatibleAdapterError("provider chunk tool_call function delta must be a mapping")
        arguments = function.get("arguments")
        if arguments is not None and not isinstance(arguments, str):
            raise OpenAICompatibleAdapterError("provider chunk tool_call function arguments must be a string")
        tool_call_id = _strip_optional_string("provider chunk tool_call id", raw_delta.get("id"))
        tool_call_type = (
            _strip_required_string("provider chunk tool_call type", raw_delta.get("type"))
            if raw_delta.get("type") is not None
            else "function"
        )
        function_name = _strip_optional_string("provider chunk tool_call function name", function.get("name"))
        tool_call_deltas.append(
            {
                "index": tool_call_index,
                "id": tool_call_id,
                "type": tool_call_type,
                "name": function_name,
                "arguments_delta": arguments,
            }
        )
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise OpenAICompatibleAdapterError("provider chunk finish_reason must be a string")
    return OpenAIChatDelta(
        response_id=response_id,
        sequence=sequence,
        choice_index=choice_index,
        content_delta=content_delta,
        tool_call_deltas=tool_call_deltas,
        finish_reason=finish_reason,
        usage_delta=usage,
    )


__all__ = [
    "OpenAIChatCompletionRequest",
    "OpenAIChatDelta",
    "OpenAIChatResponse",
    "OpenAICompatibleAdapterError",
    "OpenAIStreamingToolCallDraftAssembler",
    "openai_chat_completion_request",
    "openai_chat_delta_from_chunk",
    "openai_chat_response_from_provider",
    "openai_generation_chunk_from_delta",
    "openai_tool_call_drafts_from_response",
    "openai_usage_record_from_delta",
    "openai_usage_record_from_response",
]

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import math

from graphblocks import ContentPart, Message, ToolCallDraft, ToolDefinition, canonical_dumps


class OpenAICompatibleAdapterError(ValueError):
    """Raised when an OpenAI-compatible adapter contract is invalid."""


@dataclass(frozen=True, slots=True)
class OpenAIChatCompletionRequest:
    body: Mapping[str, object]
    metadata: Mapping[str, object] = field(default_factory=dict)
    endpoint: str = "/chat/completions"

    def __post_init__(self) -> None:
        if not self.endpoint.strip():
            raise OpenAICompatibleAdapterError("endpoint must not be empty")
        if not self.body:
            raise OpenAICompatibleAdapterError("body must not be empty")
        object.__setattr__(self, "endpoint", self.endpoint.strip())
        object.__setattr__(self, "body", deepcopy(dict(self.body)))
        object.__setattr__(self, "metadata", deepcopy(dict(self.metadata)))

    def request_contract(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "body": deepcopy(dict(self.body)),
            "metadata": deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class OpenAIChatResponse:
    response_id: str
    model: str
    parts: tuple[ContentPart, ...] = field(default_factory=tuple)
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    finish_reason: str | None = None
    usage: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.response_id.strip():
            raise OpenAICompatibleAdapterError("response_id must not be empty")
        if not self.model.strip():
            raise OpenAICompatibleAdapterError("model must not be empty")
        object.__setattr__(self, "parts", tuple(self.parts))
        object.__setattr__(self, "tool_calls", [deepcopy(call) for call in self.tool_calls])
        object.__setattr__(self, "usage", deepcopy(dict(self.usage)))

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
            "tool_calls": [deepcopy(call) for call in self.tool_calls],
            "usage": dict(sorted(deepcopy(dict(self.usage)).items())),
        }


@dataclass(frozen=True, slots=True)
class OpenAIChatDelta:
    response_id: str
    sequence: int
    choice_index: int
    content_delta: str | None = None
    tool_call_deltas: list[dict[str, object]] = field(default_factory=list)
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.response_id.strip():
            raise OpenAICompatibleAdapterError("response_id must not be empty")
        if self.sequence < 0:
            raise OpenAICompatibleAdapterError("sequence must be non-negative")
        if self.choice_index < 0:
            raise OpenAICompatibleAdapterError("choice_index must be non-negative")
        object.__setattr__(self, "tool_call_deltas", [deepcopy(delta) for delta in self.tool_call_deltas])

    def delta_contract(self) -> dict[str, object]:
        return {
            "response_id": self.response_id,
            "sequence": self.sequence,
            "choice_index": self.choice_index,
            "content_delta": self.content_delta,
            "tool_call_deltas": [deepcopy(delta) for delta in self.tool_call_deltas],
            "finish_reason": self.finish_reason,
        }


@dataclass(slots=True)
class OpenAIStreamingToolCallDraftAssembler:
    response_id: str | None = None
    _drafts_by_index: dict[int, ToolCallDraft] = field(default_factory=dict)
    _index_order: list[int] = field(default_factory=list)

    def apply_delta(self, delta: OpenAIChatDelta) -> tuple[ToolCallDraft, ...]:
        if not isinstance(delta, OpenAIChatDelta):
            raise OpenAICompatibleAdapterError("delta must be an OpenAIChatDelta")
        if self.response_id is None:
            self.response_id = delta.response_id
        elif self.response_id != delta.response_id:
            raise OpenAICompatibleAdapterError(
                f"streaming tool call delta changed response id from {self.response_id} to {delta.response_id}"
            )

        updated: list[ToolCallDraft] = []
        for tool_call_delta in delta.tool_call_deltas:
            if not isinstance(tool_call_delta, Mapping):
                raise OpenAICompatibleAdapterError("streaming tool call delta must be a mapping")
            index = tool_call_delta.get("index", 0)
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise OpenAICompatibleAdapterError("streaming tool call delta index must be a non-negative integer")

            draft = self._drafts_by_index.get(index)
            call_id = tool_call_delta.get("id")
            name = tool_call_delta.get("name")
            if draft is None:
                if not isinstance(call_id, str) or not call_id.strip():
                    raise OpenAICompatibleAdapterError("streaming tool call delta requires an id for new index")
                if not isinstance(name, str) or not name.strip():
                    raise OpenAICompatibleAdapterError("streaming tool call delta requires a name for new index")
                draft = ToolCallDraft.proposed(delta.response_id, call_id, name)
                self._drafts_by_index[index] = draft
                self._index_order.append(index)
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
                self._drafts_by_index[index] = draft
            updated.append(draft)
        return tuple(updated)

    def drafts(self) -> tuple[ToolCallDraft, ...]:
        return tuple(self._drafts_by_index[index] for index in self._index_order)

    def complete_all(self) -> tuple[ToolCallDraft, ...]:
        completed: list[ToolCallDraft] = []
        for index in self._index_order:
            draft = self._drafts_by_index[index]
            if draft.status != "arguments_complete":
                draft = draft.complete_arguments()
                self._drafts_by_index[index] = draft
            completed.append(draft)
        return tuple(completed)


def openai_chat_completion_request(
    *,
    model: str,
    messages: Sequence[Message],
    tools: Sequence[ToolDefinition] = (),
    tool_choice: str | Mapping[str, object] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    stream: bool = False,
    metadata: Mapping[str, object] | None = None,
    extra_body: Mapping[str, object] | None = None,
) -> OpenAIChatCompletionRequest:
    if not isinstance(model, str) or not model.strip():
        raise OpenAICompatibleAdapterError("model must not be empty")
    if isinstance(messages, (str, bytes)) or not messages:
        raise OpenAICompatibleAdapterError("messages must contain at least one message")

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
        "stream": bool(stream),
    }
    if tools:
        encoded_tools: list[dict[str, object]] = []
        for tool in tools:
            if not isinstance(tool, ToolDefinition):
                raise OpenAICompatibleAdapterError("tools must be ToolDefinition instances")
            encoded_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": {"$ref": tool.input_schema},
                    },
                }
            )
        body["tools"] = encoded_tools
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


def openai_chat_response_from_provider(data: Mapping[str, object]) -> OpenAIChatResponse:
    if not isinstance(data, Mapping):
        raise OpenAICompatibleAdapterError("provider response must be a mapping")
    response_id = data.get("id")
    model = data.get("model")
    choices = data.get("choices")
    if not isinstance(response_id, str) or not response_id.strip():
        raise OpenAICompatibleAdapterError("provider response id must not be empty")
    if not isinstance(model, str) or not model.strip():
        raise OpenAICompatibleAdapterError("provider response model must not be empty")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise OpenAICompatibleAdapterError("provider response choices must be a non-empty sequence")

    parts: list[ContentPart] = []
    tool_calls: list[dict[str, object]] = []
    finish_reason: str | None = None
    for choice in choices:
        if not isinstance(choice, Mapping):
            raise OpenAICompatibleAdapterError("provider response choice must be a mapping")
        choice_index = choice.get("index", 0)
        if isinstance(choice_index, bool) or not isinstance(choice_index, int):
            raise OpenAICompatibleAdapterError("provider response choice index must be an integer")
        if finish_reason is None and isinstance(choice.get("finish_reason"), str):
            finish_reason = choice["finish_reason"]
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
            if not isinstance(call_id, str) or not call_id.strip():
                raise OpenAICompatibleAdapterError("provider response tool_call id must not be empty")
            if not isinstance(name, str) or not name.strip():
                raise OpenAICompatibleAdapterError("provider response tool_call name must not be empty")
            if not isinstance(arguments, str):
                raise OpenAICompatibleAdapterError("provider response tool_call arguments must be a string")
            tool_calls.append(
                {
                    "id": call_id,
                    "type": raw_tool_call.get("type") if isinstance(raw_tool_call.get("type"), str) else "function",
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
    if not isinstance(data, Mapping):
        raise OpenAICompatibleAdapterError("provider chunk must be a mapping")
    response_id = data.get("id")
    choices = data.get("choices")
    if not isinstance(response_id, str) or not response_id.strip():
        raise OpenAICompatibleAdapterError("provider chunk id must not be empty")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise OpenAICompatibleAdapterError("provider chunk choices must be a non-empty sequence")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise OpenAICompatibleAdapterError("provider chunk choice must be a mapping")
    choice_index = choice.get("index", 0)
    if isinstance(choice_index, bool) or not isinstance(choice_index, int):
        raise OpenAICompatibleAdapterError("provider chunk choice index must be an integer")
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
        function = raw_delta.get("function", {})
        if function is None:
            function = {}
        if not isinstance(function, Mapping):
            raise OpenAICompatibleAdapterError("provider chunk tool_call function delta must be a mapping")
        tool_call_deltas.append(
            {
                "index": raw_delta.get("index", 0),
                "id": raw_delta.get("id") if isinstance(raw_delta.get("id"), str) else None,
                "type": raw_delta.get("type") if isinstance(raw_delta.get("type"), str) else "function",
                "name": function.get("name") if isinstance(function.get("name"), str) else None,
                "arguments_delta": (
                    function.get("arguments") if isinstance(function.get("arguments"), str) else None
                ),
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
    "openai_tool_call_drafts_from_response",
]

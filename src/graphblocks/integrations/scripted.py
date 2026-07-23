from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal


class ScriptedModelProviderError(ValueError):
    """Raised when a scripted model provider contract is invalid."""


def _stable_string(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ScriptedModelProviderError(
            f"{field_name} must be a stable non-empty string"
        )
    return value


def _positive_integer(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ScriptedModelProviderError(f"{field_name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class ScriptedModelResponse:
    response_id: str
    provider: str
    model: str
    text: str
    finish_reason: Literal["scripted"] = "scripted"
    usage: Mapping[str, int] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _stable_string("response_id", self.response_id)
        _stable_string("provider", self.provider)
        _stable_string("model", self.model)
        if not isinstance(self.text, str):
            raise ScriptedModelProviderError("text must be a string")
        if self.finish_reason != "scripted":
            raise ScriptedModelProviderError("finish_reason must be 'scripted'")
        if not isinstance(self.usage, Mapping):
            raise ScriptedModelProviderError("usage must be a mapping")
        if any(
            not isinstance(key, str)
            or not key
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in self.usage.items()
        ):
            raise ScriptedModelProviderError(
                "usage must map non-empty strings to non-negative integers"
            )
        if not isinstance(self.metadata, Mapping):
            raise ScriptedModelProviderError("metadata must be a mapping")
        if any(not isinstance(key, str) for key in self.metadata):
            raise ScriptedModelProviderError("metadata keys must be strings")
        object.__setattr__(self, "usage", deepcopy(dict(self.usage)))
        object.__setattr__(self, "metadata", deepcopy(dict(self.metadata)))

    def response_contract(self) -> dict[str, object]:
        return {
            "response_id": self.response_id,
            "provider": self.provider,
            "model": self.model,
            "text": self.text,
            "finish_reason": self.finish_reason,
            "usage": dict(sorted(deepcopy(dict(self.usage)).items())),
            "metadata": dict(sorted(deepcopy(dict(self.metadata)).items())),
        }


@dataclass(frozen=True, slots=True)
class ScriptedModelDelta:
    response_id: str
    sequence: int
    text_delta: str
    finished: bool = False
    finish_reason: Literal["scripted"] | None = None

    def __post_init__(self) -> None:
        _stable_string("response_id", self.response_id)
        _positive_integer("sequence", self.sequence)
        if not isinstance(self.text_delta, str):
            raise ScriptedModelProviderError("text_delta must be a string")
        if not isinstance(self.finished, bool):
            raise ScriptedModelProviderError("finished must be a boolean")
        if self.finished and self.finish_reason is None:
            raise ScriptedModelProviderError("finished delta requires finish_reason")
        if not self.finished and self.finish_reason is not None:
            raise ScriptedModelProviderError("unfinished delta must not have finish_reason")
        if self.finish_reason not in {None, "scripted"}:
            raise ScriptedModelProviderError("finish_reason is invalid")

    def delta_contract(self) -> dict[str, object]:
        return {
            "response_id": self.response_id,
            "sequence": self.sequence,
            "text_delta": self.text_delta,
            "finished": self.finished,
            "finish_reason": self.finish_reason,
        }


@dataclass(frozen=True, slots=True)
class ScriptedModelProvider:
    scripts: Mapping[str, str]
    model: str = "scripted"
    provider_id: str = "scripted"

    def __post_init__(self) -> None:
        if not isinstance(self.scripts, Mapping) or not self.scripts:
            raise ScriptedModelProviderError("scripts must be a non-empty mapping")
        scripts = dict(self.scripts)
        for prompt, response in scripts.items():
            if not isinstance(prompt, str) or prompt == "":
                raise ScriptedModelProviderError("script prompts must be non-empty strings")
            if not isinstance(response, str):
                raise ScriptedModelProviderError("script responses must be strings")
        _stable_string("model", self.model)
        _stable_string("provider_id", self.provider_id)
        object.__setattr__(self, "scripts", deepcopy(scripts))

    def capabilities(self) -> dict[str, bool]:
        return {
            "chat": True,
            "streaming": True,
            "tool_calling": False,
            "usage": True,
        }

    def generate(
        self,
        prompt: str,
        *,
        response_id: str = "scripted-response",
        metadata: Mapping[str, object] | None = None,
    ) -> ScriptedModelResponse:
        if prompt not in self.scripts:
            raise ScriptedModelProviderError(f"no scripted response for prompt {prompt!r}")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ScriptedModelProviderError("metadata must be a mapping")
        response_metadata = deepcopy(dict(metadata or {}))
        response_metadata["script_key"] = prompt
        response = self.scripts[prompt]
        return ScriptedModelResponse(
            response_id=response_id,
            provider=self.provider_id,
            model=self.model,
            text=response,
            usage={
                "input_characters": len(prompt),
                "output_characters": len(response),
            },
            metadata=response_metadata,
        )

    def stream(
        self,
        prompt: str,
        *,
        response_id: str = "scripted-response",
        chunk_size: int = 16,
    ) -> Iterator[ScriptedModelDelta]:
        _positive_integer("chunk_size", chunk_size)
        response = self.generate(prompt, response_id=response_id)
        sequence = 1
        for start in range(0, len(response.text), chunk_size):
            yield ScriptedModelDelta(
                response_id=response.response_id,
                sequence=sequence,
                text_delta=response.text[start : start + chunk_size],
            )
            sequence += 1
        yield ScriptedModelDelta(
            response_id=response.response_id,
            sequence=sequence,
            text_delta="",
            finished=True,
            finish_reason="scripted",
        )


__all__ = [
    "ScriptedModelDelta",
    "ScriptedModelProvider",
    "ScriptedModelProviderError",
    "ScriptedModelResponse",
]

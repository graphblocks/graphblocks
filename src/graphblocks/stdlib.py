from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .documents import FrozenDict


_MAX_U64 = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class ScriptedModelResponse:
    response: str
    finish_reason: str
    usage: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.response, str):
            raise ValueError("scripted model response must be a string")
        try:
            self.response.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError(
                "scripted model response must contain only Unicode scalar values"
            ) from error
        if (
            not isinstance(self.finish_reason, str)
            or not self.finish_reason.strip()
            or self.finish_reason != self.finish_reason.strip()
        ):
            raise ValueError(
                "scripted model finish_reason must be an exact non-empty string"
            )
        try:
            self.finish_reason.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError(
                "scripted model finish_reason must contain only Unicode scalar values"
            ) from error
        if not isinstance(self.usage, Mapping):
            raise ValueError("scripted model usage must be a mapping")
        try:
            usage_items = tuple(self.usage.items())
        except Exception as error:
            raise ValueError("scripted model usage must be a stable mapping") from error
        usage: dict[str, int] = {}
        for key, value in usage_items:
            if (
                not isinstance(key, str)
                or not key.strip()
                or key != key.strip()
            ):
                raise ValueError(
                    "scripted model usage keys must be exact non-empty strings"
                )
            try:
                key.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError(
                    "scripted model usage keys must contain only Unicode scalar values"
                ) from error
            if key in usage:
                raise ValueError("scripted model usage keys must be unique")
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ValueError(
                    "scripted model usage values must be non-negative integers"
                )
            if value > _MAX_U64:
                raise ValueError(
                    "scripted model usage values must fit an unsigned 64-bit integer"
                )
            usage[key] = value
        object.__setattr__(self, "usage", FrozenDict(usage))

    def response_contract(self) -> dict[str, object]:
        return {
            "response": self.response,
            "finish_reason": self.finish_reason,
            "usage": dict(sorted(self.usage.items())),
        }


def scripted_model_generate(
    prompt: object,
    *,
    script: Mapping[str, object] | None = None,
    response: object | None = None,
) -> ScriptedModelResponse:
    prompt_text = str(prompt)
    script_snapshot: dict[str, object] | None = None
    if script is not None:
        if not isinstance(script, Mapping):
            raise ValueError("script must be a mapping")
        try:
            script_items = tuple(script.items())
        except Exception as error:
            raise ValueError("script must be a stable mapping") from error
        script_snapshot = {}
        for key, value in script_items:
            if not isinstance(key, str):
                raise ValueError("script keys must be strings")
            try:
                key.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError(
                    "script keys must contain only Unicode scalar values"
                ) from error
            if key in script_snapshot:
                raise ValueError("script keys must be unique")
            script_snapshot[key] = value
    if script_snapshot is not None and prompt_text in script_snapshot:
        output = str(script_snapshot[prompt_text])
        finish_reason = "scripted"
    elif response is not None:
        output = str(response)
        finish_reason = "default_response"
    else:
        output = prompt_text
        finish_reason = "echo"

    return ScriptedModelResponse(
        response=output,
        finish_reason=finish_reason,
        usage={
            "input_chars": len(prompt_text),
            "output_chars": len(output),
        },
    )


def run_native_stdlib_graph(
    graph: dict[str, object],
    inputs: dict[str, object],
    *,
    run_id: str | None = None,
    run_store_path: str | None = None,
    journal_store_path: str | None = None,
) -> dict[str, object]:
    from graphblocks_runtime import run_stdlib_graph

    return run_stdlib_graph(
        graph,
        inputs,
        run_id=run_id,
        run_store_path=run_store_path,
        journal_store_path=journal_store_path,
    )


__all__ = [
    "ScriptedModelResponse",
    "run_native_stdlib_graph",
    "scripted_model_generate",
]

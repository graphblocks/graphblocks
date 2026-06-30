from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class ScriptedModelResponse:
    response: str
    finish_reason: str
    usage: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))

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
    if script is not None and prompt_text in script:
        output = str(script[prompt_text])
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


def run_native_stdlib_graph(graph: dict[str, object], inputs: dict[str, object]) -> dict[str, object]:
    from graphblocks_runtime import run_stdlib_graph

    return run_stdlib_graph(graph, inputs)


__all__ = [
    "ScriptedModelResponse",
    "run_native_stdlib_graph",
    "scripted_model_generate",
]

from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_stdlib_scripted_model_uses_prompt_script_mapping(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-stdlib" / "src"))
    graphblocks_stdlib = importlib.import_module("graphblocks_stdlib")

    response = graphblocks_stdlib.scripted_model_generate(
        "Answer: Hello",
        script={"Answer: Hello": "Hello from the scripted model."},
    )

    assert response.response_contract() == {
        "response": "Hello from the scripted model.",
        "finish_reason": "scripted",
        "usage": {
            "input_chars": 13,
            "output_chars": 30,
        },
    }


def test_stdlib_scripted_model_uses_default_response_then_echo(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-stdlib" / "src"))
    graphblocks_stdlib = importlib.import_module("graphblocks_stdlib")

    default_response = graphblocks_stdlib.scripted_model_generate(
        "Answer: Unknown",
        script={"Answer: Hello": "Hello from the scripted model."},
        response="Default answer.",
    )
    echo_response = graphblocks_stdlib.scripted_model_generate("Echo this")

    assert default_response.response_contract()["response"] == "Default answer."
    assert default_response.response_contract()["finish_reason"] == "default_response"
    assert echo_response.response_contract() == {
        "response": "Echo this",
        "finish_reason": "echo",
        "usage": {
            "input_chars": 9,
            "output_chars": 9,
        },
    }

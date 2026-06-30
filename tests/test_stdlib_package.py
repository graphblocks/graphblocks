from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import SimpleNamespace


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


def test_stdlib_package_lazy_native_runner_delegates_to_runtime(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-stdlib" / "src"))
    calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def run_stdlib_graph(graph: dict[str, object], inputs: dict[str, object]) -> dict[str, object]:
        calls.append((graph, inputs))
        return {"runId": "run-1", "status": "succeeded", "graph": graph, "inputs": inputs}

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(run_stdlib_graph=run_stdlib_graph),
    )
    graphblocks_stdlib = importlib.import_module("graphblocks_stdlib")

    result = graphblocks_stdlib.run_native_stdlib_graph(
        {"kind": "Graph", "metadata": {"name": "stdlib"}},
        {"message": {"text": "hello"}},
    )

    assert result == {
        "runId": "run-1",
        "status": "succeeded",
        "graph": {"kind": "Graph", "metadata": {"name": "stdlib"}},
        "inputs": {"message": {"text": "hello"}},
    }
    assert calls == [
        (
            {"kind": "Graph", "metadata": {"name": "stdlib"}},
            {"message": {"text": "hello"}},
        )
    ]
    assert "run_native_stdlib_graph" in graphblocks_stdlib.__all__

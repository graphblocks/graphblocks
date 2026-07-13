from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from graphblocks.composition import compose_documents


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _test_support import assert_example_runner
from model_selector import MODEL_CHOICES, load_model_profile


def test_workspace_assistant_composes_typed_subgraphs() -> None:
    result = compose_documents(Path(__file__).with_name("example.yaml"))
    graph = next(document for document in result.documents if document["kind"] == "Graph")

    assert "composition" not in graph["spec"]
    assert set(graph["spec"]["nodes"]) == {
        "prepare__snapshot",
        "prepare__context",
        "respond__agent",
        "respond__candidate",
    }
    assert {
        (instance.node, instance.fragment)
        for instance in result.report.instances
    } == {
        ("prepare", "workspace/workspace-context"),
        ("respond", "assistant/assistant-turn"),
    }
    assert {
        (edge["from"], edge["to"])
        for edge in graph["spec"]["edges"]
    } >= {
        ("$input.turn.workspace", "prepare__snapshot.workspace"),
        ("prepare__context.pack", "respond__agent.context"),
        ("respond__candidate.result", "$output.result"),
    }
    agent = graph["spec"]["nodes"]["respond__agent"]
    assert agent["bindings"] == {"model": "coding-model"}
    assert agent["config"]["maxSteps"] == 20
    assert agent["config"]["approval"] == {
        "workspace.commit_changeset": "required",
        "process.execute": "required",
    }


def test_tui_workspace_assistant_example() -> None:
    payload = assert_example_runner(
        Path(__file__).with_name("run.py"),
        expected_checks={
            "composition:expanded",
            "mock-graph:resolved-inputs",
            "mock-graph:final-output",
            "mock-graph:journal",
        },
        expected_boundaries={"mock-workspace-api", "scripted-llm"},
    )
    selection = payload["modelSelection"]
    assert selection["selected"]["choice"] == "gpt"
    assert set(selection["available"]) == set(MODEL_CHOICES)
    assert selection["execution"] == {
        "mode": "offline-fixture",
        "provider": "scripted-llm",
        "externalRequestSent": False,
    }


def test_each_model_choice_resolves_the_same_logical_resource() -> None:
    example_root = Path(__file__).parent
    profiles = [load_model_profile(example_root, choice) for choice in MODEL_CHOICES]

    assert {profile["choice"] for profile in profiles} == set(MODEL_CHOICES)
    assert {profile["resource"] for profile in profiles} == {"coding-model"}
    assert {profile["provider"] for profile in profiles} == {"openai", "google", "anthropic"}
    assert len({profile["model"] for profile in profiles}) == len(MODEL_CHOICES)
    assert all(str(profile["digest"]).startswith("sha256:") for profile in profiles)


def test_cli_selects_each_external_model_profile() -> None:
    script = Path(__file__).with_name("run.py")
    for choice in MODEL_CHOICES:
        result = subprocess.run(
            [sys.executable, str(script), "--model", choice],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout.splitlines()[-1])
        assert payload["modelSelection"]["selected"]["choice"] == choice

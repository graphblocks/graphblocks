from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _test_support import assert_example_runner
from model_selector import MODEL_CHOICES, load_model_profile


def test_tui_workspace_assistant_example() -> None:
    payload = assert_example_runner(
        Path(__file__).with_name("run.py"),
        expected_checks={"mock-graph:resolved-inputs", "mock-graph:final-output", "mock-graph:journal"},
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

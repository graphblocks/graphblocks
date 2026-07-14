from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import yaml

import graphblocks
from tools.check_compatibility import _check_or_update


ROOT = Path(__file__).parents[1]
COMPATIBILITY_ROOT = ROOT / "compatibility"


def test_stable_compatibility_snapshots_match_the_implementation() -> None:
    completed = subprocess.run(
        [sys.executable, "tools/check_compatibility.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_snapshot_writer_requires_explicit_update_for_drift(tmp_path, capsys) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text("{}\n", encoding="utf-8")
    contract = {"snapshotVersion": 1, "values": ["stable"]}

    assert _check_or_update(snapshot_path, contract, update=False) is False
    assert "compatibility snapshot drifted" in capsys.readouterr().err

    assert _check_or_update(snapshot_path, contract, update=True) is True
    capsys.readouterr()
    assert _check_or_update(snapshot_path, contract, update=False) is True


def test_stable_python_surface_is_deliberate_and_profile_bounded() -> None:
    policy = yaml.safe_load(
        (COMPATIBILITY_ROOT / "stable-python-surface.yaml").read_text(encoding="utf-8")
    )
    snapshot = json.loads(
        (COMPATIBILITY_ROOT / "stable-python-api.json").read_text(encoding="utf-8")
    )

    assert policy["readiness"] == snapshot["readiness"] == "candidate"
    assert {entry["profile"] for entry in policy["symbols"]} == {
        "GB-C0-SCHEMA",
        "GB-C1-LOCAL-RUNTIME",
    }
    assert [entry["path"] for entry in policy["symbols"]] == [
        entry["path"] for entry in snapshot["symbols"]
    ]
    assert len(snapshot["symbols"]) < 55
    assert all(entry["path"].startswith("graphblocks.") for entry in snapshot["symbols"])
    assert all(
        "signature" in entry or entry["kind"] == "type-alias"
        for entry in snapshot["symbols"]
    )
    stable_type_names = {
        entry["path"].split(".", 2)[1] for entry in snapshot["symbols"]
    }
    assert stable_type_names <= set(graphblocks.__all__)
    assert all(
        set(entry.get("typeReferences", ())) <= stable_type_names
        for entry in snapshot["symbols"]
    )
    for preview_type in (
        "InProcessRuntime",
        "RunDeploymentProvenance",
        "RuntimeCheckpoint",
        "RunResult",
    ):
        assert preview_type not in stable_type_names
        assert all(
            preview_type not in entry.get("typeReferences", ())
            for entry in snapshot["symbols"]
        )
    assert "waiting_callback" not in json.dumps(snapshot)


def test_stable_cli_snapshot_covers_success_failure_json_and_exit_codes() -> None:
    snapshot = json.loads(
        (COMPATIBILITY_ROOT / "stable-cli-contracts.json").read_text(encoding="utf-8")
    )

    assert snapshot["readiness"] == "candidate"
    assert snapshot["stdoutContract"] == "parsed-json"
    assert {case["command"] for case in snapshot["cases"]} == {
        "validate",
        "plan",
        "run",
    }
    for command in ("validate", "plan", "run"):
        command_cases = [case for case in snapshot["cases"] if case["command"] == command]
        assert {case["exitCode"] for case in command_cases} == {0, 1}
        assert all(isinstance(case["stdoutJson"], dict) for case in command_cases)
        assert all(case["stderr"] == "" for case in command_cases)
    plan_expand = next(
        case for case in snapshot["cases"] if case["id"] == "plan-expand-success"
    )
    assert plan_expand["argv"][-1] == "--expand"
    assert plan_expand["stdoutJson"]["graph"]["apiVersion"] == "graphblocks.ai/v1"
    unknown_opt_in_cases = {
        case["id"]: case
        for case in snapshot["cases"]
        if case["id"] in {
            "validate-unknown-block-opt-in",
            "plan-unknown-block-opt-in",
        }
    }
    assert set(unknown_opt_in_cases) == {
        "validate-unknown-block-opt-in",
        "plan-unknown-block-opt-in",
    }
    assert all(
        "--allow-unknown-blocks" in case["argv"]
        and case["exitCode"] == 0
        and case["stdoutJson"]["ok"] is True
        for case in unknown_opt_in_cases.values()
    )


def test_stable_testing_surface_is_deliberate_and_profile_bounded() -> None:
    policy = yaml.safe_load(
        (COMPATIBILITY_ROOT / "stable-testing-surface.yaml").read_text(encoding="utf-8")
    )
    snapshot = json.loads(
        (COMPATIBILITY_ROOT / "stable-testing-api.json").read_text(encoding="utf-8")
    )

    assert policy["readiness"] == snapshot["readiness"] == "candidate"
    assert {entry["profile"] for entry in policy["symbols"]} == {
        "GB-C0-SCHEMA",
        "GB-C1-LOCAL-RUNTIME",
    }
    assert [entry["path"] for entry in policy["symbols"]] == [
        entry["path"] for entry in snapshot["symbols"]
    ]
    assert all(
        entry["path"].startswith("graphblocks_testing.")
        for entry in snapshot["symbols"]
    )


def test_stable_testing_cli_snapshot_covers_list_and_run_all_contracts() -> None:
    snapshot = json.loads(
        (COMPATIBILITY_ROOT / "stable-testing-cli-contracts.json").read_text(
            encoding="utf-8"
        )
    )

    assert snapshot["readiness"] == "candidate"
    assert snapshot["stdoutContract"] == "parsed-json"
    assert {case["command"] for case in snapshot["cases"]} == {"list", "run-all"}
    assert {case["exitCode"] for case in snapshot["cases"]} == {0, 1}
    assert all(isinstance(case["stdoutJson"], dict) for case in snapshot["cases"])
    assert all(case["stderr"] == "" for case in snapshot["cases"])

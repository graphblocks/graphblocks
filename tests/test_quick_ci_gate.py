from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def test_push_and_pull_request_cannot_bypass_full_or_quick_gates_by_path() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

    assert workflow[True] == {"push": None, "pull_request": None}
    required = workflow["jobs"]["required-gates"]
    assert required["needs"] == [
        "python-quality",
        "python",
        "installed-artifacts",
        "examples",
        "rust",
        "dependency-audit",
        "codeql",
        "macos-native-smoke",
    ]
    assert required["if"] == "always()"


def test_quick_feedback_gate_has_a_bounded_release_contract_smoke() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    quick = workflow["jobs"]["python-quality"]

    assert quick["name"] == "Quick feedback (lint, contracts, unit smoke)"
    assert quick["runs-on"] == "ubuntu-latest"
    assert quick["timeout-minutes"] == 5
    steps = {step["name"]: step for step in quick["steps"]}
    assert steps["Check out repository"]["with"] == {"fetch-depth": 0}
    assert steps["Install quick-gate dependencies"]["run"] == (
        "python -m pip install pip==25.1.1\n"
        'python -m pip install -e ".[test]"\n'
    )

    lint = steps["Check lint and progressive format baseline"]["run"]
    assert "python -m ruff check ." in lint
    assert "dist/ci/quick/lint.log" in lint

    contracts = steps["Check stable contracts and audit inventory"]["run"]
    for command in (
        "python tools/check_stable_typing.py",
        "python tools/check_compatibility.py --api-only",
        "python tools/check_audit_inventory.py",
    ):
        assert command in contracts

    smoke = steps["Run core unit smoke"]["run"]
    for path in (
        "tests/test_audit_inventory.py",
        "tests/test_canonical_integer_limits.py",
        "tests/test_package_layout.py",
        "tests/test_quick_ci_gate.py",
    ):
        assert path in smoke
    assert "--junitxml=dist/ci/quick/unit-smoke.xml" in smoke

    retained = steps["Retain quick-feedback diagnostics"]
    assert retained["if"] == "always()"
    assert retained["with"]["path"] == "dist/ci/quick"
    assert retained["with"]["if-no-files-found"] == "warn"

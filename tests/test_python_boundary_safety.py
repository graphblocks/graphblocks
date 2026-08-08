from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from tools import check_python_boundary_safety


ROOT = Path(__file__).parents[1]


def test_checked_in_boundary_inventory_is_closed_and_matches_source() -> None:
    baseline = check_python_boundary_safety.load_baseline()
    report = check_python_boundary_safety.check_boundary_safety(baseline)

    assert report["passed"] is True
    assert report["broadExceptionHandlers"] == {
        "reviewed": 119,
        "observed": 119,
        "files": 27,
    }
    assert report["productionAsserts"] == {
        "reviewed": 94,
        "observed": 94,
        "files": 23,
    }


def test_boundary_inventory_rejects_unknown_fields_and_classifications() -> None:
    payload = json.loads(
        check_python_boundary_safety.BASELINE_PATH.read_text(encoding="utf-8")
    )
    with_unknown = deepcopy(payload)
    with_unknown["unexpected"] = True
    with pytest.raises(
        check_python_boundary_safety.PythonBoundarySafetyError,
        match="unknown",
    ):
        check_python_boundary_safety.parse_baseline(with_unknown)

    unknown_classification = deepcopy(payload)
    unknown_classification["productionAsserts"]["files"][0]["classifications"] = [
        "trust-me"
    ]
    with pytest.raises(
        check_python_boundary_safety.PythonBoundarySafetyError,
        match="unknown=.*trust-me",
    ):
        check_python_boundary_safety.parse_baseline(unknown_classification)


def test_boundary_inventory_loader_rejects_duplicate_fields(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        '{"schemaVersion":1,"schemaVersion":1}\n',
        encoding="utf-8",
    )

    with pytest.raises(
        check_python_boundary_safety.PythonBoundarySafetyError,
        match="duplicate JSON field",
    ):
        check_python_boundary_safety.load_baseline(baseline_path)


def test_semantic_fingerprint_rejects_changed_or_unreviewed_boundaries(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "graphblocks" / "sample.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def boundary(callback):\n"
        "    try:\n"
        "        value = callback()\n"
        "    except Exception as error:\n"
        "        raise ValueError('normalized') from error\n"
        "    assert value is not None\n"
        "    return value\n",
        encoding="utf-8",
    )
    broad, asserts = check_python_boundary_safety.collect_inventory(root=tmp_path)
    path = "src/graphblocks/sample.py"
    baseline = check_python_boundary_safety.BoundaryBaseline(
        broad_handlers={
            path: check_python_boundary_safety.ReviewedFile(
                path=path,
                count=broad[path][0],
                semantic_digest=broad[path][1],
                classifications=("external-callback-boundary",),
                rationale="Reviewed callback normalization boundary.",
            )
        },
        production_asserts={
            path: check_python_boundary_safety.ReviewedFile(
                path=path,
                count=asserts[path][0],
                semantic_digest=asserts[path][1],
                classifications=("validated-type-narrowing",),
                rationale="Reviewed narrowing after callback contract validation.",
            )
        },
    )

    source.write_text(
        source.read_text(encoding="utf-8").replace("'normalized'", "'changed'"),
        encoding="utf-8",
    )
    report = check_python_boundary_safety.check_boundary_safety(
        baseline,
        root=tmp_path,
    )

    assert report["passed"] is False
    assert report["violations"] == [
        "broad-exception-handler: src/graphblocks/sample.py semantic fingerprint changed"
    ]


def test_ci_runs_inventory_gate_and_exact_optimized_boundary_shard() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    quick_steps = {
        step["name"]: step for step in workflow["jobs"]["python-quality"]["steps"]
    }
    contracts = quick_steps["Check stable contracts and audit inventory"]["run"]
    assert "python tools/check_python_boundary_safety.py" in contracts
    assert "--report dist/ci/quick/python-boundary-safety.json" in contracts

    python_steps = {step["name"]: step for step in workflow["jobs"]["python"]["steps"]}
    optimized = python_steps["Run Python optimized boundary shard"]
    assert optimized["if"] == (
        "${{ matrix.os == 'ubuntu-latest' && matrix.python-version == '3.11' }}"
    )
    command = optimized["run"]
    assert "python -O -m pytest -q" in command
    for path in check_python_boundary_safety.OPTIMIZED_TESTS:
        assert path in command
    assert "dist/ci/python-optimized-boundaries.xml" in command

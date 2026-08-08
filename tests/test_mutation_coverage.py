from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, cast

import pytest
import yaml  # type: ignore[import-untyped]

from tools import check_mutation_coverage


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "compatibility" / "stable-mutation-budget.yaml"
CHECKER = ROOT / "tools" / "check_mutation_coverage.py"


def test_mutation_budget_covers_the_stable_seed_categories() -> None:
    budget = check_mutation_coverage.load_mutation_budget()

    assert budget.scope == "stable-core-seed"
    assert budget.minimum_score_percent == 100
    assert budget.maximum_surviving_mutants == 0
    assert budget.timeout_seconds_per_mutant == 30
    assert {mutant.category for mutant in budget.mutants} == {
        "canonical",
        "compiler",
        "policy",
        "durable-handler",
    }
    assert len(budget.mutants) == 4
    check_mutation_coverage.validate_mutation_sources(budget)


def test_mutation_budget_cli_validates_and_lists_without_running_tests() -> None:
    validated = subprocess.run(
        [sys.executable, str(CHECKER), "--validate-only"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    listed = subprocess.run(
        [sys.executable, str(CHECKER), "--list"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 0
    assert validated.stdout == "validated 4 stable mutation seeds\n"
    assert validated.stderr == ""
    assert listed.returncode == 0
    assert listed.stdout.splitlines() == [
        "canonical-sha256-digest",
        "policy-explicit-deny-precedence",
        "compiler-graph-hash-binds-normalized-ir",
        "durable-source-replay-events",
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda payload: payload.update({"unknown": True}), "unknown fields"),
        (
            lambda payload: payload["thresholds"].update(
                {"minimumMutationScorePercent": True}
            ),
            "must be an integer",
        ),
        (
            lambda payload: payload["mutants"][0].update({"source": "../outside.py"}),
            "allowed source root",
        ),
        (
            lambda payload: payload["mutants"][0].update({"tests": ["-x"]}),
            "explicit tests/ pytest node ids",
        ),
        (
            lambda payload: payload.update(
                {
                    "mutants": [
                        item
                        for item in payload["mutants"]
                        if item["category"] != "policy"
                    ]
                }
            ),
            "missing stable seed categories",
        ),
    ),
)
def test_mutation_budget_decoder_fails_closed(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    payload = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    mutation(payload)
    candidate = tmp_path / "mutation-budget.yaml"
    candidate.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(check_mutation_coverage.MutationCoverageError, match=message):
        check_mutation_coverage.load_mutation_budget(candidate)


def test_mutation_source_validation_rejects_stale_and_ambiguous_anchors(
    tmp_path: Path,
) -> None:
    budget = check_mutation_coverage.load_mutation_budget()
    source = budget.mutants[0].source
    destination = tmp_path / source
    destination.parent.mkdir(parents=True)
    destination.write_text("no mutation anchor here\n", encoding="utf-8")

    with pytest.raises(
        check_mutation_coverage.MutationCoverageError,
        match="must occur exactly once; found 0",
    ):
        check_mutation_coverage.validate_mutation_sources(
            check_mutation_coverage.MutationBudget(
                path=budget.path,
                sha256=budget.sha256,
                scope=budget.scope,
                minimum_score_percent=budget.minimum_score_percent,
                maximum_surviving_mutants=budget.maximum_surviving_mutants,
                timeout_seconds_per_mutant=budget.timeout_seconds_per_mutant,
                mutants=(budget.mutants[0],),
            ),
            root=tmp_path,
        )

    destination.write_text(budget.mutants[0].find * 2, encoding="utf-8")
    with pytest.raises(
        check_mutation_coverage.MutationCoverageError,
        match="must occur exactly once; found 2",
    ):
        check_mutation_coverage.validate_mutation_sources(
            check_mutation_coverage.MutationBudget(
                path=budget.path,
                sha256=budget.sha256,
                scope=budget.scope,
                minimum_score_percent=budget.minimum_score_percent,
                maximum_surviving_mutants=budget.maximum_surviving_mutants,
                timeout_seconds_per_mutant=budget.timeout_seconds_per_mutant,
                mutants=(budget.mutants[0],),
            ),
            root=tmp_path,
        )


def test_mutation_campaign_kills_all_checked_in_seeds_and_writes_evidence(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "stable-mutation-report.json"
    completed = subprocess.run(
        [sys.executable, str(CHECKER), "--report", str(report_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["summary"] == {
        "total": 4,
        "killed": 4,
        "survived": 0,
        "inconclusive": 0,
        "mutationScorePercent": 100.0,
    }
    assert report["survivingMutants"] == []
    assert report["inconclusiveMutants"] == []
    assert {item["status"] for item in report["mutants"]} == {"killed"}


def test_mutation_campaign_reports_surviving_mutants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def pass_tests(
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", pass_tests)

    report = check_mutation_coverage.run_mutation_budget(
        check_mutation_coverage.load_mutation_budget()
    )

    assert report["passed"] is False
    assert report["summary"] == {
        "total": 4,
        "killed": 0,
        "survived": 4,
        "inconclusive": 0,
        "mutationScorePercent": 0.0,
    }
    assert report["survivingMutants"] == [
        "canonical-sha256-digest",
        "policy-explicit-deny-precedence",
        "compiler-graph-hash-binds-normalized-ir",
        "durable-source-replay-events",
    ]


def test_mutation_campaign_rejects_a_broken_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_tests(
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, "", "collection failed")

    monkeypatch.setattr(subprocess, "run", fail_tests)

    report = check_mutation_coverage.run_mutation_budget(
        check_mutation_coverage.load_mutation_budget()
    )

    assert report["passed"] is False
    assert report["summary"] == {
        "total": 4,
        "killed": 0,
        "survived": 0,
        "inconclusive": 4,
        "mutationScorePercent": 0.0,
    }
    mutants = cast(list[dict[str, object]], report["mutants"])
    assert {item.get("reason") for item in mutants} == {
        "baseline-tests-failed"
    }


def test_mutation_campaign_treats_mutant_timeouts_as_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def alternate_timeout(
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls % 2 == 0:
            raise subprocess.TimeoutExpired(command, 30)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", alternate_timeout)

    report = check_mutation_coverage.run_mutation_budget(
        check_mutation_coverage.load_mutation_budget()
    )

    assert report["passed"] is False
    assert report["inconclusiveMutants"] == [
        "canonical-sha256-digest",
        "policy-explicit-deny-precedence",
        "compiler-graph-hash-binds-normalized-ir",
        "durable-source-replay-events",
    ]
    mutants = cast(list[dict[str, object]], report["mutants"])
    assert {item.get("reason") for item in mutants} == {"mutant-timeout"}


def test_mutation_budget_report_is_retained_by_canonical_ci_lane() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["python"]["steps"]
    mutation_step = next(
        step for step in steps if step.get("name") == "Run stable mutation budget"
    )
    diagnostics_step = next(
        step for step in steps if step.get("name") == "Retain Python CI diagnostics"
    )
    evidence_step = next(
        step for step in steps if step.get("name") == "Retain stable mutation evidence"
    )

    assert mutation_step["if"] == (
        "${{ matrix.os == 'ubuntu-latest' && matrix.python-version == '3.11' }}"
    )
    assert "tools/check_mutation_coverage.py" in mutation_step["run"]
    assert "dist/ci/stable-mutation-report.json" in mutation_step["run"]
    assert evidence_step["if"] == (
        "${{ always() && matrix.os == 'ubuntu-latest' && matrix.python-version == '3.11' }}"
    )
    assert evidence_step["with"] == {
        "name": "graphblocks-stable-mutation-evidence-${{ github.run_attempt }}",
        "path": "dist/ci/stable-mutation-report.json",
        "if-no-files-found": "error",
        "retention-days": 90,
    }
    assert diagnostics_step["with"]["path"] == "dist/ci"


def test_mutation_budget_manifest_digest_changes_with_semantics(tmp_path: Path) -> None:
    budget = check_mutation_coverage.load_mutation_budget()
    payload = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    changed = deepcopy(payload)
    changed["thresholds"]["maximumSurvivingMutants"] = 1
    candidate = tmp_path / "mutation-budget.yaml"
    candidate.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")

    changed_budget = check_mutation_coverage.load_mutation_budget(candidate)

    assert changed_budget.sha256 != budget.sha256

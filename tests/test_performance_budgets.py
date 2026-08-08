from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from tools import check_performance_budgets


ROOT = Path(__file__).parents[1]
BUDGET_PATH = ROOT / "compatibility" / "python-performance-budgets.yaml"
CHECKER = ROOT / "tools" / "check_performance_budgets.py"


def test_performance_budget_manifest_closes_the_seed_inventory() -> None:
    budgets = check_performance_budgets.load_performance_budgets()

    assert budgets.platform == "linux"
    assert budgets.python == "3.11"
    assert budgets.protocol.warmup_runs == 1
    assert budgets.protocol.measured_runs == 3
    assert budgets.protocol.statistic == "median"
    assert budgets.protocol.garbage_collection == ("collect-before-each-observation")
    assert budgets.companion_gates == (
        "compatibility/python-package-boundaries.yaml#coldImportBudgets",
    )
    assert tuple(item.benchmark_id for item in budgets.benchmarks) == (
        "canonical-decimal-scaling",
        "journal-append-scaling",
        "compiler-scaling",
        "server-retained-memory",
    )
    server_memory = budgets.benchmarks[-1]
    assert server_memory.metric == "retainedBytes"
    assert server_memory.warmup_runs == 0
    assert server_memory.measured_runs == 1


def test_performance_budget_cli_validates_and_lists_without_running_benchmarks() -> (
    None
):
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
    assert "4 benchmarks" in validated.stdout
    assert validated.stderr == ""
    assert listed.returncode == 0
    assert listed.stdout.splitlines() == [
        "canonical-decimal-scaling",
        "journal-append-scaling",
        "compiler-scaling",
        "server-retained-memory",
    ]


def test_performance_budget_evaluator_enforces_absolute_and_growth_caps() -> None:
    budget = check_performance_budgets.BenchmarkBudget(
        benchmark_id="canonical-decimal-scaling",
        metric="elapsedSeconds",
        sizes=(10, 100),
        maximum_by_size={10: 1.0, 100: 4.0},
        maximum_normalized_growth=2.0,
        warmup_runs=0,
        measured_runs=1,
    )

    assert (
        check_performance_budgets.evaluate_benchmark(
            budget,
            (
                {"size": 10, "value": 0.5},
                {"size": 100, "value": 4.0},
            ),
        )
        == ()
    )
    assert check_performance_budgets.evaluate_benchmark(
        budget,
        (
            {"size": 10, "value": 0.1},
            {"size": 100, "value": 3.0},
        ),
    ) == ("normalized growth 3.000000 exceeds 2.000000",)
    failures = check_performance_budgets.evaluate_benchmark(
        budget,
        (
            {"size": 10, "value": 1.5},
            {"size": 100, "value": 5.0},
        ),
    )
    assert failures[:2] == (
        "size 10 elapsedSeconds 1.500000000 exceeds 1.000000000",
        "size 100 elapsedSeconds 5.000000000 exceeds 4.000000000",
    )


def test_performance_budget_runner_uses_closed_operation_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgets = check_performance_budgets.load_performance_budgets()
    tick = {"value": 0.0}

    def fake_perf_counter() -> float:
        tick["value"] += 0.001
        return tick["value"]

    monkeypatch.setattr(check_performance_budgets, "perf_counter", fake_perf_counter)
    operations = {
        benchmark.benchmark_id: (
            (lambda size: size * 100)
            if benchmark.metric == "retainedBytes"
            else (lambda size: size)
        )
        for benchmark in budgets.benchmarks
    }

    report = check_performance_budgets.run_performance_budgets(
        budgets,
        operations=operations,
    )

    assert report["schemaVersion"] == 1
    assert report["budgetSha256"] == budgets.sha256
    assert len(report["benchmarks"]) == 4
    assert report["passed"] is True
    with pytest.raises(
        check_performance_budgets.PerformanceBudgetError,
        match="operation inventory",
    ):
        check_performance_budgets.run_performance_budgets(
            budgets,
            operations={"canonical-decimal-scaling": lambda size: size},
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda payload: payload.update({"unknown": True}), "unknown fields"),
        (
            lambda payload: payload["protocol"].update({"measuredRuns": True}),
            "positive integer",
        ),
        (
            lambda payload: payload["benchmarks"]["compiler-scaling"].update(
                {"sizes": [200, 50]}
            ),
            "strictly increasing",
        ),
        (
            lambda payload: payload["benchmarks"]["compiler-scaling"].update(
                {"metric": "requestsPerSecond"}
            ),
            "unsupported",
        ),
    ),
)
def test_performance_budget_decoder_fails_closed(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    payload = yaml.safe_load(BUDGET_PATH.read_text(encoding="utf-8"))
    mutation(payload)
    candidate = tmp_path / "performance.yaml"
    candidate.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        check_performance_budgets.PerformanceBudgetError,
        match=message,
    ):
        check_performance_budgets.load_performance_budgets(candidate)


def test_performance_budget_report_is_retained_by_canonical_ci_lane() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["python"]["steps"]
    performance_step = next(
        step
        for step in steps
        if step.get("name") == "Enforce deterministic Python performance budgets"
    )
    diagnostics_step = next(
        step for step in steps if step.get("name") == "Retain Python CI diagnostics"
    )

    assert performance_step["if"] == (
        "${{ matrix.os == 'ubuntu-latest' && matrix.python-version == '3.11' }}"
    )
    assert "tools/check_performance_budgets.py" in performance_step["run"]
    assert "dist/ci/python-performance-budgets.json" in performance_step["run"]
    assert diagnostics_step["with"]["path"] == "dist/ci"


def test_performance_budget_report_writer_emits_stable_json(tmp_path: Path) -> None:
    report_path = tmp_path / "nested" / "report.json"
    report = {"passed": True, "benchmarks": []}

    check_performance_budgets._write_report(report_path, report)

    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert report_path.read_text(encoding="utf-8").endswith("\n")

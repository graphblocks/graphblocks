from __future__ import annotations

from decimal import Decimal
import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_evaluation_package_exposes_gate_result_contract(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-evaluation" / "src"))
    graphblocks_evaluation = importlib.import_module("graphblocks_evaluation")

    subject = graphblocks_evaluation.ResourceSnapshotRef("candidate-1", "sha256:candidate")
    gate = graphblocks_evaluation.evaluate_gate(
        "quality",
        subject,
        metrics=[
            graphblocks_evaluation.MetricObservation(
                "accuracy",
                Decimal("0.91"),
                direction="maximize",
            )
        ],
        constraints=[graphblocks_evaluation.GateConstraint("accuracy", "at_least", Decimal("0.90"))],
    )
    trial = graphblocks_evaluation.TrialResult(
        "trial-1",
        base=graphblocks_evaluation.ResourceSnapshotRef("base-1", "sha256:base"),
        candidate=subject,
        gate=gate,
        outcome="accepted",
    )

    assert gate.decision == "pass"
    assert trial.gate == gate
    assert trial.outcome == "accepted"


def test_evaluation_package_exposes_slo_contract(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-evaluation" / "src"))
    graphblocks_evaluation = importlib.import_module("graphblocks_evaluation")

    objective = graphblocks_evaluation.SloObjective.at_most(
        "first-draft",
        "p95(turn_first_draft_ms)",
        1500.0,
        "30d",
    ).with_unit("ms")
    measurement = graphblocks_evaluation.SloMeasurement(
        "p95(turn_first_draft_ms)",
        1700.0,
        "30d",
    ).with_unit("ms")

    report = objective.evaluate(measurement)

    assert report.status == "fail"
    assert report.violated_by == 200.0
    assert "SloObjective" in graphblocks_evaluation.__all__
    assert "SloMeasurement" in graphblocks_evaluation.__all__
    assert "SloReport" in graphblocks_evaluation.__all__

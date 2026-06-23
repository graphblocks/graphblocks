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

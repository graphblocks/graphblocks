from __future__ import annotations

from decimal import Decimal

from graphblocks.diagnostics import Diagnostic
from graphblocks.documents import ArtifactRef
from graphblocks.evaluation import (
    CheckResult,
    GateConstraint,
    MetricObservation,
    ResourceSnapshotRef,
    ResultBundle,
    ReviewRecord,
    RunProvenance,
    TrialResult,
    evaluate_gate,
)
from graphblocks.policy import PrincipalRef


def test_evaluate_gate_fails_when_required_check_failed() -> None:
    subject = ResourceSnapshotRef("candidate-1", "sha256:candidate")
    checks = [
        CheckResult("lint", subject, "passed", tool={"processor_id": "lint", "version": "1"}),
        CheckResult(
            "formal",
            subject,
            "failed",
            diagnostics=[Diagnostic("GBE1001", "assertion failed")],
            tool={"processor_id": "formal", "version": "1"},
        ),
    ]

    gate = evaluate_gate("quality", subject, checks=checks, required_check_ids=["lint", "formal"])

    assert gate.decision == "fail"
    assert gate.check_ids == ["lint", "formal"]
    assert gate.violated_constraints == ["check:formal"]


def test_evaluate_gate_uses_metric_thresholds() -> None:
    subject = ResourceSnapshotRef("candidate-1", "sha256:candidate")
    metrics = [
        MetricObservation("accuracy", Decimal("0.91"), direction="maximize"),
        MetricObservation("latency_ms", Decimal("125"), unit="ms", direction="minimize"),
    ]

    passing = evaluate_gate(
        "quality",
        subject,
        metrics=metrics,
        constraints=[
            GateConstraint("accuracy", "at_least", Decimal("0.9")),
            GateConstraint("latency_ms", "at_most", Decimal("150")),
        ],
    )
    failing = evaluate_gate(
        "quality",
        subject,
        metrics=metrics,
        constraints=[GateConstraint("latency_ms", "at_most", Decimal("100"))],
    )

    assert passing.decision == "pass"
    assert failing.decision == "fail"
    assert failing.violated_constraints == ["metric:latency_ms"]


def test_review_record_is_invalid_for_changed_subject_digest() -> None:
    subject = ResourceSnapshotRef("candidate-1", "sha256:old")
    review = ReviewRecord(
        review_id="review-1",
        subject=subject,
        subject_digest="sha256:old",
        scope="quality",
        reviewer=PrincipalRef("reviewer-1"),
        decision="accept",
        created_at="2026-06-22T00:00:00Z",
    )

    assert review.is_valid_for(subject)
    assert not review.is_valid_for(ResourceSnapshotRef("candidate-1", "sha256:new"))
    assert not review.invalidate("2026-06-22T00:05:00Z").is_valid_for(subject)


def test_result_bundle_digest_is_stable_without_record_identity() -> None:
    subject = ResourceSnapshotRef("candidate-1", "sha256:candidate")
    gate = evaluate_gate("quality", subject, checks=[], metrics=[])
    provenance = RunProvenance(graph_hash="sha256:graph", started_at="2026-06-22T00:00:00Z")
    bundle = ResultBundle(
        bundle_id="bundle-1",
        run_id="run-1",
        release_id="release-1",
        inputs=[ResourceSnapshotRef("input-1", "sha256:input")],
        outputs=[],
        artifacts=[ArtifactRef("artifact-1", "file:///tmp/out.txt", checksum="sha256:out")],
        checks=[],
        metrics=[],
        evidence=[],
        reviews=[],
        usage_records=[],
        policy_decision_refs=["decision-1"],
        provenance=provenance,
    )
    same_payload = ResultBundle(
        bundle_id="bundle-2",
        run_id="run-1",
        release_id="release-1",
        inputs=list(bundle.inputs),
        outputs=[],
        artifacts=list(bundle.artifacts),
        checks=[],
        metrics=[],
        evidence=[],
        reviews=[],
        usage_records=[],
        policy_decision_refs=["decision-1"],
        provenance=provenance,
    )

    assert gate.subject == subject
    assert bundle.content_digest() == same_payload.content_digest()


def test_trial_result_carries_gate_and_outcome() -> None:
    base = ResourceSnapshotRef("base", "sha256:base")
    candidate = ResourceSnapshotRef("candidate", "sha256:candidate")
    gate = evaluate_gate("quality", candidate, checks=[], metrics=[])

    trial = TrialResult("trial-1", base=base, candidate=candidate, gate=gate, outcome="accepted")

    assert trial.gate == gate
    assert trial.outcome == "accepted"

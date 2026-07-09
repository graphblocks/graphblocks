from __future__ import annotations

from decimal import Decimal

import pytest

from graphblocks.diagnostics import Diagnostic
from graphblocks.documents import ArtifactRef
from graphblocks.evaluation import (
    CheckResult,
    ChangeSet,
    EvidenceRef,
    GateConstraint,
    GateResult,
    MetricObservation,
    ModelVisibleToolRef,
    ResourceSnapshotRef,
    ResultBundle,
    ReviewRecord,
    RunProvenance,
    SloMeasurement,
    SloObjective,
    SloReport,
    TypedValueRef,
    TrialResult,
    evaluate_gate,
)
from graphblocks.policy import PrincipalRef
from graphblocks.tools import (
    BlockToolImplementation,
    ToolBinding,
    ToolCatalog,
    ToolDefinition,
    ToolResolutionScope,
)


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


def test_check_result_validates_status_subject_and_copies_collections() -> None:
    subject = ResourceSnapshotRef("candidate-1", "sha256:candidate")
    diagnostic = Diagnostic("GBE1001", "assertion failed")
    evidence = EvidenceRef("evidence-1", subject, "log")
    artifact = ArtifactRef("artifact-1", "file:///tmp/out.txt", checksum="sha256:out")
    diagnostics = [diagnostic]
    evidence_refs = [evidence]
    artifacts = [artifact]
    tool = {"processor_id": "lint"}
    check = CheckResult(
        "lint",
        subject,
        "passed",
        diagnostics=diagnostics,
        evidence=evidence_refs,
        artifacts=artifacts,
        tool=tool,
    )
    diagnostics.append(Diagnostic("GBE1002", "mutated"))
    evidence_refs.append(EvidenceRef("evidence-2", subject, "log"))
    artifacts.append(ArtifactRef("artifact-2", "file:///tmp/other.txt"))
    tool["processor_id"] = "mutated"

    assert check.diagnostics == [diagnostic]
    assert check.evidence == [evidence]
    assert check.artifacts == [artifact]
    assert check.tool == {"processor_id": "lint"}
    with pytest.raises(ValueError, match="check result check_id must not be empty"):
        CheckResult(" ", subject, "passed")
    with pytest.raises(ValueError, match="check result subject must be a ResourceSnapshotRef"):
        CheckResult("lint", object(), "passed")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid check status maybe"):
        CheckResult("lint", subject, "maybe")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="check result diagnostics items must be Diagnostic"):
        CheckResult("lint", subject, "passed", diagnostics=[object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="check result evidence items must be EvidenceRef"):
        CheckResult("lint", subject, "passed", evidence=[object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="check result artifacts items must be ArtifactRef"):
        CheckResult("lint", subject, "passed", artifacts=[object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="check result tool must be a mapping"):
        CheckResult("lint", subject, "passed", tool=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="check result environment must be a ResourceSnapshotRef"):
        CheckResult("lint", subject, "passed", environment=object())  # type: ignore[arg-type]


def test_evidence_ref_validates_identity_source_kind_and_metadata() -> None:
    subject = ResourceSnapshotRef("candidate-1", "sha256:candidate")
    metadata = {"path": "logs/test.txt"}
    evidence = EvidenceRef("evidence-1", subject, "log", metadata=metadata)
    metadata["path"] = "mutated"

    assert evidence.metadata == {"path": "logs/test.txt"}
    with pytest.raises(ValueError, match="evidence ref evidence_id must not be empty"):
        EvidenceRef(" ", subject, "log")
    with pytest.raises(ValueError, match="evidence ref source must be a ResourceSnapshotRef or ArtifactRef"):
        EvidenceRef("evidence-1", object(), "log")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="evidence ref evidence_kind must not be empty"):
        EvidenceRef("evidence-1", subject, " ")
    with pytest.raises(ValueError, match="evidence ref metadata must be a mapping"):
        EvidenceRef("evidence-1", subject, "log", metadata=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="evidence ref metadata keys must be strings"):
        EvidenceRef("evidence-1", subject, "log", metadata={object(): "value"})  # type: ignore[dict-item]


def test_typed_value_ref_validates_identity_schema_digest_and_artifact() -> None:
    artifact = ArtifactRef("artifact-1", "file:///tmp/value.json", checksum="sha256:value")
    value_ref = TypedValueRef("value-1", "schemas/Answer@1", 1, "sha256:value", artifact=artifact)

    assert value_ref.artifact == artifact
    with pytest.raises(ValueError, match="typed value ref value_id must not be empty"):
        TypedValueRef(" ", "schemas/Answer@1", 1, "sha256:value")
    with pytest.raises(ValueError, match="typed value ref schema_id must not be empty"):
        TypedValueRef("value-1", " ", 1, "sha256:value")
    with pytest.raises(ValueError, match="typed value ref schema_version must be an integer"):
        TypedValueRef("value-1", "schemas/Answer@1", True, "sha256:value")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="typed value ref schema_version must be positive"):
        TypedValueRef("value-1", "schemas/Answer@1", 0, "sha256:value")
    with pytest.raises(ValueError, match="typed value ref digest must not be empty"):
        TypedValueRef("value-1", "schemas/Answer@1", 1, " ")
    with pytest.raises(ValueError, match="typed value ref encoding must not be empty"):
        TypedValueRef("value-1", "schemas/Answer@1", 1, "sha256:value", encoding=" ")
    with pytest.raises(ValueError, match="typed value ref artifact must be an ArtifactRef"):
        TypedValueRef("value-1", "schemas/Answer@1", 1, "sha256:value", artifact=object())  # type: ignore[arg-type]


def test_metric_observation_validates_identity_direction_and_copies_evaluator() -> None:
    evaluator = {"processor_id": "metric"}
    metric = MetricObservation("latency_ms", 12.5, unit="ms", direction="minimize", evaluator=evaluator)
    evaluator["processor_id"] = "mutated"

    assert metric.value == Decimal("12.5")
    assert metric.evaluator == {"processor_id": "metric"}
    with pytest.raises(ValueError, match="metric observation name must not be empty"):
        MetricObservation(" ", Decimal("1"))
    with pytest.raises(ValueError, match="metric observation unit must not be empty"):
        MetricObservation("latency_ms", Decimal("1"), unit=" ")
    with pytest.raises(ValueError, match="invalid metric direction sideways"):
        MetricObservation("latency_ms", Decimal("1"), direction="sideways")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="metric observation subject must be a ResourceSnapshotRef"):
        MetricObservation("latency_ms", Decimal("1"), subject=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="metric observation evaluator must be a mapping"):
        MetricObservation("latency_ms", Decimal("1"), evaluator=object())  # type: ignore[arg-type]


def test_gate_constraint_and_result_validate_literals_and_copy_lists() -> None:
    subject = ResourceSnapshotRef("candidate-1", "sha256:candidate")
    metric = MetricObservation("accuracy", Decimal("0.91"), direction="maximize")
    check_ids = ["lint"]
    violated = ["metric:accuracy"]
    metrics = [metric]
    gate = GateResult("quality", subject, "fail", check_ids=check_ids, violated_constraints=violated, metrics=metrics)
    check_ids.append("mutated")
    violated.append("mutated")
    metrics.append(MetricObservation("latency_ms", Decimal("125")))

    assert gate.check_ids == ["lint"]
    assert gate.violated_constraints == ["metric:accuracy"]
    assert gate.metrics == [metric]
    assert GateConstraint("is_safe", "equals", True).threshold is True
    assert GateConstraint("latency_ms", "at_most", 150).threshold == Decimal("150")
    with pytest.raises(ValueError, match="gate constraint metric_name must not be empty"):
        GateConstraint(" ", "equals", True)
    with pytest.raises(ValueError, match="invalid gate constraint operator around"):
        GateConstraint("latency_ms", "around", 150)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="gate result gate_id must not be empty"):
        GateResult(" ", subject, "pass")
    with pytest.raises(ValueError, match="gate result subject must be a ResourceSnapshotRef"):
        GateResult("quality", object(), "pass")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid gate decision maybe"):
        GateResult("quality", subject, "maybe")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="gate result check_ids must be a collection of strings"):
        GateResult("quality", subject, "pass", check_ids="lint")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="gate result violated_constraints item must not be empty"):
        GateResult("quality", subject, "fail", violated_constraints=[" "])
    with pytest.raises(ValueError, match="gate result metrics items must be MetricObservation"):
        GateResult("quality", subject, "pass", metrics=[object()])  # type: ignore[list-item]


def test_slo_objective_passes_when_ratio_meets_objective() -> None:
    objective = SloObjective.at_least(
        "chat-availability",
        "successful_committed_turns / admitted_turns",
        0.995,
        "30d",
    )
    measurement = SloMeasurement(
        "successful_committed_turns / admitted_turns",
        0.996,
        "30d",
    ).with_sample_count(10_000)

    report = objective.evaluate(measurement)

    assert report.status == "pass"
    assert report.slo_id == "chat-availability"
    assert report.observed_value == 0.996
    assert report.violated_by is None


def test_slo_objective_fails_when_latency_exceeds_maximum() -> None:
    objective = SloObjective.at_most("first-draft", "p95(turn_first_draft_ms)", 1500.0, "30d").with_unit("ms")
    measurement = SloMeasurement("p95(turn_first_draft_ms)", 1700.0, "30d").with_unit("ms").with_sample_count(500)

    report = objective.evaluate(measurement)

    assert report.status == "fail"
    assert report.observed_value == 1700.0
    assert report.violated_by == 200.0


def test_slo_objective_is_no_data_for_mismatched_indicator_or_window() -> None:
    objective = SloObjective.at_least("citation-validity", "validated / returned", 0.99, "30d")
    measurement = SloMeasurement("validated / returned", 0.995, "7d")

    report = objective.evaluate(measurement)

    assert report.status == "no_data"
    assert report.observed_value is None
    assert report.reason == "window_mismatch"


def test_slo_records_validate_identity_literals_and_finite_values() -> None:
    with pytest.raises(ValueError, match="SLO objective slo_id must not be empty"):
        SloObjective(" ", "availability", "at_least", 0.99, "30d")
    with pytest.raises(ValueError, match="SLO objective indicator must not be empty"):
        SloObjective("slo-1", " ", "at_least", 0.99, "30d")
    with pytest.raises(ValueError, match="SLO objective window must not be empty"):
        SloObjective("slo-1", "availability", "at_least", 0.99, " ")
    with pytest.raises(ValueError, match="SLO objective unit must not be empty"):
        SloObjective("slo-1", "latency", "at_most", 1500, "30d", unit=" ")
    with pytest.raises(ValueError, match="unsupported SLO comparison 'around'"):
        SloObjective("slo-1", "latency", "around", 1500, "30d")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="SLO objective objective must be numeric"):
        SloObjective("slo-1", "latency", "at_most", True, "30d")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="SLO objective objective must be finite"):
        SloObjective("slo-1", "latency", "at_most", float("inf"), "30d")

    with pytest.raises(ValueError, match="SLO measurement indicator must not be empty"):
        SloMeasurement(" ", 1.0, "30d")
    with pytest.raises(ValueError, match="SLO measurement window must not be empty"):
        SloMeasurement("availability", 1.0, " ")
    with pytest.raises(ValueError, match="SLO measurement unit must not be empty"):
        SloMeasurement("latency", 1.0, "30d", unit=" ")
    with pytest.raises(ValueError, match="SLO measurement value must be finite"):
        SloMeasurement("availability", float("nan"), "30d")
    with pytest.raises(ValueError, match="SLO sample_count must be an integer"):
        SloMeasurement("availability", 1.0, "30d", sample_count=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="SLO sample_count must be non-negative"):
        SloMeasurement("availability", 1.0, "30d", sample_count=-1)

    with pytest.raises(ValueError, match="SLO report slo_id must not be empty"):
        SloReport(" ", "availability", "30d", "pass", 0.99)
    with pytest.raises(ValueError, match="SLO report indicator must not be empty"):
        SloReport("slo-1", " ", "30d", "pass", 0.99)
    with pytest.raises(ValueError, match="SLO report window must not be empty"):
        SloReport("slo-1", "availability", " ", "fail", 0.99)
    with pytest.raises(ValueError, match="invalid SLO report status unknown"):
        SloReport("slo-1", "availability", "30d", "unknown", 0.99)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="SLO report objective must be finite"):
        SloReport("slo-1", "availability", "30d", "pass", float("inf"))
    with pytest.raises(ValueError, match="SLO report observed_value must be finite"):
        SloReport("slo-1", "availability", "30d", "pass", 0.99, observed_value=float("nan"))
    with pytest.raises(ValueError, match="SLO report sample_count must be an integer"):
        SloReport("slo-1", "availability", "30d", "pass", 0.99, sample_count=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="SLO report sample_count must be non-negative"):
        SloReport("slo-1", "availability", "30d", "pass", 0.99, sample_count=-1)
    with pytest.raises(ValueError, match="SLO report violated_by must be finite"):
        SloReport("slo-1", "availability", "30d", "fail", 0.99, violated_by=float("inf"))
    with pytest.raises(ValueError, match="SLO report reason must not be empty"):
        SloReport("slo-1", "availability", "30d", "no_data", 0.99, reason=" ")
    no_data_placeholder = SloReport("slo-1", "", "", "no_data", 0.0)

    assert no_data_placeholder.status == "no_data"


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


def test_review_record_validates_identity_decision_timestamps_and_lists() -> None:
    subject = ResourceSnapshotRef("candidate-1", "sha256:old")
    base = {
        "review_id": "review-1",
        "subject": subject,
        "subject_digest": "sha256:old",
        "scope": "quality",
        "reviewer": PrincipalRef("reviewer-1"),
        "decision": "accept",
        "created_at": "2026-06-22T00:00:00Z",
    }
    cases = [
        ({"review_id": " "}, "review record review_id must not be empty"),
        ({"subject": object()}, "review record subject must be a ResourceSnapshotRef"),
        ({"subject_digest": " "}, "review record subject_digest must not be empty"),
        ({"scope": " "}, "review record scope must not be empty"),
        ({"reviewer": object()}, "review record reviewer must be a PrincipalRef"),
        ({"decision": "maybe"}, "invalid review decision maybe"),
        ({"created_at": "later"}, "review record created_at must be an ISO datetime"),
        ({"invalidated_at": "later"}, "review record invalidated_at must be an ISO datetime"),
        (
            {"invalidated_at": "2026-06-21T23:59:59Z"},
            "review record invalidated_at must not be before created_at",
        ),
        ({"comments": "comment"}, "review record comments must be a collection of strings"),
        ({"comments": ["ok", object()]}, "review record comments items must be strings"),
        ({"comments": ["ok", " "]}, "review record comments item must not be empty"),
        ({"credential_refs": "cred"}, "review record credential_refs must be a collection of strings"),
        ({"credential_refs": ["cred-1", object()]}, "review record credential_refs items must be strings"),
        ({"credential_refs": ["cred-1", " "]}, "review record credential_refs item must not be empty"),
    ]

    for overrides, message in cases:
        with pytest.raises(ValueError, match=message):
            ReviewRecord(**(base | overrides))  # type: ignore[arg-type]

    comments = ["looks good"]
    credential_refs = ["cred-1"]
    review = ReviewRecord(**(base | {"comments": comments, "credential_refs": credential_refs}))
    comments.append("mutated")
    credential_refs.append("cred-2")

    assert review.comments == ["looks good"]
    assert review.credential_refs == ["cred-1"]


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    (
        (
            {"review_id": " review-1"},
            "review record review_id must not contain surrounding whitespace",
        ),
        (
            {"subject_digest": " sha256:old"},
            "review record subject_digest must not contain surrounding whitespace",
        ),
        (
            {"scope": " quality"},
            "review record scope must not contain surrounding whitespace",
        ),
        (
            {"credential_refs": [" cred-1"]},
            "review record credential_refs item must not contain surrounding whitespace",
        ),
    ),
)
def test_review_record_rejects_whitespace_wrapped_identities(
    overrides: dict[str, object],
    expected_error: str,
) -> None:
    subject = ResourceSnapshotRef("candidate-1", "sha256:old")
    base = {
        "review_id": "review-1",
        "subject": subject,
        "subject_digest": "sha256:old",
        "scope": "quality",
        "reviewer": PrincipalRef("reviewer-1"),
        "decision": "accept",
        "created_at": "2026-06-22T00:00:00Z",
    }

    with pytest.raises(ValueError, match=expected_error):
        ReviewRecord(**(base | overrides))  # type: ignore[arg-type]


def test_evaluation_timestamps_reject_non_rfc3339_forms() -> None:
    subject = ResourceSnapshotRef("candidate-1", "sha256:old")
    review_base = {
        "review_id": "review-1",
        "subject": subject,
        "subject_digest": "sha256:old",
        "scope": "quality",
        "reviewer": PrincipalRef("reviewer-1"),
        "decision": "accept",
        "created_at": "2026-06-22T00:00:00Z",
    }
    tool_ref_base = {
        "tool_name": "knowledge.search",
        "resolved_tool_id": "resolved-search",
        "definition_digest": "sha256:def",
        "binding_digest": "sha256:binding",
        "effective_policy_snapshot_id": "policy-snapshot-1",
        "allowed_for_principal": True,
    }

    invalid_review_cases = [
        ({"created_at": "2026-06-22 00:00:00Z"}, "review record created_at must be an ISO datetime"),
        ({"created_at": "2026-06-22T00:00:00"}, "review record created_at must be an ISO datetime"),
        ({"created_at": "2026-06-22T00:00:00+0000"}, "review record created_at must be an ISO datetime"),
        ({"created_at": "2026-06-22T00:00:00z"}, "review record created_at must be an ISO datetime"),
        ({"created_at": " 2026-06-22T00:00:00Z"}, "review record created_at must be an ISO datetime"),
        ({"invalidated_at": "2026-06-22T00:05:00+0000"}, "review record invalidated_at must be an ISO datetime"),
    ]
    for overrides, message in invalid_review_cases:
        with pytest.raises(ValueError, match=message):
            ReviewRecord(**(review_base | overrides))  # type: ignore[arg-type]

    for valid_until in (
        "2026-06-22 00:00:00Z",
        "2026-06-22T00:00:00",
        "2026-06-22T00:00:00+0000",
        "2026-06-22T00:00:00z",
        "2026-06-22T00:00:00Z ",
    ):
        with pytest.raises(ValueError, match="model visible tool ref valid_until must be an ISO datetime"):
            ModelVisibleToolRef(**(tool_ref_base | {"valid_until": valid_until}))  # type: ignore[arg-type]


def test_resource_snapshot_ref_validates_identity_fields_and_copies_metadata() -> None:
    metadata = {"path": "candidate/out.sv"}
    snapshot = ResourceSnapshotRef(
        "candidate-1",
        "sha256:candidate",
        resource_kind="file",
        uri="file:///tmp/candidate",
        metadata=metadata,
    )
    metadata["path"] = "mutated"

    assert snapshot.metadata == {"path": "candidate/out.sv"}
    with pytest.raises(ValueError, match="resource snapshot resource_id must not be empty"):
        ResourceSnapshotRef(" ", "sha256:candidate")
    with pytest.raises(ValueError, match="resource snapshot digest must be a string"):
        ResourceSnapshotRef("candidate-1", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="resource snapshot resource_kind must not be empty"):
        ResourceSnapshotRef("candidate-1", "sha256:candidate", resource_kind=" ")
    with pytest.raises(ValueError, match="resource snapshot metadata must be a mapping"):
        ResourceSnapshotRef("candidate-1", "sha256:candidate", metadata=object())  # type: ignore[arg-type]


def test_change_set_freezes_operation_mappings_at_construction() -> None:
    operation = {"op": "file.write", "resource_id": "a.txt"}
    operations = [operation]
    change_set = ChangeSet(
        change_set_id="change-1",
        base=ResourceSnapshotRef("base", "sha256:base"),
        candidate=ResourceSnapshotRef("candidate", "sha256:candidate"),
        operations=operations,
    )

    operation["resource_id"] = "mutated.txt"
    operations.append({"op": "file.delete", "resource_id": "b.txt"})

    assert change_set.operations == ({"op": "file.write", "resource_id": "a.txt"},)
    with pytest.raises(AttributeError):
        change_set.operations.append({"op": "file.delete"})


def test_change_set_rejects_non_mapping_operations() -> None:
    with pytest.raises(ValueError, match="change set operations must be mappings"):
        ChangeSet(
            change_set_id="change-1",
            base=ResourceSnapshotRef("base", "sha256:base"),
            candidate=ResourceSnapshotRef("candidate", "sha256:candidate"),
            operations=["file.write"],  # type: ignore[list-item]
        )


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


def test_result_bundle_digest_includes_release_plan_and_signature_provenance() -> None:
    base = (
        RunProvenance(graph_hash="sha256:graph", started_at="2026-06-22T00:00:00Z")
        .with_release("release-1", "rev-1")
        .with_physical_plan_hash("sha256:plan-1")
        .with_release_signature_digest("sha256:signature-1")
    )
    changed_signature = (
        RunProvenance(graph_hash="sha256:graph", started_at="2026-06-22T00:00:00Z")
        .with_release("release-1", "rev-1")
        .with_physical_plan_hash("sha256:plan-1")
        .with_release_signature_digest("sha256:signature-2")
    )
    changed_plan = (
        RunProvenance(graph_hash="sha256:graph", started_at="2026-06-22T00:00:00Z")
        .with_release("release-1", "rev-1")
        .with_physical_plan_hash("sha256:plan-2")
        .with_release_signature_digest("sha256:signature-1")
    )

    base_digest = ResultBundle(
        bundle_id="bundle-1",
        run_id="run-1",
        release_id="release-1",
        inputs=[],
        outputs=[],
        provenance=base,
    ).content_digest()
    changed_signature_digest = ResultBundle(
        bundle_id="bundle-2",
        run_id="run-1",
        release_id="release-1",
        inputs=[],
        outputs=[],
        provenance=changed_signature,
    ).content_digest()
    changed_plan_digest = ResultBundle(
        bundle_id="bundle-3",
        run_id="run-1",
        release_id="release-1",
        inputs=[],
        outputs=[],
        provenance=changed_plan,
    ).content_digest()

    assert base_digest != changed_signature_digest
    assert base_digest != changed_plan_digest


def test_result_bundle_digest_records_model_visible_tool_set_deterministically() -> None:
    catalog = ToolCatalog(
        definitions=(
            ToolDefinition("knowledge.search", "Search support articles.", "schemas/Search@1"),
            ToolDefinition("ticket.create", "Create a ticket.", "schemas/Ticket@1"),
        ),
        bindings=(
            ToolBinding(
                "binding-search",
                "knowledge.search",
                BlockToolImplementation("blocks.search"),
            ),
            ToolBinding(
                "binding-ticket",
                "ticket.create",
                BlockToolImplementation("blocks.ticket.create"),
            ),
        ),
    )
    resolved = catalog.resolve(ToolResolutionScope(), effective_policy_snapshot_id="policy-snapshot-1")
    stable = RunProvenance(
        graph_hash="sha256:graph",
        started_at="2026-06-22T00:00:00Z",
    ).with_model_visible_tools(resolved)
    same_tools_reversed = RunProvenance(
        graph_hash="sha256:graph",
        started_at="2026-06-22T00:00:00Z",
    ).with_model_visible_tools(reversed(resolved))
    missing_tool = RunProvenance(
        graph_hash="sha256:graph",
        started_at="2026-06-22T00:00:00Z",
    ).with_model_visible_tools(resolved[:1])

    assert len(stable.model_visible_tools) == 2
    assert stable.model_visible_tools[0].tool_name == "knowledge.search"
    assert stable.model_visible_tools[0].definition_digest == resolved[0].definition_digest
    assert ResultBundle(
        bundle_id="bundle-1",
        run_id="run-1",
        release_id="release-1",
        inputs=[],
        outputs=[],
        provenance=stable,
    ).content_digest() == ResultBundle(
        bundle_id="bundle-2",
        run_id="run-1",
        release_id="release-1",
        inputs=[],
        outputs=[],
        provenance=same_tools_reversed,
    ).content_digest()
    assert ResultBundle(
        bundle_id="bundle-3",
        run_id="run-1",
        release_id="release-1",
        inputs=[],
        outputs=[],
        provenance=missing_tool,
    ).content_digest() != ResultBundle(
        bundle_id="bundle-4",
        run_id="run-1",
        release_id="release-1",
        inputs=[],
        outputs=[],
        provenance=RunProvenance(
            graph_hash="sha256:graph",
            started_at="2026-06-22T00:00:00Z",
        ).with_model_visible_tools(resolved),
    ).content_digest()


def test_model_visible_tool_ref_validates_identity_boolean_and_expiration() -> None:
    tool_ref = ModelVisibleToolRef(
        "knowledge.search",
        "resolved-search",
        "sha256:def",
        "sha256:binding",
        "policy-snapshot-1",
        True,
        valid_until="2026-06-22T00:00:00Z",
    )

    assert tool_ref.allowed_for_principal is True
    base = {
        "tool_name": "knowledge.search",
        "resolved_tool_id": "resolved-search",
        "definition_digest": "sha256:def",
        "binding_digest": "sha256:binding",
        "effective_policy_snapshot_id": "policy-snapshot-1",
        "allowed_for_principal": True,
    }
    cases = [
        ({"tool_name": " "}, "model visible tool ref tool_name must not be empty"),
        ({"resolved_tool_id": " "}, "model visible tool ref resolved_tool_id must not be empty"),
        ({"definition_digest": " "}, "model visible tool ref definition_digest must not be empty"),
        ({"binding_digest": " "}, "model visible tool ref binding_digest must not be empty"),
        (
            {"effective_policy_snapshot_id": " "},
            "model visible tool ref effective_policy_snapshot_id must not be empty",
        ),
        ({"allowed_for_principal": "yes"}, "model visible tool ref allowed_for_principal must be a boolean"),
        ({"valid_until": "later"}, "model visible tool ref valid_until must be an ISO datetime"),
    ]

    for overrides, message in cases:
        with pytest.raises(ValueError, match=message):
            ModelVisibleToolRef(**(base | overrides))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    (
        (
            {"tool_name": " knowledge.search"},
            "model visible tool ref tool_name must not contain surrounding whitespace",
        ),
        (
            {"resolved_tool_id": " resolved-search"},
            "model visible tool ref resolved_tool_id must not contain surrounding whitespace",
        ),
        (
            {"definition_digest": " sha256:def"},
            "model visible tool ref definition_digest must not contain surrounding whitespace",
        ),
        (
            {"binding_digest": " sha256:binding"},
            "model visible tool ref binding_digest must not contain surrounding whitespace",
        ),
        (
            {"effective_policy_snapshot_id": " policy-snapshot-1"},
            "model visible tool ref effective_policy_snapshot_id must not contain surrounding whitespace",
        ),
    ),
)
def test_model_visible_tool_ref_rejects_whitespace_wrapped_identities(
    overrides: dict[str, object],
    expected_error: str,
) -> None:
    base = {
        "tool_name": "knowledge.search",
        "resolved_tool_id": "resolved-search",
        "definition_digest": "sha256:def",
        "binding_digest": "sha256:binding",
        "effective_policy_snapshot_id": "policy-snapshot-1",
        "allowed_for_principal": True,
    }

    with pytest.raises(ValueError, match=expected_error):
        ModelVisibleToolRef(**(base | overrides))  # type: ignore[arg-type]


def test_run_provenance_validates_model_visible_tools_and_copies_metadata() -> None:
    tool_ref = ModelVisibleToolRef(
        "knowledge.search",
        "resolved-search",
        "sha256:def",
        "sha256:binding",
        "policy-snapshot-1",
        True,
    )
    runner = {"worker": "worker-1"}
    metadata = {"trace": "trace-1"}
    provenance = RunProvenance(
        graph_hash="sha256:graph",
        started_at="2026-06-22T00:00:00Z",
        model_visible_tools=[tool_ref],  # type: ignore[arg-type]
        runner=runner,
        metadata=metadata,
    )
    runner["worker"] = "mutated"
    metadata["trace"] = "mutated"

    assert provenance.model_visible_tools == (tool_ref,)
    assert provenance.runner == {"worker": "worker-1"}
    assert provenance.metadata == {"trace": "trace-1"}
    with pytest.raises(ValueError, match="run provenance model_visible_tools must be a collection"):
        RunProvenance("sha256:graph", "2026-06-22T00:00:00Z", model_visible_tools=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="run provenance model_visible_tools items must be ModelVisibleToolRef"):
        RunProvenance("sha256:graph", "2026-06-22T00:00:00Z", model_visible_tools=[object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="run provenance runner must be a mapping"):
        RunProvenance("sha256:graph", "2026-06-22T00:00:00Z", runner=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="run provenance metadata keys must be strings"):
        RunProvenance(
            "sha256:graph",
            "2026-06-22T00:00:00Z",
            metadata={object(): "trace"},
        )  # type: ignore[dict-item]


def test_trial_result_carries_gate_and_outcome() -> None:
    base = ResourceSnapshotRef("base", "sha256:base")
    candidate = ResourceSnapshotRef("candidate", "sha256:candidate")
    gate = evaluate_gate("quality", candidate, checks=[], metrics=[])

    trial = TrialResult("trial-1", base=base, candidate=candidate, gate=gate, outcome="accepted")

    assert trial.gate == gate
    assert trial.outcome == "accepted"


def test_trial_result_validates_nested_records_and_copies_collections() -> None:
    base = ResourceSnapshotRef("base", "sha256:base")
    candidate = ResourceSnapshotRef("candidate", "sha256:candidate")
    checks = [CheckResult("lint", candidate, "passed")]
    metrics = [MetricObservation("accuracy", Decimal("0.91"))]
    usage = ["usage-1"]

    trial = TrialResult("trial-1", base=base, candidate=candidate, checks=checks, metrics=metrics, usage=usage)
    checks.append(CheckResult("tests", candidate, "passed"))
    metrics.append(MetricObservation("latency_ms", Decimal("125")))
    usage.append("usage-2")

    assert trial.checks == (CheckResult("lint", candidate, "passed"),)
    assert trial.metrics == (MetricObservation("accuracy", Decimal("0.91")),)
    assert trial.usage == ("usage-1",)
    with pytest.raises(ValueError, match="trial result base must be a ResourceSnapshotRef"):
        TrialResult("trial-1", base=object(), candidate=candidate)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="trial result checks items must be CheckResult"):
        TrialResult("trial-1", base=base, candidate=candidate, checks=[object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="trial result usage item must not be empty"):
        TrialResult("trial-1", base=base, candidate=candidate, usage=[" "])


def test_result_bundle_validates_nested_records_and_copies_collections() -> None:
    subject = ResourceSnapshotRef("candidate-1", "sha256:candidate")
    output = TypedValueRef("value-1", "schemas/Value", 1, "sha256:value")
    artifact = ArtifactRef("artifact-1", "file:///tmp/out.txt")
    diagnostic = Diagnostic("GBE1001", "assertion failed")
    check = CheckResult("lint", subject, "passed")
    metric = MetricObservation("accuracy", Decimal("0.91"))
    evidence = EvidenceRef("evidence-1", subject, "snapshot")
    review = ReviewRecord(
        "review-1",
        subject,
        "sha256:candidate",
        "quality",
        PrincipalRef("user", "reviewer-1"),
        "accept",
        created_at="2026-06-22T00:00:00Z",
    )
    inputs = [subject]
    outputs = [output]
    usage_records = ["usage-1"]

    bundle = ResultBundle(
        "bundle-1",
        "run-1",
        "release-1",
        inputs=inputs,
        outputs=outputs,
        artifacts=[artifact],
        diagnostics=[diagnostic],
        checks=[check],
        metrics=[metric],
        evidence=[evidence],
        reviews=[review],
        usage_records=usage_records,
        policy_decision_refs=["decision-1"],
    )
    inputs.append(ResourceSnapshotRef("input-2", "sha256:input-2"))
    outputs.append(TypedValueRef("value-2", "schemas/Value", 1, "sha256:value-2"))
    usage_records.append("usage-2")

    assert bundle.inputs == (subject,)
    assert bundle.outputs == (output,)
    assert bundle.artifacts == (artifact,)
    assert bundle.diagnostics == (diagnostic,)
    assert bundle.checks == (check,)
    assert bundle.metrics == (metric,)
    assert bundle.evidence == (evidence,)
    assert bundle.reviews == (review,)
    assert bundle.usage_records == ("usage-1",)
    assert bundle.policy_decision_refs == ("decision-1",)
    with pytest.raises(ValueError, match="result bundle bundle_id must not be empty"):
        ResultBundle(" ", "run-1", "release-1", inputs=[], outputs=[])
    with pytest.raises(ValueError, match="result bundle inputs items must be ResourceSnapshotRef"):
        ResultBundle("bundle-1", "run-1", "release-1", inputs=[object()], outputs=[])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="result bundle provenance must be a RunProvenance"):
        ResultBundle("bundle-1", "run-1", "release-1", inputs=[], outputs=[], provenance=object())  # type: ignore[arg-type]

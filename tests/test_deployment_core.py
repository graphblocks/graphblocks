from __future__ import annotations

from dataclasses import replace

import pytest

from graphblocks.deployment import (
    CanaryMetricThreshold,
    DeploymentCondition,
    DeploymentEvent,
    DeploymentEventKind,
    DeploymentObservabilityContext,
    DeploymentRecoveryProfile,
    DeploymentRevision,
    DeploymentSloProfile,
    ExecutionTarget,
    GraphDeployment,
    GraphDeploymentError,
    GraphRelease,
    GraphReleaseError,
    GraphReleaseGraph,
    GraphReleaseMutableReferencesError,
    ImageRef,
    KnowledgeBinding,
    PhysicalExecutionPlan,
    PlacementAmbiguousError,
    PlacementRule,
    PlacementSelector,
    PromptLock,
    ReleaseAttestation,
    ReleaseLockRef,
    SupplyChainLock,
    ReleaseBundle,
    RevisionDecision,
    RecoveryObjective,
    RolloutAnalysisResult,
    RolloutError,
    RolloutPlan,
    RolloutStep,
    UpgradePolicy,
    evaluate_canary_metrics,
    evaluate_rollback_and_drain,
    verify_release_attestation,
)
from graphblocks.evaluation import SloMeasurement, SloObjective


def test_deployment_revision_digest_is_stable_without_record_identity() -> None:
    left = DeploymentRevision(
        revision_id="rev-1",
        release_digest="sha256:release",
        deployment_spec_hash="sha256:deployment",
        physical_plan_hash="sha256:physical",
        resolved_binding_hash="sha256:binding",
        target_capability_hash="sha256:target",
        created_at="2026-06-23T00:00:00Z",
    )
    right = DeploymentRevision(
        revision_id="rev-2",
        release_digest="sha256:release",
        deployment_spec_hash="sha256:deployment",
        physical_plan_hash="sha256:physical",
        resolved_binding_hash="sha256:binding",
        target_capability_hash="sha256:target",
        created_at="2026-06-23T00:01:00Z",
    )

    assert left.content_digest() == right.content_digest()


def test_graph_release_digest_is_stable_for_artifact_order() -> None:
    left = (
        GraphRelease(name="enterprise-rag", version="2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.v1")
        .with_application_hash("sha256:app")
        .with_graph("chat", GraphReleaseGraph("sha256:graph-chat", "sha256:plan-chat"))
        .with_graph("ingest", GraphReleaseGraph("sha256:graph-ingest", "sha256:plan-ingest"))
        .with_image("worker", ImageRef("registry.example.com/gb/worker@sha256:abc"))
        .with_prompt_lock("answer", PromptLock.versioned("support.answer", "2026-06-23"))
    )
    right = (
        GraphRelease(name="enterprise-rag", version="2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.v1")
        .with_application_hash("sha256:app")
        .with_graph("ingest", GraphReleaseGraph("sha256:graph-ingest", "sha256:plan-ingest"))
        .with_graph("chat", GraphReleaseGraph("sha256:graph-chat", "sha256:plan-chat"))
        .with_prompt_lock("answer", PromptLock.versioned("support.answer", "2026-06-23"))
        .with_image("worker", ImageRef("registry.example.com/gb/worker@sha256:abc"))
    )

    assert left.content_digest() == right.content_digest()


def test_graph_release_digest_is_bound_to_release_name() -> None:
    left = GraphRelease(name="enterprise-rag", version="2026.06.23.1")
    right = GraphRelease(name="enterprise-rag-copy", version="2026.06.23.1")

    assert left.content_digest() != right.content_digest()


def test_release_evidence_maps_cannot_be_mutated_after_construction() -> None:
    release = GraphRelease(
        name="enterprise-rag",
        version="2026.06.23.1",
        graphs={"chat": GraphReleaseGraph("sha256:graph-chat", "sha256:plan-chat")},
    )
    bundle = ReleaseBundle(
        bundle_id="bundle-1",
        release=release,
        artifacts={"sbom": "sha256:sbom"},
        signatures={"cosign": "sha256:signature"},
    )
    release_digest = release.content_digest()
    bundle_digest = bundle.content_digest()

    with pytest.raises(TypeError):
        release.graphs["chat"] = GraphReleaseGraph("sha256:tampered", "sha256:tampered")
    with pytest.raises(TypeError):
        bundle.artifacts["sbom"] = "sha256:tampered"
    with pytest.raises(TypeError):
        bundle.signatures["cosign"] = "sha256:tampered"

    assert release.content_digest() == release_digest
    assert bundle.content_digest() == bundle_digest


def test_graph_release_records_reject_malformed_and_ambiguous_identity() -> None:
    for factory, message in (
        (
            lambda: GraphReleaseGraph(" ", "sha256:plan"),
            "release graph graph_hash",
        ),
        (lambda: ImageRef(object()), "release image"),  # type: ignore[arg-type]
        (
            lambda: KnowledgeBinding("index", " current "),
            "knowledge binding index_revision",
        ),
        (lambda: ReleaseLockRef(" "), "release lock ref"),
    ):
        with pytest.raises(GraphReleaseError, match=message):
            factory()

    for prompt_lock in (
        lambda: PromptLock("versioned", "prompt"),
        lambda: PromptLock("versioned", "prompt", "1", "production"),
        lambda: PromptLock("label", "prompt", version="1"),
        lambda: PromptLock("unknown", "prompt"),  # type: ignore[arg-type]
    ):
        with pytest.raises(GraphReleaseError, match="prompt lock"):
            prompt_lock()

    with pytest.raises(GraphReleaseError, match="bundle_media_type must not be empty"):
        GraphRelease("release", "1", bundle_media_type=" ")
    with pytest.raises(GraphReleaseError, match="graphs values must be GraphReleaseGraph"):
        GraphRelease("release", "1", graphs={"main": object()})  # type: ignore[dict-item]
    with pytest.raises(GraphReleaseError, match="knowledge key must match"):
        GraphRelease(
            "release",
            "1",
            knowledge={"alias": KnowledgeBinding("index", "revision")},
        )


def test_release_bundle_rejects_coerced_or_unstable_evidence_identity() -> None:
    release = GraphRelease("support-agent", "2026.06.23.1")

    with pytest.raises(GraphReleaseError, match="bundle_id"):
        ReleaseBundle(" ", release)
    with pytest.raises(GraphReleaseError, match="release must be a GraphRelease"):
        ReleaseBundle("bundle-1", object())  # type: ignore[arg-type]
    for field_name, evidence in (
        ("artifacts", {1: "sha256:first", "1": "sha256:second"}),
        ("artifacts", {"sbom": object()}),
        ("signatures", {" ": "sha256:signature"}),
    ):
        with pytest.raises(GraphReleaseError, match=field_name):
            ReleaseBundle(
                "bundle-1",
                release,
                **{field_name: evidence},
            )


def test_graph_release_validation_rejects_mutable_production_references() -> None:
    release = (
        GraphRelease(name="enterprise-rag", version="2026.06.23.1")
        .with_bundle("latest", "application/vnd.graphblocks.release.v1")
        .with_graph("chat", GraphReleaseGraph("main", "sha256:plan-chat"))
        .with_image("control", ImageRef("registry.example.com/gb/control:latest"))
        .with_prompt_lock("answer", PromptLock.label("support.answer", "production"))
        .with_knowledge(KnowledgeBinding("intranet_docs", "current"))
        .with_lock("python", ReleaseLockRef("pylock.toml"))
        .with_supply_chain(
            SupplyChainLock(
                sbom_ref="oci://registry/sbom:latest",
                provenance_ref="oci://registry/provenance:latest",
            )
        )
    )

    with pytest.raises(GraphReleaseMutableReferencesError) as error:
        release.validate_production_pins()

    assert error.value.references == (
        "bundle.digest",
        "graphs.chat.graph_hash",
        "images.control",
        "locks.python.digest",
        "knowledge.intranet_docs.index_revision",
        "prompts.answer",
        "supply_chain.provenance_ref",
        "supply_chain.sbom_ref",
    )


def test_graph_release_supply_chain_lock_is_part_of_release_digest_and_production_pins() -> None:
    base = (
        GraphRelease(name="enterprise-rag", version="2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.v1")
        .with_graph("chat", GraphReleaseGraph("sha256:graph-chat", "sha256:plan-chat"))
        .with_supply_chain(
            SupplyChainLock(
                sbom_ref="oci://registry/sbom@sha256:sbom",
                provenance_ref="oci://registry/provenance@sha256:provenance",
                signature_policy="production-publishers",
            )
        )
    )
    changed_policy = base.with_supply_chain(
        SupplyChainLock(
            sbom_ref="oci://registry/sbom@sha256:sbom",
            provenance_ref="oci://registry/provenance@sha256:provenance",
            signature_policy="staging-publishers",
        )
    )

    base.validate_production_pins()

    assert base.supply_chain is not None
    assert base.supply_chain.canonical_value() == {
        "sbom_ref": "oci://registry/sbom@sha256:sbom",
        "provenance_ref": "oci://registry/provenance@sha256:provenance",
        "signature_policy": "production-publishers",
    }
    assert base.content_digest() != changed_policy.content_digest()


def test_graph_release_lock_refs_are_part_of_release_digest_and_production_pins() -> None:
    base = (
        GraphRelease(name="enterprise-rag", version="2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.v1")
        .with_graph("chat", GraphReleaseGraph("sha256:graph-chat", "sha256:plan-chat"))
        .with_lock(
            "python",
            ReleaseLockRef("locks/pylock.toml", "sha256:pylock", "package"),
        )
        .with_lock(
            "policies",
            ReleaseLockRef(
                "oci://registry/policies@sha256:policy-lock",
                lock_type="policy",
            ),
        )
    )
    changed_lock = base.with_lock(
        "python",
        ReleaseLockRef("locks/pylock.toml", "sha256:other-pylock", "package"),
    )

    base.validate_production_pins()

    assert base.locks["python"].canonical_value() == {
        "ref": "locks/pylock.toml",
        "digest": "sha256:pylock",
        "lock_type": "package",
    }
    assert base.content_digest() != changed_lock.content_digest()


def test_release_bundle_digest_is_stable_for_release_and_artifact_order() -> None:
    release = (
        GraphRelease("support-agent", "2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.bundle.v1+tar")
        .with_graph("turn", GraphReleaseGraph("sha256:graph", "sha256:plan"))
    )
    left = ReleaseBundle(
        bundle_id="bundle-a",
        release=release,
        artifacts={
            "sbom": "sha256:sbom",
            "provenance": "sha256:provenance",
        },
        signatures={"cosign": "sha256:signature"},
    )
    right = ReleaseBundle(
        bundle_id="bundle-b",
        release=release,
        artifacts={
            "provenance": "sha256:provenance",
            "sbom": "sha256:sbom",
        },
        signatures={"cosign": "sha256:signature"},
    )

    assert left.content_digest() == right.content_digest()
    assert left.bundle_manifest() == {
        "bundle_id": "bundle-a",
        "release_digest": release.content_digest(),
        "release_name": "support-agent",
        "release_version": "2026.06.23.1",
        "artifacts": {"provenance": "sha256:provenance", "sbom": "sha256:sbom"},
        "signatures": {"cosign": "sha256:signature"},
    }


def test_release_attestation_verifies_trusted_signature_and_fails_closed() -> None:
    release = (
        GraphRelease("support-agent", "2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.bundle.v1+tar")
        .with_graph("turn", GraphReleaseGraph("sha256:graph", "sha256:plan"))
    )
    bundle = ReleaseBundle(
        bundle_id="bundle-1",
        release=release,
        artifacts={"provenance": "sha256:provenance", "sbom": "sha256:sbom"},
    )
    attestation = ReleaseAttestation.sign(
        bundle,
        signer_id="production-publisher-1",
        signing_key=b"local-production-publisher-key",
    )

    verified = verify_release_attestation(
        bundle,
        attestation,
        trusted_signing_keys={"production-publisher-1": b"local-production-publisher-key"},
    )
    tampered = verify_release_attestation(
        replace(bundle, artifacts={**bundle.artifacts, "sbom": "sha256:tampered"}),
        attestation,
        trusted_signing_keys={"production-publisher-1": b"local-production-publisher-key"},
    )
    untrusted = verify_release_attestation(
        bundle,
        attestation,
        trusted_signing_keys={"staging-publisher": b"local-production-publisher-key"},
    )
    invalid_signature = verify_release_attestation(
        bundle,
        replace(attestation, signature="hmac-sha256:" + ("0" * 64)),
        trusted_signing_keys={"production-publisher-1": b"local-production-publisher-key"},
    )
    wrong_subject = verify_release_attestation(
        bundle,
        replace(attestation, subject="bundle-other"),
        trusted_signing_keys={"production-publisher-1": b"local-production-publisher-key"},
    )

    assert verified.verified is True
    assert verified.reason == "trusted_signature"
    assert verified.subject == "bundle-1"
    assert verified.subject_digest == bundle.attestation_digest()
    assert tampered.verified is False
    assert tampered.reason == "subject_digest_mismatch"
    assert untrusted.verified is False
    assert untrusted.reason == "untrusted_signer"
    assert invalid_signature.verified is False
    assert invalid_signature.reason == "signature_mismatch"
    assert wrong_subject.verified is False
    assert wrong_subject.reason == "subject_mismatch"


def test_canary_metric_evaluator_enforces_minimums_and_max_regression() -> None:
    thresholds = (
        CanaryMetricThreshold("turn_success_rate", minimum=0.995),
        CanaryMetricThreshold("citation_validation_rate", minimum=0.98),
        CanaryMetricThreshold("average_cost_per_turn", max_regression=0.10),
    )

    passing = evaluate_canary_metrics(
        thresholds,
        candidate_metrics={
            "turn_success_rate": 0.997,
            "citation_validation_rate": 0.985,
            "average_cost_per_turn": 1.09,
        },
        baseline_metrics={"average_cost_per_turn": 1.00},
    )
    failing = evaluate_canary_metrics(
        thresholds,
        candidate_metrics={
            "turn_success_rate": 0.994,
            "citation_validation_rate": 0.985,
            "average_cost_per_turn": 1.11,
        },
        baseline_metrics={"average_cost_per_turn": 1.00},
    )
    missing_baseline = evaluate_canary_metrics(
        thresholds,
        candidate_metrics={
            "turn_success_rate": 0.997,
            "citation_validation_rate": 0.985,
            "average_cost_per_turn": 1.01,
        },
        baseline_metrics={},
    )

    assert passing.passed is True
    assert passing.violations == ()
    assert passing.evidence_contract()["metrics"] == [
        {
            "metric": "average_cost_per_turn",
            "observed": 1.09,
            "baseline": 1.0,
            "minimum": None,
            "maxRegression": 0.1,
            "regression": 0.09,
            "passed": True,
            "reason": "within_threshold",
        },
        {
            "metric": "citation_validation_rate",
            "observed": 0.985,
            "baseline": None,
            "minimum": 0.98,
            "maxRegression": None,
            "regression": None,
            "passed": True,
            "reason": "within_threshold",
        },
        {
            "metric": "turn_success_rate",
            "observed": 0.997,
            "baseline": None,
            "minimum": 0.995,
            "maxRegression": None,
            "regression": None,
            "passed": True,
            "reason": "within_threshold",
        },
    ]
    assert failing.passed is False
    assert failing.violations == (
        "average_cost_per_turn:max_regression_exceeded",
        "turn_success_rate:minimum_not_met",
    )
    assert missing_baseline.passed is False
    assert missing_baseline.violations == ("average_cost_per_turn:baseline_missing",)


def test_rollback_and_drain_evidence_is_deterministic_and_fail_closed() -> None:
    plan = RolloutPlan.canary(
        "rollout-rollback-1",
        "rev-stable",
        "rev-canary",
        canary_steps=(RolloutStep.canary("canary-10", traffic_percent=10),),
    )
    aborted = plan.initial_state().advance_for_test(2).evaluate_gate(
        RolloutAnalysisResult(
            step_id="canary-10",
            passed=False,
            reason="quality_gate_failed",
        )
    )
    workloads = {
        "realtime_session": ("rev-canary", True),
        "new_request": (None, False),
        "durable_job": ("rev-canary", True),
        "conversation": ("rev-canary", False),
        "existing_request": ("rev-canary", False),
    }

    evidence = evaluate_rollback_and_drain(aborted, workloads)
    repeated = evaluate_rollback_and_drain(aborted, dict(reversed(tuple(workloads.items()))))
    blocked = evaluate_rollback_and_drain(
        replace(aborted, automatic_rollback_allowed=False),
        workloads,
    )

    assert evidence.rollback_allowed is True
    assert evidence.restored_revision_id == "rev-stable"
    assert evidence.aborted_revision_id == "rev-canary"
    assert evidence.decision_contracts() == [
        {
            "workload": "conversation",
            "kind": "keep_affinity",
            "revisionId": "rev-canary",
            "fromRevisionId": None,
            "toRevisionId": None,
        },
        {
            "workload": "durable_job",
            "kind": "checkpoint_and_migrate",
            "revisionId": None,
            "fromRevisionId": "rev-canary",
            "toRevisionId": "rev-stable",
        },
        {
            "workload": "existing_request",
            "kind": "finish_on_old",
            "revisionId": "rev-canary",
            "fromRevisionId": None,
            "toRevisionId": None,
        },
        {
            "workload": "new_request",
            "kind": "admit_on_new",
            "revisionId": "rev-stable",
            "fromRevisionId": None,
            "toRevisionId": None,
        },
        {
            "workload": "realtime_session",
            "kind": "drain_on_old",
            "revisionId": "rev-canary",
            "fromRevisionId": None,
            "toRevisionId": None,
        },
    ]
    assert evidence.content_digest() == repeated.content_digest()
    assert blocked.rollback_allowed is False
    assert blocked.decision_contracts() == []


def test_graph_deployment_builds_physical_plan_from_release_graph() -> None:
    release = GraphRelease("support-agent", "2026.06.23.1").with_graph(
        "turn",
        GraphReleaseGraph("sha256:graph-turn", "sha256:plan-turn"),
    )
    control = ExecutionTarget("control", "service", "rust").with_capabilities(["graph.coordinator"])
    worker = ExecutionTarget("worker", "worker_pool", "python_worker").with_capabilities(["document.parse.pdf"])
    deployment = (
        GraphDeployment(
            deployment_id="support-prod",
            release=release,
            graph_name="turn",
            deployment_revision_id="rev-1",
            environment="production",
        )
        .with_target(worker)
        .with_target(control)
        .with_default_target("control")
        .with_placement(PlacementRule("docs", PlacementSelector.capabilities(["document.parse.pdf"]), "worker"))
    )

    plan = deployment.to_physical_plan(package_lock_hash="sha256:package")

    assert plan.release_digest == release.content_digest()
    assert plan.graph_hash == "sha256:graph-turn"
    assert plan.package_lock_hash == "sha256:package"
    assert plan.resolve_target("parse", None, "document.parse", ["document.parse.pdf"], [], None).target_id == "worker"
    assert deployment.deployment_spec_hash().startswith("sha256:")


def test_physical_plan_hash_and_resolution_are_stable() -> None:
    control = ExecutionTarget("control", "service", "rust").with_capabilities(["graph.coordinator"])
    doc_cpu = ExecutionTarget("doc-cpu", "worker_pool", "python_worker").with_capabilities(
        ["document.parse.pdf"]
    )
    sandbox = ExecutionTarget("sandbox", "sandbox_pool", "python_worker").with_effects(["process_execution"])
    left = (
        PhysicalExecutionPlan("sha256:release", "rev-1", "sha256:graph")
        .with_package_lock_hash("sha256:package")
        .with_target(doc_cpu)
        .with_target(control)
        .with_target(sandbox)
        .with_default_target("control")
        .with_placement(PlacementRule("docs", PlacementSelector.capabilities(["document.parse.pdf"]), "doc-cpu"))
        .with_placement(PlacementRule("effect", PlacementSelector.effects(["process_execution"]), "sandbox"))
        .with_placement(PlacementRule("generate", PlacementSelector.nodes(["generate"]), "control"))
    )
    right = (
        PhysicalExecutionPlan("sha256:release", "rev-1", "sha256:graph")
        .with_package_lock_hash("sha256:package")
        .with_target(control)
        .with_target(sandbox)
        .with_target(doc_cpu)
        .with_default_target("control")
        .with_placement(PlacementRule("generate", PlacementSelector.nodes(["generate"]), "control"))
        .with_placement(PlacementRule("effect", PlacementSelector.effects(["process_execution"]), "sandbox"))
        .with_placement(PlacementRule("docs", PlacementSelector.capabilities(["document.parse.pdf"]), "doc-cpu"))
    )

    assert left.plan_hash() == right.plan_hash()
    assert left.target_capability_hash() == right.target_capability_hash()
    assert left.resolve_target("generate", None, "model.generate", [], [], None).target_id == "control"
    assert (
        left.resolve_target("parse-one", None, "document.parse", ["document.parse.pdf"], [], None).target_id
        == "doc-cpu"
    )
    assert (
        left.resolve_target("run-code", None, "code.exec", [], ["process_execution"], None).target_id
        == "sandbox"
    )
    assert left.resolve_target("other", None, "value.const", [], [], None).target_id == "control"


def test_deployment_target_maps_are_immutable_identity_snapshots() -> None:
    target = ExecutionTarget("control", "service", "rust")
    supplied_targets = {"control": target}
    release = GraphRelease("support-agent", "2026.06.23.1").with_graph(
        "turn",
        GraphReleaseGraph("sha256:graph-turn", "sha256:plan-turn"),
    )
    deployment = GraphDeployment(
        deployment_id="support-prod",
        release=release,
        graph_name="turn",
        deployment_revision_id="rev-1",
        targets=supplied_targets,
    )
    plan = PhysicalExecutionPlan(
        "sha256:release",
        "rev-1",
        "sha256:graph",
        targets=supplied_targets,
    )
    deployment_digest = deployment.deployment_spec_hash()
    plan_digest = plan.plan_hash()

    supplied_targets.clear()

    assert tuple(deployment.targets) == ("control",)
    assert tuple(plan.targets) == ("control",)
    with pytest.raises(TypeError):
        deployment.targets["worker"] = target
    with pytest.raises(TypeError):
        plan.targets["worker"] = target
    assert deployment.deployment_spec_hash() == deployment_digest
    assert plan.plan_hash() == plan_digest


def test_deployment_target_maps_reject_mismatched_record_identity() -> None:
    target = ExecutionTarget("control", "service", "rust")

    with pytest.raises(GraphDeploymentError, match="target key must match target_id"):
        PhysicalExecutionPlan(
            "sha256:release",
            "rev-1",
            "sha256:graph",
            targets={"alias": target},
        )


def test_placement_resolution_rejects_same_priority_conflicts() -> None:
    plan = (
        PhysicalExecutionPlan("sha256:release", "rev-1", "sha256:graph")
        .with_target(ExecutionTarget("control", "service", "rust"))
        .with_target(ExecutionTarget("doc-cpu", "worker_pool", "python_worker"))
        .with_placement(PlacementRule("a", PlacementSelector.nodes(["generate"]), "control"))
        .with_placement(PlacementRule("b", PlacementSelector.nodes(["generate"]), "doc-cpu"))
    )

    with pytest.raises(PlacementAmbiguousError) as error:
        plan.resolve_target("generate", None, "model.generate", [], [], None)

    assert error.value.node_id == "generate"
    assert error.value.priority == "node"
    assert error.value.target_ids == ("control", "doc-cpu")


@pytest.mark.parametrize(
    "selector",
    [
        PlacementSelector.nodes,
        PlacementSelector.execution_groups,
        PlacementSelector.blocks,
        PlacementSelector.capabilities,
        PlacementSelector.effects,
        PlacementSelector.execution_classes,
    ],
)
def test_placement_selector_rejects_empty_values(selector) -> None:
    with pytest.raises(GraphDeploymentError, match="placement selector values must not be empty"):
        selector([])


def test_placement_selector_rejects_unknown_kind_and_unstable_values() -> None:
    with pytest.raises(GraphDeploymentError, match="placement selector kind"):
        PlacementSelector("unknown", ("generate",))
    for values in ((" ",), (object(),), "generate"):
        with pytest.raises(GraphDeploymentError, match="placement selector values"):
            PlacementSelector("nodes", values)  # type: ignore[arg-type]


def test_execution_target_rejects_unknown_kind_and_unstable_capabilities() -> None:
    with pytest.raises(GraphDeploymentError, match="execution target kind"):
        ExecutionTarget("control", "unknown", "rust")  # type: ignore[arg-type]
    for capabilities in ((" ",), (object(),), "graph.coordinator"):
        with pytest.raises(GraphDeploymentError, match="capabilities"):
            ExecutionTarget(
                "control",
                "service",
                "rust",
                capabilities=capabilities,  # type: ignore[arg-type]
            )


def test_deployment_event_exports_release_revision_and_cohort_attributes() -> None:
    context = (
        DeploymentObservabilityContext("release-1", "rev-1")
        .with_release_digest("sha256:release")
        .with_rollout("rollout-1", "step-2", "canary")
    )
    event = DeploymentEvent(
        event_id="event-1",
        kind=DeploymentEventKind.ROLLOUT_GATE_FAILED,
        context=context,
        occurred_at="2026-06-23T00:00:00Z",
    ).with_metadata("reason", "latency_regression")

    assert event.telemetry_attributes() == {
        "deployment.event": "rollout.gate.failed",
        "graphblocks.deployment.revision": "rev-1",
        "graphblocks.release.digest": "sha256:release",
        "graphblocks.release.id": "release-1",
        "graphblocks.rollout.cohort": "canary",
        "graphblocks.rollout.id": "rollout-1",
        "graphblocks.rollout.step": "step-2",
    }


def test_upgrade_policy_is_workload_aware() -> None:
    policy = UpgradePolicy.workload_aware("rev-old", "rev-new")

    assert policy.decide("new_request", None, False) == RevisionDecision.admit_on_new("rev-new")
    assert policy.decide("existing_request", None, False) == RevisionDecision.finish_on_old("rev-old")
    assert policy.decide("conversation", "rev-affinity", False) == RevisionDecision.keep_affinity("rev-affinity")
    assert policy.decide("conversation", None, False) == RevisionDecision.admit_on_new("rev-new")
    assert policy.decide("durable_job", "rev-old", True) == RevisionDecision.checkpoint_and_migrate(
        "rev-old", "rev-new"
    )
    assert policy.decide("realtime_session", "rev-old", True) == RevisionDecision.drain_on_old("rev-old")


def test_rollout_plan_builds_validate_shadow_canary_and_promote_sequence() -> None:
    plan = RolloutPlan.canary(
        rollout_id="rollout-1",
        stable_revision_id="rev-stable",
        candidate_revision_id="rev-canary",
        analysis_profile_ref="rag-production-rollout",
        affinity="conversation_id",
        canary_steps=(
            RolloutStep.canary("canary-1", traffic_percent=1, minimum_samples=200),
            RolloutStep.canary("canary-10", traffic_percent=10, minimum_duration_seconds=1800),
        ),
    )

    assert [step.step_id for step in plan.steps] == [
        "validate",
        "shadow",
        "canary-1",
        "canary-10",
        "promote",
    ]
    assert plan.steps[1].effects == "suppress"
    assert plan.steps[2].traffic_percent == 1
    assert plan.current_step(2).step_id == "canary-1"
    assert plan.analysis_profile_ref == "rag-production-rollout"
    assert plan.affinity == "conversation_id"


def test_rollout_models_reject_invalid_string_fields() -> None:
    with pytest.raises(RolloutError, match="rollout step_id must be a string"):
        RolloutStep(step_id=object(), kind="validate")  # type: ignore[arg-type]
    with pytest.raises(RolloutError, match="rollout analysis step_id must be a string"):
        RolloutAnalysisResult(step_id=object(), passed=True)  # type: ignore[arg-type]
    with pytest.raises(RolloutError, match="rollout_id must be a string"):
        RolloutPlan(
            rollout_id=object(),  # type: ignore[arg-type]
            stable_revision_id="rev-stable",
            candidate_revision_id="rev-canary",
            steps=(RolloutStep.validate(), RolloutStep.promote()),
        )


@pytest.mark.parametrize("value", (1.5, "10", True))
def test_rollout_models_reject_coerced_numeric_fields(value: object) -> None:
    with pytest.raises(RolloutError, match="traffic_percent"):
        RolloutStep.canary("canary", traffic_percent=value)  # type: ignore[arg-type]
    with pytest.raises(RolloutError, match="sample_count"):
        RolloutAnalysisResult(
            "canary",
            True,
            sample_count=value,  # type: ignore[arg-type]
        )


def test_rollout_models_reject_ambiguous_steps_and_mutable_metrics() -> None:
    validate = RolloutStep.validate()
    promote = RolloutStep.promote()
    with pytest.raises(RolloutError, match="step_id values must be unique"):
        RolloutPlan(
            "rollout",
            "stable",
            "candidate",
            steps=(validate, RolloutStep.validate(), promote),
        )
    with pytest.raises(RolloutError, match="revisions must be distinct"):
        RolloutPlan(
            "rollout",
            "same",
            "same",
            steps=(validate, promote),
        )

    metrics = {"quality": {"samples": [1]}}
    result = RolloutAnalysisResult("validate", True, metrics=metrics)
    metrics["quality"]["samples"].append(2)  # type: ignore[index, union-attr]

    assert result.metrics == {"quality": {"samples": (1,)}}
    with pytest.raises(TypeError):
        result.metrics["other"] = 1
    with pytest.raises(TypeError):
        result.metrics["quality"]["other"] = 2  # type: ignore[index]
    with pytest.raises(AttributeError):
        result.metrics["quality"]["samples"].append(2)  # type: ignore[index, union-attr]
    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    with pytest.raises(RolloutError, match="metrics must be canonical JSON"):
        RolloutAnalysisResult("validate", True, metrics=recursive)
    with pytest.raises(RolloutError, match="stable_revision_id must be a string"):
        RolloutPlan(
            rollout_id="rollout-1",
            stable_revision_id=object(),  # type: ignore[arg-type]
            candidate_revision_id="rev-canary",
            steps=(RolloutStep.validate(), RolloutStep.promote()),
        )
    with pytest.raises(RolloutError, match="candidate_revision_id must be a string"):
        RolloutPlan(
            rollout_id="rollout-1",
            stable_revision_id="rev-stable",
            candidate_revision_id=object(),  # type: ignore[arg-type]
            steps=(RolloutStep.validate(), RolloutStep.promote()),
        )


def test_rollout_gate_holds_until_minimum_samples_and_duration_are_met() -> None:
    plan = RolloutPlan.canary(
        "rollout-1",
        "rev-stable",
        "rev-canary",
        canary_steps=(RolloutStep.canary("canary-10", traffic_percent=10, minimum_samples=20, minimum_duration_seconds=60),),
    )
    state = plan.initial_state().advance_for_test(2)

    held = state.evaluate_gate(
        RolloutAnalysisResult(
            step_id="canary-10",
            passed=True,
            sample_count=19,
            duration_seconds=120,
            metrics={"quality": 0.98},
        )
    )
    advanced = state.evaluate_gate(
        RolloutAnalysisResult(
            step_id="canary-10",
            passed=True,
            sample_count=20,
            duration_seconds=60,
            metrics={"quality": 0.98},
        )
    )

    assert held.decision == "hold"
    assert held.reason == "minimum_samples_not_met"
    assert held.next_state.current_step_index == 2
    assert advanced.decision == "advance"
    assert advanced.next_state.current_step_index == 3


def test_rollout_gate_promotes_after_final_promote_step_passes() -> None:
    plan = RolloutPlan.canary(
        "rollout-1",
        "rev-stable",
        "rev-canary",
        canary_steps=(RolloutStep.canary("canary-50", traffic_percent=50),),
    )
    state = plan.initial_state().advance_for_test(3)

    decision = state.evaluate_gate(RolloutAnalysisResult(step_id="promote", passed=True))

    assert decision.decision == "promote"
    assert decision.next_state.status == "promoted"
    assert decision.next_state.current_step_index == 3


def test_rollout_gate_aborts_without_automatic_rollback_for_non_reversible_effects() -> None:
    plan = RolloutPlan.canary(
        "rollout-1",
        "rev-stable",
        "rev-canary",
        canary_steps=(RolloutStep.canary("canary-10", traffic_percent=10),),
    )
    state = plan.initial_state().advance_for_test(2)

    decision = state.evaluate_gate(
        RolloutAnalysisResult(
            step_id="canary-10",
            passed=False,
            reason="quality_gate_failed",
            non_reversible_effect_observed=True,
        )
    )

    assert decision.decision == "abort"
    assert decision.reason == "quality_gate_failed"
    assert decision.automatic_rollback_allowed is False
    assert decision.next_state.status == "aborted"


def test_deployment_slo_profile_evaluates_slo_within_budget_condition() -> None:
    profile = DeploymentSloProfile(
        profile_id="rag-production",
        slo_objective_ids=("availability", "p95-latency"),
    )
    availability = SloObjective.at_least("availability", "request_success_ratio", 0.995, "5m").evaluate(
        SloMeasurement("request_success_ratio", 0.997, "5m")
    )
    latency = SloObjective.at_most("p95-latency", "p95_latency_ms", 800, "5m").with_unit("ms").evaluate(
        SloMeasurement("p95_latency_ms", 900, "5m", unit="ms")
    )

    condition = profile.evaluate_slo_reports([availability, latency])

    assert condition == DeploymentCondition(
        condition_type="SLOWithinBudget",
        status="false",
        reason="slo_failed",
        message="failed SLO objectives: p95-latency",
    )
    assert profile.content_digest().startswith("sha256:")


def test_deployment_condition_rejects_invalid_string_fields() -> None:
    with pytest.raises(GraphDeploymentError, match="deployment condition type must be a string"):
        DeploymentCondition(object(), "true", "ready")  # type: ignore[arg-type]
    with pytest.raises(GraphDeploymentError, match="deployment condition reason must be a string"):
        DeploymentCondition("Ready", "true", object())  # type: ignore[arg-type]


def test_deployment_slo_profile_reports_missing_or_no_data_as_unknown() -> None:
    profile = DeploymentSloProfile(
        profile_id="rag-production",
        slo_objective_ids=("availability", "p95-latency"),
    )
    availability = SloObjective.at_least("availability", "request_success_ratio", 0.995, "5m").evaluate(
        SloMeasurement("other_indicator", 0.997, "5m")
    )

    condition = profile.evaluate_slo_reports([availability])

    assert condition.condition_type == "SLOWithinBudget"
    assert condition.status == "unknown"
    assert condition.reason == "slo_no_data"
    assert condition.message == "missing or no-data SLO objectives: availability, p95-latency"


def test_deployment_profiles_reject_invalid_string_fields() -> None:
    with pytest.raises(GraphDeploymentError, match="deployment SLO profile id must be a string"):
        DeploymentSloProfile(profile_id=object(), slo_objective_ids=("availability",))  # type: ignore[arg-type]
    with pytest.raises(GraphDeploymentError, match="deployment recovery profile id must be a string"):
        DeploymentRecoveryProfile(profile_id=object())  # type: ignore[arg-type]
    with pytest.raises(GraphDeploymentError, match="recovery objective target must be a string"):
        RecoveryObjective(target=object(), rto="15m", rpo="5m")  # type: ignore[arg-type]
    with pytest.raises(GraphDeploymentError, match="recovery objective rto must be a string"):
        RecoveryObjective(target="service", rto=object(), rpo="5m")  # type: ignore[arg-type]
    with pytest.raises(GraphDeploymentError, match="recovery objective rpo must be a string"):
        RecoveryObjective(target="service", rto="15m", rpo=object())  # type: ignore[arg-type]


def test_deployment_recovery_profile_evaluates_restore_test_freshness() -> None:
    profile = (
        DeploymentRecoveryProfile(profile_id="production-recovery")
        .with_objective("service", rto="15m", rpo="5m")
        .with_objective("durable_jobs", rto="1h", rpo="checkpoint")
        .with_knowledge_index_sources(["source_assets", "manifests", "release_bundle"])
        .with_regional_failover("active_passive")
        .with_max_restore_test_age_seconds(86_400)
    )

    current = profile.evaluate_restore_test(
        tested_at_unix_seconds=1_000,
        now_unix_seconds=80_000,
        passed=True,
    )
    stale = profile.evaluate_restore_test(
        tested_at_unix_seconds=1_000,
        now_unix_seconds=90_000,
        passed=True,
    )

    assert current == DeploymentCondition("RecoveryTestCurrent", "true", "restore_test_current")
    assert stale == DeploymentCondition(
        "RecoveryTestCurrent",
        "false",
        "restore_test_stale",
        "last restore test age 89000s exceeds 86400s",
    )
    assert profile.recovery_contract() == {
        "profile_id": "production-recovery",
        "objectives": [
            {"target": "durable_jobs", "rto": "1h", "rpo": "checkpoint"},
            {"target": "service", "rto": "15m", "rpo": "5m"},
        ],
        "knowledge_index_rebuildable_from": ["manifests", "release_bundle", "source_assets"],
        "regional_failover_mode": "active_passive",
        "max_restore_test_age_seconds": 86_400,
    }
    assert profile.content_digest().startswith("sha256:")


def test_rollout_traffic_assignment_is_deterministic_and_sticky_by_affinity() -> None:
    plan = RolloutPlan.canary(
        "rollout-1",
        "rev-stable",
        "rev-canary",
        canary_steps=(RolloutStep.canary("canary-25", traffic_percent=25),),
    )
    step = plan.steps[2]

    first = plan.assign_revision("conversation-1", step)
    second = plan.assign_revision("conversation-1", step)

    assert first == second
    assert plan.assign_revision("conversation-1", RolloutStep.canary("stable", traffic_percent=0)) == "rev-stable"
    assert plan.assign_revision("conversation-1", RolloutStep.canary("candidate", traffic_percent=100)) == "rev-canary"

from __future__ import annotations

import pytest

from graphblocks.deployment import (
    DeploymentEvent,
    DeploymentEventKind,
    DeploymentObservabilityContext,
    DeploymentRevision,
    ExecutionTarget,
    GraphRelease,
    GraphReleaseGraph,
    GraphReleaseMutableReferencesError,
    ImageRef,
    KnowledgeBinding,
    PhysicalExecutionPlan,
    PlacementAmbiguousError,
    PlacementRule,
    PlacementSelector,
    PromptLock,
    RevisionDecision,
    UpgradePolicy,
)


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
        GraphRelease(name="enterprise-rag-copy", version="2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.v1")
        .with_application_hash("sha256:app")
        .with_graph("ingest", GraphReleaseGraph("sha256:graph-ingest", "sha256:plan-ingest"))
        .with_graph("chat", GraphReleaseGraph("sha256:graph-chat", "sha256:plan-chat"))
        .with_prompt_lock("answer", PromptLock.versioned("support.answer", "2026-06-23"))
        .with_image("worker", ImageRef("registry.example.com/gb/worker@sha256:abc"))
    )

    assert left.content_digest() == right.content_digest()


def test_graph_release_validation_rejects_mutable_production_references() -> None:
    release = (
        GraphRelease(name="enterprise-rag", version="2026.06.23.1")
        .with_bundle("latest", "application/vnd.graphblocks.release.v1")
        .with_graph("chat", GraphReleaseGraph("main", "sha256:plan-chat"))
        .with_image("control", ImageRef("registry.example.com/gb/control:latest"))
        .with_prompt_lock("answer", PromptLock.label("support.answer", "production"))
        .with_knowledge(KnowledgeBinding("intranet_docs", "current"))
    )

    with pytest.raises(GraphReleaseMutableReferencesError) as error:
        release.validate_production_pins()

    assert error.value.references == (
        "bundle.digest",
        "graphs.chat.graph_hash",
        "images.control",
        "knowledge.intranet_docs.index_revision",
        "prompts.answer",
    )


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

from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_deployment_package_reexports_release_and_upgrade_contracts(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-deployment" / "src"))
    graphblocks_deployment = importlib.import_module("graphblocks_deployment")

    release = graphblocks_deployment.GraphRelease(name="support-agent", version="2026.06.23.1").with_graph(
        "turn", graphblocks_deployment.GraphReleaseGraph("sha256:graph", "sha256:plan")
    )
    policy = graphblocks_deployment.UpgradePolicy.workload_aware("rev-old", "rev-new")

    assert release.graphs["turn"].normalized_plan_hash == "sha256:plan"
    assert policy.decide("new_request", None, False) == graphblocks_deployment.RevisionDecision.admit_on_new(
        "rev-new"
    )
    bundle = graphblocks_deployment.ReleaseBundle("bundle-1", release)
    deployment = graphblocks_deployment.GraphDeployment(
        "support-prod",
        release,
        graph_name="turn",
        deployment_revision_id="rev-1",
    ).with_target(graphblocks_deployment.ExecutionTarget("control", "service", "rust")).with_default_target("control")

    assert bundle.content_digest().startswith("sha256:")
    assert deployment.to_physical_plan().default_target == "control"
    assert "GraphDeployment" in graphblocks_deployment.__all__
    assert "ReleaseBundle" in graphblocks_deployment.__all__


def test_deployment_package_reexports_rollout_gate_contracts(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-deployment" / "src"))
    graphblocks_deployment = importlib.import_module("graphblocks_deployment")

    plan = graphblocks_deployment.RolloutPlan.canary(
        "rollout-1",
        "rev-stable",
        "rev-canary",
        canary_steps=(graphblocks_deployment.RolloutStep.canary("canary-10", traffic_percent=10),),
    )
    state = plan.initial_state().advance_for_test(2)

    decision = state.evaluate_gate(
        graphblocks_deployment.RolloutAnalysisResult(step_id="canary-10", passed=True)
    )

    assert decision.decision == "advance"
    assert decision.next_state.current_step_index == 3
    assert "RolloutPlan" in graphblocks_deployment.__all__
    assert "RolloutStep" in graphblocks_deployment.__all__
    assert "RolloutAnalysisResult" in graphblocks_deployment.__all__


def test_deployment_package_reexports_slo_and_recovery_contracts(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-deployment" / "src"))
    graphblocks_deployment = importlib.import_module("graphblocks_deployment")

    slo_profile = graphblocks_deployment.DeploymentSloProfile("rag-production", ("availability",))
    recovery_profile = graphblocks_deployment.DeploymentRecoveryProfile("production-recovery").with_objective(
        "service",
        rto="15m",
        rpo="5m",
    )

    assert slo_profile.content_digest().startswith("sha256:")
    assert recovery_profile.content_digest().startswith("sha256:")
    assert "DeploymentCondition" in graphblocks_deployment.__all__
    assert "DeploymentSloProfile" in graphblocks_deployment.__all__
    assert "DeploymentRecoveryProfile" in graphblocks_deployment.__all__

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

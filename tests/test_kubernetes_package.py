from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _import_kubernetes(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-deployment" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-kubernetes" / "src"))
    return importlib.import_module("graphblocks_kubernetes")


def test_kubernetes_adapter_renders_worker_deployment_and_service(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks_deployment")
    target = (
        graphblocks_deployment.ExecutionTarget("agent-workers", "worker_pool", "rust")
        .with_capabilities(["graphblocks.runtime"])
        .with_effects(["network"])
        .with_image("ghcr.io/acme/support-agent@sha256:runtime")
    )
    options = graphblocks_kubernetes.KubernetesRenderOptions(
        namespace="support",
        labels={"app.kubernetes.io/part-of": "graphblocks"},
        annotations={"graphblocks.ai/release-id": "release-2026-06-23"},
        service_account_name="graphblocks-runtime",
    )
    ports = (graphblocks_kubernetes.KubernetesPort("http", 8080, service_port=80),)

    deployment = graphblocks_kubernetes.render_target_deployment(
        "support-agent",
        target,
        options=options,
        replicas=3,
        ports=ports,
        env={"GRAPHBLOCKS_RELEASE": "release-2026-06-23"},
    )
    service = graphblocks_kubernetes.render_target_service("support-agent", target, ports, options=options)

    assert deployment["apiVersion"] == "apps/v1"
    assert deployment["kind"] == "Deployment"
    assert deployment["metadata"]["namespace"] == "support"
    assert deployment["metadata"]["labels"]["app.kubernetes.io/part-of"] == "graphblocks"
    assert deployment["metadata"]["annotations"]["graphblocks.ai/target-id"] == "agent-workers"
    assert deployment["spec"]["replicas"] == 3
    assert deployment["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "support-agent",
        "graphblocks.ai/target-id": "agent-workers",
    }

    template_spec = deployment["spec"]["template"]["spec"]
    assert template_spec["serviceAccountName"] == "graphblocks-runtime"
    assert template_spec["containers"] == [
        {
            "name": "support-agent",
            "image": "ghcr.io/acme/support-agent@sha256:runtime",
            "ports": [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
            "env": [{"name": "GRAPHBLOCKS_RELEASE", "value": "release-2026-06-23"}],
        }
    ]
    assert service["spec"]["selector"] == deployment["spec"]["selector"]["matchLabels"]
    assert service["spec"]["ports"] == [{"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"}]


def test_kubernetes_manifest_set_digest_is_independent_of_document_order(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks_deployment")
    options = graphblocks_kubernetes.KubernetesRenderOptions(namespace="support")
    target = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:runtime",
    )
    ports = (graphblocks_kubernetes.KubernetesPort("http", 8080),)
    deployment = graphblocks_kubernetes.render_target_deployment(
        "support-agent",
        target,
        options=options,
        ports=ports,
    )
    service = graphblocks_kubernetes.render_target_service("support-agent", target, ports, options=options)

    left = graphblocks_kubernetes.KubernetesManifestSet((deployment, service))
    right = graphblocks_kubernetes.KubernetesManifestSet((service, deployment))

    assert left.content_digest() == right.content_digest()
    assert [manifest["kind"] for manifest in left.by_kind("Deployment")] == ["Deployment"]
    assert [manifest["kind"] for manifest in left.by_kind("Service")] == ["Service"]


def test_kubernetes_cluster_snapshot_tracks_capabilities_deterministically(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    deployment = graphblocks_kubernetes.KubernetesClusterCapability("apps/v1", "Deployment")
    service = graphblocks_kubernetes.KubernetesClusterCapability("v1", "Service")

    left = graphblocks_kubernetes.KubernetesClusterSnapshot(
        cluster_id="prod-us",
        server_version="1.30",
        capabilities=(deployment, service),
        namespaces=("support", "graphblocks-system"),
        runtime_classes=("gvisor", "runc"),
    )
    right = graphblocks_kubernetes.KubernetesClusterSnapshot(
        cluster_id="prod-us",
        server_version="1.30",
        capabilities=(service, deployment),
        namespaces=("graphblocks-system", "support"),
        runtime_classes=("runc", "gvisor"),
    )

    assert left.supports("apps/v1", "Deployment")
    assert not left.supports("batch/v1", "CronJob")
    assert left.content_digest() == right.content_digest()

from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _import_gitops(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-deployment" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-gitops" / "src"))
    return importlib.import_module("graphblocks_gitops")


def _release(monkeypatch):
    _import_gitops(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks_deployment")
    return (
        graphblocks_deployment.GraphRelease("support-agent", "2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.bundle.v1+tar")
        .with_graph("turn", graphblocks_deployment.GraphReleaseGraph("sha256:graph", "sha256:plan"))
    )


def test_gitops_package_renders_argocd_application(monkeypatch) -> None:
    graphblocks_gitops = _import_gitops(monkeypatch)
    release = _release(monkeypatch)
    source = graphblocks_gitops.GitOpsSource(
        repo_url="https://git.example.com/platform/graphblocks.git",
        path="clusters/prod/support-agent",
        target_revision="main",
    )
    destination = graphblocks_gitops.GitOpsDestination(namespace="support")

    application = graphblocks_gitops.render_argocd_application(
        name="support-agent",
        release=release,
        source=source,
        destination=destination,
        project="support",
        automated=True,
    )

    assert application["apiVersion"] == "argoproj.io/v1alpha1"
    assert application["kind"] == "Application"
    assert application["metadata"]["annotations"]["graphblocks.ai/release-digest"] == release.content_digest()
    assert application["spec"]["project"] == "support"
    assert application["spec"]["source"] == {
        "repoURL": "https://git.example.com/platform/graphblocks.git",
        "path": "clusters/prod/support-agent",
        "targetRevision": "main",
    }
    assert application["spec"]["destination"] == {
        "server": "https://kubernetes.default.svc",
        "namespace": "support",
    }
    assert application["spec"]["syncPolicy"] == {"automated": {"prune": True, "selfHeal": True}}


def test_gitops_package_renders_flux_kustomization(monkeypatch) -> None:
    graphblocks_gitops = _import_gitops(monkeypatch)
    release = _release(monkeypatch)
    source_ref = graphblocks_gitops.FluxSourceRef(kind="GitRepository", name="graphblocks-platform")

    kustomization = graphblocks_gitops.render_flux_kustomization(
        name="support-agent",
        release=release,
        source_ref=source_ref,
        path="./clusters/prod/support-agent",
        namespace="support",
        interval="5m",
        prune=True,
    )

    assert kustomization["apiVersion"] == "kustomize.toolkit.fluxcd.io/v1"
    assert kustomization["kind"] == "Kustomization"
    assert kustomization["metadata"]["annotations"]["graphblocks.ai/release-version"] == "2026.06.23.1"
    assert kustomization["spec"]["sourceRef"] == {"kind": "GitRepository", "name": "graphblocks-platform"}
    assert kustomization["spec"]["path"] == "./clusters/prod/support-agent"
    assert kustomization["spec"]["targetNamespace"] == "support"
    assert kustomization["spec"]["interval"] == "5m"
    assert kustomization["spec"]["prune"] is True


def test_gitops_manifest_set_digest_is_independent_of_document_order(monkeypatch) -> None:
    graphblocks_gitops = _import_gitops(monkeypatch)
    release = _release(monkeypatch)
    source = graphblocks_gitops.GitOpsSource("https://git.example.com/platform.git", "clusters/prod")
    destination = graphblocks_gitops.GitOpsDestination(namespace="support")
    application = graphblocks_gitops.render_argocd_application(
        "support-agent",
        release=release,
        source=source,
        destination=destination,
    )
    kustomization = graphblocks_gitops.render_flux_kustomization(
        "support-agent",
        release=release,
        source_ref=graphblocks_gitops.FluxSourceRef("GitRepository", "platform"),
        path="./clusters/prod",
        namespace="support",
    )

    left = graphblocks_gitops.GitOpsManifestSet((application, kustomization))
    right = graphblocks_gitops.GitOpsManifestSet((kustomization, application))

    assert left.content_digest() == right.content_digest()
    assert [manifest["kind"] for manifest in left.by_kind("Application")] == ["Application"]

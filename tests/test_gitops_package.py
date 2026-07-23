from __future__ import annotations

import importlib

import pytest


def _import_gitops(monkeypatch):
    return importlib.import_module("graphblocks.integrations.gitops")


def _release(monkeypatch):
    _import_gitops(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
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


def test_gitops_package_renders_graphblocks_desired_state(monkeypatch) -> None:
    graphblocks_gitops = _import_gitops(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    release = _release(monkeypatch)
    revision = graphblocks_deployment.DeploymentRevision(
        revision_id="rev-1",
        release_digest=release.content_digest(),
        deployment_spec_hash="sha256:deployment",
        physical_plan_hash="sha256:plan",
        resolved_binding_hash="sha256:binding",
        target_capability_hash="sha256:targets",
        created_at="2026-06-29T00:00:00Z",
    )
    desired_state = {
        "deploymentId": "support-production",
        "profile": "production",
        "targets": {"control": {"image": "registry.example.com/control@sha256:control"}},
    }

    document = graphblocks_gitops.render_graphblocks_desired_state(
        "support-production",
        release=release,
        deployment_revision=revision,
        desired_state=desired_state,
        namespace="support",
    )
    desired_state["targets"]["control"]["image"] = "mutated"

    assert document["apiVersion"] == "graphblocks.ai/gitops/v1alpha1"
    assert document["kind"] == "GraphBlocksDeploymentDesiredState"
    assert document["metadata"]["namespace"] == "support"
    assert document["metadata"]["annotations"]["graphblocks.ai/release-digest"] == release.content_digest()
    assert document["spec"]["release"] == {
        "name": "support-agent",
        "version": "2026.06.23.1",
        "digest": release.content_digest(),
        "bundleDigest": "sha256:bundle",
    }
    assert document["spec"]["deploymentRevision"] == {
        "revisionId": "rev-1",
        "releaseDigest": release.content_digest(),
        "deploymentSpecHash": "sha256:deployment",
        "physicalPlanHash": "sha256:plan",
        "resolvedBindingHash": "sha256:binding",
        "targetCapabilityHash": "sha256:targets",
        "createdAt": "2026-06-29T00:00:00Z",
        "contentDigest": revision.content_digest(),
    }
    assert document["spec"]["desiredState"]["targets"]["control"]["image"] == (
        "registry.example.com/control@sha256:control"
    )


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
    digest_before_public_mutation = left.content_digest()
    left.documents[0]["metadata"]["name"] = "mutated"  # type: ignore[index]
    assert left.content_digest() == digest_before_public_mutation
    assert left.documents[0]["metadata"]["name"] != "mutated"  # type: ignore[index]


def test_gitops_rejects_coerced_flags_and_mismatched_release_revision(
    monkeypatch,
) -> None:
    graphblocks_gitops = _import_gitops(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    release = _release(monkeypatch)
    source = graphblocks_gitops.GitOpsSource(
        "https://git.example.com/platform.git",
        "clusters/prod",
    )
    destination = graphblocks_gitops.GitOpsDestination(namespace="support")

    with pytest.raises(graphblocks_gitops.GitOpsContractError, match="automated"):
        graphblocks_gitops.render_argocd_application(
            "support-agent",
            release=release,
            source=source,
            destination=destination,
            automated="false",  # type: ignore[arg-type]
        )
    with pytest.raises(graphblocks_gitops.GitOpsContractError, match="prune"):
        graphblocks_gitops.render_flux_kustomization(
            "support-agent",
            release=release,
            source_ref=graphblocks_gitops.FluxSourceRef("GitRepository", "platform"),
            path="./clusters/prod",
            namespace="support",
            prune="false",  # type: ignore[arg-type]
        )

    revision = graphblocks_deployment.DeploymentRevision(
        revision_id="revision-foreign",
        release_digest="sha256:another-release",
        deployment_spec_hash="sha256:deployment",
        physical_plan_hash="sha256:plan",
        resolved_binding_hash="sha256:binding",
        target_capability_hash="sha256:targets",
        created_at="2026-06-29T00:00:00Z",
    )
    with pytest.raises(graphblocks_gitops.GitOpsContractError, match="release_digest"):
        graphblocks_gitops.render_graphblocks_desired_state(
            "support-production",
            release=release,
            deployment_revision=revision,
            desired_state={},
        )


def test_gitops_rejects_non_json_state_non_string_metadata_and_duplicate_identity(
    monkeypatch,
) -> None:
    graphblocks_gitops = _import_gitops(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    release = _release(monkeypatch)
    revision = graphblocks_deployment.DeploymentRevision(
        revision_id="rev-1",
        release_digest=release.content_digest(),
        deployment_spec_hash="sha256:deployment",
        physical_plan_hash="sha256:plan",
        resolved_binding_hash="sha256:binding",
        target_capability_hash="sha256:targets",
        created_at="2026-06-29T00:00:00Z",
    )

    with pytest.raises(graphblocks_gitops.GitOpsContractError, match="strict JSON"):
        graphblocks_gitops.render_graphblocks_desired_state(
            "support-production",
            release=release,
            deployment_revision=revision,
            desired_state={"threshold": float("nan")},
        )
    with pytest.raises(graphblocks_gitops.GitOpsContractError, match="strings"):
        graphblocks_gitops.render_argocd_application(
            "support-agent",
            release=release,
            source=graphblocks_gitops.GitOpsSource(
                "https://git.example.com/platform.git",
                "clusters/prod",
            ),
            destination=graphblocks_gitops.GitOpsDestination(namespace="support"),
            labels={1: "invalid"},  # type: ignore[dict-item]
        )

    document = graphblocks_gitops.render_graphblocks_desired_state(
        "support-production",
        release=release,
        deployment_revision=revision,
        desired_state={},
    )
    with pytest.raises(graphblocks_gitops.GitOpsContractError, match="duplicate"):
        graphblocks_gitops.GitOpsManifestSet((document, document))

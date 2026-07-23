from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json

from graphblocks.deployment import DeploymentRevision, GraphRelease


GitOpsManifest = dict[str, object]


class GitOpsContractError(ValueError):
    """Raised when a GitOps manifest contract is invalid."""


def _canonical_dumps(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sorted_str_mapping(values: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise GitOpsContractError("GitOps metadata must be a mapping of strings")
    copied = dict(values)
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in copied.items()
    ):
        raise GitOpsContractError("GitOps metadata keys and values must be strings")
    return {key: copied[key] for key in sorted(copied)}


def _release_annotations(release: GraphRelease, annotations: Mapping[str, str] | None) -> dict[str, str]:
    merged = {
        **_sorted_str_mapping(annotations or {}),
        "graphblocks.ai/release-name": release.name,
        "graphblocks.ai/release-version": release.version,
        "graphblocks.ai/release-digest": release.content_digest(),
    }
    if release.bundle_digest is not None:
        merged["graphblocks.ai/bundle-digest"] = release.bundle_digest
    return {key: merged[key] for key in sorted(merged)}


def _metadata(
    name: str,
    release: GraphRelease,
    labels: Mapping[str, str] | None,
    annotations: Mapping[str, str] | None,
    namespace: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "name": name,
        "labels": _sorted_str_mapping(
            {
                **(labels or {}),
                "app.kubernetes.io/managed-by": "graphblocks",
                "graphblocks.ai/release": release.name,
            }
        ),
        "annotations": _release_annotations(release, annotations),
    }
    if namespace is not None:
        metadata["namespace"] = namespace
    return metadata


@dataclass(frozen=True, slots=True)
class GitOpsSource:
    repo_url: str
    path: str
    target_revision: str = "HEAD"

    def __post_init__(self) -> None:
        for field_name, value in (
            ("repo_url", self.repo_url),
            ("path", self.path),
            ("target_revision", self.target_revision),
        ):
            if not isinstance(value, str) or not value.strip():
                raise GitOpsContractError(f"{field_name} must not be empty")

    def argocd_contract(self) -> dict[str, str]:
        return {
            "repoURL": self.repo_url,
            "path": self.path,
            "targetRevision": self.target_revision,
        }


@dataclass(frozen=True, slots=True)
class GitOpsDestination:
    server: str = "https://kubernetes.default.svc"
    namespace: str = "default"

    def __post_init__(self) -> None:
        if not isinstance(self.server, str) or not self.server.strip():
            raise GitOpsContractError("destination server must not be empty")
        if not isinstance(self.namespace, str) or not self.namespace.strip():
            raise GitOpsContractError("destination namespace must not be empty")

    def argocd_contract(self) -> dict[str, str]:
        return {
            "server": self.server,
            "namespace": self.namespace,
        }


@dataclass(frozen=True, slots=True)
class FluxSourceRef:
    kind: str
    name: str
    namespace: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise GitOpsContractError("Flux source kind must not be empty")
        if not isinstance(self.name, str) or not self.name.strip():
            raise GitOpsContractError("Flux source name must not be empty")
        if self.namespace is not None and (
            not isinstance(self.namespace, str) or not self.namespace.strip()
        ):
            raise GitOpsContractError("Flux source namespace must not be empty")

    def flux_contract(self) -> dict[str, str]:
        contract = {
            "kind": self.kind,
            "name": self.name,
        }
        if self.namespace is not None:
            contract["namespace"] = self.namespace
        return contract


@dataclass(frozen=True, slots=True, init=False)
class GitOpsManifestSet:
    _documents: tuple[GitOpsManifest, ...] = field(repr=False)

    def __init__(self, documents: Iterable[GitOpsManifest]) -> None:
        try:
            materialized = tuple(documents)
        except TypeError as error:
            raise GitOpsContractError("GitOps documents must be mappings") from error
        if any(not isinstance(document, Mapping) for document in materialized):
            raise GitOpsContractError("GitOps documents must be mappings")
        copied = tuple(deepcopy(dict(document)) for document in materialized)
        try:
            _canonical_dumps(copied)
        except (RecursionError, TypeError, ValueError) as error:
            raise GitOpsContractError("GitOps documents must contain strict JSON values") from error
        identities: set[tuple[str, str, str, str]] = set()
        for document in copied:
            metadata = document.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            identity_values = (
                document.get("apiVersion"),
                document.get("kind"),
                metadata.get("name"),
                metadata.get("namespace", ""),
            )
            if (
                not all(
                    isinstance(value, str) and value
                    for value in identity_values[:3]
                )
                or not isinstance(identity_values[3], str)
            ):
                continue
            identity = (
                identity_values[0],
                identity_values[1],
                identity_values[2],
                identity_values[3],
            )
            if identity in identities:
                raise GitOpsContractError(
                    f"GitOps documents contain duplicate resource identity {identity!r}"
                )
            identities.add(identity)
        object.__setattr__(self, "_documents", copied)

    @property
    def documents(self) -> tuple[GitOpsManifest, ...]:
        return tuple(deepcopy(document) for document in self._documents)

    def by_kind(self, kind: str) -> tuple[GitOpsManifest, ...]:
        return tuple(deepcopy(document) for document in self._documents if document.get("kind") == kind)

    def content_digest(self) -> str:
        documents = [deepcopy(document) for document in self._documents]
        documents.sort(key=_canonical_dumps)
        return "sha256:" + hashlib.sha256(
            _canonical_dumps({"documents": documents}).encode("utf-8")
        ).hexdigest()


def render_argocd_application(
    name: str,
    *,
    release: GraphRelease,
    source: GitOpsSource,
    destination: GitOpsDestination,
    project: str = "default",
    automated: bool = False,
    control_namespace: str | None = None,
    labels: Mapping[str, str] | None = None,
    annotations: Mapping[str, str] | None = None,
) -> GitOpsManifest:
    if not isinstance(name, str) or not name.strip():
        raise GitOpsContractError("Application name must not be empty")
    if not isinstance(project, str) or not project.strip():
        raise GitOpsContractError("Application project must not be empty")
    if not isinstance(automated, bool):
        raise GitOpsContractError("Application automated must be a boolean")
    if not isinstance(release, GraphRelease):
        raise GitOpsContractError("release must be a GraphRelease")
    if not isinstance(source, GitOpsSource):
        raise GitOpsContractError("source must be a GitOpsSource")
    if not isinstance(destination, GitOpsDestination):
        raise GitOpsContractError("destination must be a GitOpsDestination")
    spec: dict[str, object] = {
        "project": project,
        "source": source.argocd_contract(),
        "destination": destination.argocd_contract(),
    }
    if automated:
        spec["syncPolicy"] = {"automated": {"prune": True, "selfHeal": True}}
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": _metadata(name, release, labels, annotations, control_namespace),
        "spec": spec,
    }


def render_flux_kustomization(
    name: str,
    *,
    release: GraphRelease,
    source_ref: FluxSourceRef,
    path: str,
    namespace: str,
    interval: str = "1m",
    prune: bool = True,
    labels: Mapping[str, str] | None = None,
    annotations: Mapping[str, str] | None = None,
) -> GitOpsManifest:
    for field_name, value in (
        ("Kustomization name", name),
        ("Kustomization path", path),
        ("Kustomization namespace", namespace),
        ("Kustomization interval", interval),
    ):
        if not isinstance(value, str) or not value.strip():
            raise GitOpsContractError(f"{field_name} must not be empty")
    if not isinstance(prune, bool):
        raise GitOpsContractError("Kustomization prune must be a boolean")
    if not isinstance(release, GraphRelease):
        raise GitOpsContractError("release must be a GraphRelease")
    if not isinstance(source_ref, FluxSourceRef):
        raise GitOpsContractError("source_ref must be a FluxSourceRef")
    return {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": _metadata(name, release, labels, annotations, namespace),
        "spec": {
            "interval": interval,
            "path": path,
            "prune": prune,
            "sourceRef": source_ref.flux_contract(),
            "targetNamespace": namespace,
        },
    }


def render_graphblocks_desired_state(
    name: str,
    *,
    release: GraphRelease,
    deployment_revision: DeploymentRevision,
    desired_state: Mapping[str, object],
    namespace: str | None = None,
    labels: Mapping[str, str] | None = None,
    annotations: Mapping[str, str] | None = None,
) -> GitOpsManifest:
    if not isinstance(name, str) or not name.strip():
        raise GitOpsContractError("GraphBlocks desired state name must not be empty")
    if not isinstance(release, GraphRelease):
        raise GitOpsContractError("release must be a GraphRelease")
    if not isinstance(deployment_revision, DeploymentRevision):
        raise GitOpsContractError("deployment_revision must be a DeploymentRevision")
    if deployment_revision.release_digest != release.content_digest():
        raise GitOpsContractError(
            "deployment revision release_digest must match the rendered release"
        )
    if not isinstance(desired_state, Mapping):
        raise GitOpsContractError("desired_state must be a mapping")
    copied_desired_state = deepcopy(dict(desired_state))
    try:
        _canonical_dumps(copied_desired_state)
    except (RecursionError, TypeError, ValueError) as error:
        raise GitOpsContractError(
            "desired_state must contain strict JSON values"
        ) from error
    release_contract: dict[str, object] = {
        "name": release.name,
        "version": release.version,
        "digest": release.content_digest(),
    }
    if release.bundle_digest is not None:
        release_contract["bundleDigest"] = release.bundle_digest
    return {
        "apiVersion": "graphblocks.ai/gitops/v1alpha1",
        "kind": "GraphBlocksDeploymentDesiredState",
        "metadata": _metadata(name, release, labels, annotations, namespace),
        "spec": {
            "release": release_contract,
            "deploymentRevision": {
                "revisionId": deployment_revision.revision_id,
                "releaseDigest": deployment_revision.release_digest,
                "deploymentSpecHash": deployment_revision.deployment_spec_hash,
                "physicalPlanHash": deployment_revision.physical_plan_hash,
                "resolvedBindingHash": deployment_revision.resolved_binding_hash,
                "targetCapabilityHash": deployment_revision.target_capability_hash,
                "createdAt": deployment_revision.created_at,
                "contentDigest": deployment_revision.content_digest(),
            },
            "desiredState": copied_desired_state,
        },
    }


__all__ = [
    "FluxSourceRef",
    "GitOpsContractError",
    "GitOpsDestination",
    "GitOpsManifest",
    "GitOpsManifestSet",
    "GitOpsSource",
    "render_argocd_application",
    "render_flux_kustomization",
    "render_graphblocks_desired_state",
]

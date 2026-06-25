from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json

from graphblocks_deployment import GraphRelease


GRAPHBLOCKS_RELEASE_ARTIFACT_TYPE = "application/vnd.graphblocks.release.v1"
GRAPHBLOCKS_RELEASE_CONFIG_MEDIA_TYPE = "application/vnd.graphblocks.release.config.v1+json"
OCI_IMAGE_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"


class OciContractError(ValueError):
    """Raised when an OCI contract is invalid."""


def _canonical_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _validate_digest(digest: str) -> None:
    if not digest.startswith("sha256:") or len(digest) <= len("sha256:"):
        raise OciContractError("OCI digests must use sha256:<digest>")


def _sorted_annotations(annotations: Mapping[str, str]) -> dict[str, str]:
    return {str(key): str(value) for key, value in sorted(dict(annotations).items())}


@dataclass(frozen=True, slots=True)
class OciDescriptor:
    media_type: str
    digest: str
    size: int
    annotations: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.media_type.strip():
            raise OciContractError("descriptor media_type must not be empty")
        _validate_digest(self.digest)
        if self.size < 0:
            raise OciContractError("descriptor size must not be negative")
        object.__setattr__(self, "annotations", _sorted_annotations(self.annotations))

    def descriptor_contract(self) -> dict[str, object]:
        contract: dict[str, object] = {
            "mediaType": self.media_type,
            "digest": self.digest,
            "size": self.size,
        }
        if self.annotations:
            contract["annotations"] = deepcopy(dict(self.annotations))
        return contract


@dataclass(frozen=True, slots=True)
class OciManifest:
    config: OciDescriptor
    layers: tuple[OciDescriptor, ...] = field(default_factory=tuple)
    annotations: Mapping[str, str] = field(default_factory=dict)
    artifact_type: str = GRAPHBLOCKS_RELEASE_ARTIFACT_TYPE
    media_type: str = OCI_IMAGE_MANIFEST_MEDIA_TYPE

    def __post_init__(self) -> None:
        if not self.artifact_type.strip():
            raise OciContractError("artifact_type must not be empty")
        if not self.media_type.strip():
            raise OciContractError("manifest media_type must not be empty")
        object.__setattr__(self, "layers", tuple(self.layers))
        object.__setattr__(self, "annotations", _sorted_annotations(self.annotations))

    def manifest_contract(self) -> dict[str, object]:
        contract: dict[str, object] = {
            "schemaVersion": 2,
            "mediaType": self.media_type,
            "artifactType": self.artifact_type,
            "config": self.config.descriptor_contract(),
            "layers": [layer.descriptor_contract() for layer in self.layers],
        }
        if self.annotations:
            contract["annotations"] = deepcopy(dict(self.annotations))
        return contract

    def manifest_json(self) -> str:
        return _canonical_dumps(self.manifest_contract())

    def manifest_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.manifest_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OciArtifactReference:
    registry: str
    repository: str
    tag: str | None = None
    digest: str | None = None

    def __post_init__(self) -> None:
        if not self.registry.strip():
            raise OciContractError("registry must not be empty")
        if not self.repository.strip():
            raise OciContractError("repository must not be empty")
        if (self.tag is None) == (self.digest is None):
            raise OciContractError("exactly one of tag or digest must be provided")
        if self.tag is not None and not self.tag.strip():
            raise OciContractError("tag must not be empty")
        if self.digest is not None:
            _validate_digest(self.digest)

    def ref(self) -> str:
        base = f"{self.registry}/{self.repository}"
        if self.digest is not None:
            return f"{base}@{self.digest}"
        return f"{base}:{self.tag}"


def build_release_manifest(
    release: GraphRelease,
    *,
    bundle_descriptor: OciDescriptor,
    provenance_descriptor: OciDescriptor | None = None,
    signature_descriptor: OciDescriptor | None = None,
    config_descriptor: OciDescriptor | None = None,
    annotations: Mapping[str, str] | None = None,
) -> OciManifest:
    release_annotations = {
        **_sorted_annotations(annotations or {}),
        "graphblocks.ai/release-name": release.name,
        "graphblocks.ai/release-version": release.version,
        "graphblocks.ai/release-digest": release.content_digest(),
    }
    if release.bundle_digest is not None:
        release_annotations["graphblocks.ai/bundle-digest"] = release.bundle_digest
    if release.bundle_media_type is not None:
        release_annotations["graphblocks.ai/bundle-media-type"] = release.bundle_media_type
    layers = [bundle_descriptor]
    if provenance_descriptor is not None:
        layers.append(provenance_descriptor)
        release_annotations["graphblocks.ai/provenance-digest"] = provenance_descriptor.digest
    if signature_descriptor is not None:
        layers.append(signature_descriptor)
        release_annotations["graphblocks.ai/signature-digest"] = signature_descriptor.digest

    return OciManifest(
        config=config_descriptor
        or OciDescriptor(
            media_type=GRAPHBLOCKS_RELEASE_CONFIG_MEDIA_TYPE,
            digest=release.content_digest(),
            size=0,
        ),
        layers=tuple(layers),
        annotations=release_annotations,
    )


__all__ = [
    "GRAPHBLOCKS_RELEASE_ARTIFACT_TYPE",
    "GRAPHBLOCKS_RELEASE_CONFIG_MEDIA_TYPE",
    "OCI_IMAGE_MANIFEST_MEDIA_TYPE",
    "OciArtifactReference",
    "OciContractError",
    "OciDescriptor",
    "OciManifest",
    "build_release_manifest",
]

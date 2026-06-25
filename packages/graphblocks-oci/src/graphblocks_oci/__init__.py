from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json

from graphblocks_deployment import GraphRelease


GRAPHBLOCKS_RELEASE_ARTIFACT_TYPE = "application/vnd.graphblocks.release.v1"
GRAPHBLOCKS_RELEASE_CONFIG_MEDIA_TYPE = "application/vnd.graphblocks.release.config.v1+json"
OCI_IMAGE_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
CYCLONEDX_JSON_MEDIA_TYPE = "application/vnd.cyclonedx+json"


class OciContractError(ValueError):
    """Raised when an OCI contract is invalid."""


def _canonical_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _validate_digest(digest: str) -> None:
    if not digest.startswith("sha256:") or len(digest) <= len("sha256:"):
        raise OciContractError("OCI digests must use sha256:<digest>")


def _payload_bytes(payload: str | bytes) -> bytes:
    if isinstance(payload, bytes):
        return payload
    return payload.encode("utf-8")


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

    @classmethod
    def from_payload(
        cls,
        media_type: str,
        payload: str | bytes,
        *,
        annotations: Mapping[str, str] | None = None,
    ) -> OciDescriptor:
        payload_bytes = _payload_bytes(payload)
        return cls(
            media_type=media_type,
            digest="sha256:" + hashlib.sha256(payload_bytes).hexdigest(),
            size=len(payload_bytes),
            annotations=annotations or {},
        )


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


@dataclass(frozen=True, slots=True)
class ReleaseImageBuild:
    release: GraphRelease
    tag_reference: OciArtifactReference
    manifest: OciManifest

    @property
    def manifest_digest(self) -> str:
        return self.manifest.manifest_digest()

    @property
    def tag_ref(self) -> str:
        return self.tag_reference.ref()

    @property
    def digest_ref(self) -> str:
        return OciArtifactReference(
            registry=self.tag_reference.registry,
            repository=self.tag_reference.repository,
            digest=self.manifest_digest,
        ).ref()

    def image_build_contract(self) -> dict[str, object]:
        return {
            "release_name": self.release.name,
            "release_version": self.release.version,
            "release_digest": self.release.content_digest(),
            "tag_ref": self.tag_ref,
            "digest_ref": self.digest_ref,
            "manifest_digest": self.manifest_digest,
            "manifest_media_type": self.manifest.media_type,
            "artifact_type": self.manifest.artifact_type,
            "layers": [layer.descriptor_contract() for layer in self.manifest.layers],
        }

    def content_digest(self) -> str:
        return "sha256:" + hashlib.sha256(_canonical_dumps(self.image_build_contract()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BuildProvenanceAttestation:
    subject_name: str
    subject_digest: str
    builder_id: str
    build_type: str
    invocation_id: str | None = None
    materials: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.subject_name.strip():
            raise OciContractError("provenance subject_name must not be empty")
        _validate_digest(self.subject_digest)
        if not self.builder_id.strip():
            raise OciContractError("provenance builder_id must not be empty")
        if not self.build_type.strip():
            raise OciContractError("provenance build_type must not be empty")
        materials = {str(key): str(value) for key, value in sorted(dict(self.materials).items())}
        for digest in materials.values():
            _validate_digest(digest)
        object.__setattr__(self, "materials", materials)
        object.__setattr__(
            self,
            "metadata",
            {str(key): str(value) for key, value in sorted(dict(self.metadata).items())},
        )

    def attestation_contract(self) -> dict[str, object]:
        metadata = deepcopy(dict(self.metadata))
        if self.invocation_id is not None:
            metadata["invocationId"] = self.invocation_id
        return {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": [
                {
                    "name": self.subject_name,
                    "digest": {"sha256": self.subject_digest.removeprefix("sha256:")},
                }
            ],
            "predicateType": "https://slsa.dev/provenance/v1",
            "predicate": {
                "buildDefinition": {
                    "buildType": self.build_type,
                    "externalParameters": {"release": self.subject_name},
                    "internalParameters": {},
                    "resolvedDependencies": [
                        {
                            "uri": uri,
                            "digest": {"sha256": digest.removeprefix("sha256:")},
                        }
                        for uri, digest in self.materials.items()
                    ],
                },
                "runDetails": {
                    "builder": {"id": self.builder_id},
                    "metadata": metadata,
                },
            },
        }

    def attestation_json(self) -> str:
        return _canonical_dumps(self.attestation_contract())

    def attestation_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.attestation_json().encode("utf-8")).hexdigest()


def build_release_provenance_attestation(
    release: GraphRelease,
    *,
    builder_id: str,
    build_type: str,
    invocation_id: str | None = None,
    materials: Mapping[str, str] | None = None,
    metadata: Mapping[str, str] | None = None,
) -> BuildProvenanceAttestation:
    return BuildProvenanceAttestation(
        subject_name=f"{release.name}:{release.version}",
        subject_digest=release.content_digest(),
        builder_id=builder_id,
        build_type=build_type,
        invocation_id=invocation_id,
        materials=materials or {},
        metadata=metadata or {},
    )


@dataclass(frozen=True, slots=True)
class ReleaseSbom:
    release: GraphRelease
    components: tuple[Mapping[str, object], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        normalized = []
        for component in self.components:
            if not isinstance(component, Mapping):
                raise OciContractError("SBOM components must be mappings")
            component_type = component.get("type")
            name = component.get("name")
            if not isinstance(component_type, str) or not component_type.strip():
                raise OciContractError("SBOM component type must not be empty")
            if not isinstance(name, str) or not name.strip():
                raise OciContractError("SBOM component name must not be empty")
            item = {
                str(key): deepcopy(value)
                for key, value in sorted(component.items())
                if value is not None
            }
            normalized.append(item)
        object.__setattr__(
            self,
            "components",
            tuple(sorted(normalized, key=lambda item: (str(item.get("name")), str(item.get("type"))))),
        )

    def sbom_contract(self) -> dict[str, object]:
        return {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "metadata": {
                "component": {
                    "type": "application",
                    "name": self.release.name,
                    "version": self.release.version,
                    "bom-ref": self.release.content_digest(),
                }
            },
            "components": [deepcopy(dict(component)) for component in self.components],
        }

    def sbom_json(self) -> str:
        return _canonical_dumps(self.sbom_contract())

    def sbom_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.sbom_json().encode("utf-8")).hexdigest()

    def to_descriptor(self) -> OciDescriptor:
        return OciDescriptor.from_payload(
            CYCLONEDX_JSON_MEDIA_TYPE,
            self.sbom_json(),
            annotations={
                "graphblocks.ai/artifact-kind": "sbom",
                "graphblocks.ai/release-digest": self.release.content_digest(),
            },
        )


def build_release_sbom(
    release: GraphRelease,
    *,
    package_lock: Mapping[str, object] | None = None,
    external_components: Iterable[Mapping[str, object]] = (),
) -> ReleaseSbom:
    components: list[Mapping[str, object]] = [dict(component) for component in external_components]
    if package_lock is not None:
        raw_packages = package_lock.get("packages", ())
        if not isinstance(raw_packages, list | tuple):
            raise OciContractError("package lock packages must be a sequence")
        for raw_package in raw_packages:
            if not isinstance(raw_package, Mapping):
                raise OciContractError("package lock package entries must be mappings")
            distribution = raw_package.get("distribution", raw_package.get("name"))
            if not isinstance(distribution, str) or not distribution.strip():
                raise OciContractError("package lock package distribution must not be empty")
            version = raw_package.get("versionConstraint", raw_package.get("version_constraint"))
            component: dict[str, object] = {
                "type": "library",
                "name": distribution,
                "version": version,
            }
            properties = []
            for source_key, property_name in (
                ("kind", "graphblocks:kind"),
                ("layer", "graphblocks:layer"),
                ("stability", "graphblocks:stability"),
            ):
                value = raw_package.get(source_key)
                if isinstance(value, str) and value.strip():
                    properties.append({"name": property_name, "value": value})
            if properties:
                component["properties"] = sorted(properties, key=lambda item: str(item["name"]))
            components.append(component)
    return ReleaseSbom(release=release, components=tuple(components))


@dataclass(frozen=True, slots=True)
class SignatureVerificationResult:
    policy_id: str
    signature_digest: str
    signer: str
    subject_digest: str
    verified: bool
    reason_codes: tuple[str, ...] = field(default_factory=tuple)

    def verification_contract(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "signature_digest": self.signature_digest,
            "signer": self.signer,
            "subject_digest": self.subject_digest,
            "verified": self.verified,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class SignatureVerificationPolicy:
    policy_id: str
    trusted_signers: tuple[str, ...] = field(default_factory=tuple)
    required_annotations: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise OciContractError("signature policy_id must not be empty")
        object.__setattr__(
            self,
            "trusted_signers",
            tuple(sorted(str(signer) for signer in self.trusted_signers)),
        )
        object.__setattr__(
            self,
            "required_annotations",
            {str(key): str(value) for key, value in sorted(dict(self.required_annotations).items())},
        )

    def evaluate(
        self,
        signature_descriptor: OciDescriptor,
        *,
        signer: str,
        subject_digest: str,
    ) -> SignatureVerificationResult:
        _validate_digest(subject_digest)
        reason_codes: list[str] = []
        signed_subject = signature_descriptor.annotations.get("graphblocks.ai/release-digest")
        if signed_subject != subject_digest:
            reason_codes.append("signature.subject_digest_mismatch")
        for key, expected in self.required_annotations.items():
            if signature_descriptor.annotations.get(key) != expected:
                reason_codes.append(f"signature.annotation_mismatch:{key}")
        if self.trusted_signers and signer not in self.trusted_signers:
            reason_codes.append("signature.untrusted_signer")
        return SignatureVerificationResult(
            policy_id=self.policy_id,
            signature_digest=signature_descriptor.digest,
            signer=signer,
            subject_digest=subject_digest,
            verified=not reason_codes,
            reason_codes=tuple(reason_codes),
        )


def build_release_manifest(
    release: GraphRelease,
    *,
    bundle_descriptor: OciDescriptor,
    sbom_descriptor: OciDescriptor | None = None,
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
    if sbom_descriptor is not None:
        layers.append(sbom_descriptor)
        release_annotations["graphblocks.ai/sbom-digest"] = sbom_descriptor.digest
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


def build_release_image(
    release: GraphRelease,
    *,
    registry: str,
    repository: str,
    tag: str | None = None,
    bundle_descriptor: OciDescriptor,
    sbom_descriptor: OciDescriptor | None = None,
    provenance_descriptor: OciDescriptor | None = None,
    signature_descriptor: OciDescriptor | None = None,
    config_descriptor: OciDescriptor | None = None,
    annotations: Mapping[str, str] | None = None,
) -> ReleaseImageBuild:
    manifest = build_release_manifest(
        release,
        bundle_descriptor=bundle_descriptor,
        sbom_descriptor=sbom_descriptor,
        provenance_descriptor=provenance_descriptor,
        signature_descriptor=signature_descriptor,
        config_descriptor=config_descriptor,
        annotations=annotations,
    )
    return ReleaseImageBuild(
        release=release,
        tag_reference=OciArtifactReference(
            registry=registry,
            repository=repository,
            tag=tag or release.version,
        ),
        manifest=manifest,
    )


__all__ = [
    "BuildProvenanceAttestation",
    "CYCLONEDX_JSON_MEDIA_TYPE",
    "GRAPHBLOCKS_RELEASE_ARTIFACT_TYPE",
    "GRAPHBLOCKS_RELEASE_CONFIG_MEDIA_TYPE",
    "OCI_IMAGE_MANIFEST_MEDIA_TYPE",
    "OciArtifactReference",
    "OciContractError",
    "OciDescriptor",
    "OciManifest",
    "ReleaseImageBuild",
    "ReleaseSbom",
    "SignatureVerificationPolicy",
    "SignatureVerificationResult",
    "build_release_provenance_attestation",
    "build_release_image",
    "build_release_sbom",
    "build_release_manifest",
]

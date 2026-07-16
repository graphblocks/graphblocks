from __future__ import annotations

import importlib
import hashlib
import json

import pytest


def _import_oci(monkeypatch):
    return importlib.import_module("graphblocks.integrations.oci")


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def test_oci_package_builds_release_manifest_with_graphblocks_annotations(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    release = (
        graphblocks_deployment.GraphRelease("support-agent", "2026.06.23.1")
        .with_bundle(_digest("bundle"), "application/vnd.graphblocks.release.bundle.v1+tar")
        .with_graph("turn", graphblocks_deployment.GraphReleaseGraph("sha256:graph", "sha256:plan"))
    )
    bundle = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.graphblocks.release.bundle.v1+tar",
        digest=_digest("bundle"),
        size=4096,
    )

    manifest = graphblocks_oci.build_release_manifest(
        release,
        bundle_descriptor=bundle,
        config_descriptor=graphblocks_oci.OciDescriptor(
            media_type="application/vnd.graphblocks.release.config.v1+json",
            digest=_digest("config"),
            size=256,
        ),
    )
    contract = manifest.manifest_contract()

    assert contract["schemaVersion"] == 2
    assert contract["artifactType"] == "application/vnd.graphblocks.release.v1"
    assert contract["config"]["digest"] == _digest("config")
    assert contract["layers"] == [bundle.descriptor_contract()]
    assert contract["annotations"]["graphblocks.ai/release-name"] == "support-agent"
    assert contract["annotations"]["graphblocks.ai/release-version"] == "2026.06.23.1"
    assert contract["annotations"]["graphblocks.ai/release-digest"] == release.content_digest()
    assert manifest.manifest_digest().startswith("sha256:")


@pytest.mark.parametrize(
    ("descriptor", "message"),
    (
        (
            {"digest": _digest("different")},
            "digest must match",
        ),
        (
            {"media_type": "application/vnd.example.wrong+tar"},
            "media_type must match",
        ),
    ),
)
def test_oci_release_manifest_rejects_descriptor_that_contradicts_release_bundle(
    monkeypatch,
    descriptor: dict[str, str],
    message: str,
) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    media_type = "application/vnd.graphblocks.release.bundle.v1+tar"
    release = graphblocks_deployment.GraphRelease("support-agent", "2026.06.23.1").with_bundle(
        _digest("bundle"),
        media_type,
    )
    values = {
        "media_type": media_type,
        "digest": _digest("bundle"),
        "size": 4096,
        **descriptor,
    }

    with pytest.raises(graphblocks_oci.OciContractError, match=message):
        graphblocks_oci.build_release_manifest(
            release,
            bundle_descriptor=graphblocks_oci.OciDescriptor(**values),
        )


def test_oci_release_manifest_includes_provenance_and_signature_descriptors(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    release = (
        graphblocks_deployment.GraphRelease("support-agent", "2026.06.23.1")
        .with_bundle(_digest("bundle"), "application/vnd.graphblocks.release.bundle.v1+tar")
        .with_graph("turn", graphblocks_deployment.GraphReleaseGraph("sha256:graph", "sha256:plan"))
    )
    bundle = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.graphblocks.release.bundle.v1+tar",
        digest=_digest("bundle"),
        size=4096,
    )
    provenance = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.in-toto+json",
        digest=_digest("provenance"),
        size=512,
    )
    signature = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.dev.cosign.simplesigning.v1+json",
        digest=_digest("signature"),
        size=256,
    )

    manifest = graphblocks_oci.build_release_manifest(
        release,
        bundle_descriptor=bundle,
        provenance_descriptor=provenance,
        signature_descriptor=signature,
    )
    contract = manifest.manifest_contract()

    assert contract["config"] == {
        "mediaType": "application/vnd.oci.empty.v1+json",
        "digest": _digest("{}"),
        "size": 2,
    }
    assert contract["layers"] == [
        bundle.descriptor_contract(),
        provenance.descriptor_contract(),
        signature.descriptor_contract(),
    ]
    assert contract["annotations"]["graphblocks.ai/provenance-digest"] == _digest("provenance")
    assert contract["annotations"]["graphblocks.ai/signature-digest"] == _digest("signature")


def test_oci_release_manifest_includes_sbom_descriptor(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    release = (
        graphblocks_deployment.GraphRelease("support-agent", "2026.06.23.1")
        .with_bundle(_digest("bundle"), "application/vnd.graphblocks.release.bundle.v1+tar")
        .with_graph("turn", graphblocks_deployment.GraphReleaseGraph("sha256:graph", "sha256:plan"))
    )
    bundle = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.graphblocks.release.bundle.v1+tar",
        digest=_digest("bundle"),
        size=4096,
    )
    sbom = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.cyclonedx+json",
        digest=_digest("sbom"),
        size=1024,
        annotations={"graphblocks.ai/artifact-kind": "sbom"},
    )
    provenance = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.in-toto+json",
        digest=_digest("provenance"),
        size=512,
    )

    manifest = graphblocks_oci.build_release_manifest(
        release,
        bundle_descriptor=bundle,
        sbom_descriptor=sbom,
        provenance_descriptor=provenance,
    )
    contract = manifest.manifest_contract()

    assert contract["layers"] == [
        bundle.descriptor_contract(),
        sbom.descriptor_contract(),
        provenance.descriptor_contract(),
    ]
    assert contract["annotations"]["graphblocks.ai/sbom-digest"] == _digest("sbom")
    assert contract["annotations"]["graphblocks.ai/provenance-digest"] == _digest("provenance")


def test_oci_build_release_image_returns_tag_and_digest_references(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    release = (
        graphblocks_deployment.GraphRelease("support-agent", "2026.06.23.1")
        .with_bundle(_digest("bundle"), "application/vnd.graphblocks.release.bundle.v1+tar")
        .with_graph("turn", graphblocks_deployment.GraphReleaseGraph("sha256:graph", "sha256:plan"))
    )
    bundle = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.graphblocks.release.bundle.v1+tar",
        digest=_digest("bundle"),
        size=4096,
    )
    sbom = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.cyclonedx+json",
        digest=_digest("sbom"),
        size=1024,
    )
    provenance = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.in-toto+json",
        digest=_digest("provenance"),
        size=512,
    )
    signature = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.dev.cosign.simplesigning.v1+json",
        digest=_digest("signature"),
        size=256,
    )

    image = graphblocks_oci.build_release_image(
        release,
        registry="registry.example.com",
        repository="graphblocks/support-agent",
        tag="2026.06.23.1",
        bundle_descriptor=bundle,
        sbom_descriptor=sbom,
        provenance_descriptor=provenance,
        signature_descriptor=signature,
    )

    assert image.tag_ref == "registry.example.com/graphblocks/support-agent:2026.06.23.1"
    assert image.digest_ref == f"registry.example.com/graphblocks/support-agent@{image.manifest_digest}"
    assert image.image_build_contract() == {
        "release_name": "support-agent",
        "release_version": "2026.06.23.1",
        "release_digest": release.content_digest(),
        "tag_ref": "registry.example.com/graphblocks/support-agent:2026.06.23.1",
        "digest_ref": f"registry.example.com/graphblocks/support-agent@{image.manifest_digest}",
        "manifest_digest": image.manifest_digest,
        "manifest_media_type": "application/vnd.oci.image.manifest.v1+json",
        "artifact_type": "application/vnd.graphblocks.release.v1",
        "layers": [
            bundle.descriptor_contract(),
            sbom.descriptor_contract(),
            provenance.descriptor_contract(),
            signature.descriptor_contract(),
        ],
    }
    assert image.content_digest().startswith("sha256:")
    assert "ReleaseImageBuild" in graphblocks_oci.__all__
    assert "build_release_image" in graphblocks_oci.__all__


def test_oci_build_release_sbom_is_canonical_and_descriptor_ready(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    release = (
        graphblocks_deployment.GraphRelease("support-agent", "2026.06.23.1")
        .with_bundle(_digest("bundle"), "application/vnd.graphblocks.release.bundle.v1+tar")
        .with_graph("turn", graphblocks_deployment.GraphReleaseGraph("sha256:graph", "sha256:plan"))
        .with_lock(
            "python",
            graphblocks_deployment.ReleaseLockRef(
                "oci://registry.example.com/locks/python@sha256:pylock",
                digest="sha256:pylock",
                lock_type="package-lock",
            ),
        )
    )
    left = graphblocks_oci.build_release_sbom(
        release,
        package_lock={
            "packages": [
                {
                    "distribution": "graphblocks-runtime",
                    "versionConstraint": "~=1.0",
                    "kind": "native_wheel",
                    "layer": "native_runtime",
                    "stability": "foundation",
                },
                {
                    "distribution": "graphblocks-core",
                    "versionConstraint": "~=1.0",
                    "kind": "pure_python",
                    "layer": "schema_authoring",
                    "stability": "foundation",
                },
            ]
        },
        external_components=[
            {
                "type": "container",
                "name": "control-plane",
                "version": "sha256:image",
            }
        ],
    )
    right = graphblocks_oci.build_release_sbom(
        release,
        package_lock={
            "packages": [
                {
                    "distribution": "graphblocks-core",
                    "versionConstraint": "~=1.0",
                    "kind": "pure_python",
                    "layer": "schema_authoring",
                    "stability": "foundation",
                },
                {
                    "distribution": "graphblocks-runtime",
                    "versionConstraint": "~=1.0",
                    "kind": "native_wheel",
                    "layer": "native_runtime",
                    "stability": "foundation",
                },
            ]
        },
        external_components=[
            {
                "version": "sha256:image",
                "name": "control-plane",
                "type": "container",
            }
        ],
    )
    descriptor = left.to_descriptor()

    assert left.sbom_json() == right.sbom_json()
    assert left.sbom_digest() == right.sbom_digest()
    assert left.sbom_contract()["metadata"]["component"] == {
        "type": "application",
        "name": "support-agent",
        "version": "2026.06.23.1",
        "bom-ref": release.content_digest(),
    }
    assert left.sbom_contract()["components"] == [
        {
            "type": "container",
            "name": "control-plane",
            "version": "sha256:image",
        },
        {
            "type": "library",
            "name": "graphblocks-core",
            "version": "~=1.0",
            "properties": [
                {"name": "graphblocks:kind", "value": "pure_python"},
                {"name": "graphblocks:layer", "value": "schema_authoring"},
                {"name": "graphblocks:stability", "value": "foundation"},
            ],
        },
        {
            "type": "library",
            "name": "graphblocks-runtime",
            "version": "~=1.0",
            "properties": [
                {"name": "graphblocks:kind", "value": "native_wheel"},
                {"name": "graphblocks:layer", "value": "native_runtime"},
                {"name": "graphblocks:stability", "value": "foundation"},
            ],
        },
    ]
    assert descriptor.media_type == "application/vnd.cyclonedx+json"
    assert descriptor.digest == left.sbom_digest()
    assert descriptor.descriptor_contract()["annotations"] == {
        "graphblocks.ai/artifact-kind": "sbom",
        "graphblocks.ai/release-digest": release.content_digest(),
    }
    assert "ReleaseSbom" in graphblocks_oci.__all__
    assert "build_release_sbom" in graphblocks_oci.__all__


def test_oci_build_provenance_attestation_is_canonical_and_descriptor_ready(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    release = (
        graphblocks_deployment.GraphRelease("support-agent", "2026.06.23.1")
        .with_bundle(_digest("bundle"), "application/vnd.graphblocks.release.bundle.v1+tar")
        .with_graph("turn", graphblocks_deployment.GraphReleaseGraph("sha256:graph", "sha256:plan"))
    )

    left = graphblocks_oci.build_release_provenance_attestation(
        release,
        builder_id="https://ci.example.com/builders/release",
        build_type="https://graphblocks.ai/build/release-bundle/v1",
        invocation_id="build-123",
        materials={
            "Cargo.lock": _digest("cargo-lock"),
            "pylock.toml": _digest("pylock"),
        },
        metadata={"source": "git+https://example.com/support-agent@abc123"},
    )
    right = graphblocks_oci.build_release_provenance_attestation(
        release,
        builder_id="https://ci.example.com/builders/release",
        build_type="https://graphblocks.ai/build/release-bundle/v1",
        invocation_id="build-123",
        materials={
            "pylock.toml": _digest("pylock"),
            "Cargo.lock": _digest("cargo-lock"),
        },
        metadata={"source": "git+https://example.com/support-agent@abc123"},
    )
    descriptor = graphblocks_oci.OciDescriptor.from_payload(
        "application/vnd.in-toto+json",
        left.attestation_json(),
        annotations={"graphblocks.ai/attestation-kind": "build-provenance"},
    )

    assert left.attestation_json() == right.attestation_json()
    assert left.attestation_digest() == right.attestation_digest()
    assert left.attestation_contract()["subject"] == [
        {
            "name": "support-agent:2026.06.23.1",
            "digest": {"sha256": release.content_digest().removeprefix("sha256:")},
        }
    ]
    assert descriptor.digest == left.attestation_digest()
    assert descriptor.size == len(left.attestation_json().encode("utf-8"))
    assert descriptor.descriptor_contract()["annotations"] == {
        "graphblocks.ai/attestation-kind": "build-provenance"
    }


def test_oci_signature_policy_evaluates_descriptor_metadata(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    signature = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.dev.cosign.simplesigning.v1+json",
        digest=_digest("signature"),
        size=256,
        annotations={
            "graphblocks.ai/release-digest": _digest("release"),
            "graphblocks.ai/signature-kind": "cosign",
        },
    )
    policy = graphblocks_oci.SignatureVerificationPolicy(
        policy_id="production-publishers",
        trusted_signers=("cosign://ci.example.com/release",),
        required_annotations={"graphblocks.ai/signature-kind": "cosign"},
    )

    accepted = policy.evaluate(
        signature,
        signer="cosign://ci.example.com/release",
        subject_digest=_digest("release"),
        cryptographic_verifier=lambda descriptor, signer, subject: True,
    )
    denied = policy.evaluate(
        signature,
        signer="cosign://unknown",
        subject_digest=_digest("other-release"),
    )

    assert accepted.verified is True
    assert accepted.reason_codes == ()
    assert accepted.verification_contract() == {
        "policy_id": "production-publishers",
        "signature_digest": _digest("signature"),
        "signer": "cosign://ci.example.com/release",
        "subject_digest": _digest("release"),
        "verified": True,
        "reason_codes": [],
    }
    assert denied.verified is False
    assert denied.reason_codes == (
        "signature.subject_digest_mismatch",
        "signature.untrusted_signer",
        "signature.cryptographic_verification_required",
    )


def test_oci_signature_policy_fails_closed_without_crypto_or_trust_roots(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    subject_digest = _digest("release")
    signature = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.dev.cosign.simplesigning.v1+json",
        digest=_digest("signature"),
        size=256,
        annotations={"graphblocks.ai/release-digest": subject_digest},
    )

    missing_verifier = graphblocks_oci.SignatureVerificationPolicy(
        policy_id="production-publishers",
        trusted_signers=("cosign://ci.example.com/release",),
    ).evaluate(
        signature,
        signer="cosign://ci.example.com/release",
        subject_digest=subject_digest,
    )
    missing_trust_roots = graphblocks_oci.SignatureVerificationPolicy(
        policy_id="no-publishers",
    ).evaluate(
        signature,
        signer="cosign://ci.example.com/release",
        subject_digest=subject_digest,
        cryptographic_verifier=lambda descriptor, signer, subject: True,
    )

    assert missing_verifier.verified is False
    assert missing_verifier.reason_codes == (
        "signature.cryptographic_verification_required",
    )
    assert missing_trust_roots.verified is False
    assert missing_trust_roots.reason_codes == ("signature.no_trusted_signers",)


@pytest.mark.parametrize(
    "digest",
    (
        "sha256:",
        "sha256:abc",
        "sha256:" + ("A" * 64),
        "sha256:" + ("g" * 64),
        "sha512:" + ("a" * 128),
    ),
)
def test_oci_contract_rejects_noncanonical_sha256_digests(monkeypatch, digest) -> None:
    graphblocks_oci = _import_oci(monkeypatch)

    with pytest.raises(graphblocks_oci.OciContractError, match="canonical sha256"):
        graphblocks_oci.OciDescriptor("application/octet-stream", digest, 1)


def test_oci_manifest_digest_uses_canonical_serialization(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    config = graphblocks_oci.OciDescriptor(
        "application/vnd.graphblocks.config.v1+json", _digest("config"), 64
    )
    layer = graphblocks_oci.OciDescriptor(
        "application/vnd.graphblocks.layer.v1+tar", _digest("layer"), 512
    )
    left = graphblocks_oci.OciManifest(
        config=config,
        layers=(layer,),
        annotations={"b": "2", "a": "1"},
    )
    right = graphblocks_oci.OciManifest(
        config=config,
        layers=(layer,),
        annotations={"a": "1", "b": "2"},
    )

    assert left.manifest_json() == right.manifest_json()
    assert left.manifest_digest() == right.manifest_digest()
    assert json.loads(left.manifest_json())["annotations"] == {"a": "1", "b": "2"}


def test_oci_reference_renders_tag_and_digest_forms(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)

    tagged = graphblocks_oci.OciArtifactReference(
        registry="registry.example.com",
        repository="graphblocks/support-agent",
        tag="2026.06.23.1",
    )
    digested = graphblocks_oci.OciArtifactReference(
        registry="registry.example.com",
        repository="graphblocks/support-agent",
        digest=_digest("manifest"),
    )

    assert tagged.ref() == "registry.example.com/graphblocks/support-agent:2026.06.23.1"
    assert digested.ref() == f"registry.example.com/graphblocks/support-agent@{_digest('manifest')}"

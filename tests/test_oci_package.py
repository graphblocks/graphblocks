from __future__ import annotations

import importlib
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _import_oci(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-deployment" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-oci" / "src"))
    return importlib.import_module("graphblocks_oci")


def test_oci_package_builds_release_manifest_with_graphblocks_annotations(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks_deployment")
    release = (
        graphblocks_deployment.GraphRelease("support-agent", "2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.bundle.v1+tar")
        .with_graph("turn", graphblocks_deployment.GraphReleaseGraph("sha256:graph", "sha256:plan"))
    )
    bundle = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.graphblocks.release.bundle.v1+tar",
        digest="sha256:bundle",
        size=4096,
    )

    manifest = graphblocks_oci.build_release_manifest(
        release,
        bundle_descriptor=bundle,
        config_descriptor=graphblocks_oci.OciDescriptor(
            media_type="application/vnd.graphblocks.release.config.v1+json",
            digest="sha256:config",
            size=256,
        ),
    )
    contract = manifest.manifest_contract()

    assert contract["schemaVersion"] == 2
    assert contract["artifactType"] == "application/vnd.graphblocks.release.v1"
    assert contract["config"]["digest"] == "sha256:config"
    assert contract["layers"] == [bundle.descriptor_contract()]
    assert contract["annotations"]["graphblocks.ai/release-name"] == "support-agent"
    assert contract["annotations"]["graphblocks.ai/release-version"] == "2026.06.23.1"
    assert contract["annotations"]["graphblocks.ai/release-digest"] == release.content_digest()
    assert manifest.manifest_digest().startswith("sha256:")


def test_oci_release_manifest_includes_provenance_and_signature_descriptors(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks_deployment")
    release = (
        graphblocks_deployment.GraphRelease("support-agent", "2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.bundle.v1+tar")
        .with_graph("turn", graphblocks_deployment.GraphReleaseGraph("sha256:graph", "sha256:plan"))
    )
    bundle = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.graphblocks.release.bundle.v1+tar",
        digest="sha256:bundle",
        size=4096,
    )
    provenance = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.in-toto+json",
        digest="sha256:provenance",
        size=512,
    )
    signature = graphblocks_oci.OciDescriptor(
        media_type="application/vnd.dev.cosign.simplesigning.v1+json",
        digest="sha256:signature",
        size=256,
    )

    manifest = graphblocks_oci.build_release_manifest(
        release,
        bundle_descriptor=bundle,
        provenance_descriptor=provenance,
        signature_descriptor=signature,
    )
    contract = manifest.manifest_contract()

    assert contract["layers"] == [
        bundle.descriptor_contract(),
        provenance.descriptor_contract(),
        signature.descriptor_contract(),
    ]
    assert contract["annotations"]["graphblocks.ai/provenance-digest"] == "sha256:provenance"
    assert contract["annotations"]["graphblocks.ai/signature-digest"] == "sha256:signature"


def test_oci_build_provenance_attestation_is_canonical_and_descriptor_ready(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks_deployment")
    release = (
        graphblocks_deployment.GraphRelease("support-agent", "2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.bundle.v1+tar")
        .with_graph("turn", graphblocks_deployment.GraphReleaseGraph("sha256:graph", "sha256:plan"))
    )

    left = graphblocks_oci.build_release_provenance_attestation(
        release,
        builder_id="https://ci.example.com/builders/release",
        build_type="https://graphblocks.ai/build/release-bundle/v1",
        invocation_id="build-123",
        materials={
            "Cargo.lock": "sha256:cargo-lock",
            "pylock.toml": "sha256:pylock",
        },
        metadata={"source": "git+https://example.com/support-agent@abc123"},
    )
    right = graphblocks_oci.build_release_provenance_attestation(
        release,
        builder_id="https://ci.example.com/builders/release",
        build_type="https://graphblocks.ai/build/release-bundle/v1",
        invocation_id="build-123",
        materials={
            "pylock.toml": "sha256:pylock",
            "Cargo.lock": "sha256:cargo-lock",
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
        digest="sha256:signature",
        size=256,
        annotations={
            "graphblocks.ai/release-digest": "sha256:release",
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
        subject_digest="sha256:release",
    )
    denied = policy.evaluate(
        signature,
        signer="cosign://unknown",
        subject_digest="sha256:other-release",
    )

    assert accepted.verified is True
    assert accepted.reason_codes == ()
    assert accepted.verification_contract() == {
        "policy_id": "production-publishers",
        "signature_digest": "sha256:signature",
        "signer": "cosign://ci.example.com/release",
        "subject_digest": "sha256:release",
        "verified": True,
        "reason_codes": [],
    }
    assert denied.verified is False
    assert denied.reason_codes == (
        "signature.subject_digest_mismatch",
        "signature.untrusted_signer",
    )


def test_oci_manifest_digest_uses_canonical_serialization(monkeypatch) -> None:
    graphblocks_oci = _import_oci(monkeypatch)
    config = graphblocks_oci.OciDescriptor("application/vnd.graphblocks.config.v1+json", "sha256:config", 64)
    layer = graphblocks_oci.OciDescriptor("application/vnd.graphblocks.layer.v1+tar", "sha256:layer", 512)
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
        digest="sha256:manifest",
    )

    assert tagged.ref() == "registry.example.com/graphblocks/support-agent:2026.06.23.1"
    assert digested.ref() == "registry.example.com/graphblocks/support-agent@sha256:manifest"

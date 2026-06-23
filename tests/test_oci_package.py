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

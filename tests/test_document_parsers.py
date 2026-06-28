from __future__ import annotations

import pytest

from graphblocks.document_parsers import (
    DocumentParserError,
    DocumentParserNotFoundError,
    DocumentParserRegistry,
    ParserDescriptor,
    ParserSelectionLock,
    plain_text_parser_descriptor,
)
from graphblocks.documents import ArtifactRef, AssetRevision, SourceAsset


def test_parser_registry_selects_by_media_type_and_records_lock_inputs() -> None:
    registry = DocumentParserRegistry()
    descriptor = plain_text_parser_descriptor()
    descriptor.metadata["config_digest"] = "sha256:parser-config"
    descriptor.metadata["profile"] = "plain-text-default"
    registry.register(descriptor)
    artifact = ArtifactRef(
        artifact_id="artifact-1",
        uri="file:///tmp/policy.txt",
        media_type="text/plain",
        checksum="sha256:content",
        filename="policy.txt",
    )

    lock = registry.select(artifact)
    descriptor.metadata["profile"] = "mutated"

    assert lock.processor_id == "plain-text"
    assert lock.processor_version == "1"
    assert lock.reason == "media_type"
    assert lock.media_type == "text/plain"
    assert lock.filename == "policy.txt"
    assert lock.artifact_checksum == "sha256:content"
    assert lock.metadata == {
        "config_digest": "sha256:parser-config",
        "profile": "plain-text-default",
    }
    with pytest.raises(TypeError):
        lock.metadata["profile"] = "changed"

    resolved = registry.resolve_locked(lock)
    assert resolved.metadata == {
        "config_digest": "sha256:parser-config",
        "profile": "plain-text-default",
    }
    with pytest.raises(TypeError):
        resolved.metadata["profile"] = "changed"


def test_parser_registry_uses_extension_when_media_type_is_missing() -> None:
    registry = DocumentParserRegistry()
    registry.register(plain_text_parser_descriptor())

    lock = registry.select(ArtifactRef("artifact-1", "file:///tmp/policy.txt", filename="policy.txt"))

    assert lock.processor_id == "plain-text"
    assert lock.reason == "extension"


def test_parser_registry_selection_is_deterministic_for_equal_priority() -> None:
    registry = DocumentParserRegistry()
    registry.register(ParserDescriptor("z-parser", "1", media_types=("text/plain",), priority=10))
    registry.register(ParserDescriptor("a-parser", "2", media_types=("text/plain",), priority=10))

    lock = registry.select(ArtifactRef("artifact-1", "file:///tmp/policy.txt", media_type="text/plain"))

    assert lock.processor_id == "a-parser"
    assert lock.processor_version == "2"


def test_parser_registry_ocr_fallback_is_explicit_and_deterministic() -> None:
    registry = DocumentParserRegistry()
    registry.register(ParserDescriptor("ocr-z", "1", supports_ocr=True, priority=10))
    registry.register(ParserDescriptor("ocr-a", "2", supports_ocr=True, priority=10))
    artifact = ArtifactRef(
        "artifact-scan",
        "file:///tmp/scan.bin",
        media_type="application/octet-stream",
        filename="scan.bin",
        checksum="sha256:scan",
    )

    with pytest.raises(DocumentParserNotFoundError):
        registry.select(artifact)

    lock = registry.select(artifact, allow_ocr_fallback=True)

    assert lock.processor_id == "ocr-a"
    assert lock.processor_version == "2"
    assert lock.reason == "ocr_fallback"
    assert lock.media_type == "application/octet-stream"
    assert lock.filename == "scan.bin"
    assert lock.artifact_checksum == "sha256:scan"


def test_parser_registry_parse_locked_uses_locked_parser_version() -> None:
    registry = DocumentParserRegistry()
    registry.register(plain_text_parser_descriptor())
    asset = SourceAsset("asset-1", "file:///tmp/policy.txt", "local", current_revision_id="rev-1")
    revision = AssetRevision(
        revision_id="rev-1",
        asset_id="asset-1",
        content_hash="sha256:content",
        observed_at="2026-06-22T00:00:00Z",
        artifact=ArtifactRef("artifact-1", "file:///tmp/policy.txt", media_type="text/plain", filename="policy.txt"),
    )

    lock = registry.select(revision.artifact)
    document = registry.parse_locked(asset, revision, b"Alpha\n\nBeta\n", lock)

    assert document.parser == {"processor_id": "plain-text", "version": "1"}
    assert [element.content for element in document.elements] == ["Alpha", "Beta"]


def test_parser_registry_rejects_lock_for_different_artifact_checksum() -> None:
    registry = DocumentParserRegistry()
    registry.register(plain_text_parser_descriptor())
    asset = SourceAsset("asset-1", "file:///tmp/policy.txt", "local", current_revision_id="rev-1")
    selected_artifact = ArtifactRef(
        "artifact-1",
        "file:///tmp/policy.txt",
        media_type="text/plain",
        filename="policy.txt",
        checksum="sha256:old",
    )
    lock = registry.select(selected_artifact)
    revision = AssetRevision(
        revision_id="rev-1",
        asset_id="asset-1",
        content_hash="sha256:new",
        observed_at="2026-06-22T00:00:00Z",
        artifact=ArtifactRef(
            "artifact-1",
            "file:///tmp/policy.txt",
            media_type="text/plain",
            filename="policy.txt",
            checksum="sha256:new",
        ),
    )

    with pytest.raises(DocumentParserError, match="artifact checksum"):
        registry.parse_locked(asset, revision, b"Alpha\n", lock)


def test_parser_registry_rejects_unknown_locked_parser() -> None:
    registry = DocumentParserRegistry()
    lock = ParserSelectionLock(
        processor_id="missing",
        processor_version="1",
        reason="media_type",
        media_type="text/plain",
    )
    asset = SourceAsset("asset-1", "file:///tmp/policy.txt", "local")
    revision = AssetRevision(
        revision_id="rev-1",
        asset_id="asset-1",
        content_hash="sha256:content",
        observed_at="2026-06-22T00:00:00Z",
        artifact=ArtifactRef("artifact-1", "file:///tmp/policy.txt"),
    )

    with pytest.raises(DocumentParserNotFoundError):
        registry.parse_locked(asset, revision, b"Alpha\n", lock)

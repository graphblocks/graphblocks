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
    metadata = {
        "config_digest": "sha256:parser-config",
        "profile": "plain-text-default",
    }
    plain_descriptor = plain_text_parser_descriptor()
    descriptor = ParserDescriptor(
        processor_id=plain_descriptor.processor_id,
        version=plain_descriptor.version,
        media_types=plain_descriptor.media_types,
        extensions=plain_descriptor.extensions,
        priority=plain_descriptor.priority,
        supports_ocr=plain_descriptor.supports_ocr,
        parse=plain_descriptor.parse,
        metadata=metadata,
    )
    registry.register(descriptor)
    metadata["profile"] = "mutated"
    artifact = ArtifactRef(
        artifact_id="artifact-1",
        uri="file:///tmp/policy.txt",
        media_type="text/plain",
        checksum="sha256:content",
        filename="policy.txt",
    )

    lock = registry.select(artifact)

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
    with pytest.raises(ValueError, match="parser selection lock metadata must be a mapping"):
        ParserSelectionLock("plain-text", "1", "media_type", metadata=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="parser selection lock metadata key must not be empty"):
        ParserSelectionLock("plain-text", "1", "media_type", metadata={" ": "value"})


def test_parser_registry_normalizes_registered_fields_and_selection_case() -> None:
    registry = DocumentParserRegistry()
    registry.register(
        ParserDescriptor(
            "plain-text",
            "1",
            media_types=(" Text/Plain ",),
            extensions=(" TXT ",),
        )
    )

    media_lock = registry.select(
        ArtifactRef(
            "artifact-1",
            "file:///tmp/POLICY.TXT",
            media_type="TEXT/PLAIN",
            filename="POLICY.TXT",
        )
    )
    extension_lock = registry.select(
        ArtifactRef("artifact-2", "file:///tmp/POLICY.TXT", filename="POLICY.TXT")
    )

    assert media_lock.processor_id == "plain-text"
    assert media_lock.processor_version == "1"
    assert media_lock.reason == "media_type"
    assert media_lock.media_type == "text/plain"
    assert media_lock.filename == "POLICY.TXT"
    assert extension_lock.processor_id == "plain-text"
    assert extension_lock.reason == "extension"
    assert extension_lock.filename == "POLICY.TXT"
    assert registry.resolve_locked(media_lock).processor_id == "plain-text"


@pytest.mark.parametrize(
    ("constructor", "expected_error"),
    (
        (
            lambda: ParserDescriptor(" plain-text", "1"),
            "parser descriptor processor_id must not contain surrounding whitespace",
        ),
        (
            lambda: ParserDescriptor("plain-text", "1 "),
            "parser descriptor version must not contain surrounding whitespace",
        ),
        (
            lambda: ParserDescriptor("plain-text", "1", metadata={" profile": "plain-text"}),
            "parser descriptor metadata key must not contain surrounding whitespace",
        ),
        (
            lambda: ParserSelectionLock(" plain-text", "1", "media_type"),
            "parser selection lock processor_id must not contain surrounding whitespace",
        ),
        (
            lambda: ParserSelectionLock("plain-text", "1 ", "media_type"),
            "parser selection lock processor_version must not contain surrounding whitespace",
        ),
        (
            lambda: ParserSelectionLock("plain-text", "1", " media_type"),
            "parser selection lock reason must not contain surrounding whitespace",
        ),
        (
            lambda: ParserSelectionLock("plain-text", "1", "media_type", media_type=" text/plain"),
            "parser selection lock media_type must not contain surrounding whitespace",
        ),
        (
            lambda: ParserSelectionLock("plain-text", "1", "media_type", filename="policy.txt "),
            "parser selection lock filename must not contain surrounding whitespace",
        ),
        (
            lambda: ParserSelectionLock("plain-text", "1", "media_type", artifact_checksum=" sha256:content"),
            "parser selection lock artifact_checksum must not contain surrounding whitespace",
        ),
        (
            lambda: ParserSelectionLock("plain-text", "1", "media_type", metadata={" profile": "plain-text"}),
            "parser selection lock metadata key must not contain surrounding whitespace",
        ),
    ),
)
def test_parser_descriptor_and_lock_reject_whitespace_wrapped_identities(
    constructor: object,
    expected_error: str,
) -> None:
    with pytest.raises(ValueError, match=expected_error):
        constructor()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"processor_id": "", "version": "1"}, "processor_id"),
        ({"processor_id": "plain-text", "version": ""}, "version"),
        ({"processor_id": "plain-text", "version": "1", "media_types": (" ",)}, "media_types"),
        ({"processor_id": "plain-text", "version": "1", "extensions": (" ",)}, "extensions"),
        ({"processor_id": "plain-text", "version": "1", "extensions": (".",)}, "extensions"),
        ({"processor_id": "plain-text", "version": "1", "priority": True}, "priority"),
        ({"processor_id": "plain-text", "version": "1", "supports_ocr": "yes"}, "supports_ocr"),
        ({"processor_id": "plain-text", "version": "1", "parse": object()}, "parse"),
    ],
)
def test_parser_descriptor_rejects_invalid_contract_fields(
    kwargs: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        ParserDescriptor(**kwargs)  # type: ignore[arg-type]


def test_parser_registry_rejects_non_descriptor_registration() -> None:
    registry = DocumentParserRegistry()

    with pytest.raises(DocumentParserError, match="ParserDescriptor"):
        registry.register(object())  # type: ignore[arg-type]


def test_parser_descriptor_freezes_metadata_snapshot() -> None:
    metadata = {"nested": {"formats": ["txt", "text"]}}
    descriptor = ParserDescriptor("plain-text", "1", metadata=metadata)
    metadata["nested"]["formats"].append("md")  # type: ignore[index, union-attr]

    assert descriptor.metadata["nested"]["formats"] == ("txt", "text")  # type: ignore[index]
    with pytest.raises(TypeError):
        descriptor.metadata["nested"]["formats"] += ("md",)  # type: ignore[index, operator]


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

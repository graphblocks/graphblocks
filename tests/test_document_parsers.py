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
from graphblocks.documents import ArtifactRef, AssetRevision, ParsedDocument, SourceAsset


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


def test_parser_records_reject_recursive_and_noncanonical_metadata() -> None:
    recursive: dict[str, object] = {}
    recursive["self"] = recursive

    with pytest.raises(ValueError, match="strict canonical JSON"):
        ParserDescriptor("plain-text", "1", metadata=recursive)
    with pytest.raises(ValueError, match="strict canonical JSON"):
        ParserSelectionLock(
            "plain-text",
            "1",
            "media_type",
            metadata={"invalid_unicode": "\ud800"},
        )


def test_parser_registry_validates_and_snapshots_restored_descriptors() -> None:
    descriptor = plain_text_parser_descriptor()
    restored = {("plain-text", "1"): descriptor}
    registry = DocumentParserRegistry(restored)
    restored.clear()

    assert registry.resolve_locked(
        ParserSelectionLock("plain-text", "1", "restored")
    ).processor_id == "plain-text"
    with pytest.raises(ValueError, match="key must match descriptor identity"):
        DocumentParserRegistry({("wrong", "1"): descriptor})
    with pytest.raises(ValueError, match="descriptors must be a mapping"):
        DocumentParserRegistry(object())  # type: ignore[arg-type]


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


def test_parser_registry_parse_locked_rejects_mismatched_asset_lineage() -> None:
    registry = DocumentParserRegistry()
    registry.register(plain_text_parser_descriptor())
    asset = SourceAsset("asset-1", "file:///tmp/policy.txt", "local")
    revision = AssetRevision(
        "rev-1",
        "asset-2",
        "sha256:content",
        "2026-06-22T00:00:00Z",
        ArtifactRef(
            "artifact-1",
            "file:///tmp/policy.txt",
            media_type="text/plain",
        ),
    )
    lock = registry.select(revision.artifact)

    with pytest.raises(DocumentParserError, match="asset_id must match"):
        registry.parse_locked(asset, revision, b"Alpha\n", lock)


def test_parser_registry_falls_back_through_ordered_candidate_chain() -> None:
    attempts: list[str] = []

    def primary(asset: SourceAsset, revision: AssetRevision, body: bytes) -> ParsedDocument:
        attempts.append("parser.pdf.primary")
        raise DocumentParserError("primary quality gate failed")

    def fallback(asset: SourceAsset, revision: AssetRevision, body: bytes) -> ParsedDocument:
        attempts.append("parser.pdf.fallback")
        return ParsedDocument(
            document_id="doc-1",
            asset_id=asset.asset_id,
            revision_id=revision.revision_id,
            parser={"processor_id": "parser.pdf.fallback", "version": "1"},
        )

    registry = DocumentParserRegistry()
    registry.register(ParserDescriptor("parser.pdf.primary", "1", parse=primary))
    registry.register(ParserDescriptor("parser.pdf.fallback", "1", parse=fallback))
    asset = SourceAsset("asset-1", "file:///tmp/source.pdf", "local", current_revision_id="rev-1")
    revision = AssetRevision(
        "rev-1",
        "asset-1",
        "sha256:content",
        "2026-07-10T00:00:00Z",
        ArtifactRef(
            "artifact-1",
            "file:///tmp/source.pdf",
            media_type="application/pdf",
            checksum="sha256:content",
            filename="source.pdf",
        ),
    )

    result = registry.parse_with_candidates(
        asset,
        revision,
        b"%PDF fixture",
        (("parser.pdf.primary", "1"), ("parser.pdf.fallback", "1")),
    )

    assert attempts == ["parser.pdf.primary", "parser.pdf.fallback"]
    assert result.document.parser == {"processor_id": "parser.pdf.fallback", "version": "1"}
    assert result.selected_lock.processor_id == "parser.pdf.fallback"
    assert result.selected_lock.reason == "candidate_fallback"
    assert [lock.processor_id for lock in result.failed_locks] == ["parser.pdf.primary"]
    assert result.failed_locks[0].reason == "candidate_primary"


def test_parser_registry_falls_back_after_untyped_parser_exception() -> None:
    registry = DocumentParserRegistry()
    registry.register(plain_text_parser_descriptor())
    registry.register(
        ParserDescriptor(
            "binary-fallback",
            "1",
            parse=lambda asset, revision, body: ParsedDocument(
                document_id="doc-fallback",
                asset_id=asset.asset_id,
                revision_id=revision.revision_id,
                parser={"processor_id": "binary-fallback", "version": "1"},
            ),
        )
    )
    asset = SourceAsset(
        "asset-1",
        "file:///tmp/source.txt",
        "local",
        current_revision_id="rev-1",
    )
    revision = AssetRevision(
        "rev-1",
        "asset-1",
        "sha256:content",
        "2026-07-10T00:00:00Z",
        ArtifactRef(
            "artifact-1",
            "file:///tmp/source.txt",
            checksum="sha256:content",
        ),
    )

    result = registry.parse_with_candidates(
        asset,
        revision,
        b"\xff",
        (("plain-text", "1"), ("binary-fallback", "1")),
    )

    assert result.selected_lock.processor_id == "binary-fallback"
    assert [lock.processor_id for lock in result.failed_locks] == ["plain-text"]


def test_parser_registry_candidate_fallback_fails_when_every_candidate_fails() -> None:
    def fail(asset: SourceAsset, revision: AssetRevision, body: bytes) -> ParsedDocument:
        raise DocumentParserError("parser failed")

    registry = DocumentParserRegistry()
    registry.register(ParserDescriptor("parser.pdf.primary", "1", parse=fail))
    registry.register(ParserDescriptor("parser.pdf.fallback", "1", parse=fail))
    asset = SourceAsset("asset-1", "file:///tmp/source.pdf", "local", current_revision_id="rev-1")
    revision = AssetRevision(
        "rev-1",
        "asset-1",
        "sha256:content",
        "2026-07-10T00:00:00Z",
        ArtifactRef("artifact-1", "file:///tmp/source.pdf", checksum="sha256:content"),
    )

    with pytest.raises(
        DocumentParserError,
        match=(
            "document parser candidates exhausted after "
            "parser.pdf.primary@1, parser.pdf.fallback@1"
        ),
    ):
        registry.parse_with_candidates(
            asset,
            revision,
            b"%PDF fixture",
            (("parser.pdf.primary", "1"), ("parser.pdf.fallback", "1")),
        )


def test_parser_registry_candidate_fallback_rejects_malformed_or_duplicate_candidates() -> None:
    registry = DocumentParserRegistry()
    asset = SourceAsset("asset-1", "file:///tmp/source.pdf", "local", current_revision_id="rev-1")
    revision = AssetRevision(
        "rev-1",
        "asset-1",
        "sha256:content",
        "2026-07-10T00:00:00Z",
        ArtifactRef("artifact-1", "file:///tmp/source.pdf"),
    )

    with pytest.raises(ValueError, match="parser candidate chain must not be empty"):
        registry.parse_with_candidates(asset, revision, b"", ())
    with pytest.raises(ValueError, match="parser candidate must contain processor_id and version"):
        registry.parse_with_candidates(asset, revision, b"", (("parser.pdf.primary",),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="parser candidate chain must not contain duplicates"):
        registry.parse_with_candidates(
            asset,
            revision,
            b"",
            (("parser.pdf.primary", "1"), ("parser.pdf.primary", "1")),
        )


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

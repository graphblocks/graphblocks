from __future__ import annotations

import pytest

from graphblocks.documents import (
    chunk_document_by_lines,
    create_local_text_revision,
    parse_plain_text_document,
)


def test_parse_plain_text_document_creates_paragraph_elements_with_char_spans() -> None:
    asset, revision = create_local_text_revision(
        "file:///tmp/notes.txt",
        "Title\n\nFirst paragraph.\nSecond paragraph.\n",
        observed_at="2026-06-22T00:00:00Z",
        filename="notes.txt",
    )

    document = parse_plain_text_document(asset, revision, "Title\n\nFirst paragraph.\nSecond paragraph.\n")

    assert document.document_id == "doc:" + revision.revision_id
    assert document.asset_id == asset.asset_id
    assert document.revision_id == revision.revision_id
    assert document.plain_text == "Title\n\nFirst paragraph.\nSecond paragraph.\n"
    assert [element.content for element in document.elements] == ["Title", "First paragraph.", "Second paragraph."]
    assert [(element.location.char_start, element.location.char_end) for element in document.elements] == [
        (0, 5),
        (7, 23),
        (24, 41),
    ]


def test_parse_plain_text_document_counts_unicode_and_all_splitline_boundaries() -> None:
    text = "a\rb\u2028c\r\nd"
    asset, revision = create_local_text_revision(
        "file:///tmp/unicode-lines.txt",
        text,
        observed_at="2026-06-22T00:00:00Z",
    )

    document = parse_plain_text_document(asset, revision, text)

    assert [
        (
            element.content,
            element.location.char_start,
            element.location.char_end,
        )
        for element in document.elements
    ] == [
        ("a", 0, 1),
        ("b", 2, 3),
        ("c", 4, 5),
        ("d", 7, 8),
    ]


def test_chunk_document_by_lines_preserves_lineage_and_source_spans() -> None:
    asset, revision = create_local_text_revision(
        "file:///tmp/notes.txt",
        "Title\n\nFirst paragraph.\nSecond paragraph.\n",
        observed_at="2026-06-22T00:00:00Z",
        filename="notes.txt",
    )
    document = parse_plain_text_document(asset, revision, "Title\n\nFirst paragraph.\nSecond paragraph.\n")

    chunks = chunk_document_by_lines(document, revision, max_elements=2)

    assert [chunk.text for chunk in chunks] == ["Title\nFirst paragraph.", "Second paragraph."]
    assert chunks[0].asset_id == asset.asset_id
    assert chunks[0].revision_id == revision.revision_id
    assert chunks[0].document_id == document.document_id
    assert chunks[0].element_ids == (document.elements[0].element_id, document.elements[1].element_id)
    assert chunks[0].source_refs[0].digest == revision.content_hash
    assert chunks[0].source_refs[0].locator.asset_id == asset.asset_id
    assert chunks[0].source_refs[0].locator.revision_id == revision.revision_id
    assert chunks[0].source_refs[0].locator.document_id == document.document_id
    assert chunks[0].source_refs[0].locator.chunk_id == chunks[0].chunk_id
    assert chunks[0].source_refs[0].locator.char_start == 0
    assert chunks[0].source_refs[0].locator.char_end == 23


def test_document_processing_rejects_mismatched_lineage_and_boolean_chunk_size() -> None:
    first_asset, first_revision = create_local_text_revision(
        "file:///tmp/first.txt",
        "first\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    _, second_revision = create_local_text_revision(
        "file:///tmp/second.txt",
        "second\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(first_asset, first_revision, "first\n")

    with pytest.raises(ValueError, match="revision asset_id must match"):
        parse_plain_text_document(first_asset, second_revision, "second\n")
    with pytest.raises(ValueError, match="document asset_id must match"):
        chunk_document_by_lines(document, second_revision)
    with pytest.raises(ValueError, match="positive integer"):
        chunk_document_by_lines(document, first_revision, max_elements=True)  # type: ignore[arg-type]


def test_local_text_processing_rejects_non_scalar_unicode_and_chunk_size_overflow() -> None:
    with pytest.raises(ValueError, match="Unicode scalar"):
        create_local_text_revision(
            "file:///tmp/surrogate.txt",
            "\ud800",
            observed_at="2026-06-22T00:00:00Z",
        )

    asset, revision = create_local_text_revision(
        "file:///tmp/notes.txt",
        "line\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "line\n")
    with pytest.raises(ValueError, match="18446744073709551615"):
        chunk_document_by_lines(document, revision, max_elements=1 << 64)

from __future__ import annotations

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

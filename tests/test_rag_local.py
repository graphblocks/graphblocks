from __future__ import annotations

from graphblocks.documents import create_local_text_revision, parse_plain_text_document, chunk_document_by_lines
from graphblocks.rag import InMemoryChunkRetriever, knowledge_item_from_chunk


def test_knowledge_item_from_chunk_preserves_chunk_source_ref() -> None:
    asset, revision = create_local_text_revision(
        "file:///tmp/notes.txt",
        "alpha beta\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "alpha beta\n")
    chunk = chunk_document_by_lines(document, revision, max_elements=1)[0]

    item = knowledge_item_from_chunk(chunk)

    assert item.item_id == chunk.chunk_id
    assert item.item_kind == "document_chunk"
    assert item.source == chunk.source_refs[0]
    assert item.metadata["document_id"] == document.document_id
    assert item.metadata["asset_id"] == asset.asset_id


def test_in_memory_chunk_retriever_returns_ranked_hits_with_lineage() -> None:
    asset, revision = create_local_text_revision(
        "file:///tmp/notes.txt",
        "alpha beta\nbeta gamma\nunrelated\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "alpha beta\nbeta gamma\nunrelated\n")
    chunks = chunk_document_by_lines(document, revision, max_elements=1)
    retriever = InMemoryChunkRetriever(chunks, retriever_id="local-test")

    hits = retriever.search("beta", top_k=2)

    assert [hit.rank for hit in hits] == [1, 2]
    assert [hit.item.item_id for hit in hits] == [chunks[0].chunk_id, chunks[1].chunk_id]
    assert hits[0].normalized_score == 1.0
    assert hits[0].retriever == "local-test"
    assert hits[0].highlights[0] == chunks[0].source_refs[0]


def test_in_memory_chunk_retriever_returns_empty_for_blank_query() -> None:
    retriever = InMemoryChunkRetriever([], retriever_id="local-test")

    assert retriever.search("   ") == []


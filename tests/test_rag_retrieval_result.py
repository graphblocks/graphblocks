from __future__ import annotations

from graphblocks.documents import create_local_text_revision, parse_plain_text_document, chunk_document_by_lines
from graphblocks.rag import InMemoryChunkRetriever, RetrievalResult, SearchRequest


def test_in_memory_chunk_retriever_returns_retrieval_result() -> None:
    asset, revision = create_local_text_revision(
        "file:///tmp/notes.txt",
        "alpha beta\nbeta gamma\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "alpha beta\nbeta gamma\n")
    chunks = chunk_document_by_lines(document, revision, max_elements=1)
    retriever = InMemoryChunkRetriever(chunks, retriever_id="local-test")
    request = SearchRequest(query_text="beta", top_k=1, metadata={"trace": "t1"})

    result = retriever.retrieve(request)

    assert isinstance(result, RetrievalResult)
    assert result.retrieval_id == "local-test:sha256:92ad1ed2d770857f46e818b340b78cf1e1c770c073c2346fe93818ef728cfd60"
    assert result.request == request
    assert result.hits[0].item.item_id == chunks[0].chunk_id
    assert result.total_candidates == 2
    assert result.warnings == []
    assert result.metadata == {}


def test_in_memory_chunk_retriever_returns_empty_retrieval_result_for_blank_query() -> None:
    retriever = InMemoryChunkRetriever([], retriever_id="local-test")
    request = SearchRequest(query_text="   ", top_k=3)

    result = retriever.retrieve(request)

    assert result.request == request
    assert result.hits == []
    assert result.total_candidates == 0

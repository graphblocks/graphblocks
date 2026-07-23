from __future__ import annotations

import pytest

from graphblocks.documents import (
    DocumentSpan,
    SourceRef,
    create_local_text_revision,
    parse_plain_text_document,
    chunk_document_by_lines,
)
from graphblocks.rag import (
    AuthContext,
    ContextPack,
    FederatedRetrievalSource,
    InMemoryChunkRetriever,
    KnowledgeItemRef,
    QueryPlan,
    RetrievalResult,
    SearchHit,
    SearchRequest,
)


def _source() -> SourceRef:
    return SourceRef(
        source_id="chunk-1",
        source_kind="document_chunk",
        locator=DocumentSpan(
            asset_id="asset-1",
            revision_id="rev-1",
            document_id="doc-1",
            chunk_id="chunk-1",
        ),
    )


def _hit() -> SearchHit:
    source = _source()
    return SearchHit(
        hit_id="hit-1",
        item=KnowledgeItemRef(
            item_id="chunk-1",
            item_kind="document_chunk",
            source=source,
            preview=["alpha beta"],
        ),
        rank=1,
        retriever="local-test",
        normalized_score=1.0,
        highlights=[source],
    )


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


@pytest.mark.parametrize("top_k", [-1, True, 1.5])
def test_search_request_rejects_invalid_top_k(top_k: object) -> None:
    with pytest.raises(ValueError, match="top_k must be a non-negative integer"):
        SearchRequest(query_text="beta", top_k=top_k)  # type: ignore[arg-type]


def test_rag_request_item_hit_context_and_result_records_validate_wire_shape() -> None:
    with pytest.raises(ValueError, match="search request query_text must be a string"):
        SearchRequest(query_text=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="search request filters must be a mapping"):
        SearchRequest(query_text="beta", filters=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="search request metadata keys must not be empty"):
        SearchRequest(query_text="beta", metadata={" ": "value"})

    source = _source()
    with pytest.raises(ValueError, match="knowledge item ref item_id must not be empty"):
        KnowledgeItemRef(item_id="", item_kind="document_chunk", source=source)
    with pytest.raises(ValueError, match="knowledge item ref source must be a SourceRef"):
        KnowledgeItemRef(item_id="chunk-1", item_kind="document_chunk", source=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="knowledge item ref preview must be a list of strings"):
        KnowledgeItemRef(item_id="chunk-1", item_kind="document_chunk", source=source, preview="alpha")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="knowledge item ref acl metadata keys must be strings"):
        KnowledgeItemRef(item_id="chunk-1", item_kind="document_chunk", source=source, acl={object(): "value"})  # type: ignore[dict-item]

    hit = _hit()
    with pytest.raises(ValueError, match="search hit hit_id must not be empty"):
        SearchHit(hit_id="", item=hit.item, rank=1, retriever="local-test")
    with pytest.raises(ValueError, match="search hit item must be a KnowledgeItemRef"):
        SearchHit(hit_id="hit-1", item=object(), rank=1, retriever="local-test")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="search hit rank must be positive"):
        SearchHit(hit_id="hit-1", item=hit.item, rank=0, retriever="local-test")
    with pytest.raises(ValueError, match="search hit normalized_score must be between 0 and 1"):
        SearchHit(hit_id="hit-1", item=hit.item, rank=1, retriever="local-test", normalized_score=1.1)
    with pytest.raises(ValueError, match="search hit highlights must be a list of SourceRef records"):
        SearchHit(hit_id="hit-1", item=hit.item, rank=1, retriever="local-test", highlights=[object()])  # type: ignore[list-item]

    with pytest.raises(ValueError, match="context pack context_id must not be empty"):
        ContextPack(context_id=" ", hits=[])
    with pytest.raises(ValueError, match="context pack hits must be a list of SearchHit records"):
        ContextPack(context_id="ctx-1", hits=[object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="context pack token_count must not exceed token_budget"):
        ContextPack(context_id="ctx-1", hits=[], token_budget=1, token_count=2)

    request = SearchRequest(query_text="beta")
    with pytest.raises(ValueError, match="retrieval result retrieval_id must not be empty"):
        RetrievalResult(retrieval_id="", request=request, hits=[])
    with pytest.raises(ValueError, match="retrieval result request must be a SearchRequest"):
        RetrievalResult(retrieval_id="ret-1", request=object(), hits=[])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="retrieval result hits must be a list of SearchHit records"):
        RetrievalResult(retrieval_id="ret-1", request=request, hits=[object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="retrieval result total_candidates must be non-negative"):
        RetrievalResult(retrieval_id="ret-1", request=request, hits=[], total_candidates=-1)
    with pytest.raises(ValueError, match="retrieval result latency_ms must be finite"):
        RetrievalResult(retrieval_id="ret-1", request=request, hits=[], latency_ms=float("nan"))
    with pytest.raises(ValueError, match="hit_id values must be unique"):
        RetrievalResult(retrieval_id="ret-1", request=request, hits=[hit, hit])
    with pytest.raises(ValueError, match="must not be less than hits length"):
        RetrievalResult(
            retrieval_id="ret-1",
            request=request,
            hits=[hit],
            total_candidates=0,
        )
    with pytest.raises(ValueError, match="context pack hit_id values must be unique"):
        ContextPack(context_id="ctx-1", hits=[hit, hit])


@pytest.mark.parametrize(
    ("factory", "expected_error"),
    (
        (
            lambda: SearchRequest(query_text="beta", metadata={" trace": "t1"}),
            "search request metadata keys must not contain surrounding whitespace",
        ),
        (
            lambda: QueryPlan(original="beta", rewritten=[" beta"]),
            "query plan rewritten item must not contain surrounding whitespace",
        ),
        (
            lambda: AuthContext(tenant_id=" tenant-1", principal_id="user-1"),
            "auth context tenant_id must not contain surrounding whitespace",
        ),
        (
            lambda: AuthContext(tenant_id="tenant-1", principal_id="user-1", groups={" support"}),
            "auth context groups item must not contain surrounding whitespace",
        ),
        (
            lambda: AuthContext(tenant_id="tenant-1", principal_id="user-1", attributes={" role": "support"}),
            "auth context attributes metadata keys must not contain surrounding whitespace",
        ),
        (
            lambda: RetrievalResult(retrieval_id=" ret-1", request=SearchRequest("beta"), hits=[]),
            "retrieval result retrieval_id must not contain surrounding whitespace",
        ),
        (
            lambda: RetrievalResult(retrieval_id="ret-1", request=SearchRequest("beta"), hits=[], warnings=[" stale"]),
            "retrieval result warnings item must not contain surrounding whitespace",
        ),
        (
            lambda: FederatedRetrievalSource(source_id=" local", result=RetrievalResult("ret-1", SearchRequest("beta"), [])),
            "federated retrieval source source_id must not contain surrounding whitespace",
        ),
        (
            lambda: KnowledgeItemRef(item_id=" chunk-1", item_kind="document_chunk", source=_source()),
            "knowledge item ref item_id must not contain surrounding whitespace",
        ),
        (
            lambda: KnowledgeItemRef(
                item_id="chunk-1",
                item_kind="document_chunk",
                source=_source(),
                schema_ref=" schemas/Chunk@1",
            ),
            "knowledge item ref schema_ref must not contain surrounding whitespace",
        ),
        (
            lambda: KnowledgeItemRef(
                item_id="chunk-1",
                item_kind="document_chunk",
                source=_source(),
                preview=[" alpha"],
            ),
            "knowledge item ref preview item must not contain surrounding whitespace",
        ),
        (
            lambda: SearchHit(hit_id=" hit-1", item=_hit().item, rank=1, retriever="local-test"),
            "search hit hit_id must not contain surrounding whitespace",
        ),
        (
            lambda: SearchHit(hit_id="hit-1", item=_hit().item, rank=1, retriever=" local-test"),
            "search hit retriever must not contain surrounding whitespace",
        ),
        (
            lambda: SearchHit(hit_id="hit-1", item=_hit().item, rank=1, retriever="local-test", score_kind=" bm25"),
            "search hit score_kind must not contain surrounding whitespace",
        ),
        (
            lambda: ContextPack(context_id=" ctx-1", hits=[]),
            "context pack context_id must not contain surrounding whitespace",
        ),
        (
            lambda: ContextPack(context_id="ctx-1", hits=[], metadata={" source": "kb"}),
            "context pack metadata keys must not contain surrounding whitespace",
        ),
    ),
)
def test_rag_retrieval_records_reject_whitespace_wrapped_identities(
    factory: object,
    expected_error: str,
) -> None:
    with pytest.raises(ValueError, match=expected_error):
        factory()


def test_federated_retrieval_source_validates_identity_result_and_weight() -> None:
    request = SearchRequest(query_text="beta")
    result = RetrievalResult(retrieval_id="ret-1", request=request, hits=[])

    source = FederatedRetrievalSource(source_id="local", result=result, weight=2)

    assert source.weight == 2.0

    with pytest.raises(ValueError, match="federated retrieval source source_id must not be empty"):
        FederatedRetrievalSource(source_id=" ", result=result)
    with pytest.raises(ValueError, match="federated retrieval source result must be a RetrievalResult"):
        FederatedRetrievalSource(source_id="local", result=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="federated retrieval source weight must be positive"):
        FederatedRetrievalSource(source_id="local", result=result, weight=0)

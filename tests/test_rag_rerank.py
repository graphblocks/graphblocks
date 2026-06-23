from __future__ import annotations

from graphblocks.documents import DocumentSpan, SourceRef
from graphblocks.rag import KnowledgeItemRef, SearchHit, rerank_search_hits


def _hit(hit_id: str, item_id: str, document_id: str, preview: str, rank: int) -> SearchHit:
    source = SourceRef(
        source_id=item_id,
        source_kind="document_chunk",
        locator=DocumentSpan(
            asset_id="asset-1",
            revision_id="rev-1",
            document_id=document_id,
            chunk_id=item_id,
        ),
    )
    return SearchHit(
        hit_id=hit_id,
        item=KnowledgeItemRef(item_id=item_id, item_kind="document_chunk", source=source, preview=[preview]),
        rank=rank,
        retriever="local",
        highlights=[source],
    )


def test_rerank_search_hits_scores_query_terms_and_records_provenance() -> None:
    hits = [
        _hit("hit-a", "chunk-a", "doc-1", "alpha", 1),
        _hit("hit-b", "chunk-b", "doc-1", "beta beta alpha", 2),
        _hit("hit-c", "chunk-c", "doc-1", "beta", 3),
    ]

    result = rerank_search_hits(hits, reranker_id="rank.rule", query_terms=["beta"])

    assert [ranked.hit.hit_id for ranked in result.ranked_hits] == ["hit-b", "hit-c", "hit-a"]
    assert result.ranked_hits[0].rerank_score == 2.0
    assert result.ranked_hits[0].reranker == "rank.rule"
    assert result.ranked_hits[0].explanation == "matched 2 query term occurrence(s)"
    assert result.ranked_hits[0].metadata["original_rank"] == 2
    assert result.ranked_hits[0].metadata["source_hit_id"] == "hit-b"
    assert result.metadata["query_terms"] == ["beta"]


def test_rerank_search_hits_applies_input_limit_and_reports_truncation() -> None:
    hits = [
        _hit("hit-a", "chunk-a", "doc-1", "alpha", 1),
        _hit("hit-b", "chunk-b", "doc-1", "beta", 2),
        _hit("hit-c", "chunk-c", "doc-1", "beta beta", 3),
    ]

    result = rerank_search_hits(hits, reranker_id="rank.rule", query_terms=["beta"], input_limit=2)

    assert result.input_count == 3
    assert result.evaluated_count == 2
    assert result.truncated_hit_ids == ["hit-c"]
    assert [ranked.hit.hit_id for ranked in result.ranked_hits] == ["hit-b", "hit-a"]

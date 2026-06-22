from __future__ import annotations

import pytest

from graphblocks.documents import DocumentSpan, SourceRef
from graphblocks.rag import KnowledgeItemRef, SearchHit, fuse_search_hits


def _hit(hit_id: str, item_id: str, rank: int, retriever: str) -> SearchHit:
    source = SourceRef(
        source_id=item_id,
        source_kind="document_chunk",
        locator=DocumentSpan(
            asset_id="asset-1",
            revision_id="rev-1",
            document_id="doc-1",
            chunk_id=item_id,
        ),
    )
    return SearchHit(
        hit_id=hit_id,
        item=KnowledgeItemRef(
            item_id=item_id,
            item_kind="document_chunk",
            source=source,
            preview=[item_id],
            metadata={"document_id": "doc-1"},
        ),
        rank=rank,
        retriever=retriever,
        highlights=[source],
    )


def test_fuse_search_hits_uses_reciprocal_rank_fusion_and_preserves_source_ranks() -> None:
    keyword_hits = [_hit("kw-b", "chunk-b", 1, "keyword"), _hit("kw-a", "chunk-a", 2, "keyword")]
    dense_hits = [_hit("dense-a", "chunk-a", 1, "dense")]

    fused = fuse_search_hits([keyword_hits, dense_hits], strategy="reciprocal_rank_fusion", k=60, retriever_id="fused")

    assert [hit.item.item_id for hit in fused] == ["chunk-a", "chunk-b"]
    assert [hit.rank for hit in fused] == [1, 2]
    assert fused[0].retriever == "fused"
    assert fused[0].score_kind == "reciprocal_rank_fusion"
    assert fused[0].metadata["source_hit_ids"] == ["kw-a", "dense-a"]
    assert fused[0].metadata["source_ranks"] == {"keyword": 2, "dense": 1}


def test_fuse_search_hits_concatenate_keeps_first_duplicate() -> None:
    fused = fuse_search_hits(
        [[_hit("kw-a", "chunk-a", 1, "keyword")], [_hit("dense-a", "chunk-a", 1, "dense")]],
        strategy="concatenate",
        retriever_id="concat",
    )

    assert [hit.hit_id for hit in fused] == ["concat:chunk-a"]
    assert fused[0].rank == 1
    assert fused[0].retriever == "concat"
    assert fused[0].metadata["source_hit_ids"] == ["kw-a", "dense-a"]


def test_fuse_search_hits_rejects_unknown_strategy() -> None:
    with pytest.raises(ValueError):
        fuse_search_hits([], strategy="unknown")

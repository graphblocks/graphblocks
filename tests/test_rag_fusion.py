from __future__ import annotations

from dataclasses import replace

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


def test_fuse_search_hits_deduplicates_equivalent_source_spans() -> None:
    keyword_hit = _hit("kw-a", "chunk-a", 1, "keyword")
    provider_source = SourceRef(
        source_id="provider-chunk-a",
        source_kind="document_chunk",
        locator=DocumentSpan(
            asset_id="asset-1",
            revision_id="rev-1",
            document_id="doc-1",
            chunk_id="chunk-a",
        ),
    )
    provider_hit = SearchHit(
        hit_id="provider-a",
        item=KnowledgeItemRef(
            item_id="provider-chunk-a",
            item_kind="document_chunk",
            source=provider_source,
            preview=["provider copy"],
            metadata={"document_id": "doc-1"},
        ),
        rank=1,
        retriever="provider",
        highlights=[provider_source],
    )

    fused = fuse_search_hits(
        [[keyword_hit], [provider_hit]],
        strategy="reciprocal_rank_fusion",
        retriever_id="fused",
    )

    assert [hit.item.item_id for hit in fused] == ["chunk-a"]
    assert fused[0].metadata["source_hit_ids"] == ["kw-a", "provider-a"]
    assert fused[0].metadata["dedupe_key"].startswith("source_span:")


def test_fuse_search_hits_assigns_unique_ids_to_distinct_spans_of_same_item() -> None:
    first = _hit("first", "chunk-a", 1, "keyword")
    second = _hit("second", "chunk-a", 2, "keyword")
    second_source = replace(
        second.item.source,
        locator=replace(
            second.item.source.locator,
            char_start=5,
            char_end=10,
        ),
    )
    second = replace(
        second,
        item=replace(second.item, source=second_source),
        highlights=[second_source],
    )

    fused = fuse_search_hits(
        [[first, second]],
        strategy="reciprocal_rank_fusion",
        retriever_id="fused",
    )

    assert len(fused) == 2
    assert len({hit.hit_id for hit in fused}) == 2
    assert all(hit.hit_id.startswith("fused:chunk-a:sha256:") for hit in fused)


def test_fuse_search_hits_clamps_legacy_zero_rank_for_rrf_parity() -> None:
    hit = _hit("legacy", "chunk-a", 1, "keyword")
    object.__setattr__(hit, "rank", 0)

    fused = fuse_search_hits(
        [[hit]],
        strategy="reciprocal_rank_fusion",
        k=60,
    )

    assert fused[0].raw_score == 1 / 61


def test_fuse_search_hits_rejects_unknown_strategy() -> None:
    with pytest.raises(ValueError):
        fuse_search_hits([], strategy="unknown")


@pytest.mark.parametrize(
    "kwargs",
    (
        {"k": True},
        {"weights": [True]},
        {"weights": [0.0]},
        {"weights": [float("inf")]},
        {"weights": [10**1_000]},
    ),
)
def test_fuse_search_hits_rejects_invalid_numeric_inputs(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        fuse_search_hits(
            [[_hit("kw-a", "chunk-a", 1, "keyword")]],
            **kwargs,  # type: ignore[arg-type]
        )


def test_fuse_search_hits_handles_huge_rrf_k_without_overflow() -> None:
    fused = fuse_search_hits(
        [[_hit("kw-a", "chunk-a", 1, "keyword")]],
        k=10**1_000,
    )

    assert fused[0].raw_score == 0.0


def test_fuse_search_hits_supports_weighted_rank_strategy() -> None:
    keyword_hits = [_hit("kw-a", "chunk-a", 1, "keyword"), _hit("kw-b", "chunk-b", 2, "keyword")]
    dense_hits = [_hit("dense-b", "chunk-b", 1, "dense"), _hit("dense-a", "chunk-a", 2, "dense")]

    fused = fuse_search_hits(
        [keyword_hits, dense_hits],
        strategy="weighted_rank",
        weights=[0.5, 2.0],
        retriever_id="weighted",
    )

    assert [hit.item.item_id for hit in fused] == ["chunk-b", "chunk-a"]
    assert fused[0].raw_score == 2.25
    assert fused[0].normalized_score == 1.0
    assert fused[0].score_kind == "weighted_rank"
    assert fused[0].metadata["fusion_strategy"] == "weighted_rank"


def test_fuse_search_hits_supports_normalized_score_strategy() -> None:
    keyword_hits = [
        _hit("kw-a", "chunk-a", 1, "keyword"),
        _hit("kw-b", "chunk-b", 2, "keyword"),
    ]
    keyword_hits[0] = replace(keyword_hits[0], normalized_score=0.1)
    keyword_hits[1] = replace(keyword_hits[1], normalized_score=0.9)
    dense_hits = [_hit("dense-a", "chunk-a", 1, "dense")]
    dense_hits[0] = replace(dense_hits[0], normalized_score=0.6)

    fused = fuse_search_hits(
        [keyword_hits, dense_hits],
        strategy="normalized_score",
        retriever_id="score",
    )

    assert [hit.item.item_id for hit in fused] == ["chunk-b", "chunk-a"]
    assert fused[0].raw_score == 0.9
    assert fused[1].raw_score == 0.7
    assert fused[0].normalized_score == 1.0
    assert fused[0].score_kind == "normalized_score"


def test_fuse_search_hits_supports_interleave_strategy() -> None:
    keyword_hits = [_hit("kw-a", "chunk-a", 1, "keyword"), _hit("kw-c", "chunk-c", 2, "keyword")]
    dense_hits = [_hit("dense-b", "chunk-b", 1, "dense"), _hit("dense-a", "chunk-a", 2, "dense")]

    fused = fuse_search_hits(
        [keyword_hits, dense_hits],
        strategy="interleave",
        retriever_id="interleave",
    )

    assert [hit.item.item_id for hit in fused] == ["chunk-a", "chunk-b", "chunk-c"]
    assert fused[0].raw_score is None
    assert fused[0].score_kind == "interleave"
    assert fused[0].metadata["source_hit_ids"] == ["kw-a", "dense-a"]

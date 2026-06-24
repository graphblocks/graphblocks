from __future__ import annotations

from dataclasses import replace

from graphblocks.documents import DocumentSpan, SourceRef
from graphblocks.rag import KnowledgeItemRef, SearchHit, build_context_pack


def _hit(hit_id: str, document_id: str, text: str, rank: int) -> SearchHit:
    source = SourceRef(
        source_id=hit_id,
        source_kind="document_chunk",
        locator=DocumentSpan(
            asset_id="asset-1",
            revision_id="rev-1",
            document_id=document_id,
            chunk_id=hit_id,
        ),
    )
    return SearchHit(
        hit_id=hit_id,
        item=KnowledgeItemRef(
            item_id=hit_id,
            item_kind="document_chunk",
            source=source,
            preview=[text],
            metadata={"document_id": document_id},
        ),
        rank=rank,
        retriever="local",
        normalized_score=1.0 / rank,
        highlights=[source],
    )


def test_build_context_pack_respects_token_budget_and_records_provenance() -> None:
    hits = [
        _hit("hit-1", "doc-1", "alpha beta", 1),
        _hit("hit-2", "doc-2", "gamma delta", 2),
        _hit("hit-3", "doc-3", "epsilon", 3),
    ]

    context = build_context_pack("ctx-1", hits, token_budget=3)

    assert [hit.hit_id for hit in context.hits] == ["hit-1", "hit-3"]
    assert context.token_budget == 3
    assert context.token_count == 3
    assert context.metadata["selected_hit_ids"] == ["hit-1", "hit-3"]
    assert context.metadata["dropped_hit_ids"] == ["hit-2"]
    assert context.metadata["drop_reasons"] == {"hit-2": "token_budget"}


def test_build_context_pack_reserves_output_tokens_from_budget() -> None:
    hits = [
        _hit("hit-1", "doc-1", "alpha beta", 1),
        _hit("hit-2", "doc-2", "gamma delta", 2),
        _hit("hit-3", "doc-3", "epsilon", 3),
    ]

    context = build_context_pack("ctx-1", hits, token_budget=4, reserve_output_tokens=1)

    assert [hit.hit_id for hit in context.hits] == ["hit-1", "hit-3"]
    assert context.token_budget == 4
    assert context.token_count == 3
    assert context.metadata["reserve_output_tokens"] == 1
    assert context.metadata["effective_context_token_budget"] == 3
    assert context.metadata["dropped_hit_ids"] == ["hit-2"]
    assert context.metadata["drop_reasons"] == {"hit-2": "token_budget"}


def test_build_context_pack_limits_chunks_per_document() -> None:
    hits = [
        _hit("hit-1", "doc-1", "alpha", 1),
        _hit("hit-2", "doc-1", "beta", 2),
        _hit("hit-3", "doc-2", "gamma", 3),
    ]

    context = build_context_pack("ctx-1", hits, token_budget=10, per_document_max_chunks=1)

    assert [hit.hit_id for hit in context.hits] == ["hit-1", "hit-3"]
    assert context.metadata["dropped_hit_ids"] == ["hit-2"]
    assert context.metadata["drop_reasons"] == {"hit-2": "per_document_max_chunks"}


def test_build_context_pack_limits_chunks_per_section() -> None:
    first = replace(
        _hit("hit-1", "doc-1", "alpha", 1),
        metadata={"section_id": "section-a"},
    )
    same_section = replace(
        _hit("hit-2", "doc-1", "beta", 2),
        metadata={"section_id": "section-a"},
    )
    other_section = replace(
        _hit("hit-3", "doc-1", "gamma", 3),
        metadata={"section_id": "section-b"},
    )

    context = build_context_pack(
        "ctx-1",
        [first, same_section, other_section],
        token_budget=10,
        per_section_max_chunks=1,
    )

    assert [hit.hit_id for hit in context.hits] == ["hit-1", "hit-3"]
    assert context.metadata["dropped_hit_ids"] == ["hit-2"]
    assert context.metadata["drop_reasons"] == {"hit-2": "per_section_max_chunks"}
    assert context.metadata["per_section_max_chunks"] == 1


def test_build_context_pack_limits_chunks_per_source() -> None:
    first = replace(_hit("hit-1", "doc-1", "alpha", 1), retriever="retriever-a")
    same_source = replace(
        _hit("hit-2", "doc-2", "beta", 2),
        retriever="retriever-a",
    )
    other_source = replace(
        _hit("hit-3", "doc-3", "gamma", 3),
        retriever="retriever-b",
    )

    context = build_context_pack(
        "ctx-1",
        [first, same_source, other_source],
        token_budget=10,
        per_source_max_chunks=1,
    )

    assert [hit.hit_id for hit in context.hits] == ["hit-1", "hit-3"]
    assert context.metadata["dropped_hit_ids"] == ["hit-2"]
    assert context.metadata["drop_reasons"] == {"hit-2": "per_source_max_chunks"}
    assert context.metadata["per_source_max_chunks"] == 1


def test_build_context_pack_deduplicates_items_by_default() -> None:
    first = _hit("hit-1", "doc-1", "alpha", 1)
    duplicate = _hit("hit-1", "doc-1", "alpha", 2)

    context = build_context_pack("ctx-1", [first, duplicate], token_budget=10)

    assert [hit.hit_id for hit in context.hits] == ["hit-1"]
    assert context.metadata["dropped_hit_ids"] == ["hit-1"]
    assert context.metadata["drop_reasons"] == {"hit-1": "duplicate"}


def test_build_context_pack_filters_hits_by_minimum_source_modified_at() -> None:
    fresh = replace(
        _hit("hit-fresh", "doc-1", "fresh", 1),
        metadata={"source_modified_at": "2026-06-22T00:00:00Z"},
    )
    stale = replace(
        _hit("hit-stale", "doc-2", "stale", 2),
        metadata={"source_modified_at": "2026-06-20T00:00:00Z"},
    )
    unknown = _hit("hit-unknown", "doc-3", "unknown", 3)

    context = build_context_pack(
        "ctx-1",
        [fresh, stale, unknown],
        token_budget=10,
        minimum_source_modified_at="2026-06-21T00:00:00Z",
    )

    assert [hit.hit_id for hit in context.hits] == ["hit-fresh"]
    assert context.metadata["dropped_hit_ids"] == ["hit-stale", "hit-unknown"]
    assert context.metadata["drop_reasons"] == {
        "hit-stale": "freshness",
        "hit-unknown": "freshness",
    }
    assert context.metadata["minimum_source_modified_at"] == "2026-06-21T00:00:00Z"

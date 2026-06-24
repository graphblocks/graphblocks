from __future__ import annotations

import pytest

from graphblocks.documents import DocumentSpan, SourceRef
from graphblocks.rag import (
    FederatedRetrievalError,
    FederatedRetrievalSource,
    KnowledgeItemRef,
    RetrievalResult,
    SearchHit,
    SearchRequest,
    federated_retrieve,
)


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
        item=KnowledgeItemRef(item_id=item_id, item_kind="document_chunk", source=source, preview=[item_id]),
        rank=rank,
        retriever=retriever,
        highlights=[source],
    )


def test_federated_retrieve_partially_fuses_successful_sources_and_records_failures() -> None:
    request = SearchRequest("password reset", top_k=2)
    policy = RetrievalResult(
        retrieval_id="policy-ret",
        request=request,
        hits=[_hit("policy-a", "chunk-a", 1, "policy"), _hit("policy-b", "chunk-b", 2, "policy")],
    )
    ticket = RetrievalResult(
        retrieval_id="ticket-ret",
        request=request,
        hits=[_hit("ticket-b", "chunk-b", 1, "ticket")],
    )

    result = federated_retrieve(
        "federated",
        request,
        [
            FederatedRetrievalSource("policy", result=policy, weight=0.5),
            FederatedRetrievalSource("ticket", result=ticket, weight=2.0),
            FederatedRetrievalSource("web", error="timeout", weight=0.3),
        ],
        failure_mode="partial",
        fusion_strategy="weighted_rank",
    )

    assert [hit.item.item_id for hit in result.hits] == ["chunk-b", "chunk-a"]
    assert result.hits[0].raw_score == 2.25
    assert result.total_candidates == 3
    assert result.warnings == ["federated source web failed: timeout"]
    assert result.metadata["failed_sources"] == [{"source_id": "web", "error": "timeout"}]
    assert result.metadata["successful_sources"] == ["policy", "ticket"]
    assert result.metadata["fusion_strategy"] == "weighted_rank"


def test_federated_retrieve_fail_mode_rejects_failed_source() -> None:
    request = SearchRequest("password reset", top_k=2)

    with pytest.raises(FederatedRetrievalError, match="federated source web failed: timeout"):
        federated_retrieve(
            "federated",
            request,
            [FederatedRetrievalSource("web", error="timeout")],
            failure_mode="fail",
        )


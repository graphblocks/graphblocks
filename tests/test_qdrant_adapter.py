from __future__ import annotations

import importlib

import pytest

from graphblocks import SearchRequest


def test_qdrant_search_request_encodes_named_vector_and_filters(monkeypatch) -> None:
    graphblocks_qdrant = importlib.import_module("graphblocks.integrations.qdrant")
    request = SearchRequest(
        query_text="refund policy",
        top_k=3,
        filters={"classification": "internal", "tags": ["billing", "refund"]},
        metadata={"trace": "search-1"},
    )

    search = graphblocks_qdrant.qdrant_search_request(
        request,
        collection=graphblocks_qdrant.QdrantCollectionRef(
            collection="support_chunks",
            vector_name="text",
        ),
        vector=(0.1, 0.2, 0.3),
        score_threshold=0.25,
    )

    assert search.request_contract() == {
        "collection": "support_chunks",
        "body": {
            "filter": {
                "must": [
                    {"key": "classification", "match": {"value": "internal"}},
                    {"key": "tags", "match": {"any": ["billing", "refund"]}},
                ]
            },
            "limit": 3,
            "score_threshold": 0.25,
            "vector": {"name": "text", "vector": [0.1, 0.2, 0.3]},
            "with_payload": True,
            "with_vector": False,
        },
        "query_text": "refund policy",
        "metadata": {"trace": "search-1"},
    }


def test_qdrant_search_request_rejects_invalid_inputs(monkeypatch) -> None:
    graphblocks_qdrant = importlib.import_module("graphblocks.integrations.qdrant")

    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="collection"):
        graphblocks_qdrant.QdrantCollectionRef(collection=" ")

    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="top_k"):
        graphblocks_qdrant.qdrant_search_request(
            SearchRequest(query_text="refund", top_k=0),
            collection=graphblocks_qdrant.QdrantCollectionRef(collection="support_chunks"),
            vector=(0.1,),
        )

    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="vector"):
        graphblocks_qdrant.qdrant_search_request(
            SearchRequest(query_text="refund", top_k=1),
            collection=graphblocks_qdrant.QdrantCollectionRef(collection="support_chunks"),
            vector=(),
        )

    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="must not override its key"):
        graphblocks_qdrant.qdrant_search_request(
            SearchRequest(
                query_text="refund",
                top_k=1,
                filters={"tenant_id": {"key": "public", "match": {"value": "acme"}}},
            ),
            collection=graphblocks_qdrant.QdrantCollectionRef(collection="support_chunks"),
            vector=(0.1,),
        )


def test_qdrant_points_map_to_search_hits_with_source_acl_and_preview(monkeypatch) -> None:
    graphblocks_qdrant = importlib.import_module("graphblocks.integrations.qdrant")
    points = [
        {
            "id": "chunk-1",
            "score": 0.82,
            "payload": {
                "item_id": "chunk-1",
                "item_kind": "document_chunk",
                "schema_ref": "schemas/SupportChunk@1",
                "payload_ref": "blob://chunks/chunk-1",
                "preview": ["Refunds are available within 30 days."],
                "acl": {"tenant_id": "acme", "groups": ["support"]},
                "metadata": {"document_id": "doc-1", "section": "refunds"},
                "source": {
                    "source_id": "doc-1",
                    "source_kind": "document",
                    "revision": "rev-1",
                    "digest": "sha256:doc",
                    "trust": "retrieved_untrusted",
                    "metadata": {"path": "kb/refunds.md"},
                },
            },
        }
    ]

    hits = graphblocks_qdrant.qdrant_hits_from_points(points, retriever_id="qdrant-support")
    points[0]["payload"]["preview"][0] = "mutated"

    assert len(hits) == 1
    assert hits[0].hit_id == "qdrant-support:1:chunk-1"
    assert hits[0].rank == 1
    assert hits[0].retriever == "qdrant-support"
    assert hits[0].raw_score == 0.82
    assert hits[0].normalized_score == 0.82
    assert hits[0].score_kind == "qdrant_score"
    assert hits[0].item.item_id == "chunk-1"
    assert hits[0].item.item_kind == "document_chunk"
    assert hits[0].item.schema_ref == "schemas/SupportChunk@1"
    assert hits[0].item.payload_ref == "blob://chunks/chunk-1"
    assert hits[0].item.preview == ["Refunds are available within 30 days."]
    assert hits[0].item.acl == {"tenant_id": "acme", "groups": ["support"]}
    assert hits[0].item.metadata == {"document_id": "doc-1", "section": "refunds"}
    assert hits[0].item.source.source_id == "doc-1"
    assert hits[0].item.source.source_kind == "document"
    assert hits[0].item.source.revision == "rev-1"
    assert hits[0].item.source.digest == "sha256:doc"
    assert hits[0].item.source.metadata == {"path": "kb/refunds.md"}
    assert hits[0].highlights == [hits[0].item.source]
    assert hits[0].metadata == {"qdrant_point_id": "chunk-1"}


def test_qdrant_points_without_source_use_untrusted_point_reference(monkeypatch) -> None:
    graphblocks_qdrant = importlib.import_module("graphblocks.integrations.qdrant")

    hits = graphblocks_qdrant.qdrant_hits_from_points(
        ({"id": 7, "score": 3.5, "payload": {}} for _ in range(1)),
        retriever_id="qdrant-support",
    )

    assert hits[0].hit_id == "qdrant-support:1:7"
    assert hits[0].normalized_score is None
    assert hits[0].item.item_id == "7"
    assert hits[0].item.source.source_id == "qdrant:7"
    assert hits[0].item.source.source_kind == "vector_point"
    assert hits[0].item.source.trust == "retrieved_untrusted"


def test_qdrant_hits_reject_blank_score_kind(monkeypatch) -> None:
    graphblocks_qdrant = importlib.import_module("graphblocks.integrations.qdrant")

    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="score_kind"):
        graphblocks_qdrant.qdrant_hits_from_points(
            [{"id": "chunk-1", "payload": {}}],
            retriever_id="qdrant-support",
            score_kind=" ",
        )

from __future__ import annotations

import importlib

import pytest

from graphblocks import SearchRequest


_POINT_UUID = "550e8400-e29b-41d4-a716-446655440000"


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


def test_qdrant_adapter_rejects_coerced_thresholds_and_invalid_point_ids(
    monkeypatch,
) -> None:
    graphblocks_qdrant = importlib.import_module("graphblocks.integrations.qdrant")
    collection = graphblocks_qdrant.QdrantCollectionRef(collection="support_chunks")
    request = SearchRequest(query_text="refund", top_k=1)

    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="finite number"):
        graphblocks_qdrant.qdrant_search_request(
            request,
            collection=collection,
            vector=(0.1,),
            score_threshold="0.5",  # type: ignore[arg-type]
        )
    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="point id"):
        graphblocks_qdrant.qdrant_hits_from_points(
            [{"id": True, "payload": {}}],
            retriever_id="qdrant-support",
        )
    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="unsigned 64-bit"):
        graphblocks_qdrant.qdrant_hits_from_points(
            [{"id": 1 << 64, "payload": {}}],
            retriever_id="qdrant-support",
        )
    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="UUID"):
        graphblocks_qdrant.qdrant_hits_from_points(
            [{"id": "not-a-uuid", "payload": {}}],
            retriever_id="qdrant-support",
        )
    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="collection"):
        graphblocks_qdrant.QdrantCollectionRef(object())  # type: ignore[arg-type]


def test_qdrant_search_request_rejects_malformed_contract_boundaries(
    monkeypatch,
) -> None:
    graphblocks_qdrant = importlib.import_module("graphblocks.integrations.qdrant")
    request = SearchRequest(query_text="refund", top_k=1)
    collection = graphblocks_qdrant.QdrantCollectionRef("support_chunks")

    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="SearchRequest"):
        graphblocks_qdrant.qdrant_search_request(
            object(),  # type: ignore[arg-type]
            collection=collection,
            vector=(0.1,),
        )
    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="QdrantCollectionRef"):
        graphblocks_qdrant.qdrant_search_request(
            request,
            collection="support_chunks",  # type: ignore[arg-type]
            vector=(0.1,),
        )
    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="collection"):
        graphblocks_qdrant.QdrantSearchRequest(
            collection="support/chunks",
            body={"limit": 1},
            query_text="refund",
        )
    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="strict JSON"):
        graphblocks_qdrant.QdrantSearchRequest(
            collection="support_chunks",
            body={"threshold": float("nan")},
            query_text="refund",
        )
    malformed_request = SearchRequest(query_text="refund", top_k=1)
    object.__setattr__(
        malformed_request,
        "filters",
        {" tenant_id": "acme"},
    )
    with pytest.raises(graphblocks_qdrant.QdrantAdapterError, match="filter keys"):
        graphblocks_qdrant.qdrant_search_request(
            malformed_request,
            collection=collection,
            vector=(0.1,),
        )


def test_qdrant_search_request_preserves_null_filter(monkeypatch) -> None:
    graphblocks_qdrant = importlib.import_module("graphblocks.integrations.qdrant")

    search = graphblocks_qdrant.qdrant_search_request(
        SearchRequest(query_text="unclassified", top_k=1, filters={"classification": None}),
        collection=graphblocks_qdrant.QdrantCollectionRef(collection="support_chunks"),
        vector=(0.1,),
    )

    assert search.body["filter"] == {
        "must": [{"is_null": {"key": "classification"}}],
    }


def test_qdrant_points_map_to_search_hits_with_source_acl_and_preview(monkeypatch) -> None:
    graphblocks_qdrant = importlib.import_module("graphblocks.integrations.qdrant")
    points = [
        {
            "id": _POINT_UUID,
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
    assert hits[0].hit_id == f"qdrant-support:1:{_POINT_UUID}"
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
    assert hits[0].metadata == {"qdrant_point_id": _POINT_UUID}


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
            [{"id": 1, "payload": {}}],
            retriever_id="qdrant-support",
            score_kind=" ",
        )


@pytest.mark.parametrize(
    "point",
    (
        {"id": 1, "point_id": 1, "payload": {}},
        {"id": 1, "payload": {"source": None}},
        {
            "id": 1,
            "payload": {
                "source": {
                    "source_id": "doc-1",
                    "source_kind": "document",
                    "revision": 1,
                }
            },
        },
        {"id": 1, "payload": {"item_id": 1}},
        {"id": 1, "payload": {"item_id": " item-1"}},
        {"id": 1, "payload": {"preview": [1]}},
        {
            "id": 1,
            "payload": {
                "source": {
                    "source_id": "doc-1",
                    "source_kind": "document",
                    "trust": "trusted",
                }
            },
        },
    ),
)
def test_qdrant_hits_reject_ambiguous_or_coerced_payloads(
    monkeypatch,
    point: dict[str, object],
) -> None:
    graphblocks_qdrant = importlib.import_module("graphblocks.integrations.qdrant")

    with pytest.raises(graphblocks_qdrant.QdrantAdapterError):
        graphblocks_qdrant.qdrant_hits_from_points(
            [point],
            retriever_id="qdrant-support",
        )

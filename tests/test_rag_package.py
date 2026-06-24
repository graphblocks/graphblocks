from __future__ import annotations

import importlib
from pathlib import Path

from graphblocks import (
    FederatedRetrievalError,
    FederatedRetrievalSource,
    InMemoryKnowledgeIndex,
    KnowledgeDeleteMode,
    QueryPlan,
    RagResultBundle,
    RagResultPayload,
    chunk_document_by_lines,
    create_local_text_revision,
    federated_retrieve,
    parse_plain_text_document,
    render_context_pack,
    validate_answer_grounding,
)


ROOT = Path(__file__).parents[1]


def test_rag_package_exposes_in_memory_retrieval_and_context_helpers(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-rag" / "src"))
    graphblocks_rag = importlib.import_module("graphblocks_rag")
    asset, revision = create_local_text_revision(
        "file:///kb/support.txt",
        "Billing help\nShipping help\n",
        "2026-06-23T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "Billing help\nShipping help\n")
    chunks = chunk_document_by_lines(document, revision, max_elements=1)

    retriever = graphblocks_rag.InMemoryChunkRetriever(chunks, retriever_id="local-kb")
    result = retriever.retrieve(graphblocks_rag.SearchRequest("billing", top_k=2))
    context = graphblocks_rag.build_context_pack(
        "context-1",
        result.hits,
        token_budget=16,
        per_document_max_chunks=2,
    )

    assert result.total_candidates == 1
    assert result.hits[0].item.preview == ["Billing help"]
    assert result.hits[0].retriever == "local-kb"
    assert context.context_id == "context-1"
    assert context.metadata["selected_hit_ids"] == [result.hits[0].hit_id]


def test_rag_package_exposes_knowledge_index_facade(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-rag" / "src"))
    graphblocks_rag = importlib.import_module("graphblocks_rag")

    assert graphblocks_rag.InMemoryKnowledgeIndex is InMemoryKnowledgeIndex
    assert graphblocks_rag.KnowledgeDeleteMode is KnowledgeDeleteMode


def test_rag_package_exposes_result_bundle_profile(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-rag" / "src"))
    graphblocks_rag = importlib.import_module("graphblocks_rag")

    assert graphblocks_rag.QueryPlan is QueryPlan
    assert graphblocks_rag.RagResultPayload is RagResultPayload
    assert graphblocks_rag.RagResultBundle is RagResultBundle


def test_rag_package_exposes_context_renderer(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-rag" / "src"))
    graphblocks_rag = importlib.import_module("graphblocks_rag")

    assert graphblocks_rag.render_context_pack is render_context_pack


def test_rag_package_exposes_federated_retrieval_contract(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-rag" / "src"))
    graphblocks_rag = importlib.import_module("graphblocks_rag")

    assert graphblocks_rag.FederatedRetrievalSource is FederatedRetrievalSource
    assert graphblocks_rag.FederatedRetrievalError is FederatedRetrievalError
    assert graphblocks_rag.federated_retrieve is federated_retrieve


def test_rag_package_exposes_grounding_validator(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-rag" / "src"))
    graphblocks_rag = importlib.import_module("graphblocks_rag")

    assert graphblocks_rag.validate_answer_grounding is validate_answer_grounding

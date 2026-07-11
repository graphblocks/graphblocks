from __future__ import annotations

import pytest

from graphblocks.runtime import InProcessRuntime, stdlib_registry
from graphblocks.stdlib_rag import answer_validate_grounding, retrieve_execute_plan


def _hit(hit_id: str, item_id: str, rank: int, retriever: str, preview: str) -> dict[str, object]:
    source = {
        "sourceId": f"source-{item_id}",
        "sourceKind": "document_chunk",
        "revision": "revision-1",
        "digest": None,
        "locator": {
            "assetId": "asset-1",
            "revisionId": "revision-1",
            "documentId": "policy",
            "elementId": "paragraph-1",
            "chunkId": item_id,
            "page": None,
            "bbox": None,
            "charStart": None,
            "charEnd": None,
            "sheet": None,
            "cellRange": None,
            "slide": None,
        },
        "observedAt": None,
        "relevantAsOf": None,
        "trust": "verified",
        "accessPolicy": None,
        "metadata": {},
    }
    return {
        "hitId": hit_id,
        "item": {
            "itemId": item_id,
            "itemKind": "document_chunk",
            "source": source,
            "schemaRef": None,
            "payloadRef": None,
            "preview": [preview],
            "acl": None,
            "metadata": {"document_id": "policy"},
        },
        "rank": rank,
        "retriever": retriever,
        "rawScore": None,
        "normalizedScore": None,
        "scoreKind": None,
        "highlights": [source],
        "metadata": {},
    }


def test_rag_blocks_execute_as_one_runtime_graph_with_injected_sources() -> None:
    registry = stdlib_registry()
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "executable-rag-blocks"},
        "spec": {
            "nodes": {
                "retrieve": {
                    "block": "retrieve.execute_plan@1",
                    "inputs": {"query": "$input.query", "sources": "$input.sources"},
                    "config": {"minimumSuccessfulSources": 1, "topK": 5},
                },
                "fuse": {
                    "block": "retrieve.fuse@1",
                    "inputs": {"sources": "retrieve.result.sources"},
                    "config": {"algorithm": "reciprocal_rank_fusion"},
                },
                "rank": {
                    "block": "rank.documents@1",
                    "inputs": {"query": "$input.query", "hits": "fuse.hits"},
                    "config": {"rerankerId": "local-test"},
                },
                "context": {
                    "block": "context.build@1",
                    "inputs": {"evidence": "rank.hits"},
                    "config": {"contextId": "context-1", "maxTokens": 100},
                },
                "validate": {
                    "block": "answer.validate_grounding@1",
                    "inputs": {"response": "$input.answer", "context": "context.pack"},
                    "config": {"requireCitation": True, "onInsufficientEvidence": "abstain"},
                    "outputs": {
                        "candidate": "$output.candidate",
                        "validation": "$output.validation",
                    },
                },
            }
        },
    }
    policy_hit = _hit("policy-a", "chunk-a", 1, "policy", "Audit logs are required.")
    ticket_hit = _hit("ticket-a", "chunk-a", 1, "tickets", "Audit logs are required.")
    answer = {
        "answerId": "answer-1",
        "text": "Audit logs are required.",
        "claims": [
            {
                "claimId": "claim-1",
                "text": "Audit logs are required.",
                "citationIds": ["citation-1"],
            }
        ],
        "citations": [
            {
                "citationId": "citation-1",
                "claimId": "claim-1",
                "source": policy_hit["item"]["source"],  # type: ignore[index]
                "citedText": "Audit logs are required.",
            }
        ],
    }

    result = InProcessRuntime(registry).run(
        graph,
        {
            "query": {"original": "audit logs", "topK": 5},
            "sources": [
                {"sourceId": "policy", "hits": [policy_hit], "weight": 1.0},
                {"sourceId": "tickets", "hits": [ticket_hit], "weight": 0.5},
                {"sourceId": "offline", "error": "mock timeout"},
            ],
            "answer": answer,
        },
        run_id="run-rag-blocks-1",
    )

    assert result.status == "succeeded"
    assert result.outputs["validation"] == {
        "ok": True,
        "issues": [],
        "abstention": None,
        "repaired": False,
    }
    assert result.outputs["candidate"]["text"] == "Audit logs are required."
    assert result.outputs["candidate"]["citations"][0]["citationId"] == "citation-1"
    assert [record.payload["node"] for record in result.journal.records if record.kind == "node_succeeded"] == [
        "retrieve",
        "fuse",
        "rank",
        "context",
        "validate",
    ]


def test_retrieve_execute_plan_is_deterministic_and_enforces_minimum_sources() -> None:
    source = {"sourceId": "policy", "hits": [_hit("hit-1", "chunk-1", 1, "policy", "policy")]}

    first = retrieve_execute_plan(
        {"query": "policy", "sources": [source]},
        {"minimumSuccessfulSources": 1},
        {},
    )
    second = retrieve_execute_plan(
        {"query": "policy", "sources": [source]},
        {"minimumSuccessfulSources": 1},
        {},
    )

    assert first == second
    assert first["result"]["retrievalId"].startswith("federated:")
    assert first["result"]["sources"] == first["sources"]

    with pytest.raises(RuntimeError, match="requires 2 successful source"):
        retrieve_execute_plan(
            {"query": "policy", "sources": [source, {"sourceId": "offline", "error": "timeout"}]},
            {"minimumSuccessfulSources": 2},
            {},
        )


def test_grounding_block_returns_a_graph_candidate_for_abstention() -> None:
    output = answer_validate_grounding(
        {
            "response": "An unsupported answer.",
            "context": {
                "contextId": "empty",
                "hits": [],
                "tokenBudget": 100,
                "tokenCount": 0,
                "metadata": {},
            },
        },
        {"requireCitation": True, "onInsufficientEvidence": "abstain"},
        {},
    )

    assert output["validation"]["ok"] is False
    assert output["validation"]["issues"][0]["code"] == "grounding.insufficient_context"
    assert output["candidate"]["text"] == "I do not have enough retrieved context to answer."
    assert output["candidate"]["abstention"]["reason"] == "insufficient_context"

from __future__ import annotations

from pathlib import Path
import sys

from graphblocks.canonical import canonical_dumps
from graphblocks.runtime import InProcessRuntime, stdlib_registry


EXAMPLE_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(EXAMPLE_ROOT))

from runtime_contract import normalize_runtime_result


def source_ref(
    source_id: str,
    *,
    asset_id: str,
    document_id: str,
    element_id: str,
    chunk_id: str,
) -> dict[str, object]:
    return {
        "sourceId": source_id,
        "sourceKind": "document_chunk",
        "revision": "revision-1",
        "digest": None,
        "locator": {
            "assetId": asset_id,
            "revisionId": "revision-1",
            "documentId": document_id,
            "elementId": element_id,
            "chunkId": chunk_id,
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


ROTATION_SOURCE = source_ref(
    "source-chunk-rotation",
    asset_id="asset-handbook",
    document_id="security-handbook",
    element_id="paragraph-rotation",
    chunk_id="chunk-rotation",
)
TICKET_SOURCE = source_ref(
    "source-chunk-ticket",
    asset_id="asset-tickets",
    document_id="support-tickets",
    element_id="ticket-approvals",
    chunk_id="chunk-ticket",
)


def hit(
    hit_id: str,
    chunk_id: str,
    retriever: str,
    preview: str,
    source: dict[str, object],
) -> dict[str, object]:
    return {
        "hitId": hit_id,
        "item": {
            "itemId": chunk_id,
            "itemKind": "document_chunk",
            "source": source,
            "schemaRef": None,
            "payloadRef": None,
            "preview": [preview],
            "acl": None,
            "metadata": {"document_id": source["locator"]["documentId"]},  # type: ignore[index]
        },
        "rank": 1,
        "retriever": retriever,
        "rawScore": None,
        "normalizedScore": None,
        "scoreKind": None,
        "highlights": [],
        "metadata": {},
    }


ANSWER = {
    "answerId": "answer-key-rotation",
    "text": "Use the security console and obtain two approvals.",
    "claims": [
        {
            "claimId": "claim-console",
            "text": "Rotate through the security console.",
            "citationIds": ["citation-rotation"],
        },
        {
            "claimId": "claim-approvals",
            "text": "Require two approvers.",
            "citationIds": ["citation-ticket"],
        },
    ],
    "citations": [
        {
            "citationId": "citation-rotation",
            "claimId": "claim-console",
            "source": ROTATION_SOURCE,
            "citedText": "Rotate through the security console.",
        },
        {
            "citationId": "citation-ticket",
            "claimId": "claim-approvals",
            "source": TICKET_SOURCE,
            "citedText": "Require two approvers.",
        },
    ],
}


GRAPH = {
    "apiVersion": "graphblocks.ai/v1alpha3",
    "kind": "Graph",
    "metadata": {"name": "enterprise-rag-runtime-parity"},
    "spec": {
        "interface": {
            "inputs": {
                "query": "graphblocks.ai/SearchRequest@1",
                "sources": "graphblocks.ai/FederatedSources@1",
            },
            "outputs": {
                "candidate": "graphblocks.ai/Answer@1",
                "validation": "graphblocks.ai/GroundingValidation@1",
            },
        },
        "nodes": {
            "retrieve": {
                "block": "retrieve.execute_plan@1",
                "inputs": {"query": "$input.query", "sources": "$input.sources"},
                "config": {"minimumSuccessfulSources": 2, "topK": 5},
            },
            "fuse": {
                "block": "retrieve.fuse@1",
                "inputs": {"sources": "retrieve.sources"},
                "config": {"algorithm": "reciprocal_rank_fusion", "k": 60},
            },
            "rerank": {
                "block": "rank.documents@1",
                "inputs": {"query": "$input.query", "hits": "fuse.hits"},
                "config": {"rerankerId": "deterministic-lexical"},
            },
            "context": {
                "block": "context.build@1",
                "inputs": {"evidence": "rerank.hits"},
                "config": {
                    "contextId": "context-key-rotation",
                    "maxTokens": 1000,
                    "reserveOutputTokens": 100,
                },
            },
            "generate": {
                "block": "model.structured_generate@1",
                "inputs": {"context": "context.pack"},
                "config": {
                    "outputSchema": "graphblocks.ai/Answer@1",
                    "response": ANSWER,
                },
            },
            "validate": {
                "block": "answer.validate_grounding@1",
                "inputs": {
                    "response": "generate.response",
                    "context": "context.pack",
                },
                "config": {
                    "requireCitation": True,
                    "onInsufficientEvidence": "abstain",
                },
                "outputs": {
                    "candidate": "$output.candidate",
                    "validation": "$output.validation",
                },
            },
        },
    },
}

INPUTS = {
    "query": {"original": "production signing key rotation approvals", "topK": 5},
    "sources": [
        {
            "sourceId": "handbook",
            "weight": 1.0,
            "hits": [
                hit(
                    "hit-rotation",
                    "chunk-rotation",
                    "handbook",
                    "Rotate through the security console.",
                    ROTATION_SOURCE,
                )
            ],
        },
        {
            "sourceId": "tickets",
            "weight": 1.0,
            "hits": [
                hit(
                    "hit-ticket",
                    "chunk-ticket",
                    "tickets",
                    "Require two approvers.",
                    TICKET_SOURCE,
                )
            ],
        },
    ],
}


def execute() -> dict[str, object]:
    result = InProcessRuntime(stdlib_registry()).run(
        GRAPH,
        INPUTS,
        run_id="example-01-2-python",
    )
    payload = {
        "status": result.status,
        "outputs": result.outputs,
        "journal": [record.to_dict() for record in result.journal.records],
    }
    return normalize_runtime_result(payload, runtime="python-api", graph=GRAPH)


def main() -> int:
    print(canonical_dumps(execute()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

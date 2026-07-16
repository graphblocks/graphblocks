from __future__ import annotations

from pathlib import Path
import sys

from graphblocks.canonical import canonical_dumps
from graphblocks.runtime import InProcessRuntime, stdlib_registry
from graphblocks.stdlib_blocks import (
    ANSWER as ANSWER_TYPE,
    FEDERATED_SOURCES,
    GROUNDING_VALIDATION,
    SEARCH_REQUEST,
    AnswerValidateGrounding,
    ContextBuild,
    RankDocuments,
    RetrieveExecutePlan,
    RetrieveFuse,
    StructuredGenerate,
)
from graphblocks.typed import GraphBuilder


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


def build_graph() -> dict[str, object]:
    graph = GraphBuilder(
        "enterprise-rag-runtime-parity",
        api_version="graphblocks.ai/v1alpha3",
    )
    query = graph.input("query", SEARCH_REQUEST)
    sources = graph.input("sources", FEDERATED_SOURCES)
    candidate = graph.output("candidate", ANSWER_TYPE)
    validation = graph.output("validation", GROUNDING_VALIDATION)

    retrieve = graph.add(
        "retrieve",
        RetrieveExecutePlan(minimum_successful_sources=2, top_k=5).bind(
            query=query,
            sources=sources,
        ),
    )
    fuse = graph.add(
        "fuse",
        RetrieveFuse(algorithm="reciprocal_rank_fusion", k=60).bind(
            sources=retrieve.sources
        ),
    )
    rerank = graph.add(
        "rerank",
        RankDocuments(reranker_id="deterministic-lexical").bind(
            query=query,
            hits=fuse.hits,
        ),
    )
    context = graph.add(
        "context",
        ContextBuild(
            context_id="context-key-rotation",
            max_tokens=1000,
            reserve_output_tokens=100,
        ).bind(evidence=rerank.hits),
    )
    generate = graph.add(
        "generate",
        StructuredGenerate(output_schema=ANSWER_TYPE, response=ANSWER).bind(
            context=context.pack
        ),
    )
    grounded = graph.add(
        "validate",
        AnswerValidateGrounding(
            require_citation=True,
            on_insufficient_evidence="abstain",
        ).bind(response=generate.response, context=context.pack),
    )
    graph.publish(candidate, grounded.candidate)
    graph.publish(validation, grounded.validation)
    return graph.build()


GRAPH = build_graph()

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

from __future__ import annotations

from decimal import Decimal

import graphblocks
from graphblocks.documents import (
    chunk_document_by_lines,
    create_local_text_revision,
    parse_plain_text_document,
)
from graphblocks.rag import (
    Answer,
    Citation,
    Claim,
    ContextPack,
    InMemoryChunkRetriever,
    evaluate_rag_answer_metrics,
    validate_answer_citations,
)


def _single_hit_context() -> ContextPack:
    text = "Alpha policy requires audit logs.\nBeta policy requires approval.\n"
    asset, revision = create_local_text_revision(
        "file:///tmp/policy.txt",
        text,
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, text)
    chunks = chunk_document_by_lines(document, revision, max_elements=1)
    hits = InMemoryChunkRetriever(chunks, retriever_id="local-test").search("audit", top_k=1)
    return ContextPack(context_id="ctx-1", hits=hits)


def test_evaluate_rag_answer_metrics_reports_citation_precision() -> None:
    context = _single_hit_context()
    valid = Citation(
        citation_id="cite-valid",
        source=context.hits[0].item.source,
        cited_text="requires audit logs",
    )
    invalid = Citation(
        citation_id="cite-invalid",
        source=context.hits[0].item.source,
        cited_text="unrelated phrase",
    )
    answer = Answer(
        answer_id="answer-1",
        text="Alpha policy requires audit logs.",
        claims=[
            Claim(
                claim_id="claim-1",
                text="Alpha policy requires audit logs.",
                citation_ids=["cite-valid", "cite-invalid"],
            )
        ],
        citations=[valid, invalid],
    )
    validation = validate_answer_citations(answer, context)

    metrics = evaluate_rag_answer_metrics(answer, validation)
    exported_metrics = graphblocks.evaluate_rag_answer_metrics(answer, validation)
    by_name = {metric.name: metric for metric in metrics}

    assert [metric.name for metric in exported_metrics] == [metric.name for metric in metrics]
    assert by_name["citation_precision"].value == Decimal("0.5")
    assert by_name["citation_precision"].direction == "maximize"
    assert by_name["unsupported_claim_rate"].value == Decimal("0")
    assert by_name["unsupported_claim_rate"].direction == "minimize"


def test_evaluate_rag_answer_metrics_reports_unsupported_claim_rate() -> None:
    context = _single_hit_context()
    citation = Citation(
        citation_id="cite-1",
        source=context.hits[0].item.source,
        cited_text="requires audit logs",
    )
    answer = Answer(
        answer_id="answer-1",
        text="Beta policy requires approval.",
        claims=[
            Claim(
                claim_id="claim-1",
                text="Beta policy requires approval.",
                citation_ids=["cite-1"],
            )
        ],
        citations=[citation],
    )
    validation = validate_answer_citations(answer, context)

    by_name = {
        metric.name: metric for metric in evaluate_rag_answer_metrics(answer, validation)
    }

    assert by_name["citation_precision"].value == Decimal("0")
    assert by_name["unsupported_claim_rate"].value == Decimal("1")

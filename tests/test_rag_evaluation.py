from __future__ import annotations

from decimal import Decimal
import math

import graphblocks
from graphblocks.documents import (
    SourceRef,
    chunk_document_by_lines,
    create_local_text_revision,
    parse_plain_text_document,
)
from graphblocks.rag import (
    Abstention,
    Answer,
    AuthContext,
    Citation,
    CitationValidationResult,
    Claim,
    ContextPack,
    InMemoryChunkRetriever,
    KnowledgeItemRef,
    RetrievalResult,
    SearchHit,
    SearchRequest,
    evaluate_context_metrics,
    evaluate_rag_answer_metrics,
    evaluate_retrieval_metrics,
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


def _hit(item_id: str, rank: int, acl: dict[str, object] | None = None) -> SearchHit:
    source = SourceRef(source_id=item_id, source_kind="document_chunk")
    return SearchHit(
        hit_id=f"hit-{item_id}",
        item=KnowledgeItemRef(item_id, "document_chunk", source, acl=acl),
        rank=rank,
        retriever="local-test",
    )


def test_evaluate_retrieval_metrics_reports_recall_precision_and_mrr() -> None:
    retrieval = RetrievalResult(
        retrieval_id="retrieval-1",
        request=SearchRequest("policy", top_k=3),
        hits=[
            _hit("doc-a", 1),
            _hit("doc-b", 2, {"tenant_id": "acme", "groups": ["finance"]}),
            _hit("doc-c", 3),
        ],
        total_candidates=3,
    )
    auth = AuthContext(tenant_id="acme", principal_id="user-1", groups={"support"})

    metrics = evaluate_retrieval_metrics(retrieval, {"doc-a", "doc-c"}, k=3, auth=auth)
    exported_metrics = graphblocks.evaluate_retrieval_metrics(
        retrieval,
        {"doc-a", "doc-c"},
        k=3,
        auth=auth,
    )
    by_name = {metric.name: metric for metric in metrics}

    assert [metric.name for metric in exported_metrics] == [metric.name for metric in metrics]
    assert by_name["recall_at_k"].value == Decimal("1")
    assert by_name["precision_at_k"].value == Decimal(2) / Decimal(3)
    assert by_name["average_precision_at_k"].value == (
        Decimal(1) + Decimal(2) / Decimal(3)
    ) / Decimal(2)
    expected_ndcg = (1.0 + 1.0 / math.log2(4.0)) / (
        1.0 + 1.0 / math.log2(3.0)
    )
    assert by_name["ndcg_at_k"].value == Decimal(str(expected_ndcg))
    assert by_name["mrr"].value == Decimal("1")
    assert by_name["coverage_at_k"].value == Decimal("1")
    assert by_name["coverage_at_k"].direction == "maximize"
    assert by_name["acl_precision"].value == Decimal(2) / Decimal(3)
    assert by_name["acl_precision"].direction == "maximize"
    assert by_name["recall_at_k"].direction == "maximize"
    assert by_name["precision_at_k"].evaluator == {"k": 3}


def test_evaluate_retrieval_metrics_returns_no_data_without_relevant_items() -> None:
    retrieval = RetrievalResult(
        retrieval_id="retrieval-1",
        request=SearchRequest("policy", top_k=3),
        hits=[_hit("doc-a", 1)],
        total_candidates=1,
    )

    by_name = {
        metric.name: metric for metric in evaluate_retrieval_metrics(retrieval, set())
    }

    assert by_name["recall_at_k"].value is None
    assert by_name["precision_at_k"].value is None
    assert by_name["average_precision_at_k"].value is None
    assert by_name["ndcg_at_k"].value is None
    assert by_name["mrr"].value is None
    assert by_name["coverage_at_k"].value == Decimal(1) / Decimal(3)
    assert by_name["acl_precision"].value is None


def test_evaluate_context_metrics_reports_source_diversity_and_token_efficiency() -> None:
    context = ContextPack(
        context_id="ctx-1",
        hits=[
            SearchHit(
                hit_id="hit-a",
                item=_hit("doc-a", 1).item,
                rank=1,
                retriever="policy",
                normalized_score=0.9,
            ),
            SearchHit(
                hit_id="hit-b",
                item=_hit("doc-b", 2).item,
                rank=2,
                retriever="ticket",
                normalized_score=0.6,
            ),
            SearchHit(
                hit_id="hit-c",
                item=_hit("doc-c", 3).item,
                rank=3,
                retriever="policy",
                normalized_score=0.75,
            ),
        ],
        token_budget=8,
        token_count=6,
    )

    metrics = evaluate_context_metrics(context, {"doc-a", "doc-c"})
    exported_metrics = graphblocks.evaluate_context_metrics(context, {"doc-a", "doc-c"})
    by_name = {metric.name: metric for metric in metrics}

    assert [metric.name for metric in exported_metrics] == [metric.name for metric in metrics]
    assert by_name["source_diversity"].value == Decimal("2")
    assert by_name["source_diversity"].unit == "sources"
    assert by_name["source_diversity"].direction == "maximize"
    assert by_name["context_token_efficiency"].value == Decimal("0.75")
    assert by_name["context_token_efficiency"].direction == "maximize"
    assert by_name["context_precision"].value == Decimal(2) / Decimal(3)
    assert by_name["context_precision"].direction == "maximize"
    assert by_name["context_relevance"].value == Decimal("0.75")
    assert by_name["context_relevance"].direction == "maximize"


def test_evaluate_context_metrics_returns_no_data_without_token_budget() -> None:
    context = ContextPack(context_id="ctx-1", hits=[_hit("doc-a", 1)])

    by_name = {metric.name: metric for metric in evaluate_context_metrics(context)}

    assert by_name["source_diversity"].value == Decimal("1")
    assert by_name["context_token_efficiency"].value is None
    assert by_name["context_precision"].value is None
    assert by_name["context_relevance"].value is None
    assert by_name["freshness_satisfaction"].value is None


def test_evaluate_context_metrics_reports_freshness_satisfaction() -> None:
    context = ContextPack(
        context_id="ctx-1",
        hits=[_hit("doc-fresh", 1)],
        metadata={
            "minimum_source_modified_at": "2026-06-21T00:00:00Z",
            "drop_reasons": {
                "hit-stale": "freshness",
                "hit-budget": "token_budget",
            },
        },
    )

    by_name = {metric.name: metric for metric in evaluate_context_metrics(context)}

    assert by_name["freshness_satisfaction"].value == Decimal("0.5")
    assert by_name["freshness_satisfaction"].direction == "maximize"


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
        metadata={"answer_relevance": 0.8},
    )
    validation = validate_answer_citations(answer, context)

    metrics = evaluate_rag_answer_metrics(answer, validation)
    exported_metrics = graphblocks.evaluate_rag_answer_metrics(answer, validation)
    by_name = {metric.name: metric for metric in metrics}

    assert [metric.name for metric in exported_metrics] == [metric.name for metric in metrics]
    assert by_name["citation_precision"].value == Decimal("0.5")
    assert by_name["citation_precision"].direction == "maximize"
    assert by_name["citation_recall"].value == Decimal("1")
    assert by_name["citation_recall"].direction == "maximize"
    assert by_name["citation_source_accuracy"].value == Decimal("1")
    assert by_name["citation_source_accuracy"].direction == "maximize"
    assert by_name["answer_relevance"].value == Decimal("0.8")
    assert by_name["answer_relevance"].direction == "maximize"
    assert by_name["faithfulness"].value == Decimal("1")
    assert by_name["faithfulness"].direction == "maximize"
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
    assert by_name["citation_recall"].value == Decimal("0")
    assert by_name["citation_source_accuracy"].value == Decimal("1")
    assert by_name["answer_relevance"].value is None
    assert by_name["faithfulness"].value == Decimal("0")
    assert by_name["unsupported_claim_rate"].value == Decimal("1")


def test_evaluate_rag_answer_metrics_reports_abstention_precision_and_recall() -> None:
    abstained = Answer(
        answer_id="answer-1",
        text="I do not have enough context.",
        abstention=Abstention(
            reason="insufficient_context",
            user_message="I do not have enough context.",
        ),
        metadata={"expected_abstention": True},
    )

    abstained_metrics = {
        metric.name: metric
        for metric in evaluate_rag_answer_metrics(abstained, CitationValidationResult(ok=True))
    }

    assert abstained_metrics["abstention_precision"].value == Decimal("1")
    assert abstained_metrics["abstention_precision"].direction == "maximize"
    assert abstained_metrics["abstention_recall"].value == Decimal("1")
    assert abstained_metrics["abstention_recall"].direction == "maximize"

    missed = Answer(
        answer_id="answer-2",
        text="A direct answer.",
        metadata={"expected_abstention": True},
    )

    missed_metrics = {
        metric.name: metric
        for metric in evaluate_rag_answer_metrics(missed, CitationValidationResult(ok=True))
    }

    assert missed_metrics["abstention_precision"].value is None
    assert missed_metrics["abstention_recall"].value == Decimal("0")

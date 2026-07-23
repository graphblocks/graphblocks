from __future__ import annotations

import pytest

from graphblocks.canonical import canonical_hash
from graphblocks.documents import DocumentSpan, SourceRef
from graphblocks.rag import (
    ContextPack,
    KnowledgeItemRef,
    SearchHit,
    build_abstention_answer,
    build_answer_from_model_response,
)


def _context() -> ContextPack:
    source = SourceRef(
        source_id="chunk-1",
        source_kind="document_chunk",
        locator=DocumentSpan(
            asset_id="asset-1",
            revision_id="rev-1",
            document_id="doc-1",
            chunk_id="chunk-1",
        ),
    )
    return ContextPack(
        context_id="ctx-1",
        hits=[
            SearchHit(
                hit_id="hit-1",
                item=KnowledgeItemRef(
                    item_id="chunk-1",
                    item_kind="document_chunk",
                    source=source,
                    preview=["Alpha policy requires audit logs."],
                ),
                rank=1,
                retriever="local",
                highlights=[source],
            )
        ],
    )


def test_build_answer_from_model_response_preserves_structured_output_metadata() -> None:
    model_response = {
        "response_id": "response-1",
        "provider": "scripted",
        "model": "model-test",
        "finish_reason": "stop",
        "output_text": "Alpha policy requires audit logs.",
        "claims": [
            {
                "claim_id": "claim-1",
                "text": "Alpha policy requires audit logs.",
                "citation_ids": ["cite-1"],
            }
        ],
    }

    answer = build_answer_from_model_response("answer-1", model_response)

    assert answer.answer_id == "answer-1"
    assert answer.text == "Alpha policy requires audit logs."
    assert [claim.claim_id for claim in answer.claims] == ["claim-1"]
    assert answer.claims[0].citation_ids == ["cite-1"]
    assert answer.metadata["model_response_digest"] == canonical_hash(model_response)
    assert answer.metadata["provider_response_id"] == "response-1"
    assert answer.metadata["provider"] == "scripted"
    assert answer.metadata["model"] == "model-test"
    assert answer.metadata["finish_reason"] == "stop"


def test_build_answer_from_model_response_resolves_structured_citations_from_context() -> None:
    model_response = {
        "response_id": "response-1",
        "output_text": "Alpha policy requires audit logs.",
        "claims": [
            {
                "claim_id": "claim-1",
                "text": "Alpha policy requires audit logs.",
                "citation_ids": ["cite-1"],
            }
        ],
        "citations": [
            {
                "citation_id": "cite-1",
                "claim_id": "claim-1",
                "source_id": "chunk-1",
                "cited_text": "requires audit logs",
                "confidence": 0.91,
            }
        ],
    }

    answer = build_answer_from_model_response("answer-1", model_response, context=_context())

    assert [citation.citation_id for citation in answer.citations] == ["cite-1"]
    assert answer.citations[0].claim_id == "claim-1"
    assert answer.citations[0].source.source_id == "chunk-1"
    assert answer.citations[0].cited_text == "requires audit logs"
    assert answer.citations[0].confidence == 0.91


def test_build_answer_from_model_response_rejects_unknown_citation_source() -> None:
    try:
        build_answer_from_model_response(
            "answer-1",
            {
                "output_text": "Alpha policy requires audit logs.",
                "citations": [{"citation_id": "cite-1", "source_id": "missing"}],
            },
            context=_context(),
        )
    except ValueError as error:
        assert str(error) == "citation source 'missing' was not found in context"
    else:
        raise AssertionError("answer assembly should reject citations outside context")


def test_build_answer_from_model_response_requires_text() -> None:
    try:
        build_answer_from_model_response("answer-1", {"response_id": "response-1"})
    except ValueError as error:
        assert str(error) == "model_response must contain string output_text or text"
    else:
        raise AssertionError("answer assembly should require model output text")


@pytest.mark.parametrize(
    "model_response",
    (
        {
            "output_text": "answer",
            "claims": [
                {
                    "claim_id": "claim-1",
                    "text": "answer",
                    "citation_ids": ["cite-1", 1],
                }
            ],
        },
        {
            "output_text": "answer",
            "citations": [
                {
                    "citation_id": "cite-1",
                    "source_id": "chunk-1",
                    "confidence": True,
                }
            ],
        },
        {
            "output_text": "answer",
            "citations": [
                {
                    "citation_id": "cite-1",
                    "source_id": "chunk-1",
                    "claim_id": 1,
                }
            ],
        },
    ),
)
def test_build_answer_from_model_response_rejects_malformed_evidence_fields(
    model_response: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="model_response"):
        build_answer_from_model_response(
            "answer-1",
            model_response,
            context=_context(),
        )


def test_build_abstention_answer_sets_terminal_answer_and_diagnostics() -> None:
    answer = build_abstention_answer(
        "answer-1",
        "insufficient_context",
        "I do not have enough validated source support to answer.",
        diagnostics={"issue_codes": ["grounding.insufficient_context"]},
    )

    assert answer.answer_id == "answer-1"
    assert answer.text == "I do not have enough validated source support to answer."
    assert answer.claims == []
    assert answer.citations == []
    assert answer.abstention is not None
    assert answer.abstention.reason == "insufficient_context"
    assert answer.abstention.diagnostics == {
        "issue_codes": ["grounding.insufficient_context"]
    }
    assert answer.metadata["answer_kind"] == "abstention"

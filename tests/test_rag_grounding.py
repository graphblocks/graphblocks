from __future__ import annotations

from graphblocks.documents import create_local_text_revision, parse_plain_text_document, chunk_document_by_lines
from graphblocks.rag import (
    Answer,
    Citation,
    Claim,
    ContextPack,
    InMemoryChunkRetriever,
    validate_answer_grounding,
)


def _grounded_context() -> ContextPack:
    asset, revision = create_local_text_revision(
        "file:///tmp/grounding.txt",
        "Alpha policy requires audit logs.\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "Alpha policy requires audit logs.\n")
    chunks = chunk_document_by_lines(document, revision, max_elements=1)
    hits = InMemoryChunkRetriever(chunks, retriever_id="local-test").search("audit", top_k=1)
    return ContextPack(context_id="ctx-1", hits=hits)


def test_validate_answer_grounding_accepts_cited_current_context_source() -> None:
    context = _grounded_context()
    citation = Citation(
        citation_id="cite-1",
        source=context.hits[0].item.source,
        cited_text="requires audit logs",
    )
    answer = Answer(
        answer_id="answer-1",
        text="Alpha policy requires audit logs.",
        claims=[Claim(claim_id="claim-1", text="Alpha policy requires audit logs.", citation_ids=["cite-1"])],
        citations=[citation],
    )

    result = validate_answer_grounding(answer, context)

    assert result.ok is True
    assert result.issues == []
    assert result.abstention is None


def test_validate_answer_grounding_abstains_when_context_is_empty() -> None:
    answer = Answer(
        answer_id="answer-1",
        text="Alpha policy requires audit logs.",
        claims=[Claim(claim_id="claim-1", text="Alpha policy requires audit logs.", citation_ids=["cite-1"])],
    )

    result = validate_answer_grounding(answer, ContextPack(context_id="ctx-empty", hits=[]))

    assert result.ok is False
    assert [issue.code for issue in result.issues] == ["grounding.insufficient_context"]
    assert result.abstention is not None
    assert result.abstention.reason == "insufficient_context"
    assert result.abstention.diagnostics == {"issue_codes": ["grounding.insufficient_context"]}


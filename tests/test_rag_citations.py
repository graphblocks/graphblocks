from __future__ import annotations

from dataclasses import replace

from graphblocks.documents import create_local_text_revision, parse_plain_text_document, chunk_document_by_lines
from graphblocks.rag import (
    Answer,
    AuthContext,
    Citation,
    Claim,
    ContextPack,
    InMemoryChunkRetriever,
    resolve_citation_source_trace,
    validate_answer_citation_authorization,
    validate_answer_citations,
)


def _single_hit_context() -> ContextPack:
    asset, revision = create_local_text_revision(
        "file:///tmp/policy.txt",
        "Alpha policy requires audit logs.\nBeta policy requires approval.\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "Alpha policy requires audit logs.\nBeta policy requires approval.\n")
    chunks = chunk_document_by_lines(document, revision, max_elements=1)
    hits = InMemoryChunkRetriever(chunks, retriever_id="local-test").search("audit", top_k=1)
    return ContextPack(context_id="ctx-1", hits=hits)


def test_validate_answer_citations_accepts_current_context_source() -> None:
    context = _single_hit_context()
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

    result = validate_answer_citations(answer, context)

    assert result.ok is True
    assert result.issues == []
    assert result.abstention is None


def test_validate_answer_citations_rejects_wrong_locator_on_matching_source() -> None:
    context = _single_hit_context()
    source = context.hits[0].item.source
    assert source.locator is not None
    wrong_source = replace(
        source,
        locator=replace(source.locator, chunk_id="wrong-chunk"),
    )
    citation = Citation(
        citation_id="cite-1",
        source=wrong_source,
        cited_text="requires audit logs",
    )
    answer = Answer(
        answer_id="answer-1",
        text="Alpha policy requires audit logs.",
        claims=[Claim(claim_id="claim-1", text="Alpha policy requires audit logs.", citation_ids=["cite-1"])],
        citations=[citation],
    )

    result = validate_answer_citations(answer, context)

    assert result.ok is False
    assert [issue.code for issue in result.issues] == ["citation.source_not_in_context"]
    assert result.issues[0].citation_id == "cite-1"


def test_resolve_citation_source_trace_links_citation_to_context_hit_and_document_span() -> None:
    context = _single_hit_context()
    hit = replace(
        context.hits[0],
        item=replace(context.hits[0].item, acl={"tenant_id": "acme", "groups": ["support"]}),
    )
    context = replace(context, hits=[hit])
    citation = Citation(
        citation_id="cite-1",
        source=hit.item.source,
        cited_text="requires audit logs",
    )
    answer = Answer(
        answer_id="answer-1",
        text="Alpha policy requires audit logs.",
        claims=[Claim(claim_id="claim-1", text="Alpha policy requires audit logs.", citation_ids=["cite-1"])],
        citations=[citation],
    )

    trace = resolve_citation_source_trace(answer, context, "cite-1")

    assert trace.citation_id == "cite-1"
    assert trace.claim_id == "claim-1"
    assert trace.context_id == "ctx-1"
    assert trace.hit_id == hit.hit_id
    assert trace.retriever == "local-test"
    assert trace.item_id == hit.item.item_id
    assert trace.item_kind == "document_chunk"
    assert trace.acl == {"tenant_id": "acme", "groups": ["support"]}
    assert trace.element_ids == hit.item.metadata["element_ids"]
    assert trace.locator is not None
    assert trace.locator.asset_id == hit.item.source.locator.asset_id
    assert trace.locator.revision_id == hit.item.source.locator.revision_id
    assert trace.locator.document_id == hit.item.source.locator.document_id
    assert trace.locator.chunk_id == hit.item.item_id


def test_resolve_citation_source_trace_rejects_wrong_locator_on_matching_source() -> None:
    context = _single_hit_context()
    source = context.hits[0].item.source
    assert source.locator is not None
    wrong_source = replace(
        source,
        locator=replace(source.locator, chunk_id="wrong-chunk"),
    )
    citation = Citation(
        citation_id="cite-1",
        source=wrong_source,
        cited_text="requires audit logs",
    )
    answer = Answer(
        answer_id="answer-1",
        text="Alpha policy requires audit logs.",
        claims=[Claim(claim_id="claim-1", text="Alpha policy requires audit logs.", citation_ids=["cite-1"])],
        citations=[citation],
    )

    try:
        resolve_citation_source_trace(answer, context, "cite-1")
    except ValueError as error:
        assert str(error) == "citation 'cite-1' does not point to the current context"
    else:
        raise AssertionError("wrong citation locator should not resolve to the context hit")


def test_validate_answer_citations_rejects_uncited_claim_when_required() -> None:
    context = _single_hit_context()
    answer = Answer(
        answer_id="answer-1",
        text="Alpha policy requires audit logs.",
        claims=[Claim(claim_id="claim-1", text="Alpha policy requires audit logs.")],
    )

    result = validate_answer_citations(answer, context)

    assert result.ok is False
    assert [issue.code for issue in result.issues] == ["claim.missing_citation"]
    assert result.issues[0].claim_id == "claim-1"


def test_validate_answer_citations_rejects_unknown_citation_id() -> None:
    context = _single_hit_context()
    answer = Answer(
        answer_id="answer-1",
        text="Alpha policy requires audit logs.",
        claims=[Claim(claim_id="claim-1", text="Alpha policy requires audit logs.", citation_ids=["missing"])],
    )

    result = validate_answer_citations(answer, context)

    assert result.ok is False
    assert [issue.code for issue in result.issues] == ["citation_id.missing"]
    assert result.issues[0].citation_id == "missing"


def test_validate_answer_citations_rejects_source_outside_current_context() -> None:
    context = _single_hit_context()
    foreign_context = _single_hit_context()
    foreign_source = foreign_context.hits[0].item.source
    foreign_source = type(foreign_source)(
        source_id="foreign-chunk",
        source_kind=foreign_source.source_kind,
        revision=foreign_source.revision,
        digest=foreign_source.digest,
        locator=foreign_source.locator,
    )
    citation = Citation(citation_id="cite-1", source=foreign_source, cited_text="requires audit logs")
    answer = Answer(
        answer_id="answer-1",
        text="Alpha policy requires audit logs.",
        claims=[Claim(claim_id="claim-1", text="Alpha policy requires audit logs.", citation_ids=["cite-1"])],
        citations=[citation],
    )

    result = validate_answer_citations(answer, context)

    assert result.ok is False
    assert [issue.code for issue in result.issues] == ["citation.source_not_in_context"]
    assert result.issues[0].citation_id == "cite-1"


def test_validate_answer_citations_rejects_claim_unsupported_by_cited_source() -> None:
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

    result = validate_answer_citations(answer, context)

    assert result.ok is False
    assert [issue.code for issue in result.issues] == [
        "claim.unsupported_by_citation"
    ]
    assert result.issues[0].citation_id == "cite-1"
    assert result.issues[0].claim_id == "claim-1"


def test_validate_answer_citations_can_abstain_on_invalid_citation() -> None:
    context = _single_hit_context()
    citation = Citation(
        citation_id="cite-1",
        source=context.hits[0].item.source,
        cited_text="unrelated phrase",
    )
    answer = Answer(
        answer_id="answer-1",
        text="Alpha policy requires audit logs.",
        claims=[Claim(claim_id="claim-1", text="Alpha policy requires audit logs.", citation_ids=["cite-1"])],
        citations=[citation],
    )

    result = validate_answer_citations(answer, context, failure_policy="abstain")

    assert result.ok is False
    assert [issue.code for issue in result.issues] == ["citation.text_mismatch"]
    assert result.abstention is not None
    assert result.abstention.reason == "citation_validation_failed"


def test_validate_answer_citations_can_remove_invalid_citations() -> None:
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

    result = validate_answer_citations(answer, context, failure_policy="remove_invalid")

    assert result.ok is True
    assert [issue.code for issue in result.issues] == ["citation.text_mismatch"]
    assert result.repaired_answer is not None
    assert [citation.citation_id for citation in result.repaired_answer.citations] == [
        "cite-valid"
    ]
    assert result.repaired_answer.claims[0].citation_ids == ["cite-valid"]
    assert result.abstention is None


def test_validate_answer_citations_can_repair_when_valid_support_remains() -> None:
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

    result = validate_answer_citations(answer, context, failure_policy="repair")

    assert result.ok is True
    assert [issue.code for issue in result.issues] == ["citation.text_mismatch"]
    assert result.repaired_answer is not None
    assert [citation.citation_id for citation in result.repaired_answer.citations] == [
        "cite-valid"
    ]
    assert result.repaired_answer.claims[0].citation_ids == ["cite-valid"]


def test_validate_answer_citations_repair_fails_when_claim_loses_support() -> None:
    context = _single_hit_context()
    citation = Citation(
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
                citation_ids=["cite-invalid"],
            )
        ],
        citations=[citation],
    )

    result = validate_answer_citations(answer, context, failure_policy="repair")

    assert result.ok is False
    assert [issue.code for issue in result.issues] == [
        "citation.text_mismatch",
        "claim.missing_citation",
    ]
    assert result.repaired_answer is not None
    assert result.repaired_answer.citations == []
    assert result.repaired_answer.claims[0].citation_ids == []


def test_validate_answer_citation_authorization_rejects_unauthorized_source() -> None:
    context = _single_hit_context()
    hit = replace(
        context.hits[0],
        item=replace(context.hits[0].item, acl={"tenant_id": "acme", "principals": ["user-2"]}),
    )
    context = replace(context, hits=[hit])
    citation = Citation(
        citation_id="cite-1",
        source=hit.item.source,
        cited_text="requires audit logs",
    )
    answer = Answer(
        answer_id="answer-1",
        text="Alpha policy requires audit logs.",
        claims=[Claim(claim_id="claim-1", text="Alpha policy requires audit logs.", citation_ids=["cite-1"])],
        citations=[citation],
    )

    result = validate_answer_citation_authorization(
        answer,
        context,
        AuthContext(tenant_id="acme", principal_id="user-1"),
    )

    assert result.ok is False
    assert [issue.code for issue in result.issues] == ["citation.source_not_authorized"]
    assert result.issues[0].citation_id == "cite-1"

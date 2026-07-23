from __future__ import annotations

from dataclasses import replace

import pytest

from graphblocks.documents import create_local_text_revision, parse_plain_text_document, chunk_document_by_lines
from graphblocks.evaluation import ResultBundle
from graphblocks.rag import (
    Answer,
    AuthContext,
    Abstention,
    Citation,
    CitationSourceTrace,
    CitationValidationIssue,
    CitationValidationResult,
    Claim,
    ContextPack,
    InMemoryChunkRetriever,
    QueryPlan,
    RagResultBundle,
    RagResultPayload,
    RetrievalResult,
    SearchRequest,
    build_context_pack,
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


def test_validate_answer_citations_warns_when_source_has_limited_precision() -> None:
    context = _single_hit_context()
    source = replace(context.hits[0].item.source, locator=None)
    hit = replace(
        context.hits[0],
        item=replace(context.hits[0].item, source=source),
        highlights=[],
    )
    context = replace(context, hits=[hit])
    citation = Citation(
        citation_id="cite-1",
        source=source,
        cited_text="requires audit logs",
    )
    answer = Answer(
        answer_id="answer-1",
        text="Alpha policy requires audit logs.",
        claims=[
            Claim(
                claim_id="claim-1",
                text="Alpha policy requires audit logs.",
                citation_ids=["cite-1"],
            )
        ],
        citations=[citation],
    )

    result = validate_answer_citations(answer, context)

    assert result.ok is True
    assert [issue.code for issue in result.issues] == ["citation.precision_limited"]
    assert result.issues[0].severity == "warning"
    assert result.issues[0].citation_id == "cite-1"
    assert result.repaired_answer is None


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
        rank=7,
        raw_score=12.5,
        normalized_score=0.72,
        score_kind="bm25",
        metadata={"source_id": "local-index", "section_id": "policy-section"},
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
    assert trace.rank == 7
    assert trace.raw_score == 12.5
    assert trace.normalized_score == 0.72
    assert trace.score_kind == "bm25"
    assert trace.hit_metadata == {"source_id": "local-index", "section_id": "policy-section"}
    assert trace.acl == {"tenant_id": "acme", "groups": ["support"]}
    assert trace.element_ids == hit.item.metadata["element_ids"]
    assert trace.locator is not None
    assert trace.locator.asset_id == hit.item.source.locator.asset_id
    assert trace.locator.revision_id == hit.item.source.locator.revision_id
    assert trace.locator.document_id == hit.item.source.locator.document_id
    assert trace.locator.chunk_id == hit.item.item_id


@pytest.mark.parametrize(
    ("factory", "expected_error"),
    (
        (
            lambda: Citation(" cite-1", _single_hit_context().hits[0].item.source),
            "citation citation_id must not contain surrounding whitespace",
        ),
        (
            lambda: Citation("cite-1", _single_hit_context().hits[0].item.source, claim_id=" claim-1"),
            "citation claim_id must not contain surrounding whitespace",
        ),
        (
            lambda: Citation("cite-1", _single_hit_context().hits[0].item.source, metadata={" source": "kb"}),
            "citation metadata keys must not contain surrounding whitespace",
        ),
        (
            lambda: Claim(" claim-1", "Alpha policy", citation_ids=["cite-1"]),
            "claim claim_id must not contain surrounding whitespace",
        ),
        (
            lambda: Claim("claim-1", "Alpha policy", citation_ids=[" cite-1"]),
            "claim citation_ids item must not contain surrounding whitespace",
        ),
        (
            lambda: Abstention(" no_evidence", "I do not have enough evidence."),
            "abstention reason must not contain surrounding whitespace",
        ),
        (
            lambda: Answer(" answer-1", "Alpha policy", metadata={" source": "kb"}),
            "answer answer_id must not contain surrounding whitespace",
        ),
        (
            lambda: CitationValidationIssue(" citation.missing", "Citation is missing."),
            "citation validation issue code must not contain surrounding whitespace",
        ),
        (
            lambda: CitationValidationIssue("citation.missing", "Citation is missing.", citation_id=" cite-1"),
            "citation validation issue citation_id must not contain surrounding whitespace",
        ),
        (
            lambda: CitationSourceTrace(
                citation_id=" cite-1",
                claim_id=None,
                context_id="ctx-1",
                hit_id="hit-1",
                retriever="local-test",
                item_id="chunk-1",
                item_kind="document_chunk",
                source=_single_hit_context().hits[0].item.source,
                locator=None,
            ),
            "citation source trace citation_id must not contain surrounding whitespace",
        ),
        (
            lambda: CitationSourceTrace(
                citation_id="cite-1",
                claim_id="claim-1",
                context_id="ctx-1",
                hit_id="hit-1",
                retriever="local-test",
                item_id="chunk-1",
                item_kind="document_chunk",
                source=_single_hit_context().hits[0].item.source,
                locator=None,
                element_ids=[" el-1"],
            ),
            "citation source trace element_ids item must not contain surrounding whitespace",
        ),
    ),
)
def test_rag_citation_records_reject_whitespace_wrapped_identities(
    factory: object,
    expected_error: str,
) -> None:
    with pytest.raises(ValueError, match=expected_error):
        factory()


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


def test_context_pack_freshness_rejects_non_rfc3339_timestamps() -> None:
    context = _single_hit_context()
    valid_hit = replace(
        context.hits[0],
        metadata={**context.hits[0].metadata, "source_modified_at": "2026-06-22T00:01:00Z"},
    )
    hit = replace(
        context.hits[0],
        metadata={**context.hits[0].metadata, "source_modified_at": "2026-06-22 00:01:00Z"},
    )

    with pytest.raises(ValueError, match="minimum_source_modified_at must be an ISO datetime"):
        build_context_pack(
            "ctx-fresh",
            [valid_hit],
            token_budget=32,
            minimum_source_modified_at="2026-06-22 00:00:00Z",
        )

    filtered = build_context_pack(
        "ctx-fresh",
        [hit],
        token_budget=32,
        minimum_source_modified_at="2026-06-22T00:00:00Z",
    )

    assert filtered.hits == []
    assert filtered.metadata["drop_reasons"] == {hit.hit_id: "freshness"}


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


def test_answer_citation_and_validation_records_validate_wire_shape() -> None:
    context = _single_hit_context()
    source = context.hits[0].item.source
    citation = Citation(citation_id="cite-1", source=source, cited_text="requires audit logs")
    claim = Claim(claim_id="claim-1", text="Alpha policy requires audit logs.", citation_ids=["cite-1"])
    answer = Answer(answer_id="answer-1", text="Alpha policy requires audit logs.", claims=[claim], citations=[citation])

    with pytest.raises(ValueError, match="citation citation_id must not be empty"):
        Citation(citation_id=" ", source=source)
    with pytest.raises(ValueError, match="citation source must be a SourceRef"):
        Citation(citation_id="cite-1", source=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="citation confidence must be between 0 and 1"):
        Citation(citation_id="cite-1", source=source, confidence=1.1)
    with pytest.raises(ValueError, match="citation cited_text must not be empty"):
        Citation(citation_id="cite-1", source=source, cited_text=" ")

    with pytest.raises(ValueError, match="claim claim_id must not be empty"):
        Claim(claim_id="", text="claim")
    with pytest.raises(ValueError, match="claim text must be a string"):
        Claim(claim_id="claim-1", text=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="claim citation_ids must be a list of strings"):
        Claim(claim_id="claim-1", text="claim", citation_ids="cite-1")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="abstention reason must not be empty"):
        Abstention(reason="", user_message="Cannot answer.")
    with pytest.raises(ValueError, match="abstention user_message must not be empty"):
        Abstention(reason="insufficient_context", user_message=" ")
    with pytest.raises(ValueError, match="abstention diagnostics metadata keys must be strings"):
        Abstention(reason="insufficient_context", user_message="Cannot answer.", diagnostics={object(): "value"})  # type: ignore[dict-item]

    with pytest.raises(ValueError, match="answer answer_id must not be empty"):
        Answer(answer_id=" ", text="answer")
    with pytest.raises(ValueError, match="answer claims must be a list of Claim records"):
        Answer(answer_id="answer-1", text="answer", claims=[object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="answer citations must be a list of Citation records"):
        Answer(answer_id="answer-1", text="answer", citations=[object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="answer abstention must be an Abstention"):
        Answer(answer_id="answer-1", text="answer", abstention=object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="citation validation issue code must not be empty"):
        CitationValidationIssue(code="", message="invalid")
    with pytest.raises(ValueError, match="citation validation issue severity must be warning or error"):
        CitationValidationIssue(code="citation.invalid", message="invalid", severity="fatal")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="citation validation result ok must be a boolean"):
        CitationValidationResult(ok="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="citation validation result issues must be a list of CitationValidationIssue records"):
        CitationValidationResult(ok=False, issues=[object()])  # type: ignore[list-item]

    trace = resolve_citation_source_trace(answer, context, "cite-1")
    assert trace.citation_id == "cite-1"
    with pytest.raises(ValueError, match="citation source trace hit_id must not be empty"):
        CitationSourceTrace(
            citation_id="cite-1",
            claim_id="claim-1",
            context_id="ctx-1",
            hit_id="",
            retriever="local-test",
            item_id="chunk-1",
            item_kind="document_chunk",
            rank=1,
            source=source,
            locator=source.locator,
        )


def test_rag_result_payload_and_bundle_validate_nested_records() -> None:
    context = _single_hit_context()
    answer = Answer(answer_id="answer-1", text="Alpha policy requires audit logs.")
    retrieval = RetrievalResult(
        retrieval_id="retrieval-1",
        request=SearchRequest(query_text="audit"),
        hits=list(context.hits),
    )
    payload = RagResultPayload(
        query_plan=QueryPlan(original="audit policy", rewritten=["audit policy"]),
        retrievals=[retrieval],
        context=context,
        model_response={"response_id": "response-1"},
        answer=answer,
    )
    bundle = RagResultBundle(
        base=ResultBundle(bundle_id="bundle-1", run_id="run-1", release_id="release-1", inputs=[], outputs=[]),
        payload=payload,
    )

    assert bundle.profile == "rag"

    with pytest.raises(ValueError, match="query plan rewritten must be a list of strings"):
        QueryPlan(original="audit", rewritten="audit")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="rag result payload query_plan must be a QueryPlan"):
        RagResultPayload(query_plan=object(), retrievals=[retrieval], context=context, model_response={}, answer=answer)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="rag result payload retrievals must be a list of RetrievalResult records"):
        RagResultPayload(query_plan=payload.query_plan, retrievals=[object()], context=context, model_response={}, answer=answer)  # type: ignore[list-item]
    with pytest.raises(ValueError, match="retrieval_id values must be unique"):
        RagResultPayload(
            query_plan=payload.query_plan,
            retrievals=[retrieval, retrieval],
            context=context,
            model_response={},
            answer=answer,
        )
    with pytest.raises(ValueError, match="rag result payload model_response must be a mapping"):
        RagResultPayload(query_plan=payload.query_plan, retrievals=[retrieval], context=context, model_response=object(), answer=answer)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="rag result bundle profile must be rag"):
        RagResultBundle(base=bundle.base, payload=payload, profile="plain")  # type: ignore[arg-type]

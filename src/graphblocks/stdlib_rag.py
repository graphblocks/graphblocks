from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .canonical import canonical_hash
from .documents import DocumentSpan, SourceRef
from .rag import (
    Abstention,
    Answer,
    Citation,
    Claim,
    ContextPack,
    FederatedRetrievalSource,
    KnowledgeItemRef,
    RetrievalResult,
    SearchHit,
    SearchRequest,
    build_context_pack,
    federated_retrieve,
    fuse_search_hits,
    rerank_search_hits,
    validate_answer_grounding,
)

RagBlockCallable = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    return dict(value)


def _sequence(value: object, label: str) -> list[Any]:
    if isinstance(value, str | bytes | bytearray) or isinstance(value, Mapping):
        raise TypeError(f"{label} must be a sequence")
    if not isinstance(value, Sequence):
        raise TypeError(f"{label} must be a sequence")
    return list(value)


def _value(record: Mapping[str, Any], camel_key: str, snake_key: str, default: Any = None) -> Any:
    if camel_key in record:
        return record[camel_key]
    return record.get(snake_key, default)


def _document_span_from_wire(value: object) -> DocumentSpan:
    record = _mapping(value, "document span")
    return DocumentSpan(
        asset_id=_value(record, "assetId", "asset_id"),
        revision_id=_value(record, "revisionId", "revision_id"),
        document_id=_value(record, "documentId", "document_id"),
        element_id=_value(record, "elementId", "element_id"),
        chunk_id=_value(record, "chunkId", "chunk_id"),
        page=record.get("page"),
        bbox=record.get("bbox"),
        char_start=_value(record, "charStart", "char_start"),
        char_end=_value(record, "charEnd", "char_end"),
        sheet=record.get("sheet"),
        cell_range=_value(record, "cellRange", "cell_range"),
        slide=record.get("slide"),
    )


def _document_span_to_wire(span: DocumentSpan) -> dict[str, object]:
    return {
        "assetId": span.asset_id,
        "revisionId": span.revision_id,
        "documentId": span.document_id,
        "elementId": span.element_id,
        "chunkId": span.chunk_id,
        "page": span.page,
        "bbox": None if span.bbox is None else dict(span.bbox),
        "charStart": span.char_start,
        "charEnd": span.char_end,
        "sheet": span.sheet,
        "cellRange": span.cell_range,
        "slide": span.slide,
    }


def _source_ref_from_wire(value: object) -> SourceRef:
    record = _mapping(value, "source reference")
    locator = record.get("locator")
    return SourceRef(
        source_id=_value(record, "sourceId", "source_id"),
        source_kind=_value(record, "sourceKind", "source_kind"),
        revision=record.get("revision"),
        digest=record.get("digest"),
        locator=None if locator is None else _document_span_from_wire(locator),
        observed_at=_value(record, "observedAt", "observed_at"),
        relevant_as_of=_value(record, "relevantAsOf", "relevant_as_of"),
        trust=record.get("trust", "unknown"),
        access_policy=_value(record, "accessPolicy", "access_policy"),
        metadata=dict(record.get("metadata", {})),
    )


def _source_ref_to_wire(source: SourceRef) -> dict[str, object]:
    return {
        "sourceId": source.source_id,
        "sourceKind": source.source_kind,
        "revision": source.revision,
        "digest": source.digest,
        "locator": None if source.locator is None else _document_span_to_wire(source.locator),
        "observedAt": source.observed_at,
        "relevantAsOf": source.relevant_as_of,
        "trust": source.trust,
        "accessPolicy": None if source.access_policy is None else dict(source.access_policy),
        "metadata": dict(source.metadata),
    }


def _hit_from_wire(value: object) -> SearchHit:
    record = _mapping(value, "search hit")
    item_record = _mapping(record.get("item"), "search hit item")
    return SearchHit(
        hit_id=_value(record, "hitId", "hit_id"),
        item=KnowledgeItemRef(
            item_id=_value(item_record, "itemId", "item_id"),
            item_kind=_value(item_record, "itemKind", "item_kind"),
            source=_source_ref_from_wire(item_record.get("source")),
            schema_ref=_value(item_record, "schemaRef", "schema_ref"),
            payload_ref=_value(item_record, "payloadRef", "payload_ref"),
            preview=_sequence(item_record.get("preview", []), "search hit item preview"),
            acl=item_record.get("acl"),
            metadata=dict(item_record.get("metadata", {})),
        ),
        rank=record.get("rank"),
        retriever=record.get("retriever"),
        raw_score=_value(record, "rawScore", "raw_score"),
        normalized_score=_value(record, "normalizedScore", "normalized_score"),
        score_kind=_value(record, "scoreKind", "score_kind"),
        highlights=[
            _source_ref_from_wire(item)
            for item in _sequence(record.get("highlights", []), "search hit highlights")
        ],
        metadata=dict(record.get("metadata", {})),
    )


def _hit_to_wire(hit: SearchHit) -> dict[str, object]:
    return {
        "hitId": hit.hit_id,
        "item": {
            "itemId": hit.item.item_id,
            "itemKind": hit.item.item_kind,
            "source": _source_ref_to_wire(hit.item.source),
            "schemaRef": hit.item.schema_ref,
            "payloadRef": hit.item.payload_ref,
            "preview": list(hit.item.preview),
            "acl": None if hit.item.acl is None else dict(hit.item.acl),
            "metadata": dict(hit.item.metadata),
        },
        "rank": hit.rank,
        "retriever": hit.retriever,
        "rawScore": hit.raw_score,
        "normalizedScore": hit.normalized_score,
        "scoreKind": hit.score_kind,
        "highlights": [_source_ref_to_wire(source) for source in hit.highlights],
        "metadata": dict(hit.metadata),
    }


def _request_from_wire(value: object) -> SearchRequest:
    if isinstance(value, str):
        return SearchRequest(value)
    record = _mapping(value, "search request")
    query_text = _value(record, "queryText", "query_text")
    if query_text is None:
        query_text = record.get("original", record.get("text"))
    return SearchRequest(
        query_text=query_text,
        top_k=_value(record, "topK", "top_k", 10),
        filters=dict(record.get("filters", {})),
        metadata=dict(record.get("metadata", {})),
    )


def _request_to_wire(request: SearchRequest) -> dict[str, object]:
    return {
        "queryText": request.query_text,
        "topK": request.top_k,
        "filters": dict(request.filters),
        "metadata": dict(request.metadata),
    }


def _retrieval_from_wire(value: object, fallback_request: SearchRequest) -> RetrievalResult:
    record = _mapping(value, "retrieval result")
    request_value = record.get("request")
    hits = [_hit_from_wire(item) for item in _sequence(record.get("hits", []), "retrieval result hits")]
    retrieval_id = _value(record, "retrievalId", "retrieval_id")
    if retrieval_id is None:
        retrieval_id = "injected:" + canonical_hash(
            {"request": _request_to_wire(fallback_request), "hits": [_hit_to_wire(hit) for hit in hits]}
        )
    return RetrievalResult(
        retrieval_id=retrieval_id,
        request=fallback_request if request_value is None else _request_from_wire(request_value),
        hits=hits,
        total_candidates=_value(record, "totalCandidates", "total_candidates"),
        latency_ms=_value(record, "latencyMs", "latency_ms"),
        warnings=_sequence(record.get("warnings", []), "retrieval result warnings"),
        metadata=dict(record.get("metadata", {})),
    )


def _retrieval_to_wire(result: RetrievalResult) -> dict[str, object]:
    return {
        "retrievalId": result.retrieval_id,
        "request": _request_to_wire(result.request),
        "hits": [_hit_to_wire(hit) for hit in result.hits],
        "totalCandidates": result.total_candidates,
        "latencyMs": result.latency_ms,
        "warnings": list(result.warnings),
        "metadata": dict(result.metadata),
    }


def _context_from_wire(value: object) -> ContextPack:
    record = _mapping(value, "context pack")
    return ContextPack(
        context_id=_value(record, "contextId", "context_id"),
        hits=[_hit_from_wire(item) for item in _sequence(record.get("hits", []), "context pack hits")],
        token_budget=_value(record, "tokenBudget", "token_budget"),
        token_count=_value(record, "tokenCount", "token_count"),
        metadata=dict(record.get("metadata", {})),
    )


def _context_to_wire(context: ContextPack) -> dict[str, object]:
    return {
        "contextId": context.context_id,
        "hits": [_hit_to_wire(hit) for hit in context.hits],
        "tokenBudget": context.token_budget,
        "tokenCount": context.token_count,
        "metadata": dict(context.metadata),
    }


def _abstention_from_wire(value: object) -> Abstention:
    record = _mapping(value, "answer abstention")
    return Abstention(
        reason=record.get("reason"),
        user_message=_value(record, "userMessage", "user_message"),
        diagnostics=dict(record.get("diagnostics", {})),
    )


def _abstention_to_wire(abstention: Abstention) -> dict[str, object]:
    return {
        "reason": abstention.reason,
        "userMessage": abstention.user_message,
        "diagnostics": dict(abstention.diagnostics),
    }


def _answer_from_wire(value: object) -> Answer:
    if isinstance(value, str):
        return Answer(
            answer_id="answer:" + canonical_hash({"text": value}),
            text=value,
        )
    record = _mapping(value, "answer")
    nested = record.get("answer")
    if isinstance(nested, Mapping):
        record = dict(nested)
    text = record.get("text", record.get("content", ""))
    answer_id = _value(record, "answerId", "answer_id")
    if answer_id is None:
        answer_id = "answer:" + canonical_hash(record)
    claims = []
    for item in _sequence(record.get("claims", []), "answer claims"):
        claim = _mapping(item, "answer claim")
        claims.append(
            Claim(
                claim_id=_value(claim, "claimId", "claim_id"),
                text=claim.get("text"),
                citation_ids=_sequence(
                    _value(claim, "citationIds", "citation_ids", []),
                    "answer claim citationIds",
                ),
                metadata=dict(claim.get("metadata", {})),
            )
        )
    citations = []
    for item in _sequence(record.get("citations", []), "answer citations"):
        citation = _mapping(item, "answer citation")
        citations.append(
            Citation(
                citation_id=_value(citation, "citationId", "citation_id"),
                source=_source_ref_from_wire(citation.get("source")),
                claim_id=_value(citation, "claimId", "claim_id"),
                cited_text=_value(citation, "citedText", "cited_text"),
                confidence=citation.get("confidence"),
                metadata=dict(citation.get("metadata", {})),
            )
        )
    abstention = record.get("abstention")
    return Answer(
        answer_id=answer_id,
        text=text,
        claims=claims,
        citations=citations,
        abstention=None if abstention is None else _abstention_from_wire(abstention),
        metadata=dict(record.get("metadata", {})),
    )


def _answer_to_wire(answer: Answer) -> dict[str, object]:
    return {
        "answerId": answer.answer_id,
        "text": answer.text,
        "claims": [
            {
                "claimId": claim.claim_id,
                "text": claim.text,
                "citationIds": list(claim.citation_ids),
                "metadata": dict(claim.metadata),
            }
            for claim in answer.claims
        ],
        "citations": [
            {
                "citationId": citation.citation_id,
                "source": _source_ref_to_wire(citation.source),
                "claimId": citation.claim_id,
                "citedText": citation.cited_text,
                "confidence": citation.confidence,
                "metadata": dict(citation.metadata),
            }
            for citation in answer.citations
        ],
        "abstention": None if answer.abstention is None else _abstention_to_wire(answer.abstention),
        "metadata": dict(answer.metadata),
    }


def retrieve_execute_plan(
    inputs: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    del context
    request = _request_from_wire(inputs.get("query", inputs.get("request")))
    configured_top_k = _value(config, "topK", "top_k")
    if configured_top_k is not None:
        request = SearchRequest(
            query_text=request.query_text,
            top_k=configured_top_k,
            filters=request.filters,
            metadata=request.metadata,
        )
    raw_sources = inputs.get("sources", config.get("sources"))
    if raw_sources is None:
        raise TypeError(
            "retrieve.execute_plan@1 requires inputs.sources or config.sources; "
            "resource adapters may inject the same deterministic source contract"
        )
    sources = []
    for index, item in enumerate(_sequence(raw_sources, "retrieve.execute_plan@1 sources")):
        source = _mapping(item, f"retrieve.execute_plan@1 sources[{index}]")
        source_id = _value(source, "sourceId", "source_id")
        raw_result = source.get("result")
        if raw_result is None and "hits" in source:
            raw_result = source
        sources.append(
            FederatedRetrievalSource(
                source_id=source_id,
                result=None if raw_result is None else _retrieval_from_wire(raw_result, request),
                error=source.get("error"),
                weight=source.get("weight", 1.0),
                metadata=dict(source.get("metadata", {})),
            )
        )
    minimum_successful = _value(config, "minimumSuccessfulSources", "minimum_successful_sources", 1)
    successful = sum(source.result is not None and source.error is None for source in sources)
    if successful < minimum_successful:
        raise RuntimeError(
            f"retrieve.execute_plan@1 requires {minimum_successful} successful source(s), got {successful}"
        )
    result = federated_retrieve(
        config.get("retrieverId", "federated"),
        request,
        sources,
        failure_mode=_value(config, "failureMode", "failure_mode", "partial"),
        fusion_strategy=config.get("algorithm", "reciprocal_rank_fusion"),
        k=config.get("k", 60),
    )
    source_contracts = [
        {
            "sourceId": source.source_id,
            "result": None if source.result is None else _retrieval_to_wire(source.result),
            "error": source.error,
            "weight": source.weight,
            "metadata": dict(source.metadata),
        }
        for source in sources
    ]
    result_contract = _retrieval_to_wire(result)
    result_contract["sources"] = source_contracts
    return {"result": result_contract, "sources": source_contracts}


def retrieve_fuse(
    inputs: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    del context
    hit_sets: list[list[SearchHit]] = []
    weights: list[float] = []
    for index, item in enumerate(_sequence(inputs.get("sources"), "retrieve.fuse@1 inputs.sources")):
        if isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray | Mapping):
            hit_sets.append([_hit_from_wire(hit) for hit in item])
            weights.append(1.0)
            continue
        source = _mapping(item, f"retrieve.fuse@1 inputs.sources[{index}]")
        result = source.get("result", source)
        if result is None:
            continue
        result_record = _mapping(result, f"retrieve.fuse@1 inputs.sources[{index}].result")
        hit_sets.append(
            [_hit_from_wire(hit) for hit in _sequence(result_record.get("hits", []), "source hits")]
        )
        weights.append(float(source.get("weight", 1.0)))
    strategy = config.get("algorithm", config.get("strategy", "reciprocal_rank_fusion"))
    fused = fuse_search_hits(
        hit_sets,
        strategy=strategy,
        k=config.get("k", 60),
        weights=weights,
        retriever_id=_value(config, "retrieverId", "retriever_id", "fused"),
    )
    top_k = _value(config, "topK", "top_k")
    if top_k is not None:
        fused = fused[:top_k]
    return {
        "hits": [_hit_to_wire(hit) for hit in fused],
        "metadata": {"algorithm": strategy, "sourceCount": len(hit_sets)},
    }


def rank_documents(
    inputs: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    del context
    query = inputs.get("query", "")
    if isinstance(query, Mapping):
        query_record = dict(query)
        query_terms = _value(query_record, "queryTerms", "query_terms")
        if query_terms is None:
            query_text = query_record.get("original", query_record.get("queryText", query_record.get("text", "")))
            query_terms = str(query_text).split()
    else:
        query_terms = str(query).split()
    configured_terms = _value(config, "queryTerms", "query_terms")
    if configured_terms is not None:
        query_terms = _sequence(configured_terms, "rank.documents@1 config.queryTerms")
    hits = [_hit_from_wire(item) for item in _sequence(inputs.get("hits"), "rank.documents@1 inputs.hits")]
    result = rerank_search_hits(
        hits,
        reranker_id=_value(config, "rerankerId", "reranker_id", "lexical"),
        query_terms=query_terms,
        input_limit=_value(config, "inputLimit", "input_limit"),
    )
    return {
        "hits": [_hit_to_wire(item.hit) for item in result.ranked_hits],
        "result": {
            "rankedHits": [
                {
                    "hit": _hit_to_wire(item.hit),
                    "rerankScore": item.rerank_score,
                    "reranker": item.reranker,
                    "explanation": item.explanation,
                    "metadata": dict(item.metadata),
                }
                for item in result.ranked_hits
            ],
            "reranker": result.reranker,
            "inputCount": result.input_count,
            "evaluatedCount": result.evaluated_count,
            "truncatedHitIds": list(result.truncated_hit_ids),
            "metadata": dict(result.metadata),
        },
    }


def context_build(
    inputs: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    hits_value = inputs.get("evidence", inputs.get("hits"))
    hits = [_hit_from_wire(item) for item in _sequence(hits_value, "context.build@1 evidence")]
    context_id = _value(config, "contextId", "context_id")
    if context_id is None:
        context_id = "context:" + canonical_hash(
            {
                "runId": context.get("run_id", context.get("runId")),
                "nodeId": context.get("node_id", context.get("nodeId")),
                "hitIds": [hit.hit_id for hit in hits],
            }
        )
    pack = build_context_pack(
        context_id,
        hits,
        token_budget=_value(config, "maxTokens", "max_tokens", 4096),
        reserve_output_tokens=_value(config, "reserveOutputTokens", "reserve_output_tokens", 0),
        per_document_max_chunks=_value(config, "perDocumentMaxChunks", "per_document_max_chunks"),
        per_section_max_chunks=_value(config, "perSectionMaxChunks", "per_section_max_chunks"),
        per_source_max_chunks=_value(config, "perSourceMaxChunks", "per_source_max_chunks"),
        deduplicate=config.get("deduplicate", True),
        minimum_source_modified_at=_value(
            config,
            "minimumSourceModifiedAt",
            "minimum_source_modified_at",
        ),
        metadata=dict(config.get("metadata", {})),
    )
    return {"pack": _context_to_wire(pack)}


def answer_validate_grounding(
    inputs: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    del context
    answer = _answer_from_wire(inputs.get("response", inputs.get("answer")))
    context_pack = _context_from_wire(inputs.get("context"))
    failure_policy = _value(config, "onInsufficientEvidence", "on_insufficient_evidence", "abstain")
    result = validate_answer_grounding(
        answer,
        context_pack,
        require_citations=_value(config, "requireCitation", "require_citation", True),
        failure_policy=failure_policy,
    )
    candidate = result.repaired_answer or answer
    if result.abstention is not None:
        candidate = Answer(
            answer_id=answer.answer_id,
            text=result.abstention.user_message,
            abstention=result.abstention,
            metadata={**answer.metadata, "validation": "abstained"},
        )
    validation = {
        "ok": result.ok,
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                "citationId": issue.citation_id,
                "claimId": issue.claim_id,
                "severity": issue.severity,
                "metadata": dict(issue.metadata),
            }
            for issue in result.issues
        ],
        "abstention": None if result.abstention is None else _abstention_to_wire(result.abstention),
        "repaired": result.repaired_answer is not None,
    }
    return {
        "candidate": _answer_to_wire(candidate),
        "response": _answer_to_wire(candidate),
        "result": validation,
        "validation": validation,
    }


RAG_BLOCKS: dict[str, RagBlockCallable] = {
    "retrieve.execute_plan@1": retrieve_execute_plan,
    "retrieve.fuse@1": retrieve_fuse,
    "rank.documents@1": rank_documents,
    "context.build@1": context_build,
    "answer.validate_grounding@1": answer_validate_grounding,
}


__all__ = [
    "RAG_BLOCKS",
    "RagBlockCallable",
    "answer_validate_grounding",
    "context_build",
    "rank_documents",
    "retrieve_execute_plan",
    "retrieve_fuse",
]

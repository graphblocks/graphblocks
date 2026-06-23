from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Literal

from .canonical import canonical_hash
from .documents import DocumentChunk, DocumentSpan, SourceRef


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query_text: str
    top_k: int = 10
    filters: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuthContext:
    tenant_id: str
    principal_id: str
    groups: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    retrieval_id: str
    request: SearchRequest
    hits: list[SearchHit]
    total_candidates: int | None = None
    latency_ms: float | None = None
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KnowledgeItemRef:
    item_id: str
    item_kind: str
    source: SourceRef
    schema_ref: str | None = None
    payload_ref: str | None = None
    preview: list[str] = field(default_factory=list)
    acl: dict[str, object] | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchHit:
    hit_id: str
    item: KnowledgeItemRef
    rank: int
    retriever: str
    raw_score: float | None = None
    normalized_score: float | None = None
    score_kind: str | None = None
    highlights: list[SourceRef] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContextPack:
    context_id: str
    hits: list[SearchHit]
    token_budget: int | None = None
    token_count: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Citation:
    citation_id: str
    source: SourceRef
    claim_id: str | None = None
    cited_text: str | None = None
    confidence: float | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Claim:
    claim_id: str
    text: str
    citation_ids: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Abstention:
    reason: str
    user_message: str
    diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Answer:
    answer_id: str
    text: str
    claims: list[Claim] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    abstention: Abstention | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CitationValidationIssue:
    code: str
    message: str
    citation_id: str | None = None
    claim_id: str | None = None
    severity: Literal["warning", "error"] = "error"
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CitationValidationResult:
    ok: bool
    issues: list[CitationValidationIssue] = field(default_factory=list)
    abstention: Abstention | None = None


@dataclass(frozen=True, slots=True)
class CitationSourceTrace:
    citation_id: str
    claim_id: str | None
    context_id: str
    hit_id: str
    retriever: str
    item_id: str
    item_kind: str
    source: SourceRef
    locator: DocumentSpan | None
    acl: dict[str, object] | None = None
    element_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RankedHit:
    hit: SearchHit
    rerank_score: float | None = None
    reranker: str | None = None
    explanation: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RerankResult:
    ranked_hits: list[RankedHit]
    reranker: str
    input_count: int
    evaluated_count: int
    truncated_hit_ids: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


def build_context_pack(
    context_id: str,
    hits: list[SearchHit],
    *,
    token_budget: int,
    per_document_max_chunks: int | None = None,
    deduplicate: bool = True,
    metadata: dict[str, object] | None = None,
) -> ContextPack:
    if token_budget < 0:
        raise ValueError("token_budget must be non-negative")
    if per_document_max_chunks is not None and per_document_max_chunks < 1:
        raise ValueError("per_document_max_chunks must be at least 1")

    selected: list[SearchHit] = []
    selected_hit_ids: list[str] = []
    dropped_hit_ids: list[str] = []
    drop_reasons: dict[str, str] = {}
    selected_item_ids: set[str] = set()
    chunks_per_document: dict[str, int] = {}
    token_count = 0

    for hit in sorted(hits, key=lambda item: (item.rank, item.hit_id)):
        if deduplicate and hit.item.item_id in selected_item_ids:
            dropped_hit_ids.append(hit.hit_id)
            drop_reasons[hit.hit_id] = "duplicate"
            continue

        document_id = hit.item.metadata.get("document_id")
        if not isinstance(document_id, str):
            locator = hit.item.source.locator
            document_id = locator.document_id if locator is not None else hit.item.item_id

        current_document_chunks = chunks_per_document.get(document_id, 0)
        if per_document_max_chunks is not None and current_document_chunks >= per_document_max_chunks:
            dropped_hit_ids.append(hit.hit_id)
            drop_reasons[hit.hit_id] = "per_document_max_chunks"
            continue

        estimated_tokens = sum(len(preview.split()) for preview in hit.item.preview)
        if token_count + estimated_tokens > token_budget:
            dropped_hit_ids.append(hit.hit_id)
            drop_reasons[hit.hit_id] = "token_budget"
            continue

        selected.append(hit)
        selected_hit_ids.append(hit.hit_id)
        selected_item_ids.add(hit.item.item_id)
        chunks_per_document[document_id] = current_document_chunks + 1
        token_count += estimated_tokens

    context_metadata = dict(metadata or {})
    context_metadata.update(
        {
            "selected_hit_ids": selected_hit_ids,
            "dropped_hit_ids": dropped_hit_ids,
            "drop_reasons": drop_reasons,
        }
    )
    return ContextPack(
        context_id=context_id,
        hits=selected,
        token_budget=token_budget,
        token_count=token_count,
        metadata=context_metadata,
    )


def fuse_search_hits(
    hit_sets: list[list[SearchHit]],
    *,
    strategy: Literal["concatenate", "reciprocal_rank_fusion"] = "reciprocal_rank_fusion",
    k: int = 60,
    weights: list[float] | None = None,
    retriever_id: str = "fused",
) -> list[SearchHit]:
    if strategy not in {"concatenate", "reciprocal_rank_fusion"}:
        raise ValueError("strategy must be concatenate or reciprocal_rank_fusion")
    if k < 1:
        raise ValueError("k must be at least 1")
    if weights is not None and len(weights) != len(hit_sets):
        raise ValueError("weights length must match hit_sets length")

    grouped_hits: dict[str, list[SearchHit]] = {}
    first_seen_item_ids: list[str] = []
    rrf_scores: dict[str, float] = {}
    for set_index, hit_set in enumerate(hit_sets):
        weight = weights[set_index] if weights is not None else 1.0
        for hit in hit_set:
            item_id = hit.item.item_id
            if item_id not in grouped_hits:
                grouped_hits[item_id] = []
                first_seen_item_ids.append(item_id)
            grouped_hits[item_id].append(hit)
            if strategy == "reciprocal_rank_fusion":
                rrf_scores[item_id] = rrf_scores.get(item_id, 0.0) + weight / (k + hit.rank)

    if strategy == "concatenate":
        ordered_item_ids = first_seen_item_ids
        score_kind = "concatenate"
        max_score = None
    else:
        ordered_item_ids = sorted(
            grouped_hits,
            key=lambda item_id: (-rrf_scores[item_id], min(hit.rank for hit in grouped_hits[item_id]), item_id),
        )
        score_kind = "reciprocal_rank_fusion"
        max_score = rrf_scores[ordered_item_ids[0]] if ordered_item_ids else None

    fused_hits: list[SearchHit] = []
    for rank, item_id in enumerate(ordered_item_ids, start=1):
        source_hits = grouped_hits[item_id]
        representative = source_hits[0]
        source_hit_ids = [hit.hit_id for hit in source_hits]
        source_ranks = {hit.retriever: hit.rank for hit in source_hits}
        highlights: list[SourceRef] = []
        seen_source_ids: set[str] = set()
        for hit in source_hits:
            for source_ref in hit.highlights or [hit.item.source]:
                if source_ref.source_id in seen_source_ids:
                    continue
                highlights.append(source_ref)
                seen_source_ids.add(source_ref.source_id)
        metadata = dict(representative.metadata)
        metadata.update(
            {
                "source_hit_ids": source_hit_ids,
                "source_ranks": source_ranks,
                "fusion_strategy": strategy,
            }
        )
        raw_score = rrf_scores[item_id] if strategy == "reciprocal_rank_fusion" else None
        metadata["fusion_score"] = raw_score
        fused_hits.append(
            SearchHit(
                hit_id=f"{retriever_id}:{item_id}",
                item=representative.item,
                rank=rank,
                retriever=retriever_id,
                raw_score=raw_score,
                normalized_score=(raw_score / max_score) if raw_score is not None and max_score else None,
                score_kind=score_kind,
                highlights=highlights,
                metadata=metadata,
            )
        )
    return fused_hits


def rerank_search_hits(
    hits: list[SearchHit],
    *,
    reranker_id: str,
    query_terms: list[str],
    input_limit: int | None = None,
) -> RerankResult:
    if input_limit is not None and input_limit < 1:
        raise ValueError("input_limit must be at least 1")

    normalized_terms = [term.lower() for term in query_terms if term]
    ordered_hits = sorted(hits, key=lambda hit: (hit.rank, hit.hit_id))
    input_count = len(ordered_hits)
    evaluated_count = min(input_limit, input_count) if input_limit is not None else input_count
    truncated_hit_ids = [hit.hit_id for hit in ordered_hits[evaluated_count:]]
    ranked_hits: list[RankedHit] = []

    for hit in ordered_hits[:evaluated_count]:
        preview_text = "\n".join(hit.item.preview).lower()
        score = sum(preview_text.count(term) for term in normalized_terms)
        ranked_hits.append(
            RankedHit(
                hit=hit,
                rerank_score=float(score),
                reranker=reranker_id,
                explanation=f"matched {score} query term occurrence(s)",
                metadata={
                    "original_rank": hit.rank,
                    "source_hit_id": hit.hit_id,
                    "query_terms": normalized_terms,
                },
            )
        )

    ranked_hits = sorted(
        ranked_hits,
        key=lambda ranked: (-(ranked.rerank_score or 0.0), ranked.hit.rank, ranked.hit.hit_id),
    )
    ranked_hits = [
        RankedHit(
            hit=SearchHit(
                hit_id=ranked.hit.hit_id,
                item=ranked.hit.item,
                rank=rank,
                retriever=ranked.hit.retriever,
                raw_score=ranked.hit.raw_score,
                normalized_score=ranked.hit.normalized_score,
                score_kind=ranked.hit.score_kind,
                highlights=list(ranked.hit.highlights),
                metadata=dict(ranked.hit.metadata),
            ),
            rerank_score=ranked.rerank_score,
            reranker=ranked.reranker,
            explanation=ranked.explanation,
            metadata=dict(ranked.metadata),
        )
        for rank, ranked in enumerate(ranked_hits, start=1)
    ]

    return RerankResult(
        ranked_hits=ranked_hits,
        reranker=reranker_id,
        input_count=input_count,
        evaluated_count=evaluated_count,
        truncated_hit_ids=truncated_hit_ids,
        metadata={
            "query_terms": normalized_terms,
            "truncated_hit_ids": truncated_hit_ids,
        },
    )


def resolve_citation_source_trace(answer: Answer, context: ContextPack, citation_id: str) -> CitationSourceTrace:
    citation = next((item for item in answer.citations if item.citation_id == citation_id), None)
    if citation is None:
        raise ValueError(f"citation {citation_id!r} was not found")
    claim_id = next((claim.claim_id for claim in answer.claims if citation_id in claim.citation_ids), citation.claim_id)

    for hit in context.hits:
        for source_ref in [hit.item.source, *hit.highlights]:
            if (
                source_ref.source_id == citation.source.source_id
                and (citation.source.revision is None or citation.source.revision == source_ref.revision)
                and (citation.source.digest is None or citation.source.digest == source_ref.digest)
            ):
                raw_element_ids = hit.item.metadata.get("element_ids")
                element_ids = [item for item in raw_element_ids if isinstance(item, str)] if isinstance(raw_element_ids, list) else []
                if not element_ids and source_ref.locator is not None and source_ref.locator.element_id is not None:
                    element_ids = [source_ref.locator.element_id]
                return CitationSourceTrace(
                    citation_id=citation.citation_id,
                    claim_id=claim_id,
                    context_id=context.context_id,
                    hit_id=hit.hit_id,
                    retriever=hit.retriever,
                    item_id=hit.item.item_id,
                    item_kind=hit.item.item_kind,
                    source=source_ref,
                    locator=source_ref.locator,
                    acl=hit.item.acl,
                    element_ids=element_ids,
                )

    raise ValueError(f"citation {citation.citation_id!r} does not point to the current context")


def authorize_search_hits(hits: list[SearchHit], auth: AuthContext | None) -> list[SearchHit]:
    return [hit for hit in hits if _acl_allows(hit.hit_id, hit.item.acl, auth)]


def validate_answer_citation_authorization(
    answer: Answer,
    context: ContextPack,
    auth: AuthContext | None,
) -> CitationValidationResult:
    issues: list[CitationValidationIssue] = []
    for citation in answer.citations:
        matched_context_source = False
        has_authorized_source = False
        for hit in context.hits:
            for source_ref in [hit.item.source, *hit.highlights]:
                if (
                    source_ref.source_id == citation.source.source_id
                    and (citation.source.revision is None or citation.source.revision == source_ref.revision)
                    and (citation.source.digest is None or citation.source.digest == source_ref.digest)
                ):
                    matched_context_source = True
                    if _acl_allows(hit.hit_id, hit.item.acl, auth):
                        has_authorized_source = True
        if not matched_context_source:
            issues.append(
                CitationValidationIssue(
                    code="citation.source_not_in_context",
                    message=f"citation {citation.citation_id!r} does not point to the current context",
                    citation_id=citation.citation_id,
                )
            )
        elif not has_authorized_source:
            issues.append(
                CitationValidationIssue(
                    code="citation.source_not_authorized",
                    message=(
                        f"citation {citation.citation_id!r} points to a source outside "
                        "the principal authorization scope"
                    ),
                    citation_id=citation.citation_id,
                )
            )
    return CitationValidationResult(ok=not issues, issues=issues)


def validate_answer_citations(
    answer: Answer,
    context: ContextPack,
    *,
    require_citations: bool = True,
    failure_policy: Literal["warn", "fail", "abstain"] = "fail",
) -> CitationValidationResult:
    if failure_policy not in {"warn", "fail", "abstain"}:
        raise ValueError("failure_policy must be one of warn, fail, or abstain")

    severity: Literal["warning", "error"] = "warning" if failure_policy == "warn" else "error"
    issues: list[CitationValidationIssue] = []
    citations_by_id: dict[str, Citation] = {}
    context_source_texts: dict[tuple[str, str | None, str | None], str] = {}

    for citation in answer.citations:
        if citation.citation_id in citations_by_id:
            issues.append(
                CitationValidationIssue(
                    code="citation_id.duplicate",
                    message=f"citation {citation.citation_id!r} is defined more than once",
                    citation_id=citation.citation_id,
                    severity=severity,
                )
            )
        else:
            citations_by_id[citation.citation_id] = citation

    for hit in context.hits:
        preview_text = "\n".join(hit.item.preview)
        for source_ref in [hit.item.source, *hit.highlights]:
            context_source_texts.setdefault(
                (source_ref.source_id, source_ref.revision, source_ref.digest),
                preview_text,
            )

    for claim in answer.claims:
        if require_citations and claim.text.strip() and not claim.citation_ids:
            issues.append(
                CitationValidationIssue(
                    code="claim.missing_citation",
                    message=f"claim {claim.claim_id!r} has no citation",
                    claim_id=claim.claim_id,
                    severity=severity,
                )
            )
        for citation_id in claim.citation_ids:
            citation = citations_by_id.get(citation_id)
            if citation is None:
                issues.append(
                    CitationValidationIssue(
                        code="citation_id.missing",
                        message=f"claim {claim.claim_id!r} references missing citation {citation_id!r}",
                        citation_id=citation_id,
                        claim_id=claim.claim_id,
                        severity=severity,
                    )
                )
                continue
            if citation.claim_id is not None and citation.claim_id != claim.claim_id:
                issues.append(
                    CitationValidationIssue(
                        code="citation.claim_mismatch",
                        message=(
                            f"citation {citation.citation_id!r} is attached to claim "
                            f"{citation.claim_id!r}, not {claim.claim_id!r}"
                        ),
                        citation_id=citation.citation_id,
                        claim_id=claim.claim_id,
                        severity=severity,
                    )
                )

    for citation in answer.citations:
        matching_texts = [
            text
            for (source_id, revision, digest), text in context_source_texts.items()
            if source_id == citation.source.source_id
            and (citation.source.revision is None or citation.source.revision == revision)
            and (citation.source.digest is None or citation.source.digest == digest)
        ]
        if not matching_texts:
            issues.append(
                CitationValidationIssue(
                    code="citation.source_not_in_context",
                    message=f"citation {citation.citation_id!r} does not point to the current context",
                    citation_id=citation.citation_id,
                    severity=severity,
                )
            )
            continue
        if citation.cited_text is not None:
            quoted_text = " ".join(citation.cited_text.split()).lower()
            if quoted_text and not any(quoted_text in " ".join(text.split()).lower() for text in matching_texts):
                issues.append(
                    CitationValidationIssue(
                        code="citation.text_mismatch",
                        message=f"citation {citation.citation_id!r} cites text outside the source preview",
                        citation_id=citation.citation_id,
                        severity=severity,
                    )
                )

    if not issues:
        return CitationValidationResult(ok=True)
    if failure_policy == "warn":
        return CitationValidationResult(ok=True, issues=issues)
    if failure_policy == "abstain":
        return CitationValidationResult(
            ok=False,
            issues=issues,
            abstention=Abstention(
                reason="citation_validation_failed",
                user_message="I do not have enough validated source support to answer.",
                diagnostics={"issue_codes": [issue.code for issue in issues]},
            ),
        )
    return CitationValidationResult(ok=False, issues=issues)


def _acl_allows(resource_id: str, acl: dict[str, object] | None, auth: AuthContext | None) -> bool:
    if acl is None:
        return True
    if not isinstance(acl, dict):
        raise ValueError(f"ACL for {resource_id!r} must be an object")
    if acl.get("public") is True:
        return True
    if auth is None:
        raise PermissionError(f"authorization context required for {resource_id!r}")
    tenant_id = acl.get("tenant_id")
    if isinstance(tenant_id, str) and tenant_id != auth.tenant_id:
        return False

    has_selector = False
    principals = acl.get("principals")
    if principals is not None:
        if not isinstance(principals, list):
            raise ValueError("principals must be a list")
        has_selector = True
        if auth.principal_id in principals:
            return True

    groups = acl.get("groups")
    if groups is not None:
        if not isinstance(groups, list):
            raise ValueError("groups must be a list")
        has_selector = True
        if any(isinstance(group, str) and group in auth.groups for group in groups):
            return True

    roles = acl.get("roles")
    if roles is not None:
        if not isinstance(roles, list):
            raise ValueError("roles must be a list")
        has_selector = True
        if any(isinstance(role, str) and role in auth.roles for role in roles):
            return True

    attributes = acl.get("attributes")
    if attributes is not None:
        if not isinstance(attributes, dict):
            raise ValueError("attributes must be an object")
        has_selector = True
        if all(auth.attributes.get(name) == expected for name, expected in attributes.items()):
            return True

    return not has_selector


def knowledge_item_from_chunk(chunk: DocumentChunk) -> KnowledgeItemRef:
    return KnowledgeItemRef(
        item_id=chunk.chunk_id,
        item_kind="document_chunk",
        source=chunk.source_refs[0],
        preview=[chunk.text],
        acl=chunk.acl,
        metadata={
            "document_id": chunk.document_id,
            "asset_id": chunk.asset_id,
            "revision_id": chunk.revision_id,
            "element_ids": list(chunk.element_ids),
        },
    )


@dataclass(slots=True)
class InMemoryChunkRetriever:
    chunks: list[DocumentChunk]
    retriever_id: str = "local-chunk"

    def search(self, query_text: str, top_k: int = 10) -> list[SearchHit]:
        return self.retrieve(SearchRequest(query_text=query_text, top_k=top_k)).hits

    def retrieve(self, request: SearchRequest) -> RetrievalResult:
        request_hash = canonical_hash(
            {
                "query_text": request.query_text,
                "top_k": request.top_k,
                "filters": request.filters,
            }
        )
        retrieval_id = f"{self.retriever_id}:{request_hash}"
        terms = [term for term in re.findall(r"[A-Za-z0-9_]+", request.query_text.lower()) if term]
        if not terms:
            return RetrievalResult(retrieval_id=retrieval_id, request=request, hits=[], total_candidates=0)
        scored: list[tuple[int, int, DocumentChunk]] = []
        for index, chunk in enumerate(self.chunks):
            haystack = chunk.text.lower()
            score = sum(haystack.count(term) for term in terms)
            if score > 0:
                scored.append((score, index, chunk))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if not scored:
            return RetrievalResult(retrieval_id=retrieval_id, request=request, hits=[], total_candidates=0)
        max_score = scored[0][0]
        hits: list[SearchHit] = []
        for rank, (score, _index, chunk) in enumerate(scored[: request.top_k], start=1):
            hits.append(
                SearchHit(
                    hit_id=f"{self.retriever_id}:{chunk.chunk_id}",
                    item=knowledge_item_from_chunk(chunk),
                    rank=rank,
                    retriever=self.retriever_id,
                    raw_score=float(score),
                    normalized_score=score / max_score,
                    score_kind="term_frequency",
                    highlights=list(chunk.source_refs),
                )
            )
        return RetrievalResult(
            retrieval_id=retrieval_id,
            request=request,
            hits=hits,
            total_candidates=len(scored),
        )

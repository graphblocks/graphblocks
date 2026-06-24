from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import re
from typing import Literal, TypeAlias

from .canonical import canonical_hash
from .documents import DocumentChunk, DocumentSpan, SourceRef
from .evaluation import ResultBundle

KnowledgeDeleteMode: TypeAlias = Literal["tombstone", "hard"]
KnowledgeRecordStatus: TypeAlias = Literal["active", "tombstoned"]
FederatedFailureMode: TypeAlias = Literal["fail", "partial"]


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query_text: str
    top_k: int = 10
    filters: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QueryPlan:
    original: str
    rewritten: list[str]
    subqueries: list[str] = field(default_factory=list)
    filters: dict[str, object] | None = None
    rationale_summary: str | None = None


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


class FederatedRetrievalError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FederatedRetrievalSource:
    source_id: str
    result: RetrievalResult | None = None
    error: str | None = None
    weight: float = 1.0
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
class RagResultPayload:
    query_plan: QueryPlan
    retrievals: list[RetrievalResult]
    context: ContextPack
    model_response: dict[str, object]
    answer: Answer


@dataclass(frozen=True, slots=True)
class RagResultBundle:
    base: ResultBundle
    payload: RagResultPayload
    profile: Literal["rag"] = "rag"


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


class KnowledgeIndexError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KnowledgeIndexRecord:
    chunk: DocumentChunk
    status: KnowledgeRecordStatus


@dataclass(frozen=True, slots=True)
class KnowledgeWriteReport:
    operation: str
    affected_count: int
    chunk_ids: list[str]
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KnowledgePublishResult:
    index_id: str
    asset_id: str
    revision_id: str
    published_chunk_ids: list[str]
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KnowledgeIndexCapabilities:
    upsert: bool
    delete: bool
    metadata_update: bool
    acl_update: bool
    publish: bool
    hard_delete: bool
    tombstone: bool
    retriever_adapter: bool


@dataclass(frozen=True, slots=True)
class KnowledgeIndexHealth:
    healthy: bool
    indexed_chunks: int
    active_chunks: int
    tombstoned_chunks: int
    published_revisions: int


def build_context_pack(
    context_id: str,
    hits: list[SearchHit],
    *,
    token_budget: int,
    reserve_output_tokens: int = 0,
    per_document_max_chunks: int | None = None,
    per_section_max_chunks: int | None = None,
    per_source_max_chunks: int | None = None,
    deduplicate: bool = True,
    minimum_source_modified_at: str | None = None,
    metadata: dict[str, object] | None = None,
) -> ContextPack:
    if token_budget < 0:
        raise ValueError("token_budget must be non-negative")
    if reserve_output_tokens < 0:
        raise ValueError("reserve_output_tokens must be non-negative")
    if per_document_max_chunks is not None and per_document_max_chunks < 1:
        raise ValueError("per_document_max_chunks must be at least 1")
    if per_section_max_chunks is not None and per_section_max_chunks < 1:
        raise ValueError("per_section_max_chunks must be at least 1")
    if per_source_max_chunks is not None and per_source_max_chunks < 1:
        raise ValueError("per_source_max_chunks must be at least 1")

    effective_context_token_budget = max(token_budget - reserve_output_tokens, 0)
    selected: list[SearchHit] = []
    selected_hit_ids: list[str] = []
    dropped_hit_ids: list[str] = []
    drop_reasons: dict[str, str] = {}
    selected_item_ids: set[str] = set()
    chunks_per_document: dict[str, int] = {}
    chunks_per_section: dict[str, int] = {}
    chunks_per_source: dict[str, int] = {}
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

        section_id = hit.metadata.get("section_id")
        if not isinstance(section_id, str):
            section_id = hit.item.metadata.get("section_id")
        if not isinstance(section_id, str):
            source_ref = next(
                (
                    source_ref
                    for source_ref in [hit.item.source, *hit.highlights]
                    if source_ref.locator is not None
                    and source_ref.locator.element_id is not None
                ),
                None,
            )
            section_id = (
                source_ref.locator.element_id
                if source_ref is not None and source_ref.locator is not None
                else None
            )
        current_section_chunks = (
            chunks_per_section.get(section_id, 0) if isinstance(section_id, str) else 0
        )
        if (
            isinstance(section_id, str)
            and per_section_max_chunks is not None
            and current_section_chunks >= per_section_max_chunks
        ):
            dropped_hit_ids.append(hit.hit_id)
            drop_reasons[hit.hit_id] = "per_section_max_chunks"
            continue

        source_id = hit.metadata.get("source_id")
        if not isinstance(source_id, str):
            source_id = hit.retriever
        current_source_chunks = chunks_per_source.get(source_id, 0)
        if (
            per_source_max_chunks is not None
            and current_source_chunks >= per_source_max_chunks
        ):
            dropped_hit_ids.append(hit.hit_id)
            drop_reasons[hit.hit_id] = "per_source_max_chunks"
            continue

        if minimum_source_modified_at is not None:
            source_modified_at = hit.metadata.get("source_modified_at")
            if not isinstance(source_modified_at, str):
                source_modified_at = hit.item.metadata.get("source_modified_at")
            if (
                not isinstance(source_modified_at, str)
                or source_modified_at < minimum_source_modified_at
            ):
                dropped_hit_ids.append(hit.hit_id)
                drop_reasons[hit.hit_id] = "freshness"
                continue

        estimated_tokens = sum(len(preview.split()) for preview in hit.item.preview)
        if token_count + estimated_tokens > effective_context_token_budget:
            dropped_hit_ids.append(hit.hit_id)
            drop_reasons[hit.hit_id] = "token_budget"
            continue

        selected.append(hit)
        selected_hit_ids.append(hit.hit_id)
        selected_item_ids.add(hit.item.item_id)
        chunks_per_document[document_id] = current_document_chunks + 1
        if isinstance(section_id, str):
            chunks_per_section[section_id] = current_section_chunks + 1
        chunks_per_source[source_id] = current_source_chunks + 1
        token_count += estimated_tokens

    context_metadata = dict(metadata or {})
    context_metadata.update(
        {
            "selected_hit_ids": selected_hit_ids,
            "dropped_hit_ids": dropped_hit_ids,
            "drop_reasons": drop_reasons,
        }
    )
    if minimum_source_modified_at is not None:
        context_metadata["minimum_source_modified_at"] = minimum_source_modified_at
    if per_section_max_chunks is not None:
        context_metadata["per_section_max_chunks"] = per_section_max_chunks
    if per_source_max_chunks is not None:
        context_metadata["per_source_max_chunks"] = per_source_max_chunks
    if reserve_output_tokens > 0:
        context_metadata["reserve_output_tokens"] = reserve_output_tokens
        context_metadata["effective_context_token_budget"] = effective_context_token_budget
    return ContextPack(
        context_id=context_id,
        hits=selected,
        token_budget=token_budget,
        token_count=token_count,
        metadata=context_metadata,
    )


def render_context_pack(context: ContextPack) -> str:
    lines = [
        "GRAPHBLOCKS_CONTEXT_PACK_BEGIN "
        + _compact_json(
            {"context_id": context.context_id, "trust_boundary": "retrieved_untrusted"}
        )
    ]
    for hit in context.hits:
        source_refs = hit.highlights or [hit.item.source]
        sources = []
        for source in source_refs:
            locator = source.locator
            sources.append(
                {
                    "source_id": source.source_id,
                    "source_kind": source.source_kind,
                    "revision": source.revision,
                    "digest": source.digest,
                    "locator": (
                        {
                            "asset_id": locator.asset_id,
                            "revision_id": locator.revision_id,
                            "document_id": locator.document_id,
                            "element_id": locator.element_id,
                            "chunk_id": locator.chunk_id,
                            "page": locator.page,
                            "bbox": locator.bbox,
                            "char_start": locator.char_start,
                            "char_end": locator.char_end,
                            "sheet": locator.sheet,
                            "cell_range": locator.cell_range,
                            "slide": locator.slide,
                        }
                        if locator is not None
                        else None
                    ),
                    "observed_at": source.observed_at,
                    "relevant_as_of": source.relevant_as_of,
                    "trust": "retrieved_untrusted",
                }
            )
        lines.append(
            "GRAPHBLOCKS_RETRIEVED_ITEM_BEGIN "
            + _compact_json(
                {
                    "hit_id": hit.hit_id,
                    "item_id": hit.item.item_id,
                    "rank": hit.rank,
                    "retriever": hit.retriever,
                    "sources": sources,
                    "trust": "retrieved_untrusted",
                }
            )
        )
        lines.append(_compact_json("\n".join(hit.item.preview)))
        lines.append("GRAPHBLOCKS_RETRIEVED_ITEM_END")
    lines.append("GRAPHBLOCKS_CONTEXT_PACK_END")
    return "\n".join(lines)


def build_answer_from_model_response(answer_id: str, model_response: dict[str, object]) -> Answer:
    text = model_response.get("output_text")
    if not isinstance(text, str):
        text = model_response.get("text")
    if not isinstance(text, str):
        raise ValueError("model_response must contain string output_text or text")

    claims: list[Claim] = []
    raw_claims = model_response.get("claims", [])
    if raw_claims is not None and not isinstance(raw_claims, list):
        raise ValueError("model_response claims must be a list when present")
    if isinstance(raw_claims, list):
        for raw_claim in raw_claims:
            if not isinstance(raw_claim, dict):
                raise ValueError("model_response claims must contain mapping items")
            claim_id = raw_claim.get("claim_id")
            claim_text = raw_claim.get("text")
            if not isinstance(claim_id, str) or not isinstance(claim_text, str):
                raise ValueError("model_response claims must contain string claim_id and text")
            raw_citation_ids = raw_claim.get("citation_ids", [])
            citation_ids = (
                [item for item in raw_citation_ids if isinstance(item, str)]
                if isinstance(raw_citation_ids, list)
                else []
            )
            claims.append(
                Claim(
                    claim_id=claim_id,
                    text=claim_text,
                    citation_ids=citation_ids,
                )
            )

    metadata: dict[str, object] = {
        "model_response_digest": canonical_hash(model_response),
    }
    response_id = model_response.get("response_id")
    if isinstance(response_id, str):
        metadata["provider_response_id"] = response_id
    for key in ("provider", "model", "finish_reason"):
        value = model_response.get(key)
        if isinstance(value, str):
            metadata[key] = value
    return Answer(answer_id=answer_id, text=text, claims=claims, metadata=metadata)


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def federated_retrieve(
    retriever_id: str,
    request: SearchRequest,
    sources: list[FederatedRetrievalSource],
    *,
    failure_mode: FederatedFailureMode = "partial",
    fusion_strategy: Literal[
        "concatenate",
        "reciprocal_rank_fusion",
        "weighted_rank",
        "normalized_score",
        "interleave",
    ] = "reciprocal_rank_fusion",
    k: int = 60,
) -> RetrievalResult:
    if failure_mode not in {"fail", "partial"}:
        raise ValueError("failure_mode must be fail or partial")

    hit_sets: list[list[SearchHit]] = []
    weights: list[float] = []
    successful_sources: list[str] = []
    failed_sources: list[dict[str, str]] = []
    warnings: list[str] = []
    total_candidates = 0

    for source in sources:
        if source.result is not None and source.error is None:
            total_candidates += len(source.result.hits)
            hit_sets.append(source.result.hits)
            weights.append(source.weight)
            successful_sources.append(source.source_id)
            continue

        message = source.error or "missing retrieval result"
        if failure_mode == "fail" or source.result is not None:
            raise FederatedRetrievalError(f"federated source {source.source_id} failed: {message}")
        failed_sources.append({"source_id": source.source_id, "error": message})
        warnings.append(f"federated source {source.source_id} failed: {message}")

    hits = fuse_search_hits(
        hit_sets,
        strategy=fusion_strategy,
        k=k,
        weights=weights,
        retriever_id=retriever_id,
    )[: request.top_k]
    retrieval_digest = canonical_hash(
        {
            "query_text": request.query_text,
            "top_k": request.top_k,
            "filters": request.filters,
            "successful_sources": successful_sources,
            "failed_sources": failed_sources,
            "fusion_strategy": fusion_strategy,
        }
    )
    retrieval_id = f"{retriever_id}:{retrieval_digest}"
    return RetrievalResult(
        retrieval_id=retrieval_id,
        request=request,
        hits=hits,
        total_candidates=total_candidates,
        warnings=warnings,
        metadata={
            "successful_sources": successful_sources,
            "failed_sources": failed_sources,
            "fusion_strategy": fusion_strategy,
            "retriever_id": retriever_id,
        },
    )


def fuse_search_hits(
    hit_sets: list[list[SearchHit]],
    *,
    strategy: Literal[
        "concatenate",
        "reciprocal_rank_fusion",
        "weighted_rank",
        "normalized_score",
        "interleave",
    ] = "reciprocal_rank_fusion",
    k: int = 60,
    weights: list[float] | None = None,
    retriever_id: str = "fused",
) -> list[SearchHit]:
    valid_strategies = {
        "concatenate",
        "reciprocal_rank_fusion",
        "weighted_rank",
        "normalized_score",
        "interleave",
    }
    if strategy not in valid_strategies:
        raise ValueError(
            "strategy must be concatenate, reciprocal_rank_fusion, weighted_rank, normalized_score, or interleave"
        )
    if k < 1:
        raise ValueError("k must be at least 1")
    if weights is not None and len(weights) != len(hit_sets):
        raise ValueError("weights length must match hit_sets length")

    grouped_hits: dict[str, list[SearchHit]] = {}
    first_seen_keys: list[str] = []
    fusion_scores: dict[str, float] = {}
    dedupe_keys_by_hit: dict[int, str] = {}
    for set_index, hit_set in enumerate(hit_sets):
        weight = weights[set_index] if weights is not None else 1.0
        for hit in hit_set:
            source_ref = next(
                (
                    source_ref
                    for source_ref in [hit.item.source, *hit.highlights]
                    if source_ref.locator is not None
                ),
                None,
            )
            locator = source_ref.locator if source_ref is not None else None
            if locator is not None:
                dedupe_key = "source_span:" + _compact_json(
                    {
                        "asset_id": locator.asset_id,
                        "revision_id": locator.revision_id,
                        "document_id": locator.document_id,
                        "element_id": locator.element_id,
                        "chunk_id": locator.chunk_id,
                        "page": locator.page,
                        "bbox": locator.bbox,
                        "char_start": locator.char_start,
                        "char_end": locator.char_end,
                        "sheet": locator.sheet,
                        "cell_range": locator.cell_range,
                        "slide": locator.slide,
                    }
                )
            else:
                dedupe_key = f"item:{hit.item.item_id}"
            dedupe_keys_by_hit[id(hit)] = dedupe_key
            if dedupe_key not in grouped_hits:
                grouped_hits[dedupe_key] = []
                first_seen_keys.append(dedupe_key)
            grouped_hits[dedupe_key].append(hit)
            if strategy == "reciprocal_rank_fusion":
                fusion_scores[dedupe_key] = (
                    fusion_scores.get(dedupe_key, 0.0) + weight / (k + hit.rank)
                )
            elif strategy == "weighted_rank":
                fusion_scores[dedupe_key] = (
                    fusion_scores.get(dedupe_key, 0.0) + weight / max(hit.rank, 1)
                )
            elif strategy == "normalized_score":
                fusion_scores[dedupe_key] = (
                    fusion_scores.get(dedupe_key, 0.0) + weight * (hit.normalized_score or 0.0)
                )

    if strategy == "concatenate":
        ordered_keys = first_seen_keys
        score_kind = "concatenate"
        max_score = None
    elif strategy == "interleave":
        sorted_hit_sets = [sorted(hit_set, key=lambda hit: (hit.rank, hit.hit_id)) for hit_set in hit_sets]
        ordered_keys = []
        seen_keys: set[str] = set()
        max_len = max((len(hit_set) for hit_set in sorted_hit_sets), default=0)
        for index in range(max_len):
            for hit_set in sorted_hit_sets:
                if index >= len(hit_set):
                    continue
                dedupe_key = dedupe_keys_by_hit[id(hit_set[index])]
                if dedupe_key in seen_keys:
                    continue
                ordered_keys.append(dedupe_key)
                seen_keys.add(dedupe_key)
        score_kind = "interleave"
        max_score = None
    else:
        ordered_keys = sorted(
            grouped_hits,
            key=lambda dedupe_key: (
                -fusion_scores.get(dedupe_key, 0.0),
                min(hit.rank for hit in grouped_hits[dedupe_key]),
                dedupe_key,
            ),
        )
        score_kind = strategy
        max_score = fusion_scores.get(ordered_keys[0]) if ordered_keys else None

    fused_hits: list[SearchHit] = []
    for rank, dedupe_key in enumerate(ordered_keys, start=1):
        source_hits = grouped_hits[dedupe_key]
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
                "dedupe_key": dedupe_key,
            }
        )
        raw_score = (
            fusion_scores.get(dedupe_key)
            if strategy in {"reciprocal_rank_fusion", "weighted_rank", "normalized_score"}
            else None
        )
        metadata["fusion_score"] = raw_score
        fused_hits.append(
            SearchHit(
                hit_id=f"{retriever_id}:{representative.item.item_id}",
                item=representative.item,
                rank=rank,
                retriever=retriever_id,
                raw_score=raw_score,
                normalized_score=(
                    (raw_score / max_score)
                    if raw_score is not None and max_score and max_score > 0.0
                    else None
                ),
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
            if _source_ref_matches(citation.source, source_ref):
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
                if _source_ref_matches(citation.source, source_ref):
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
    context_source_texts: list[tuple[SourceRef, str]] = []

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
            context_source_texts.append((source_ref, preview_text))

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
            for source_ref, text in context_source_texts
            if _source_ref_matches(citation.source, source_ref)
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


def validate_answer_grounding(
    answer: Answer,
    context: ContextPack,
    *,
    require_citations: bool = True,
    failure_policy: Literal["warn", "fail", "abstain"] = "abstain",
) -> CitationValidationResult:
    if failure_policy not in {"warn", "fail", "abstain"}:
        raise ValueError("failure_policy must be one of warn, fail, or abstain")
    if not context.hits and (answer.text.strip() or any(claim.text.strip() for claim in answer.claims)):
        severity: Literal["warning", "error"] = "warning" if failure_policy == "warn" else "error"
        issues = [
            CitationValidationIssue(
                code="grounding.insufficient_context",
                message="answer grounding requires at least one context hit",
                severity=severity,
            )
        ]
        if failure_policy == "warn":
            return CitationValidationResult(ok=True, issues=issues)
        if failure_policy == "abstain":
            return CitationValidationResult(
                ok=False,
                issues=issues,
                abstention=Abstention(
                    reason="insufficient_context",
                    user_message="I do not have enough retrieved context to answer.",
                    diagnostics={"issue_codes": ["grounding.insufficient_context"]},
                ),
            )
        return CitationValidationResult(ok=False, issues=issues)

    return validate_answer_citations(
        answer,
        context,
        require_citations=require_citations,
        failure_policy=failure_policy,
    )


def _source_ref_matches(citation_source: SourceRef, context_source: SourceRef) -> bool:
    if citation_source.source_id != context_source.source_id:
        return False
    if citation_source.revision is not None and citation_source.revision != context_source.revision:
        return False
    if citation_source.digest is not None and citation_source.digest != context_source.digest:
        return False
    return _locator_matches(citation_source.locator, context_source.locator)


def _locator_matches(citation_locator: DocumentSpan | None, context_locator: DocumentSpan | None) -> bool:
    if citation_locator is None:
        return True
    if context_locator is None:
        return False
    if citation_locator.asset_id != context_locator.asset_id:
        return False
    if citation_locator.revision_id != context_locator.revision_id:
        return False
    if citation_locator.document_id != context_locator.document_id:
        return False
    for attribute in (
        "element_id",
        "chunk_id",
        "page",
        "bbox",
        "char_start",
        "char_end",
        "sheet",
        "cell_range",
        "slide",
    ):
        expected = getattr(citation_locator, attribute)
        if expected is not None and expected != getattr(context_locator, attribute):
            return False
    return True


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
    source = (
        chunk.source_refs[0]
        if chunk.source_refs
        else SourceRef(
            source_id=chunk.chunk_id,
            source_kind="document_chunk",
            revision=chunk.revision_id,
            digest="",
            locator=DocumentSpan(
                asset_id=chunk.asset_id,
                revision_id=chunk.revision_id,
                document_id=chunk.document_id,
                chunk_id=chunk.chunk_id,
            ),
        )
    )
    metadata = dict(chunk.metadata)
    metadata.update(
        {
            "document_id": chunk.document_id,
            "asset_id": chunk.asset_id,
            "revision_id": chunk.revision_id,
            "element_ids": list(chunk.element_ids),
        }
    )
    return KnowledgeItemRef(
        item_id=chunk.chunk_id,
        item_kind="document_chunk",
        source=source,
        preview=[chunk.text],
        acl=chunk.acl,
        metadata=metadata,
    )


@dataclass(slots=True)
class InMemoryKnowledgeIndex:
    index_id: str
    _records: dict[str, KnowledgeIndexRecord] = field(default_factory=dict)
    _published_revisions: dict[str, str] = field(default_factory=dict)

    def upsert_chunks(self, chunks: list[DocumentChunk]) -> KnowledgeWriteReport:
        chunk_ids: list[str] = []
        for chunk in chunks:
            chunk_ids.append(chunk.chunk_id)
            self._records[chunk.chunk_id] = KnowledgeIndexRecord(chunk=chunk, status="active")
        return KnowledgeWriteReport(
            operation="upsert",
            affected_count=len(chunk_ids),
            chunk_ids=chunk_ids,
            metadata={"index_id": self.index_id},
        )

    def delete_asset(self, asset_id: str, mode: KnowledgeDeleteMode) -> KnowledgeWriteReport:
        if mode not in {"hard", "tombstone"}:
            raise ValueError("mode must be hard or tombstone")
        chunk_ids = [
            chunk_id
            for chunk_id, record in sorted(self._records.items())
            if record.chunk.asset_id == asset_id
        ]
        if mode == "hard":
            for chunk_id in chunk_ids:
                self._records.pop(chunk_id, None)
        else:
            for chunk_id in chunk_ids:
                record = self._records.get(chunk_id)
                if record is not None:
                    self._records[chunk_id] = replace(record, status="tombstoned")
        self._published_revisions.pop(asset_id, None)
        return KnowledgeWriteReport(
            operation="delete",
            affected_count=len(chunk_ids),
            chunk_ids=chunk_ids,
            metadata={"asset_id": asset_id, "delete_mode": mode},
        )

    def update_chunk_metadata(self, chunk_id: str, metadata: dict[str, object]) -> KnowledgeWriteReport:
        record = self._require_record(chunk_id)
        merged_metadata = dict(record.chunk.metadata)
        for key in sorted(metadata):
            merged_metadata[key] = metadata[key]
        self._records[chunk_id] = replace(record, chunk=replace(record.chunk, metadata=merged_metadata))
        return KnowledgeWriteReport(
            operation="update_metadata",
            affected_count=1,
            chunk_ids=[chunk_id],
            metadata={"metadata_keys": sorted(metadata)},
        )

    def update_chunk_acl(self, chunk_id: str, acl: dict[str, object] | None) -> KnowledgeWriteReport:
        record = self._require_record(chunk_id)
        self._records[chunk_id] = replace(record, chunk=replace(record.chunk, acl=acl))
        return KnowledgeWriteReport(
            operation="update_acl",
            affected_count=1,
            chunk_ids=[chunk_id],
        )

    def publish_revision(self, asset_id: str, revision_id: str) -> KnowledgePublishResult:
        published_chunk_ids = [
            chunk_id
            for chunk_id, record in sorted(self._records.items())
            if record.status == "active"
            and record.chunk.asset_id == asset_id
            and record.chunk.revision_id == revision_id
        ]
        if not published_chunk_ids:
            item_id = f"{asset_id}:{revision_id}"
            raise KnowledgeIndexError(f"knowledge item {item_id!r} was not found")
        self._published_revisions[asset_id] = revision_id
        return KnowledgePublishResult(
            index_id=self.index_id,
            asset_id=asset_id,
            revision_id=revision_id,
            published_chunk_ids=published_chunk_ids,
            metadata={"active_chunk_count": len(published_chunk_ids)},
        )

    def is_revision_published(self, asset_id: str, revision_id: str) -> bool:
        return self._published_revisions.get(asset_id) == revision_id

    def capabilities(self) -> KnowledgeIndexCapabilities:
        return KnowledgeIndexCapabilities(
            upsert=True,
            delete=True,
            metadata_update=True,
            acl_update=True,
            publish=True,
            hard_delete=True,
            tombstone=True,
            retriever_adapter=True,
        )

    def health(self) -> KnowledgeIndexHealth:
        tombstoned_chunks = sum(1 for record in self._records.values() if record.status == "tombstoned")
        return KnowledgeIndexHealth(
            healthy=True,
            indexed_chunks=len(self._records),
            active_chunks=len(self._records) - tombstoned_chunks,
            tombstoned_chunks=tombstoned_chunks,
            published_revisions=len(self._published_revisions),
        )

    def record(self, chunk_id: str) -> KnowledgeIndexRecord | None:
        return self._records.get(chunk_id)

    def retriever(self, retriever_id: str) -> InMemoryChunkRetriever:
        chunks = [
            record.chunk
            for record in self._records.values()
            if record.status == "active"
        ]
        chunks.sort(key=lambda chunk: (chunk.asset_id, chunk.revision_id, chunk.chunk_id))
        return InMemoryChunkRetriever(chunks, retriever_id=retriever_id)

    def _require_record(self, chunk_id: str) -> KnowledgeIndexRecord:
        try:
            return self._records[chunk_id]
        except KeyError as error:
            raise KnowledgeIndexError(f"knowledge item {chunk_id!r} was not found") from error


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

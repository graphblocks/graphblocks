from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import re
from typing import Literal, TypeAlias

from .canonical import canonical_dumps, canonical_hash
from .documents import (
    DocumentChunk,
    DocumentSpan,
    FrozenDict,
    FrozenList,
    SourceRef,
    sha256_digest_bytes,
)
from .evaluation import MetricObservation, ResultBundle

KnowledgeDeleteMode: TypeAlias = Literal["tombstone", "hard"]
KnowledgeRecordStatus: TypeAlias = Literal["active", "tombstoned"]
FederatedFailureMode: TypeAlias = Literal["fail", "partial"]


def _validate_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    return value


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    value = _validate_string(owner, field_name, value)
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{owner} {field_name} must not contain surrounding whitespace")
    return value


def _validate_optional_non_empty_string(owner: str, field_name: str, value: object | None) -> str | None:
    if value is None:
        return None
    return _validate_non_empty_string(owner, field_name, value)


def _validate_non_negative_int(owner: str, field_name: str, value: object | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{owner} {field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{owner} {field_name} must be non-negative")
    return value


def _validate_positive_int(owner: str, field_name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{owner} {field_name} must be an integer")
    if value < 1:
        raise ValueError(f"{owner} {field_name} must be positive")
    return value


def _validate_optional_finite_float(owner: str, field_name: str, value: object | None) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"{owner} {field_name} must be a number")
    try:
        converted = float(value)
    except OverflowError as error:
        raise ValueError(f"{owner} {field_name} must be finite") from error
    if not math.isfinite(converted):
        raise ValueError(f"{owner} {field_name} must be finite")
    return converted


def _copy_metadata(owner: str, value: object, *, field_name: str = "metadata") -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{owner} {field_name} must be a mapping")
    metadata = dict(value)
    for key in metadata:
        if not isinstance(key, str):
            raise ValueError(f"{owner} {field_name} keys must be strings")
        if not key.strip():
            raise ValueError(f"{owner} {field_name} keys must not be empty")
        if key != key.strip():
            raise ValueError(f"{owner} {field_name} keys must not contain surrounding whitespace")
    try:
        canonical_dumps(metadata)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{owner} {field_name} must contain strict canonical JSON"
        ) from error
    return {
        key: _copy_metadata_value(item)
        for key, item in metadata.items()
    }


def _copy_metadata_value(value: object) -> object:
    if isinstance(value, FrozenDict):
        return FrozenDict(
            {key: _copy_metadata_value(item) for key, item in value.items()}
        )
    if isinstance(value, Mapping):
        return {
            key: _copy_metadata_value(item)
            for key, item in value.items()
        }
    if isinstance(value, FrozenList):
        return FrozenList(_copy_metadata_value(item) for item in value)
    if isinstance(value, list):
        return [_copy_metadata_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_metadata_value(item) for item in value)
    return value


def _copy_string_list(owner: str, field_name: str, value: object) -> list[str]:
    if isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a list of strings")
    try:
        items = list(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{owner} {field_name} must be a list of strings") from error
    for item in items:
        _validate_non_empty_string(owner, f"{field_name} item", item)
    return items


def _copy_typed_list(owner: str, field_name: str, value: object, item_type: type[object]) -> list[object]:
    if isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a list of {item_type.__name__} records")
    try:
        items = list(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{owner} {field_name} must be a list of {item_type.__name__} records") from error
    for item in items:
        if not isinstance(item, item_type):
            raise ValueError(f"{owner} {field_name} must be a list of {item_type.__name__} records")
    return items


def _parse_iso_datetime(value: str, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO datetime")
    normalized = value
    if normalized != normalized.strip() or len(normalized) <= 19 or normalized[10] != "T":
        raise ValueError(f"{field} must be an ISO datetime")
    timezone_start = 19
    if normalized[timezone_start] == ".":
        timezone_start += 1
        while timezone_start < len(normalized) and normalized[timezone_start].isdigit():
            timezone_start += 1
        if timezone_start == 20:
            raise ValueError(f"{field} must be an ISO datetime")
    suffix = normalized[timezone_start:]
    if normalized.endswith("Z"):
        if suffix != "Z":
            raise ValueError(f"{field} must be an ISO datetime")
        normalized = f"{normalized[:timezone_start]}+00:00"
    elif (
        len(suffix) == 6
        and suffix[0] in {"+", "-"}
        and suffix[1:3].isdigit()
        and suffix[3] == ":"
        and suffix[4:6].isdigit()
    ):
        offset_hours = int(suffix[1:3])
        offset_minutes = int(suffix[4:6])
        if offset_hours > 23 or offset_minutes > 59:
            raise ValueError(f"{field} must be an ISO datetime")
    else:
        raise ValueError(f"{field} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO datetime") from error
    return parsed.astimezone(timezone.utc)


def _source_modified_at_satisfies(source_modified_at: object, minimum_source_modified_at: str) -> bool:
    if not isinstance(source_modified_at, str):
        return False
    try:
        source_modified_at_time = _parse_iso_datetime(source_modified_at, field="source_modified_at")
    except ValueError:
        return False
    minimum_source_modified_at_time = _parse_iso_datetime(
        minimum_source_modified_at,
        field="minimum_source_modified_at",
    )
    return source_modified_at_time >= minimum_source_modified_at_time


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query_text: str
    top_k: int = 10
    filters: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_string("search request", "query_text", self.query_text)
        if not isinstance(self.top_k, int) or isinstance(self.top_k, bool) or self.top_k < 0:
            raise ValueError("search request top_k must be a non-negative integer")
        object.__setattr__(self, "filters", _copy_metadata("search request", self.filters, field_name="filters"))
        object.__setattr__(self, "metadata", _copy_metadata("search request", self.metadata))


@dataclass(frozen=True, slots=True)
class QueryPlan:
    original: str
    rewritten: list[str]
    subqueries: list[str] = field(default_factory=list)
    filters: dict[str, object] | None = None
    rationale_summary: str | None = None

    def __post_init__(self) -> None:
        _validate_string("query plan", "original", self.original)
        object.__setattr__(self, "rewritten", _copy_string_list("query plan", "rewritten", self.rewritten))
        object.__setattr__(self, "subqueries", _copy_string_list("query plan", "subqueries", self.subqueries))
        object.__setattr__(
            self,
            "filters",
            None if self.filters is None else _copy_metadata("query plan", self.filters, field_name="filters"),
        )
        object.__setattr__(
            self,
            "rationale_summary",
            _validate_optional_non_empty_string("query plan", "rationale_summary", self.rationale_summary),
        )


@dataclass(frozen=True, slots=True)
class AuthContext:
    tenant_id: str
    principal_id: str
    groups: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    attributes: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("auth context", "tenant_id", self.tenant_id)
        _validate_non_empty_string("auth context", "principal_id", self.principal_id)
        object.__setattr__(self, "groups", set(_copy_string_list("auth context", "groups", self.groups)))
        object.__setattr__(self, "roles", set(_copy_string_list("auth context", "roles", self.roles)))
        object.__setattr__(self, "attributes", _copy_metadata("auth context attributes", self.attributes))


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    retrieval_id: str
    request: SearchRequest
    hits: list[SearchHit]
    total_candidates: int | None = None
    latency_ms: float | None = None
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("retrieval result", "retrieval_id", self.retrieval_id)
        if not isinstance(self.request, SearchRequest):
            raise ValueError("retrieval result request must be a SearchRequest")
        hits = _copy_typed_list("retrieval result", "hits", self.hits, SearchHit)
        hit_ids = [hit.hit_id for hit in hits]
        if len(hit_ids) != len(set(hit_ids)):
            raise ValueError("retrieval result hit_id values must be unique")
        object.__setattr__(self, "hits", hits)
        object.__setattr__(
            self,
            "total_candidates",
            _validate_non_negative_int("retrieval result", "total_candidates", self.total_candidates),
        )
        if self.total_candidates is not None and self.total_candidates < len(hits):
            raise ValueError(
                "retrieval result total_candidates must not be less than hits length"
            )
        latency_ms = _validate_optional_finite_float("retrieval result", "latency_ms", self.latency_ms)
        if latency_ms is not None and latency_ms < 0:
            raise ValueError("retrieval result latency_ms must be non-negative")
        object.__setattr__(self, "latency_ms", latency_ms)
        object.__setattr__(self, "warnings", _copy_string_list("retrieval result", "warnings", self.warnings))
        object.__setattr__(self, "metadata", _copy_metadata("retrieval result", self.metadata))


class FederatedRetrievalError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FederatedRetrievalSource:
    source_id: str
    result: RetrievalResult | None = None
    error: str | None = None
    weight: float = 1.0
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("federated retrieval source", "source_id", self.source_id)
        if self.result is not None and not isinstance(self.result, RetrievalResult):
            raise ValueError("federated retrieval source result must be a RetrievalResult")
        _validate_optional_non_empty_string("federated retrieval source", "error", self.error)
        weight = _validate_optional_finite_float("federated retrieval source", "weight", self.weight)
        if weight is None or weight <= 0:
            raise ValueError("federated retrieval source weight must be positive")
        object.__setattr__(self, "weight", weight)
        object.__setattr__(self, "metadata", _copy_metadata("federated retrieval source", self.metadata))


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

    def __post_init__(self) -> None:
        _validate_non_empty_string("knowledge item ref", "item_id", self.item_id)
        _validate_non_empty_string("knowledge item ref", "item_kind", self.item_kind)
        if not isinstance(self.source, SourceRef):
            raise ValueError("knowledge item ref source must be a SourceRef")
        object.__setattr__(
            self,
            "schema_ref",
            _validate_optional_non_empty_string("knowledge item ref", "schema_ref", self.schema_ref),
        )
        object.__setattr__(
            self,
            "payload_ref",
            _validate_optional_non_empty_string("knowledge item ref", "payload_ref", self.payload_ref),
        )
        object.__setattr__(self, "preview", _copy_string_list("knowledge item ref", "preview", self.preview))
        object.__setattr__(
            self,
            "acl",
            None if self.acl is None else _copy_metadata("knowledge item ref acl", self.acl),
        )
        object.__setattr__(self, "metadata", _copy_metadata("knowledge item ref", self.metadata))


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

    def __post_init__(self) -> None:
        _validate_non_empty_string("search hit", "hit_id", self.hit_id)
        if not isinstance(self.item, KnowledgeItemRef):
            raise ValueError("search hit item must be a KnowledgeItemRef")
        object.__setattr__(self, "rank", _validate_positive_int("search hit", "rank", self.rank))
        _validate_non_empty_string("search hit", "retriever", self.retriever)
        object.__setattr__(
            self,
            "raw_score",
            _validate_optional_finite_float("search hit", "raw_score", self.raw_score),
        )
        normalized_score = _validate_optional_finite_float("search hit", "normalized_score", self.normalized_score)
        if normalized_score is not None and not 0 <= normalized_score <= 1:
            raise ValueError("search hit normalized_score must be between 0 and 1")
        object.__setattr__(self, "normalized_score", normalized_score)
        object.__setattr__(
            self,
            "score_kind",
            _validate_optional_non_empty_string("search hit", "score_kind", self.score_kind),
        )
        object.__setattr__(
            self,
            "highlights",
            _copy_typed_list("search hit", "highlights", self.highlights, SourceRef),
        )
        object.__setattr__(self, "metadata", _copy_metadata("search hit", self.metadata))


@dataclass(frozen=True, slots=True)
class ContextPack:
    context_id: str
    hits: list[SearchHit]
    token_budget: int | None = None
    token_count: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("context pack", "context_id", self.context_id)
        hits = _copy_typed_list("context pack", "hits", self.hits, SearchHit)
        hit_ids = [hit.hit_id for hit in hits]
        if len(hit_ids) != len(set(hit_ids)):
            raise ValueError("context pack hit_id values must be unique")
        object.__setattr__(self, "hits", hits)
        token_budget = _validate_non_negative_int("context pack", "token_budget", self.token_budget)
        token_count = _validate_non_negative_int("context pack", "token_count", self.token_count)
        if token_budget is not None and token_count is not None and token_count > token_budget:
            raise ValueError("context pack token_count must not exceed token_budget")
        object.__setattr__(self, "token_budget", token_budget)
        object.__setattr__(self, "token_count", token_count)
        object.__setattr__(self, "metadata", _copy_metadata("context pack", self.metadata))


@dataclass(frozen=True, slots=True)
class Citation:
    citation_id: str
    source: SourceRef
    claim_id: str | None = None
    cited_text: str | None = None
    confidence: float | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("citation", "citation_id", self.citation_id)
        if not isinstance(self.source, SourceRef):
            raise ValueError("citation source must be a SourceRef")
        object.__setattr__(
            self,
            "claim_id",
            _validate_optional_non_empty_string("citation", "claim_id", self.claim_id),
        )
        object.__setattr__(
            self,
            "cited_text",
            _validate_optional_non_empty_string("citation", "cited_text", self.cited_text),
        )
        confidence = _validate_optional_finite_float("citation", "confidence", self.confidence)
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("citation confidence must be between 0 and 1")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "metadata", _copy_metadata("citation", self.metadata))


@dataclass(frozen=True, slots=True)
class Claim:
    claim_id: str
    text: str
    citation_ids: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("claim", "claim_id", self.claim_id)
        _validate_string("claim", "text", self.text)
        object.__setattr__(self, "citation_ids", _copy_string_list("claim", "citation_ids", self.citation_ids))
        object.__setattr__(self, "metadata", _copy_metadata("claim", self.metadata))


@dataclass(frozen=True, slots=True)
class Abstention:
    reason: str
    user_message: str
    diagnostics: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("abstention", "reason", self.reason)
        _validate_non_empty_string("abstention", "user_message", self.user_message)
        object.__setattr__(self, "diagnostics", _copy_metadata("abstention diagnostics", self.diagnostics))


@dataclass(frozen=True, slots=True)
class Answer:
    answer_id: str
    text: str
    claims: list[Claim] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    abstention: Abstention | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("answer", "answer_id", self.answer_id)
        _validate_string("answer", "text", self.text)
        object.__setattr__(self, "claims", _copy_typed_list("answer", "claims", self.claims, Claim))
        object.__setattr__(
            self,
            "citations",
            _copy_typed_list("answer", "citations", self.citations, Citation),
        )
        if self.abstention is not None and not isinstance(self.abstention, Abstention):
            raise ValueError("answer abstention must be an Abstention")
        object.__setattr__(self, "metadata", _copy_metadata("answer", self.metadata))


@dataclass(frozen=True, slots=True)
class RagResultPayload:
    query_plan: QueryPlan
    retrievals: list[RetrievalResult]
    context: ContextPack
    model_response: dict[str, object]
    answer: Answer

    def __post_init__(self) -> None:
        if not isinstance(self.query_plan, QueryPlan):
            raise ValueError("rag result payload query_plan must be a QueryPlan")
        retrievals = _copy_typed_list(
            "rag result payload",
            "retrievals",
            self.retrievals,
            RetrievalResult,
        )
        retrieval_ids = [retrieval.retrieval_id for retrieval in retrievals]
        if len(retrieval_ids) != len(set(retrieval_ids)):
            raise ValueError(
                "rag result payload retrieval_id values must be unique"
            )
        object.__setattr__(self, "retrievals", retrievals)
        if not isinstance(self.context, ContextPack):
            raise ValueError("rag result payload context must be a ContextPack")
        object.__setattr__(
            self,
            "model_response",
            _copy_metadata("rag result payload", self.model_response, field_name="model_response"),
        )
        if not isinstance(self.answer, Answer):
            raise ValueError("rag result payload answer must be an Answer")


@dataclass(frozen=True, slots=True)
class RagResultBundle:
    base: ResultBundle
    payload: RagResultPayload
    profile: Literal["rag"] = "rag"

    def __post_init__(self) -> None:
        if not isinstance(self.base, ResultBundle):
            raise ValueError("rag result bundle base must be a ResultBundle")
        if not isinstance(self.payload, RagResultPayload):
            raise ValueError("rag result bundle payload must be a RagResultPayload")
        if self.profile != "rag":
            raise ValueError("rag result bundle profile must be rag")


@dataclass(frozen=True, slots=True)
class CitationValidationIssue:
    code: str
    message: str
    citation_id: str | None = None
    claim_id: str | None = None
    severity: Literal["warning", "error"] = "error"
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("citation validation issue", "code", self.code)
        _validate_non_empty_string("citation validation issue", "message", self.message)
        object.__setattr__(
            self,
            "citation_id",
            _validate_optional_non_empty_string("citation validation issue", "citation_id", self.citation_id),
        )
        object.__setattr__(
            self,
            "claim_id",
            _validate_optional_non_empty_string("citation validation issue", "claim_id", self.claim_id),
        )
        if self.severity not in {"warning", "error"}:
            raise ValueError("citation validation issue severity must be warning or error")
        object.__setattr__(self, "metadata", _copy_metadata("citation validation issue", self.metadata))


@dataclass(frozen=True, slots=True)
class CitationValidationResult:
    ok: bool
    issues: list[CitationValidationIssue] = field(default_factory=list)
    abstention: Abstention | None = None
    repaired_answer: Answer | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise ValueError("citation validation result ok must be a boolean")
        object.__setattr__(
            self,
            "issues",
            _copy_typed_list("citation validation result", "issues", self.issues, CitationValidationIssue),
        )
        if self.abstention is not None and not isinstance(self.abstention, Abstention):
            raise ValueError("citation validation result abstention must be an Abstention")
        if self.repaired_answer is not None and not isinstance(self.repaired_answer, Answer):
            raise ValueError("citation validation result repaired_answer must be an Answer")


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
    rank: int = 1
    raw_score: float | None = None
    normalized_score: float | None = None
    score_kind: str | None = None
    acl: dict[str, object] | None = None
    element_ids: list[str] = field(default_factory=list)
    hit_metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("citation_id", "context_id", "hit_id", "retriever", "item_id", "item_kind"):
            _validate_non_empty_string("citation source trace", field_name, getattr(self, field_name))
        object.__setattr__(self, "rank", _validate_positive_int("citation source trace", "rank", self.rank))
        object.__setattr__(
            self,
            "claim_id",
            _validate_optional_non_empty_string("citation source trace", "claim_id", self.claim_id),
        )
        if not isinstance(self.source, SourceRef):
            raise ValueError("citation source trace source must be a SourceRef")
        if self.locator is not None and not isinstance(self.locator, DocumentSpan):
            raise ValueError("citation source trace locator must be a DocumentSpan")
        object.__setattr__(
            self,
            "raw_score",
            _validate_optional_finite_float("citation source trace", "raw_score", self.raw_score),
        )
        normalized_score = _validate_optional_finite_float(
            "citation source trace",
            "normalized_score",
            self.normalized_score,
        )
        if normalized_score is not None and not 0 <= normalized_score <= 1:
            raise ValueError("citation source trace normalized_score must be between 0 and 1")
        object.__setattr__(self, "normalized_score", normalized_score)
        object.__setattr__(
            self,
            "score_kind",
            _validate_optional_non_empty_string("citation source trace", "score_kind", self.score_kind),
        )
        object.__setattr__(
            self,
            "acl",
            None if self.acl is None else _copy_metadata("citation source trace acl", self.acl),
        )
        object.__setattr__(
            self,
            "element_ids",
            _copy_string_list("citation source trace", "element_ids", self.element_ids),
        )
        object.__setattr__(
            self,
            "hit_metadata",
            _copy_metadata("citation source trace hit_metadata", self.hit_metadata),
        )


@dataclass(frozen=True, slots=True)
class RankedHit:
    hit: SearchHit
    rerank_score: float | None = None
    reranker: str | None = None
    explanation: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.hit, SearchHit):
            raise ValueError("ranked hit hit must be a SearchHit")
        object.__setattr__(
            self,
            "rerank_score",
            _validate_optional_finite_float("ranked hit", "rerank_score", self.rerank_score),
        )
        object.__setattr__(
            self,
            "reranker",
            _validate_optional_non_empty_string("ranked hit", "reranker", self.reranker),
        )
        object.__setattr__(
            self,
            "explanation",
            _validate_optional_non_empty_string("ranked hit", "explanation", self.explanation),
        )
        object.__setattr__(self, "metadata", _copy_metadata("ranked hit", self.metadata))


@dataclass(frozen=True, slots=True)
class RerankResult:
    ranked_hits: list[RankedHit]
    reranker: str
    input_count: int
    evaluated_count: int
    truncated_hit_ids: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ranked_hits",
            _copy_typed_list("rerank result", "ranked_hits", self.ranked_hits, RankedHit),
        )
        _validate_non_empty_string("rerank result", "reranker", self.reranker)
        input_count = _validate_non_negative_int("rerank result", "input_count", self.input_count)
        evaluated_count = _validate_non_negative_int("rerank result", "evaluated_count", self.evaluated_count)
        assert input_count is not None
        assert evaluated_count is not None
        if evaluated_count > input_count:
            raise ValueError("rerank result evaluated_count must not exceed input_count")
        if len(self.ranked_hits) > evaluated_count:
            raise ValueError("rerank result ranked_hits must not exceed evaluated_count")
        object.__setattr__(self, "input_count", input_count)
        object.__setattr__(self, "evaluated_count", evaluated_count)
        object.__setattr__(
            self,
            "truncated_hit_ids",
            _copy_string_list("rerank result", "truncated_hit_ids", self.truncated_hit_ids),
        )
        object.__setattr__(self, "metadata", _copy_metadata("rerank result", self.metadata))


class KnowledgeIndexError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KnowledgeIndexRecord:
    chunk: DocumentChunk
    status: KnowledgeRecordStatus

    def __post_init__(self) -> None:
        if not isinstance(self.chunk, DocumentChunk):
            raise ValueError("knowledge index record chunk must be a DocumentChunk")
        if self.status not in {"active", "tombstoned"}:
            raise ValueError("knowledge index record status must be active or tombstoned")


@dataclass(frozen=True, slots=True)
class KnowledgeWriteReport:
    operation: str
    affected_count: int
    chunk_ids: list[str]
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("knowledge write report", "operation", self.operation)
        affected_count = _validate_non_negative_int("knowledge write report", "affected_count", self.affected_count)
        assert affected_count is not None
        object.__setattr__(self, "affected_count", affected_count)
        chunk_ids = _copy_string_list("knowledge write report", "chunk_ids", self.chunk_ids)
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("knowledge write report chunk_ids must be unique")
        if affected_count != len(chunk_ids):
            raise ValueError("knowledge write report affected_count must match chunk_ids length")
        object.__setattr__(self, "chunk_ids", chunk_ids)
        object.__setattr__(self, "metadata", _copy_metadata("knowledge write report", self.metadata))


@dataclass(frozen=True, slots=True)
class KnowledgePublishResult:
    index_id: str
    asset_id: str
    revision_id: str
    published_chunk_ids: list[str]
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("index_id", "asset_id", "revision_id"):
            _validate_non_empty_string("knowledge publish result", field_name, getattr(self, field_name))
        published_chunk_ids = _copy_string_list(
            "knowledge publish result",
            "published_chunk_ids",
            self.published_chunk_ids,
        )
        if len(published_chunk_ids) != len(set(published_chunk_ids)):
            raise ValueError(
                "knowledge publish result published_chunk_ids must be unique"
            )
        object.__setattr__(self, "published_chunk_ids", published_chunk_ids)
        object.__setattr__(self, "metadata", _copy_metadata("knowledge publish result", self.metadata))


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

    def __post_init__(self) -> None:
        for field_name in (
            "upsert",
            "delete",
            "metadata_update",
            "acl_update",
            "publish",
            "hard_delete",
            "tombstone",
            "retriever_adapter",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"knowledge index capabilities {field_name} must be a boolean")


@dataclass(frozen=True, slots=True)
class KnowledgeIndexHealth:
    healthy: bool
    indexed_chunks: int
    active_chunks: int
    tombstoned_chunks: int
    published_revisions: int

    def __post_init__(self) -> None:
        if not isinstance(self.healthy, bool):
            raise ValueError("knowledge index health healthy must be a boolean")
        for field_name in ("indexed_chunks", "active_chunks", "tombstoned_chunks", "published_revisions"):
            value = _validate_non_negative_int("knowledge index health", field_name, getattr(self, field_name))
            assert value is not None
            object.__setattr__(self, field_name, value)
        if self.active_chunks + self.tombstoned_chunks > self.indexed_chunks:
            raise ValueError("knowledge index health active and tombstoned chunks must not exceed indexed_chunks")


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
    validated_token_budget = _validate_non_negative_int(
        "context builder",
        "token_budget",
        token_budget,
    )
    validated_reserve_output_tokens = _validate_non_negative_int(
        "context builder",
        "reserve_output_tokens",
        reserve_output_tokens,
    )
    assert validated_token_budget is not None
    assert validated_reserve_output_tokens is not None
    token_budget = validated_token_budget
    reserve_output_tokens = validated_reserve_output_tokens
    if per_document_max_chunks is not None:
        per_document_max_chunks = _validate_positive_int(
            "context builder",
            "per_document_max_chunks",
            per_document_max_chunks,
        )
    if per_section_max_chunks is not None:
        per_section_max_chunks = _validate_positive_int(
            "context builder",
            "per_section_max_chunks",
            per_section_max_chunks,
        )
    if per_source_max_chunks is not None:
        per_source_max_chunks = _validate_positive_int(
            "context builder",
            "per_source_max_chunks",
            per_source_max_chunks,
        )
    if not isinstance(deduplicate, bool):
        raise ValueError("context builder deduplicate must be a boolean")

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
            if not _source_modified_at_satisfies(source_modified_at, minimum_source_modified_at):
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


def build_answer_from_model_response(
    answer_id: str,
    model_response: dict[str, object],
    *,
    context: ContextPack | None = None,
) -> Answer:
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
            if not isinstance(raw_citation_ids, list) or any(
                not isinstance(item, str) for item in raw_citation_ids
            ):
                raise ValueError(
                    "model_response claim citation_ids must be a list of strings"
                )
            claims.append(
                Claim(
                    claim_id=claim_id,
                    text=claim_text,
                    citation_ids=list(raw_citation_ids),
                )
            )

    citations: list[Citation] = []
    raw_citations = model_response.get("citations", [])
    if raw_citations is not None and not isinstance(raw_citations, list):
        raise ValueError("model_response citations must be a list when present")
    if isinstance(raw_citations, list) and raw_citations:
        if context is None:
            raise ValueError("model_response citations require context for source resolution")
        for raw_citation in raw_citations:
            if not isinstance(raw_citation, dict):
                raise ValueError("model_response citations must contain mapping items")
            citation_id = raw_citation.get("citation_id")
            source_id = raw_citation.get("source_id")
            if not isinstance(citation_id, str) or not isinstance(source_id, str):
                raise ValueError(
                    "model_response citations must contain string citation_id and source_id"
                )
            source = None
            for hit in context.hits:
                for source_ref in [hit.item.source, *hit.highlights]:
                    if source_ref.source_id == source_id:
                        source = source_ref
                        break
                if source is not None:
                    break
            if source is None:
                raise ValueError(f"citation source {source_id!r} was not found in context")
            claim_id = raw_citation.get("claim_id")
            cited_text = raw_citation.get("cited_text")
            confidence = raw_citation.get("confidence")
            if claim_id is not None and not isinstance(claim_id, str):
                raise ValueError(
                    "model_response citation claim_id must be a string when present"
                )
            if cited_text is not None and not isinstance(cited_text, str):
                raise ValueError(
                    "model_response citation cited_text must be a string when present"
                )
            if confidence is not None and (
                isinstance(confidence, bool)
                or not isinstance(confidence, int | float)
            ):
                raise ValueError(
                    "model_response citation confidence must be a number when present"
                )
            validated_confidence = _validate_optional_finite_float(
                "model_response citation",
                "confidence",
                confidence,
            )
            citations.append(
                Citation(
                    citation_id=citation_id,
                    source=source,
                    claim_id=claim_id,
                    cited_text=cited_text,
                    confidence=validated_confidence,
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
    return Answer(
        answer_id=answer_id,
        text=text,
        claims=claims,
        citations=citations,
        metadata=metadata,
    )


def build_abstention_answer(
    answer_id: str,
    reason: str,
    user_message: str,
    *,
    diagnostics: dict[str, object] | None = None,
) -> Answer:
    return Answer(
        answer_id=answer_id,
        text=user_message,
        abstention=Abstention(
            reason=reason,
            user_message=user_message,
            diagnostics=dict(diagnostics or {}),
        ),
        metadata={"answer_kind": "abstention"},
    )


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
    k = _validate_positive_int("search hit fusion", "k", k)
    if weights is not None:
        if len(weights) != len(hit_sets):
            raise ValueError("weights length must match hit_sets length")
        normalized_weights: list[float] = []
        for weight in weights:
            normalized_weight = _validate_optional_finite_float(
                "search hit fusion",
                "weight",
                weight,
            )
            if normalized_weight is None or normalized_weight <= 0:
                raise ValueError("search hit fusion weight must be positive")
            normalized_weights.append(normalized_weight)
        weights = normalized_weights

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
                try:
                    score_increment = weight / (k + max(hit.rank, 1))
                except OverflowError:
                    score_increment = 0.0
                fusion_scores[dedupe_key] = (
                    fusion_scores.get(dedupe_key, 0.0)
                    + score_increment
                )
            elif strategy == "weighted_rank":
                try:
                    score_increment = weight / max(hit.rank, 1)
                except OverflowError:
                    score_increment = 0.0
                fusion_scores[dedupe_key] = (
                    fusion_scores.get(dedupe_key, 0.0) + score_increment
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
    item_id_counts: dict[str, int] = {}
    for dedupe_key in ordered_keys:
        item_id = grouped_hits[dedupe_key][0].item.item_id
        item_id_counts[item_id] = item_id_counts.get(item_id, 0) + 1
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
        fused_hit_id = f"{retriever_id}:{representative.item.item_id}"
        if item_id_counts[representative.item.item_id] > 1:
            fused_hit_id += f":{canonical_hash(dedupe_key)}"
        fused_hits.append(
            SearchHit(
                hit_id=fused_hit_id,
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
    if input_limit is not None:
        input_limit = _validate_positive_int(
            "rerank",
            "input_limit",
            input_limit,
        )

    normalized_terms = [
        term.lower()
        for term in _copy_string_list("rerank", "query_terms", query_terms)
    ]
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
                    rank=hit.rank,
                    source=source_ref,
                    locator=source_ref.locator,
                    raw_score=hit.raw_score,
                    normalized_score=hit.normalized_score,
                    score_kind=hit.score_kind,
                    acl=hit.item.acl,
                    element_ids=element_ids,
                    hit_metadata=hit.metadata,
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


def evaluate_rag_answer_metrics(
    answer: Answer,
    validation: CitationValidationResult,
) -> list[MetricObservation]:
    if not isinstance(answer, Answer):
        raise ValueError("rag answer metrics answer must be an Answer")
    if not isinstance(validation, CitationValidationResult):
        raise ValueError("rag answer metrics validation must be a CitationValidationResult")
    citation_ids = {citation.citation_id for citation in answer.citations}
    invalid_citation_ids = {
        issue.citation_id
        for issue in validation.issues
        if issue.severity == "error"
        and issue.citation_id is not None
        and issue.citation_id in citation_ids
    }
    citation_precision = (
        None
        if not answer.citations
        else Decimal(len(answer.citations) - len(invalid_citation_ids))
        / Decimal(len(answer.citations))
    )
    source_inaccurate_citation_ids = {
        issue.citation_id
        for issue in validation.issues
        if issue.severity == "error"
        and issue.code == "citation.source_not_in_context"
        and issue.citation_id is not None
        and issue.citation_id in citation_ids
    }
    citation_source_accuracy = (
        None
        if not answer.citations
        else Decimal(len(answer.citations) - len(source_inaccurate_citation_ids))
        / Decimal(len(answer.citations))
    )

    claim_ids = {claim.claim_id for claim in answer.claims}
    unsupported_claim_ids = {
        issue.claim_id
        for issue in validation.issues
        if issue.severity == "error"
        and issue.code in {"claim.unsupported_by_citation", "claim.missing_citation"}
        and issue.claim_id is not None
        and issue.claim_id in claim_ids
    }
    citation_recall = (
        None
        if not answer.claims
        else Decimal(len(answer.claims) - len(unsupported_claim_ids))
        / Decimal(len(answer.claims))
    )
    faithfulness = citation_recall
    unsupported_claim_rate = (
        None
        if not answer.claims
        else Decimal(len(unsupported_claim_ids)) / Decimal(len(answer.claims))
    )
    raw_answer_relevance = answer.metadata.get(
        "answer_relevance",
        answer.metadata.get("answer_relevance_score"),
    )
    answer_relevance = (
        Decimal(str(raw_answer_relevance))
        if not isinstance(raw_answer_relevance, bool)
        and isinstance(raw_answer_relevance, int | float | Decimal)
        and (
            not isinstance(raw_answer_relevance, float)
            or math.isfinite(raw_answer_relevance)
        )
        else None
    )
    expected_abstention = answer.metadata.get(
        "expected_abstention",
        answer.metadata.get("should_abstain"),
    )
    actual_abstention = answer.abstention is not None
    if isinstance(expected_abstention, bool):
        abstention_precision = (
            Decimal(1 if expected_abstention else 0)
            if actual_abstention
            else None
        )
        abstention_recall = (
            Decimal(1 if actual_abstention else 0)
            if expected_abstention
            else None
        )
    else:
        abstention_precision = None
        abstention_recall = None

    return [
        MetricObservation(
            "citation_precision",
            citation_precision,
            direction="maximize",
        ),
        MetricObservation(
            "citation_recall",
            citation_recall,
            direction="maximize",
        ),
        MetricObservation(
            "citation_source_accuracy",
            citation_source_accuracy,
            direction="maximize",
        ),
        MetricObservation(
            "answer_relevance",
            answer_relevance,
            direction="maximize",
        ),
        MetricObservation(
            "faithfulness",
            faithfulness,
            direction="maximize",
        ),
        MetricObservation(
            "abstention_precision",
            abstention_precision,
            direction="maximize",
        ),
        MetricObservation(
            "abstention_recall",
            abstention_recall,
            direction="maximize",
        ),
        MetricObservation(
            "unsupported_claim_rate",
            unsupported_claim_rate,
            direction="minimize",
        ),
    ]


def evaluate_retrieval_metrics(
    retrieval: RetrievalResult,
    relevant_item_ids: Iterable[str],
    *,
    k: int | None = None,
    auth: AuthContext | None = None,
) -> list[MetricObservation]:
    if not isinstance(retrieval, RetrievalResult):
        raise ValueError("retrieval metrics retrieval must be a RetrievalResult")
    relevant = set(_copy_string_list("retrieval metrics", "relevant_item_ids", relevant_item_ids))
    if k is not None:
        k = _validate_non_negative_int("retrieval metrics", "k", k)
    if auth is not None and not isinstance(auth, AuthContext):
        raise ValueError("retrieval metrics auth must be an AuthContext")
    cutoff = retrieval.request.top_k if k is None else k
    hits_at_k = retrieval.hits[:cutoff]
    relevant_hits_at_k = sum(1 for hit in hits_at_k if hit.item.item_id in relevant)
    if not relevant:
        recall = None
        precision = None
        average_precision = None
        ndcg = None
        mrr = None
    else:
        recall = Decimal(relevant_hits_at_k) / Decimal(len(relevant))
        precision = None if cutoff == 0 else Decimal(relevant_hits_at_k) / Decimal(cutoff)
        relevant_seen = 0
        precision_sum = Decimal(0)
        for index, hit in enumerate(hits_at_k, start=1):
            if hit.item.item_id in relevant:
                relevant_seen += 1
                precision_sum += Decimal(relevant_seen) / Decimal(index)
        average_precision = precision_sum / Decimal(len(relevant))
        dcg = sum(
            1.0 / math.log2(index + 1)
            for index, hit in enumerate(hits_at_k, start=1)
            if hit.item.item_id in relevant
        )
        idcg = sum(
            1.0 / math.log2(index + 1)
            for index in range(1, min(len(relevant), cutoff) + 1)
        )
        ndcg = None if idcg == 0.0 else dcg / idcg
        first_relevant_rank = next(
            (
                index + 1
                for index, hit in enumerate(hits_at_k)
                if hit.item.item_id in relevant
            ),
            None,
        )
        mrr = (
            Decimal(0)
            if first_relevant_rank is None
            else Decimal(1) / Decimal(first_relevant_rank)
        )
    coverage = None if cutoff == 0 else Decimal(len(hits_at_k)) / Decimal(cutoff)
    acl_precision = (
        None
        if auth is None or not hits_at_k
        else Decimal(
            sum(1 for hit in hits_at_k if _acl_allows(hit.hit_id, hit.item.acl, auth))
        )
        / Decimal(len(hits_at_k))
    )
    minimum_source_modified_at = retrieval.metadata.get("minimum_source_modified_at")
    if isinstance(minimum_source_modified_at, str) and hits_at_k:
        fresh_hits = 0
        for hit in hits_at_k:
            source_modified_at = hit.metadata.get("source_modified_at")
            if not isinstance(source_modified_at, str):
                source_modified_at = hit.item.metadata.get("source_modified_at")
            if _source_modified_at_satisfies(source_modified_at, minimum_source_modified_at):
                fresh_hits += 1
        freshness_satisfaction = Decimal(fresh_hits) / Decimal(len(hits_at_k))
    else:
        freshness_satisfaction = None

    evaluator = {"k": cutoff}
    return [
        MetricObservation(
            "recall_at_k",
            recall,
            direction="maximize",
            evaluator=evaluator,
        ),
        MetricObservation(
            "precision_at_k",
            precision,
            direction="maximize",
            evaluator=evaluator,
        ),
        MetricObservation(
            "average_precision_at_k",
            average_precision,
            direction="maximize",
            evaluator=evaluator,
        ),
        MetricObservation(
            "ndcg_at_k",
            ndcg,
            direction="maximize",
            evaluator=evaluator,
        ),
        MetricObservation(
            "mrr",
            mrr,
            direction="maximize",
            evaluator=evaluator,
        ),
        MetricObservation(
            "coverage_at_k",
            coverage,
            direction="maximize",
            evaluator=evaluator,
        ),
        MetricObservation(
            "acl_precision",
            acl_precision,
            direction="maximize",
            evaluator=evaluator,
        ),
        MetricObservation(
            "freshness_satisfaction",
            freshness_satisfaction,
            direction="maximize",
            evaluator=evaluator,
        ),
    ]


def evaluate_context_metrics(
    context: ContextPack,
    relevant_item_ids: Iterable[str] | None = None,
) -> list[MetricObservation]:
    if not isinstance(context, ContextPack):
        raise ValueError("context metrics context must be a ContextPack")
    source_diversity = len({hit.retriever for hit in context.hits})
    token_efficiency = (
        None
        if context.token_count is None
        or context.token_budget is None
        or context.token_budget <= 0
        else Decimal(context.token_count) / Decimal(context.token_budget)
    )
    if relevant_item_ids is None:
        context_precision = None
    else:
        relevant_item_id_set = set(_copy_string_list("context metrics", "relevant_item_ids", relevant_item_ids))
        if not relevant_item_id_set or not context.hits:
            context_precision = None
        else:
            relevant_hits = sum(1 for hit in context.hits if hit.item.item_id in relevant_item_id_set)
            context_precision = Decimal(relevant_hits) / Decimal(len(context.hits))
    normalized_scores = [
        Decimal(str(hit.normalized_score))
        for hit in context.hits
        if hit.normalized_score is not None and math.isfinite(hit.normalized_score)
    ]
    context_relevance = (
        None
        if not normalized_scores
        else sum(normalized_scores, Decimal(0)) / Decimal(len(normalized_scores))
    )
    if "minimum_source_modified_at" not in context.metadata:
        freshness_satisfaction = None
    else:
        drop_reasons = context.metadata.get("drop_reasons")
        freshness_drops = (
            sum(1 for reason in drop_reasons.values() if reason == "freshness")
            if isinstance(drop_reasons, dict)
            else 0
        )
        freshness_denominator = len(context.hits) + freshness_drops
        freshness_satisfaction = (
            None
            if freshness_denominator == 0
            else Decimal(len(context.hits)) / Decimal(freshness_denominator)
        )
    raw_middle_sensitivity = context.metadata.get(
        "lost_in_the_middle_sensitivity",
        context.metadata.get("lost_in_middle_sensitivity"),
    )
    middle_sensitivity = (
        Decimal(str(raw_middle_sensitivity))
        if not isinstance(raw_middle_sensitivity, bool)
        and isinstance(raw_middle_sensitivity, int | float | Decimal)
        and (
            not isinstance(raw_middle_sensitivity, float)
            or math.isfinite(raw_middle_sensitivity)
        )
        else None
    )

    return [
        MetricObservation(
            "source_diversity",
            Decimal(source_diversity),
            unit="sources",
            direction="maximize",
        ),
        MetricObservation(
            "context_token_efficiency",
            token_efficiency,
            direction="maximize",
        ),
        MetricObservation(
            "context_precision",
            context_precision,
            direction="maximize",
        ),
        MetricObservation(
            "context_relevance",
            context_relevance,
            direction="maximize",
        ),
        MetricObservation(
            "freshness_satisfaction",
            freshness_satisfaction,
            direction="maximize",
        ),
        MetricObservation(
            "lost_in_the_middle_sensitivity",
            middle_sensitivity,
            direction="minimize",
        ),
    ]


def validate_answer_citations(
    answer: Answer,
    context: ContextPack,
    *,
    require_citations: bool = True,
    failure_policy: Literal["warn", "fail", "abstain", "repair", "remove_invalid"] = "fail",
) -> CitationValidationResult:
    if failure_policy not in {"warn", "fail", "abstain", "repair", "remove_invalid"}:
        raise ValueError("failure_policy must be one of warn, fail, abstain, repair, or remove_invalid")

    severity: Literal["warning", "error"] = "warning" if failure_policy == "warn" else "error"
    issues: list[CitationValidationIssue] = []
    citations_by_id: dict[str, Citation] = {}
    claim_ids = {claim.claim_id for claim in answer.claims}
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
        if citation.claim_id is not None and citation.claim_id not in claim_ids:
            issues.append(
                CitationValidationIssue(
                    code="citation.claim_missing",
                    message=(
                        f"citation {citation.citation_id!r} references missing "
                        f"claim {citation.claim_id!r}"
                    ),
                    citation_id=citation.citation_id,
                    claim_id=citation.claim_id,
                    severity=severity,
                )
            )

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
            normalized_claim_text = " ".join(claim.text.split()).lower()
            if normalized_claim_text:
                matching_texts = [
                    text
                    for source_ref, text in context_source_texts
                    if _source_ref_matches(citation.source, source_ref)
                ]
                if matching_texts and not any(
                    normalized_claim_text in " ".join(text.split()).lower()
                    for text in matching_texts
                ):
                    issues.append(
                        CitationValidationIssue(
                            code="claim.unsupported_by_citation",
                            message=(
                                f"claim {claim.claim_id!r} is not supported by "
                                f"citation {citation.citation_id!r}"
                            ),
                            citation_id=citation.citation_id,
                            claim_id=claim.claim_id,
                            severity=severity,
                        )
                    )

    for citation in answer.citations:
        matching_sources = [
            (source_ref, text)
            for source_ref, text in context_source_texts
            if _source_ref_matches(citation.source, source_ref)
        ]
        if not matching_sources:
            issues.append(
                CitationValidationIssue(
                    code="citation.source_not_in_context",
                    message=f"citation {citation.citation_id!r} does not point to the current context",
                    citation_id=citation.citation_id,
                    severity=severity,
                )
            )
            continue
        if citation.source.locator is None and any(
            source_ref.locator is None for source_ref, _ in matching_sources
        ):
            issues.append(
                CitationValidationIssue(
                    code="citation.precision_limited",
                    message=(
                        f"citation {citation.citation_id!r} has no page, cell, "
                        "slide, or span locator"
                    ),
                    citation_id=citation.citation_id,
                    severity="warning",
                )
            )
        if citation.cited_text is not None:
            quoted_text = " ".join(citation.cited_text.split()).lower()
            if quoted_text and not any(
                quoted_text in " ".join(text.split()).lower()
                for _, text in matching_sources
            ):
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
    if not any(issue.severity == "error" for issue in issues):
        return CitationValidationResult(ok=True, issues=issues)
    if failure_policy == "warn":
        return CitationValidationResult(ok=True, issues=issues)
    if failure_policy in {"repair", "remove_invalid"}:
        invalid_citation_ids = {
            issue.citation_id
            for issue in issues
            if issue.severity == "error" and issue.citation_id is not None
        }
        repaired_citations = [
            citation
            for citation in answer.citations
            if citation.citation_id not in invalid_citation_ids
        ]
        remaining_citation_ids = {citation.citation_id for citation in repaired_citations}
        repaired_claims = [
            replace(
                claim,
                citation_ids=[
                    citation_id
                    for citation_id in claim.citation_ids
                    if citation_id in remaining_citation_ids
                ],
            )
            for claim in answer.claims
        ]
        repaired_answer = replace(
            answer,
            claims=repaired_claims,
            citations=repaired_citations,
        )
        if failure_policy == "remove_invalid":
            return CitationValidationResult(
                ok=True,
                issues=issues,
                repaired_answer=repaired_answer,
            )
        if failure_policy == "repair":
            repaired_result = validate_answer_citations(
                repaired_answer,
                context,
                require_citations=require_citations,
                failure_policy="fail",
            )
            repaired_issues = [
                issue
                for issue in repaired_result.issues
                if not any(
                    original.code == issue.code
                    and original.citation_id == issue.citation_id
                    and original.claim_id == issue.claim_id
                    for original in issues
                )
            ]
            return CitationValidationResult(
                ok=repaired_result.ok,
                issues=[*issues, *repaired_issues],
                repaired_answer=repaired_answer,
            )
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
    failure_policy: Literal["warn", "fail", "abstain", "repair", "remove_invalid"] = "abstain",
) -> CitationValidationResult:
    if failure_policy not in {"warn", "fail", "abstain", "repair", "remove_invalid"}:
        raise ValueError("failure_policy must be one of warn, fail, abstain, repair, or remove_invalid")
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
        if not isinstance(principals, (list, tuple)):
            raise ValueError("principals must be a sequence")
        has_selector = True
        if auth.principal_id in principals:
            return True

    groups = acl.get("groups")
    if groups is not None:
        if not isinstance(groups, (list, tuple)):
            raise ValueError("groups must be a sequence")
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
        if attributes and all(
            auth.attributes.get(name) == expected
            for name, expected in attributes.items()
        ):
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
            digest=sha256_digest_bytes(chunk.text.encode("utf-8")),
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

    def __post_init__(self) -> None:
        _validate_non_empty_string("knowledge index", "index_id", self.index_id)
        if not isinstance(self._records, Mapping):
            raise ValueError("knowledge index records must be a mapping")
        records: dict[str, KnowledgeIndexRecord] = {}
        for chunk_id, record in self._records.items():
            _validate_non_empty_string("knowledge index", "chunk_id", chunk_id)
            if not isinstance(record, KnowledgeIndexRecord):
                raise ValueError(
                    "knowledge index records must be KnowledgeIndexRecord records"
                )
            if record.chunk.chunk_id != chunk_id:
                raise ValueError(
                    "knowledge index record key must match chunk chunk_id"
                )
            records[chunk_id] = record
        if not isinstance(self._published_revisions, Mapping):
            raise ValueError("knowledge index published revisions must be a mapping")
        published_revisions: dict[str, str] = {}
        for asset_id, revision_id in self._published_revisions.items():
            _validate_non_empty_string("knowledge index", "asset_id", asset_id)
            _validate_non_empty_string("knowledge index", "revision_id", revision_id)
            if not any(
                record.status == "active"
                and record.chunk.asset_id == asset_id
                and record.chunk.revision_id == revision_id
                for record in records.values()
            ):
                raise ValueError(
                    "knowledge index published revision must reference an active chunk"
                )
            published_revisions[asset_id] = revision_id
        self._records = records
        self._published_revisions = published_revisions

    def upsert_chunks(self, chunks: list[DocumentChunk]) -> KnowledgeWriteReport:
        if isinstance(chunks, (str, bytes, bytearray)):
            raise ValueError("knowledge index chunks must be DocumentChunk records")
        try:
            chunk_records = tuple(chunks)
        except TypeError as error:
            raise ValueError(
                "knowledge index chunks must be DocumentChunk records"
            ) from error
        if any(not isinstance(chunk, DocumentChunk) for chunk in chunk_records):
            raise ValueError("knowledge index chunks must be DocumentChunk records")
        chunk_ids = [chunk.chunk_id for chunk in chunk_records]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("knowledge index chunks must not contain duplicate chunk_ids")
        for chunk in chunk_records:
            existing = self._records.get(chunk.chunk_id)
            if existing is not None and (
                existing.chunk.asset_id,
                existing.chunk.revision_id,
                existing.chunk.document_id,
            ) != (chunk.asset_id, chunk.revision_id, chunk.document_id):
                raise KnowledgeIndexError(
                    f"knowledge item {chunk.chunk_id!r} cannot change lineage"
                )
        for chunk in chunk_records:
            self._records[chunk.chunk_id] = KnowledgeIndexRecord(chunk=chunk, status="active")
        return KnowledgeWriteReport(
            operation="upsert",
            affected_count=len(chunk_ids),
            chunk_ids=chunk_ids,
            metadata={"index_id": self.index_id},
        )

    def delete_asset(self, asset_id: str, mode: KnowledgeDeleteMode) -> KnowledgeWriteReport:
        _validate_non_empty_string("knowledge index", "asset_id", asset_id)
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
        metadata_copy = _copy_metadata(
            "knowledge index update", metadata, field_name="metadata"
        )
        merged_metadata = dict(record.chunk.metadata)
        for key in sorted(metadata_copy):
            merged_metadata[key] = metadata_copy[key]
        self._records[chunk_id] = replace(record, chunk=replace(record.chunk, metadata=merged_metadata))
        return KnowledgeWriteReport(
            operation="update_metadata",
            affected_count=1,
            chunk_ids=[chunk_id],
            metadata={"metadata_keys": sorted(metadata_copy)},
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
        _validate_non_empty_string("knowledge index", "asset_id", asset_id)
        _validate_non_empty_string("knowledge index", "revision_id", revision_id)
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

    def __post_init__(self) -> None:
        _validate_non_empty_string("chunk retriever", "retriever_id", self.retriever_id)
        if isinstance(self.chunks, (str, bytes, bytearray)):
            raise ValueError("chunk retriever chunks must be DocumentChunk records")
        try:
            chunks = list(self.chunks)
        except TypeError as error:
            raise ValueError(
                "chunk retriever chunks must be DocumentChunk records"
            ) from error
        if any(not isinstance(chunk, DocumentChunk) for chunk in chunks):
            raise ValueError("chunk retriever chunks must be DocumentChunk records")
        chunk_ids = [chunk.chunk_id for chunk in chunks]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("chunk retriever chunks must not contain duplicate chunk_ids")
        self.chunks = chunks

    def search(self, query_text: str, top_k: int = 10) -> list[SearchHit]:
        return self.retrieve(SearchRequest(query_text=query_text, top_k=top_k)).hits

    def retrieve(self, request: SearchRequest) -> RetrievalResult:
        if not isinstance(request, SearchRequest):
            raise ValueError("chunk retriever request must be a SearchRequest")
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

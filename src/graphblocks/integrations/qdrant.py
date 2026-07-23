from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import math
from uuid import UUID

from graphblocks import SourceRef, canonical_dumps
from graphblocks.rag import KnowledgeItemRef, SearchHit, SearchRequest


class QdrantAdapterError(ValueError):
    """Raised when a Qdrant adapter contract is invalid."""


_VALID_SOURCE_TRUSTS = frozenset(
    {
        "authoritative",
        "verified",
        "application",
        "user_supplied",
        "retrieved_untrusted",
        "generated",
        "unknown",
    }
)


def _strict_json_mapping(
    field_name: str,
    value: Mapping[object, object],
) -> dict[str, object]:
    materialized = dict(value)
    try:
        canonical_dumps(materialized)
    except (TypeError, ValueError) as error:
        raise QdrantAdapterError(
            f"{field_name} must be a strict JSON object"
        ) from error
    return deepcopy(materialized)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class QdrantCollectionRef:
    collection: str
    vector_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.collection, str):
            raise QdrantAdapterError("collection must be a string")
        collection = self.collection.strip()
        if not collection:
            raise QdrantAdapterError("collection must not be empty")
        if collection != self.collection:
            raise QdrantAdapterError(
                "collection must not contain surrounding whitespace"
            )
        if any(separator in collection for separator in ("/", "?", "#")):
            raise QdrantAdapterError(f"collection contains an invalid separator: {self.collection!r}")
        if self.vector_name is not None and not isinstance(self.vector_name, str):
            raise QdrantAdapterError("vector_name must be a string")
        vector_name = self.vector_name.strip() if self.vector_name is not None else None
        if vector_name == "":
            raise QdrantAdapterError("vector_name must not be empty")
        if vector_name is not None and vector_name != self.vector_name:
            raise QdrantAdapterError(
                "vector_name must not contain surrounding whitespace"
            )
        object.__setattr__(self, "collection", collection)
        object.__setattr__(self, "vector_name", vector_name)


@dataclass(frozen=True, slots=True)
class QdrantSearchRequest:
    collection: str
    body: Mapping[str, object]
    query_text: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        collection = QdrantCollectionRef(self.collection).collection
        if not isinstance(self.body, Mapping):
            raise QdrantAdapterError("body must be a mapping")
        body = dict(self.body)
        if not body:
            raise QdrantAdapterError("body must not be empty")
        if not isinstance(self.query_text, str) or not self.query_text.strip():
            raise QdrantAdapterError("query_text must not be empty")
        if not isinstance(self.metadata, Mapping):
            raise QdrantAdapterError("metadata must be a mapping")
        object.__setattr__(self, "collection", collection)
        object.__setattr__(self, "body", _strict_json_mapping("body", body))
        object.__setattr__(
            self,
            "metadata",
            _strict_json_mapping("metadata", self.metadata),
        )

    def request_contract(self) -> dict[str, object]:
        return {
            "collection": self.collection,
            "body": deepcopy(dict(self.body)),
            "query_text": self.query_text,
            "metadata": deepcopy(dict(self.metadata)),
        }


def qdrant_search_request(
    request: SearchRequest,
    *,
    collection: QdrantCollectionRef,
    vector: Sequence[float],
    score_threshold: float | None = None,
) -> QdrantSearchRequest:
    if not isinstance(request, SearchRequest):
        raise QdrantAdapterError("request must be a SearchRequest")
    if not isinstance(collection, QdrantCollectionRef):
        raise QdrantAdapterError("collection must be a QdrantCollectionRef")
    if request.top_k < 1:
        raise QdrantAdapterError("top_k must be at least 1")
    if isinstance(vector, (str, bytes)) or not vector:
        raise QdrantAdapterError("vector must contain at least one numeric value")
    vector_values: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise QdrantAdapterError("vector values must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise QdrantAdapterError("vector values must be finite")
        vector_values.append(number)

    body: dict[str, object] = {
        "limit": request.top_k,
        "vector": (
            {"name": collection.vector_name, "vector": vector_values}
            if collection.vector_name is not None
            else vector_values
        ),
        "with_payload": True,
        "with_vector": False,
    }
    filter_terms: list[dict[str, object]] = []
    for key, value in sorted(request.filters.items()):
        if not isinstance(key, str) or not key.strip():
            raise QdrantAdapterError("filter keys must be non-empty strings")
        if key != key.strip():
            raise QdrantAdapterError(
                "filter keys must not contain surrounding whitespace"
            )
        if value is None:
            null_condition: dict[str, object] = {"is_null": {"key": key}}
            filter_terms.append(null_condition)
            continue
        if isinstance(value, Mapping):
            if "key" in value:
                raise QdrantAdapterError(
                    f"filter {key!r} mapping must not override its key"
                )
            condition = {"key": key}
            condition.update(deepcopy(dict(value)))
            filter_terms.append(condition)
        elif isinstance(value, (list, tuple, set, frozenset)):
            filter_terms.append(
                {
                    "key": key,
                    "match": {"any": sorted(deepcopy(list(value)), key=repr)},
                }
            )
        else:
            filter_terms.append({"key": key, "match": {"value": deepcopy(value)}})
    if filter_terms:
        body["filter"] = {"must": filter_terms}
    if score_threshold is not None:
        if (
            isinstance(score_threshold, bool)
            or not isinstance(score_threshold, (int, float))
            or not math.isfinite(float(score_threshold))
        ):
            raise QdrantAdapterError("score_threshold must be a finite number")
        body["score_threshold"] = float(score_threshold)

    return QdrantSearchRequest(
        collection=collection.collection,
        body=body,
        query_text=request.query_text,
        metadata=request.metadata,
    )


def qdrant_hits_from_points(
    points: Iterable[Mapping[str, object]],
    *,
    retriever_id: str,
    score_kind: str = "qdrant_score",
) -> list[SearchHit]:
    if not isinstance(retriever_id, str) or not retriever_id.strip():
        raise QdrantAdapterError("retriever_id must not be empty")
    if retriever_id != retriever_id.strip():
        raise QdrantAdapterError(
            "retriever_id must not contain surrounding whitespace"
        )
    if not isinstance(score_kind, str):
        raise QdrantAdapterError("score_kind must be a string")
    score_kind = score_kind.strip()
    if not score_kind:
        raise QdrantAdapterError("score_kind must not be empty")

    hits: list[SearchHit] = []
    for rank, point in enumerate(points, start=1):
        if not isinstance(point, Mapping):
            raise QdrantAdapterError("Qdrant point must be a mapping")
        point = _strict_json_mapping("Qdrant point", point)
        if "id" in point and "point_id" in point:
            raise QdrantAdapterError(
                "Qdrant point must not contain both id and point_id"
            )
        point_id = point.get("id", point.get("point_id"))
        if point_id is None:
            raise QdrantAdapterError("Qdrant point is missing id")
        if isinstance(point_id, bool) or not isinstance(point_id, (str, int)):
            raise QdrantAdapterError("Qdrant point id must be a string or integer")
        if isinstance(point_id, int) and not 0 <= point_id <= (1 << 64) - 1:
            raise QdrantAdapterError(
                "Qdrant integer point id must be an unsigned 64-bit integer"
            )
        if isinstance(point_id, str):
            if not point_id.strip():
                raise QdrantAdapterError("Qdrant string point id must not be empty")
            if point_id != point_id.strip():
                raise QdrantAdapterError(
                    "Qdrant string point id must not contain surrounding whitespace"
                )
            try:
                UUID(point_id)
            except ValueError as error:
                raise QdrantAdapterError(
                    "Qdrant string point id must be a UUID"
                ) from error
        payload = point.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            raise QdrantAdapterError("Qdrant point payload must be a mapping")
        payload = _strict_json_mapping("Qdrant point payload", payload)
        score = point.get("score")
        raw_score: float | None = None
        normalized_score: float | None = None
        if score is not None:
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise QdrantAdapterError("Qdrant point score must be numeric")
            raw_score = float(score)
            if not math.isfinite(raw_score):
                raise QdrantAdapterError("Qdrant point score must be finite")
            if 0 <= raw_score <= 1:
                normalized_score = raw_score

        source_payload = payload.get("source")
        if "source" in payload and not isinstance(source_payload, Mapping):
            raise QdrantAdapterError("Qdrant source payload must be a mapping")
        if isinstance(source_payload, Mapping):
            source_id = source_payload.get("source_id")
            source_kind = source_payload.get("source_kind")
            if not isinstance(source_id, str) or not source_id.strip():
                raise QdrantAdapterError("Qdrant source payload requires source_id")
            if not isinstance(source_kind, str) or not source_kind.strip():
                raise QdrantAdapterError("Qdrant source payload requires source_kind")
            if source_id != source_id.strip() or source_kind != source_kind.strip():
                raise QdrantAdapterError(
                    "Qdrant source identity fields must not contain surrounding whitespace"
                )
            source_metadata = source_payload.get("metadata", {})
            if source_metadata is None:
                source_metadata = {}
            if not isinstance(source_metadata, Mapping):
                raise QdrantAdapterError("Qdrant source metadata must be a mapping")
            source_access_policy = source_payload.get("access_policy")
            if source_access_policy is not None and not isinstance(source_access_policy, Mapping):
                raise QdrantAdapterError("Qdrant source access_policy must be a mapping")
            for optional_field in (
                "revision",
                "digest",
                "observed_at",
                "relevant_as_of",
            ):
                optional_value = source_payload.get(optional_field)
                if optional_value is not None and (
                    not isinstance(optional_value, str)
                    or not optional_value.strip()
                    or optional_value != optional_value.strip()
                ):
                    raise QdrantAdapterError(
                        f"Qdrant source {optional_field} must be a non-empty string"
                    )
            source_trust = source_payload.get("trust", "retrieved_untrusted")
            if source_trust not in _VALID_SOURCE_TRUSTS:
                raise QdrantAdapterError("Qdrant source trust is invalid")
            try:
                source = SourceRef(
                    source_id=source_id,
                    source_kind=source_kind,
                    revision=source_payload.get("revision"),  # type: ignore[arg-type]
                    digest=source_payload.get("digest"),  # type: ignore[arg-type]
                    observed_at=source_payload.get("observed_at"),  # type: ignore[arg-type]
                    relevant_as_of=source_payload.get("relevant_as_of"),  # type: ignore[arg-type]
                    trust=source_trust,  # type: ignore[arg-type]
                    access_policy=(
                        _strict_json_mapping(
                            "Qdrant source access_policy",
                            source_access_policy,
                        )
                        if isinstance(source_access_policy, Mapping)
                        else None
                    ),
                    metadata=_strict_json_mapping(
                        "Qdrant source metadata",
                        source_metadata,
                    ),
                )
            except (TypeError, ValueError) as error:
                raise QdrantAdapterError(str(error)) from error
        else:
            source = SourceRef(
                source_id=f"qdrant:{point_id}",
                source_kind="vector_point",
                trust="retrieved_untrusted",
                metadata={"qdrant_point_id": str(point_id)},
            )

        preview = payload.get("preview", [])
        if preview is None:
            preview = []
        if isinstance(preview, str):
            preview = [preview]
        if not isinstance(preview, list | tuple):
            raise QdrantAdapterError("Qdrant preview must be a sequence")
        if any(not isinstance(part, str) for part in preview):
            raise QdrantAdapterError("Qdrant preview parts must be strings")
        if any(not part.strip() or part != part.strip() for part in preview):
            raise QdrantAdapterError(
                "Qdrant preview parts must be stable non-empty strings"
            )
        acl = payload.get("acl")
        if acl is not None and not isinstance(acl, Mapping):
            raise QdrantAdapterError("Qdrant ACL must be a mapping")
        item_metadata = payload.get("metadata", {})
        if item_metadata is None:
            item_metadata = {}
        if not isinstance(item_metadata, Mapping):
            raise QdrantAdapterError("Qdrant item metadata must be a mapping")
        item_id = payload.get("item_id")
        if item_id is None:
            item_id = str(point_id)
        elif (
            not isinstance(item_id, str)
            or not item_id.strip()
            or item_id != item_id.strip()
        ):
            raise QdrantAdapterError(
                "Qdrant item_id must be a non-empty string"
            )
        item_kind = payload.get("item_kind", "document_chunk")
        if (
            not isinstance(item_kind, str)
            or not item_kind.strip()
            or item_kind != item_kind.strip()
        ):
            raise QdrantAdapterError(
                "Qdrant item_kind must be a non-empty string"
            )
        schema_ref = payload.get("schema_ref")
        if schema_ref is not None and (
            not isinstance(schema_ref, str)
            or not schema_ref.strip()
            or schema_ref != schema_ref.strip()
        ):
            raise QdrantAdapterError(
                "Qdrant schema_ref must be a non-empty string"
            )
        payload_ref = payload.get("payload_ref")
        if payload_ref is not None and (
            not isinstance(payload_ref, str)
            or not payload_ref.strip()
            or payload_ref != payload_ref.strip()
        ):
            raise QdrantAdapterError(
                "Qdrant payload_ref must be a non-empty string"
            )

        try:
            item = KnowledgeItemRef(
                item_id=item_id,
                item_kind=item_kind,
                source=source,
                schema_ref=schema_ref,  # type: ignore[arg-type]
                payload_ref=payload_ref,  # type: ignore[arg-type]
                preview=list(preview),
                acl=(
                    _strict_json_mapping("Qdrant ACL", acl)
                    if isinstance(acl, Mapping)
                    else None
                ),
                metadata=_strict_json_mapping(
                    "Qdrant item metadata",
                    item_metadata,
                ),
            )
        except (TypeError, ValueError) as error:
            raise QdrantAdapterError(str(error)) from error
        hits.append(
            SearchHit(
                hit_id=f"{retriever_id}:{rank}:{point_id}",
                item=item,
                rank=rank,
                retriever=retriever_id,
                raw_score=raw_score,
                normalized_score=normalized_score,
                score_kind=score_kind,
                highlights=[source],
                metadata={"qdrant_point_id": str(point_id)},
            )
        )
    return hits


__all__ = [
    "QdrantAdapterError",
    "QdrantCollectionRef",
    "QdrantSearchRequest",
    "qdrant_hits_from_points",
    "qdrant_search_request",
]

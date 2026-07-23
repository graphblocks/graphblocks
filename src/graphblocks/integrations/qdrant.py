from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import math

from graphblocks.rag import KnowledgeItemRef, SearchHit, SearchRequest
from graphblocks import SourceRef


class QdrantAdapterError(ValueError):
    """Raised when a Qdrant adapter contract is invalid."""


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
        if any(separator in collection for separator in ("/", "?", "#")):
            raise QdrantAdapterError(f"collection contains an invalid separator: {self.collection!r}")
        if self.vector_name is not None and not isinstance(self.vector_name, str):
            raise QdrantAdapterError("vector_name must be a string")
        vector_name = self.vector_name.strip() if self.vector_name is not None else None
        if vector_name == "":
            raise QdrantAdapterError("vector_name must not be empty")
        object.__setattr__(self, "collection", collection)
        object.__setattr__(self, "vector_name", vector_name)


@dataclass(frozen=True, slots=True)
class QdrantSearchRequest:
    collection: str
    body: Mapping[str, object]
    query_text: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.collection, str) or not self.collection.strip():
            raise QdrantAdapterError("collection must not be empty")
        if not isinstance(self.body, Mapping) or not self.body:
            raise QdrantAdapterError("body must not be empty")
        if not isinstance(self.query_text, str) or not self.query_text.strip():
            raise QdrantAdapterError("query_text must not be empty")
        if not isinstance(self.metadata, Mapping):
            raise QdrantAdapterError("metadata must be a mapping")
        object.__setattr__(self, "collection", self.collection.strip())
        object.__setattr__(self, "body", deepcopy(dict(self.body)))
        object.__setattr__(self, "metadata", deepcopy(dict(self.metadata)))

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
        if value is None:
            null_condition: dict[str, object] = {"is_null": {"key": key.strip()}}
            filter_terms.append(null_condition)
            continue
        if isinstance(value, Mapping):
            if "key" in value:
                raise QdrantAdapterError(
                    f"filter {key.strip()!r} mapping must not override its key"
                )
            condition = {"key": key.strip()}
            condition.update(deepcopy(dict(value)))
            filter_terms.append(condition)
        elif isinstance(value, (list, tuple, set, frozenset)):
            filter_terms.append(
                {
                    "key": key.strip(),
                    "match": {"any": sorted(deepcopy(list(value)), key=repr)},
                }
            )
        else:
            filter_terms.append({"key": key.strip(), "match": {"value": deepcopy(value)}})
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
    if not isinstance(score_kind, str):
        raise QdrantAdapterError("score_kind must be a string")
    score_kind = score_kind.strip()
    if not score_kind:
        raise QdrantAdapterError("score_kind must not be empty")

    hits: list[SearchHit] = []
    for rank, point in enumerate(points, start=1):
        if not isinstance(point, Mapping):
            raise QdrantAdapterError("Qdrant point must be a mapping")
        point_id = point.get("id", point.get("point_id"))
        if point_id is None:
            raise QdrantAdapterError("Qdrant point is missing id")
        if isinstance(point_id, bool) or not isinstance(point_id, (str, int)):
            raise QdrantAdapterError("Qdrant point id must be a string or integer")
        if isinstance(point_id, int) and point_id < 0:
            raise QdrantAdapterError("Qdrant integer point id must be non-negative")
        if isinstance(point_id, str) and not point_id.strip():
            raise QdrantAdapterError("Qdrant string point id must not be empty")
        payload = point.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            raise QdrantAdapterError("Qdrant point payload must be a mapping")
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
        if isinstance(source_payload, Mapping):
            source_id = source_payload.get("source_id")
            source_kind = source_payload.get("source_kind")
            if not isinstance(source_id, str) or not source_id.strip():
                raise QdrantAdapterError("Qdrant source payload requires source_id")
            if not isinstance(source_kind, str) or not source_kind.strip():
                raise QdrantAdapterError("Qdrant source payload requires source_kind")
            source_metadata = source_payload.get("metadata", {})
            if source_metadata is None:
                source_metadata = {}
            if not isinstance(source_metadata, Mapping):
                raise QdrantAdapterError("Qdrant source metadata must be a mapping")
            source_access_policy = source_payload.get("access_policy")
            if source_access_policy is not None and not isinstance(source_access_policy, Mapping):
                raise QdrantAdapterError("Qdrant source access_policy must be a mapping")
            source = SourceRef(
                source_id=source_id,
                source_kind=source_kind,
                revision=source_payload.get("revision") if isinstance(source_payload.get("revision"), str) else None,
                digest=source_payload.get("digest") if isinstance(source_payload.get("digest"), str) else None,
                observed_at=(
                    source_payload.get("observed_at") if isinstance(source_payload.get("observed_at"), str) else None
                ),
                relevant_as_of=(
                    source_payload.get("relevant_as_of")
                    if isinstance(source_payload.get("relevant_as_of"), str)
                    else None
                ),
                trust=(
                    source_payload.get("trust")
                    if source_payload.get("trust")
                    in {
                        "authoritative",
                        "verified",
                        "application",
                        "user_supplied",
                        "retrieved_untrusted",
                        "generated",
                        "unknown",
                    }
                    else "retrieved_untrusted"
                ),
                access_policy=deepcopy(dict(source_access_policy)) if isinstance(source_access_policy, Mapping) else None,
                metadata=deepcopy(dict(source_metadata)),
            )
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
        if not isinstance(preview, Sequence):
            raise QdrantAdapterError("Qdrant preview must be a sequence")
        acl = payload.get("acl")
        if acl is not None and not isinstance(acl, Mapping):
            raise QdrantAdapterError("Qdrant ACL must be a mapping")
        item_metadata = payload.get("metadata", {})
        if item_metadata is None:
            item_metadata = {}
        if not isinstance(item_metadata, Mapping):
            raise QdrantAdapterError("Qdrant item metadata must be a mapping")

        item = KnowledgeItemRef(
            item_id=str(payload.get("item_id", point_id)),
            item_kind=str(payload.get("item_kind", "document_chunk")),
            source=source,
            schema_ref=payload.get("schema_ref") if isinstance(payload.get("schema_ref"), str) else None,
            payload_ref=payload.get("payload_ref") if isinstance(payload.get("payload_ref"), str) else None,
            preview=[str(part) for part in preview],
            acl=deepcopy(dict(acl)) if isinstance(acl, Mapping) else None,
            metadata=deepcopy(dict(item_metadata)),
        )
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

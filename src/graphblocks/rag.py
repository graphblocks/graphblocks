from __future__ import annotations

from dataclasses import dataclass, field
import re

from .documents import DocumentChunk, SourceRef


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
        terms = [term for term in re.findall(r"[A-Za-z0-9_]+", query_text.lower()) if term]
        if not terms:
            return []
        scored: list[tuple[int, int, DocumentChunk]] = []
        for index, chunk in enumerate(self.chunks):
            haystack = chunk.text.lower()
            score = sum(haystack.count(term) for term in terms)
            if score > 0:
                scored.append((score, index, chunk))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if not scored:
            return []
        max_score = scored[0][0]
        hits: list[SearchHit] = []
        for rank, (score, _index, chunk) in enumerate(scored[:top_k], start=1):
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
        return hits


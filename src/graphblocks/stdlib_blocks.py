"""Typed definitions for standard-library blocks.

These classes describe authoring-time ports and configuration.  They do not
replace the runtime implementations in :mod:`graphblocks.stdlib_rag`; a bound
definition materializes the same block id, input references, and JSON config
that the portable Graph contract uses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Generic, Literal, TypeAlias, TypeVar

from .typed import BoundBlock, InputRef, NodeOutput, PortType


class SearchRequestValue:
    """Type marker for ``graphblocks.ai/SearchRequest@1`` values."""


class FederatedSourcesValue:
    """Type marker for configured federated retrieval sources."""


class RetrievalSourcesValue:
    """Type marker for executed federated retrieval source results."""


class SearchHitsValue:
    """Type marker for ranked or fused search-hit lists."""


class ContextPackValue:
    """Type marker for ``graphblocks.ai/ContextPack@1`` values."""


class AnswerValue:
    """Type marker for ``graphblocks.ai/Answer@1`` values."""


class GroundingValidationValue:
    """Type marker for grounding validation results."""


class RetrievalResultValue:
    """Type marker for a federated retrieval result."""


class StructuredItemsValue:
    """Type marker for optional structured-generation item lists."""


class StringValue:
    """Type marker for string-valued block ports."""


FederatedFailureMode: TypeAlias = Literal["fail", "partial"]
RetrievalFusionAlgorithm: TypeAlias = Literal[
    "concatenate",
    "reciprocal_rank_fusion",
    "weighted_rank",
    "normalized_score",
    "interleave",
]
GroundingFailurePolicy: TypeAlias = Literal[
    "warn",
    "fail",
    "abstain",
    "repair",
    "remove_invalid",
]


SEARCH_REQUEST: PortType[SearchRequestValue] = PortType("graphblocks.ai/SearchRequest@1")
FEDERATED_SOURCES: PortType[FederatedSourcesValue] = PortType(
    "graphblocks.ai/FederatedSources@1"
)
ANSWER: PortType[AnswerValue] = PortType("graphblocks.ai/Answer@1")
GROUNDING_VALIDATION: PortType[GroundingValidationValue] = PortType(
    "graphblocks.ai/GroundingValidation@1"
)


@dataclass(frozen=True, slots=True)
class RetrieveExecutePlanOutputs:
    result: NodeOutput[RetrievalResultValue]
    sources: NodeOutput[RetrievalSourcesValue]


@dataclass(frozen=True, slots=True)
class RetrieveExecutePlan:
    minimum_successful_sources: int = 1
    top_k: int | None = None
    retriever_id: str | None = None
    failure_mode: FederatedFailureMode | None = None

    def __post_init__(self) -> None:
        if self.minimum_successful_sources < 1:
            raise ValueError("minimum_successful_sources must be positive")
        if self.top_k is not None and self.top_k < 1:
            raise ValueError("top_k must be positive when provided")
        if self.failure_mode not in {None, "fail", "partial"}:
            raise ValueError("failure_mode must be fail or partial")

    def bind(
        self,
        *,
        query: InputRef[SearchRequestValue],
        sources: InputRef[FederatedSourcesValue],
    ) -> BoundBlock[RetrieveExecutePlanOutputs]:
        config: dict[str, object] = {
            "minimumSuccessfulSources": self.minimum_successful_sources,
        }
        if self.top_k is not None:
            config["topK"] = self.top_k
        if self.retriever_id is not None:
            config["retrieverId"] = self.retriever_id
        if self.failure_mode is not None:
            config["failureMode"] = self.failure_mode
        return BoundBlock(
            "retrieve.execute_plan@1",
            {"query": query, "sources": sources},
            config,
            lambda node_id, owner: RetrieveExecutePlanOutputs(
                result=NodeOutput(node_id, "result", owner),
                sources=NodeOutput(node_id, "sources", owner),
            ),
        )


@dataclass(frozen=True, slots=True)
class RetrieveFuseOutputs:
    hits: NodeOutput[SearchHitsValue]


@dataclass(frozen=True, slots=True)
class RetrieveFuse:
    algorithm: RetrievalFusionAlgorithm = "reciprocal_rank_fusion"
    k: int = 60
    top_k: int | None = None
    retriever_id: str | None = None

    def __post_init__(self) -> None:
        if self.algorithm not in {
            "concatenate",
            "reciprocal_rank_fusion",
            "weighted_rank",
            "normalized_score",
            "interleave",
        }:
            raise ValueError("algorithm must name a supported retrieval fusion strategy")
        if self.k < 1:
            raise ValueError("k must be positive")
        if self.top_k is not None and self.top_k < 1:
            raise ValueError("top_k must be positive when provided")

    def bind(self, *, sources: InputRef[RetrievalSourcesValue]) -> BoundBlock[RetrieveFuseOutputs]:
        config: dict[str, object] = {"algorithm": self.algorithm, "k": self.k}
        if self.top_k is not None:
            config["topK"] = self.top_k
        if self.retriever_id is not None:
            config["retrieverId"] = self.retriever_id
        return BoundBlock(
            "retrieve.fuse@1",
            {"sources": sources},
            config,
            lambda node_id, owner: RetrieveFuseOutputs(
                hits=NodeOutput(node_id, "hits", owner)
            ),
        )


@dataclass(frozen=True, slots=True)
class RankDocumentsOutputs:
    hits: NodeOutput[SearchHitsValue]


@dataclass(frozen=True, slots=True)
class RankDocuments:
    reranker_id: str = "lexical"
    input_limit: int | None = None

    def __post_init__(self) -> None:
        if not self.reranker_id:
            raise ValueError("reranker_id must be non-empty")
        if self.input_limit is not None and self.input_limit < 1:
            raise ValueError("input_limit must be positive when provided")

    def bind(
        self,
        *,
        query: InputRef[SearchRequestValue],
        hits: InputRef[SearchHitsValue],
    ) -> BoundBlock[RankDocumentsOutputs]:
        config: dict[str, object] = {"rerankerId": self.reranker_id}
        if self.input_limit is not None:
            config["inputLimit"] = self.input_limit
        return BoundBlock(
            "rank.documents@1",
            {"query": query, "hits": hits},
            config,
            lambda node_id, owner: RankDocumentsOutputs(
                hits=NodeOutput(node_id, "hits", owner),
            ),
        )


@dataclass(frozen=True, slots=True)
class ContextBuildOutputs:
    pack: NodeOutput[ContextPackValue]


@dataclass(frozen=True, slots=True)
class ContextBuild:
    context_id: str | None = None
    max_tokens: int = 4096
    reserve_output_tokens: int = 0
    deduplicate: bool | None = None

    def __post_init__(self) -> None:
        if self.context_id == "":
            raise ValueError("context_id must be non-empty when provided")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        if self.reserve_output_tokens < 0:
            raise ValueError("reserve_output_tokens must not be negative")

    def bind(self, *, evidence: InputRef[SearchHitsValue]) -> BoundBlock[ContextBuildOutputs]:
        config: dict[str, object] = {
            "maxTokens": self.max_tokens,
            "reserveOutputTokens": self.reserve_output_tokens,
        }
        if self.context_id is not None:
            config["contextId"] = self.context_id
        if self.deduplicate is not None:
            config["deduplicate"] = self.deduplicate
        return BoundBlock(
            "context.build@1",
            {"evidence": evidence},
            config,
            lambda node_id, owner: ContextBuildOutputs(
                pack=NodeOutput(node_id, "pack", owner)
            ),
        )


StructuredValueT = TypeVar("StructuredValueT")


@dataclass(frozen=True)
class StructuredGenerateOutputs(Generic[StructuredValueT]):
    value: NodeOutput[StructuredValueT]
    response: NodeOutput[StructuredValueT]
    items: NodeOutput[StructuredItemsValue]
    schema_id: NodeOutput[StringValue]
    schema_ref: NodeOutput[StringValue]
    content_digest: NodeOutput[StringValue]


@dataclass(frozen=True)
class StructuredGenerate(Generic[StructuredValueT]):
    output_schema: PortType[StructuredValueT]
    response: Mapping[str, object]

    def bind(
        self,
        *,
        context: InputRef[ContextPackValue],
    ) -> BoundBlock[StructuredGenerateOutputs[StructuredValueT]]:
        return BoundBlock(
            "model.structured_generate@1",
            {"context": context},
            {"outputSchema": self.output_schema.schema, "response": dict(self.response)},
            lambda node_id, owner: StructuredGenerateOutputs(
                value=NodeOutput(node_id, "value", owner),
                response=NodeOutput(node_id, "response", owner),
                items=NodeOutput(node_id, "items", owner),
                schema_id=NodeOutput(node_id, "schemaId", owner),
                schema_ref=NodeOutput(node_id, "schemaRef", owner),
                content_digest=NodeOutput(node_id, "contentDigest", owner),
            ),
        )


@dataclass(frozen=True, slots=True)
class AnswerValidateGroundingOutputs:
    candidate: NodeOutput[AnswerValue]
    response: NodeOutput[AnswerValue]
    result: NodeOutput[GroundingValidationValue]
    validation: NodeOutput[GroundingValidationValue]


@dataclass(frozen=True, slots=True)
class AnswerValidateGrounding:
    require_citation: bool = True
    on_insufficient_evidence: GroundingFailurePolicy = "abstain"

    def __post_init__(self) -> None:
        if self.on_insufficient_evidence not in {
            "warn",
            "fail",
            "abstain",
            "repair",
            "remove_invalid",
        }:
            raise ValueError("on_insufficient_evidence must name a grounding failure policy")

    def bind(
        self,
        *,
        response: InputRef[AnswerValue],
        context: InputRef[ContextPackValue],
    ) -> BoundBlock[AnswerValidateGroundingOutputs]:
        return BoundBlock(
            "answer.validate_grounding@1",
            {"response": response, "context": context},
            {
                "requireCitation": self.require_citation,
                "onInsufficientEvidence": self.on_insufficient_evidence,
            },
            lambda node_id, owner: AnswerValidateGroundingOutputs(
                candidate=NodeOutput(node_id, "candidate", owner),
                response=NodeOutput(node_id, "response", owner),
                result=NodeOutput(node_id, "result", owner),
                validation=NodeOutput(node_id, "validation", owner),
            ),
        )


__all__ = [
    "ANSWER",
    "FEDERATED_SOURCES",
    "GROUNDING_VALIDATION",
    "SEARCH_REQUEST",
    "AnswerValidateGrounding",
    "AnswerValidateGroundingOutputs",
    "AnswerValue",
    "ContextBuild",
    "ContextBuildOutputs",
    "ContextPackValue",
    "FederatedSourcesValue",
    "GroundingValidationValue",
    "GroundingFailurePolicy",
    "RankDocuments",
    "RankDocumentsOutputs",
    "RetrievalResultValue",
    "RetrievalSourcesValue",
    "RetrieveExecutePlan",
    "RetrieveExecutePlanOutputs",
    "RetrieveFuse",
    "RetrieveFuseOutputs",
    "RetrievalFusionAlgorithm",
    "SearchHitsValue",
    "SearchRequestValue",
    "StringValue",
    "StructuredGenerate",
    "StructuredGenerateOutputs",
    "StructuredItemsValue",
    "FederatedFailureMode",
]

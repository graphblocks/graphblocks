"""Typed definitions for standard-library blocks.

These classes describe authoring-time ports and configuration.  They do not
replace the runtime implementations in :mod:`graphblocks.stdlib_rag`; a bound
definition materializes the same block id, input references, and JSON config
that the portable Graph contract uses.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Generic, Literal, TypeAlias, TypeVar

from .canonical import canonical_dumps, canonical_loads
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


SEARCH_REQUEST: PortType[SearchRequestValue] = PortType(
    "graphblocks.ai/SearchRequest@1",
    SearchRequestValue,
)
FEDERATED_SOURCES: PortType[FederatedSourcesValue] = PortType(
    "graphblocks.ai/FederatedSources@1",
    FederatedSourcesValue,
)
RETRIEVAL_RESULT: PortType[RetrievalResultValue] = PortType(
    "graphblocks.ai/RetrievalResult@1",
    RetrievalResultValue,
)
RETRIEVAL_SOURCES: PortType[RetrievalSourcesValue] = PortType(
    "graphblocks.ai/RetrievalSources@1",
    RetrievalSourcesValue,
)
SEARCH_HITS: PortType[SearchHitsValue] = PortType(
    "graphblocks.ai/SearchHits@1",
    SearchHitsValue,
)
CONTEXT_PACK: PortType[ContextPackValue] = PortType(
    "graphblocks.ai/ContextPack@1",
    ContextPackValue,
)
ANSWER: PortType[AnswerValue] = PortType("graphblocks.ai/Answer@1", AnswerValue)
GROUNDING_VALIDATION: PortType[GroundingValidationValue] = PortType(
    "graphblocks.ai/GroundingValidation@1",
    GroundingValidationValue,
)
STRUCTURED_ITEMS: PortType[StructuredItemsValue] = PortType(
    "graphblocks.ai/StructuredItems@1",
    StructuredItemsValue,
)
STRING: PortType[StringValue] = PortType(
    "graphblocks.ai/String@1",
    StringValue,
)


def _validate_positive_integer(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < 1:
        raise ValueError(f"{field_name} must be positive")
    return value


def _validate_optional_exact_string(field_name: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string when provided")
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty when provided")
    if value != value.strip():
        raise ValueError(
            f"{field_name} must not contain surrounding whitespace"
        )
    return value


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
        _validate_positive_integer(
            "minimum_successful_sources",
            self.minimum_successful_sources,
        )
        if self.top_k is not None:
            _validate_positive_integer("top_k", self.top_k)
        _validate_optional_exact_string("retriever_id", self.retriever_id)
        if self.failure_mode is not None and (
            not isinstance(self.failure_mode, str)
            or self.failure_mode not in {"fail", "partial"}
        ):
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
            block_id="retrieve.execute_plan@1",
            inputs={"query": query, "sources": sources},
            expected_inputs={"query": SEARCH_REQUEST, "sources": FEDERATED_SOURCES},
            expected_outputs={"result": RETRIEVAL_RESULT, "sources": RETRIEVAL_SOURCES},
            config=config,
            _outputs=lambda node_id, owner: RetrieveExecutePlanOutputs(
                result=NodeOutput(node_id, "result", RETRIEVAL_RESULT, owner),
                sources=NodeOutput(node_id, "sources", RETRIEVAL_SOURCES, owner),
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
        if (
            not isinstance(self.algorithm, str)
            or self.algorithm
            not in {
                "concatenate",
                "reciprocal_rank_fusion",
                "weighted_rank",
                "normalized_score",
                "interleave",
            }
        ):
            raise ValueError("algorithm must name a supported retrieval fusion strategy")
        _validate_positive_integer("k", self.k)
        if self.top_k is not None:
            _validate_positive_integer("top_k", self.top_k)
        _validate_optional_exact_string("retriever_id", self.retriever_id)

    def bind(self, *, sources: InputRef[RetrievalSourcesValue]) -> BoundBlock[RetrieveFuseOutputs]:
        config: dict[str, object] = {"algorithm": self.algorithm, "k": self.k}
        if self.top_k is not None:
            config["topK"] = self.top_k
        if self.retriever_id is not None:
            config["retrieverId"] = self.retriever_id
        return BoundBlock(
            block_id="retrieve.fuse@1",
            inputs={"sources": sources},
            expected_inputs={"sources": RETRIEVAL_SOURCES},
            expected_outputs={"hits": SEARCH_HITS},
            config=config,
            _outputs=lambda node_id, owner: RetrieveFuseOutputs(
                hits=NodeOutput(node_id, "hits", SEARCH_HITS, owner)
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
        if _validate_optional_exact_string("reranker_id", self.reranker_id) is None:
            raise ValueError("reranker_id must be non-empty")
        if self.input_limit is not None:
            _validate_positive_integer("input_limit", self.input_limit)

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
            block_id="rank.documents@1",
            inputs={"query": query, "hits": hits},
            expected_inputs={"query": SEARCH_REQUEST, "hits": SEARCH_HITS},
            expected_outputs={"hits": SEARCH_HITS},
            config=config,
            _outputs=lambda node_id, owner: RankDocumentsOutputs(
                hits=NodeOutput(node_id, "hits", SEARCH_HITS, owner),
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
        _validate_optional_exact_string("context_id", self.context_id)
        _validate_positive_integer("max_tokens", self.max_tokens)
        if (
            isinstance(self.reserve_output_tokens, bool)
            or not isinstance(self.reserve_output_tokens, int)
        ):
            raise ValueError("reserve_output_tokens must be an integer")
        if self.reserve_output_tokens < 0:
            raise ValueError("reserve_output_tokens must not be negative")
        if self.deduplicate is not None and not isinstance(
            self.deduplicate,
            bool,
        ):
            raise ValueError("deduplicate must be a boolean when provided")

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
            block_id="context.build@1",
            inputs={"evidence": evidence},
            expected_inputs={"evidence": SEARCH_HITS},
            expected_outputs={"pack": CONTEXT_PACK},
            config=config,
            _outputs=lambda node_id, owner: ContextBuildOutputs(
                pack=NodeOutput(node_id, "pack", CONTEXT_PACK, owner)
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

    def __post_init__(self) -> None:
        if not isinstance(self.output_schema, PortType):
            raise ValueError("output_schema must be a PortType")
        if not isinstance(self.response, Mapping):
            raise ValueError("response must be a mapping")
        response = deepcopy(dict(self.response))
        try:
            canonical_dumps(response)
        except (TypeError, ValueError) as error:
            raise ValueError("response must be canonical JSON") from error
        frozen_containers: dict[int, object] = {}
        pending: list[tuple[object, bool]] = [(response, False)]
        while pending:
            value, visited = pending.pop()
            if not isinstance(value, (Mapping, list, tuple)):
                continue
            if not visited:
                pending.append((value, True))
                if isinstance(value, Mapping):
                    pending.extend((item, False) for item in value.values())
                else:
                    pending.extend((item, False) for item in value)
                continue
            if isinstance(value, Mapping):
                frozen_containers[id(value)] = MappingProxyType(
                    {
                        key: (
                            frozen_containers[id(item)]
                            if isinstance(item, (Mapping, list, tuple))
                            else item
                        )
                        for key, item in value.items()
                    }
                )
            else:
                frozen_containers[id(value)] = tuple(
                    (
                        frozen_containers[id(item)]
                        if isinstance(item, (Mapping, list, tuple))
                        else item
                    )
                    for item in value
                )
        object.__setattr__(
            self,
            "response",
            frozen_containers[id(response)],
        )

    def bind(
        self,
        *,
        context: InputRef[ContextPackValue],
    ) -> BoundBlock[StructuredGenerateOutputs[StructuredValueT]]:
        return BoundBlock(
            block_id="model.structured_generate@1",
            inputs={"context": context},
            expected_inputs={"context": CONTEXT_PACK},
            expected_outputs={
                "value": self.output_schema,
                "response": self.output_schema,
                "items": STRUCTURED_ITEMS,
                "schemaId": STRING,
                "schemaRef": STRING,
                "contentDigest": STRING,
            },
            config={
                "outputSchema": self.output_schema.schema,
                "response": canonical_loads(canonical_dumps(self.response)),
            },
            _outputs=lambda node_id, owner: StructuredGenerateOutputs(
                value=NodeOutput(node_id, "value", self.output_schema, owner),
                response=NodeOutput(node_id, "response", self.output_schema, owner),
                items=NodeOutput(node_id, "items", STRUCTURED_ITEMS, owner),
                schema_id=NodeOutput(node_id, "schemaId", STRING, owner),
                schema_ref=NodeOutput(node_id, "schemaRef", STRING, owner),
                content_digest=NodeOutput(node_id, "contentDigest", STRING, owner),
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
        if not isinstance(self.require_citation, bool):
            raise ValueError("require_citation must be a boolean")
        if (
            not isinstance(self.on_insufficient_evidence, str)
            or self.on_insufficient_evidence
            not in {
                "warn",
                "fail",
                "abstain",
                "repair",
                "remove_invalid",
            }
        ):
            raise ValueError("on_insufficient_evidence must name a grounding failure policy")

    def bind(
        self,
        *,
        response: InputRef[AnswerValue],
        context: InputRef[ContextPackValue],
    ) -> BoundBlock[AnswerValidateGroundingOutputs]:
        return BoundBlock(
            block_id="answer.validate_grounding@1",
            inputs={"response": response, "context": context},
            expected_inputs={"response": ANSWER, "context": CONTEXT_PACK},
            expected_outputs={
                "candidate": ANSWER,
                "response": ANSWER,
                "result": GROUNDING_VALIDATION,
                "validation": GROUNDING_VALIDATION,
            },
            config={
                "requireCitation": self.require_citation,
                "onInsufficientEvidence": self.on_insufficient_evidence,
            },
            _outputs=lambda node_id, owner: AnswerValidateGroundingOutputs(
                candidate=NodeOutput(node_id, "candidate", ANSWER, owner),
                response=NodeOutput(node_id, "response", ANSWER, owner),
                result=NodeOutput(node_id, "result", GROUNDING_VALIDATION, owner),
                validation=NodeOutput(
                    node_id,
                    "validation",
                    GROUNDING_VALIDATION,
                    owner,
                ),
            ),
        )


__all__ = [
    "ANSWER",
    "CONTEXT_PACK",
    "FEDERATED_SOURCES",
    "GROUNDING_VALIDATION",
    "RETRIEVAL_RESULT",
    "RETRIEVAL_SOURCES",
    "SEARCH_REQUEST",
    "SEARCH_HITS",
    "STRING",
    "STRUCTURED_ITEMS",
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

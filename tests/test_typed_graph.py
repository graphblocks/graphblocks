from __future__ import annotations

import pytest

from graphblocks.compiler import compile_graph
from graphblocks.plugins import BlockCatalog, discover_plugins
from graphblocks.stdlib_blocks import (
    ANSWER,
    FEDERATED_SOURCES,
    GROUNDING_VALIDATION,
    SEARCH_REQUEST,
    AnswerValidateGrounding,
    ContextBuild,
    RankDocuments,
    RetrieveExecutePlan,
    RetrieveExecutePlanOutputs,
    RetrieveFuse,
    StructuredGenerate,
)
from graphblocks.typed import GraphBuilder, PortType


def _build_rag_graph() -> dict[str, object]:
    graph = GraphBuilder("typed-rag")
    query = graph.input("query", SEARCH_REQUEST)
    sources = graph.input("sources", FEDERATED_SOURCES)
    candidate = graph.output("candidate", ANSWER)
    validation = graph.output("validation", GROUNDING_VALIDATION)

    retrieve = graph.add(
        "retrieve",
        RetrieveExecutePlan(minimum_successful_sources=2, top_k=5).bind(
            query=query,
            sources=sources,
        ),
    )
    assert isinstance(retrieve, RetrieveExecutePlanOutputs)
    fused = graph.add("fuse", RetrieveFuse().bind(sources=retrieve.sources))
    ranked = graph.add(
        "rank",
        RankDocuments(reranker_id="deterministic").bind(
            query=query,
            hits=fused.hits,
        ),
    )
    context = graph.add(
        "context",
        ContextBuild(context_id="typed-context", max_tokens=1000).bind(
            evidence=ranked.hits
        ),
    )
    generated = graph.add(
        "generate",
        StructuredGenerate(
            output_schema=ANSWER,
            response={"answerId": "answer-1", "text": "Typed."},
        ).bind(context=context.pack),
    )
    grounded = graph.add(
        "validate",
        AnswerValidateGrounding().bind(
            response=generated.response,
            context=context.pack,
        ),
    )
    graph.publish(candidate, grounded.candidate)
    graph.publish(validation, grounded.validation)
    return graph.build()


def test_typed_stdlib_blocks_materialize_portable_graph() -> None:
    document = _build_rag_graph()

    assert document["apiVersion"] == "graphblocks.ai/v1alpha3"
    spec = document["spec"]
    assert isinstance(spec, dict)
    nodes = spec["nodes"]
    assert isinstance(nodes, dict)
    assert nodes["retrieve"] == {
        "block": "retrieve.execute_plan@1",
        "inputs": {"query": "$input.query", "sources": "$input.sources"},
        "config": {"minimumSuccessfulSources": 2, "topK": 5},
    }
    assert nodes["fuse"]["inputs"] == {"sources": "retrieve.sources"}
    assert nodes["validate"]["outputs"] == {
        "candidate": "$output.candidate",
        "validation": "$output.validation",
    }

    registry = discover_plugins(include_installed=False)
    plan = compile_graph(
        document,
        block_catalog=BlockCatalog.from_manifests(registry.manifests),
    )
    assert plan.diagnostics.ok


def test_typed_graph_rejects_references_from_another_builder() -> None:
    first = GraphBuilder("first")
    query = first.input("query", SEARCH_REQUEST)
    second = GraphBuilder("second")
    sources = second.input("sources", FEDERATED_SOURCES)

    with pytest.raises(ValueError, match="different GraphBuilder"):
        first.add(
            "retrieve",
            RetrieveExecutePlan().bind(query=query, sources=sources),
        )


def test_typed_graph_requires_every_declared_output_to_be_published() -> None:
    graph = GraphBuilder("unpublished-output")
    graph.output("answer", ANSWER)

    with pytest.raises(ValueError, match="graph outputs are not published: answer"):
        graph.build()


def test_port_type_requires_a_canonical_schema_id() -> None:
    with pytest.raises(ValueError, match="major version suffix"):
        PortType("graphblocks.ai/Answer")


def test_typed_stdlib_configs_reject_invalid_literals() -> None:
    with pytest.raises(ValueError, match="failure_mode"):
        RetrieveExecutePlan(failure_mode="continue")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fusion strategy"):
        RetrieveFuse(algorithm="unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="grounding failure policy"):
        AnswerValidateGrounding(  # type: ignore[arg-type]
            on_insufficient_evidence="continue"
        )

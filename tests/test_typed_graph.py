from __future__ import annotations

from typing import Any, cast

import pytest

from graphblocks.compiler import compile_graph
from graphblocks.plugins import BlockCatalog, discover_plugins
from graphblocks.stdlib_blocks import (
    ANSWER,
    FEDERATED_SOURCES,
    GROUNDING_VALIDATION,
    RETRIEVAL_RESULT,
    SEARCH_REQUEST,
    AnswerValue,
    AnswerValidateGrounding,
    ContextBuild,
    RankDocuments,
    RetrieveExecutePlan,
    RetrieveExecutePlanOutputs,
    RetrieveFuse,
    StructuredGenerate,
)
from graphblocks.typed import BoundBlock, GraphBuilder, GraphOutput, NodeOutput, PortType


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

    assert document["apiVersion"] == "graphblocks.ai/v1"
    spec = document["spec"]
    assert isinstance(spec, dict)
    nodes = spec["nodes"]
    assert isinstance(nodes, dict)
    assert nodes["retrieve"] == {
        "block": "retrieve.execute_plan@1",
        "config": {"minimumSuccessfulSources": 2, "topK": 5},
    }
    assert {tuple(edge.values()) for edge in spec["edges"]} >= {
        ("$input.query", "retrieve.query"),
        ("$input.sources", "retrieve.sources"),
        ("retrieve.sources", "fuse.sources"),
        ("validate.candidate", "$output.candidate"),
        ("validate.validation", "$output.validation"),
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
        PortType("graphblocks.ai/Answer", AnswerValue)


def test_graph_interface_rejects_internal_collection_type_references() -> None:
    graph = GraphBuilder("invalid-interface-type")

    with pytest.raises(ValueError, match="major version"):
        graph.input("hits", PortType("List<graphblocks.ai/SearchHit@1>", object))


def test_typed_block_rejects_incompatible_input_wiring_at_runtime() -> None:
    graph = GraphBuilder("wrong-input-types")
    query = graph.input("query", SEARCH_REQUEST)
    sources = graph.input("sources", FEDERATED_SOURCES)

    with pytest.raises(TypeError, match="input 'query'.*SearchRequestValue"):
        graph.add(
            "retrieve",
            RetrieveExecutePlan().bind(
                query=cast(Any, sources),
                sources=cast(Any, query),
            ),
        )


def test_typed_block_rejects_missing_and_unexpected_input_keys() -> None:
    graph = GraphBuilder("wrong-input-keys")
    query = graph.input("query", SEARCH_REQUEST)

    with pytest.raises(ValueError, match="input keys.*missing.*query.*unexpected.*message"):
        BoundBlock(
            block_id="test.echo@1",
            inputs={"message": query},
            expected_inputs={"query": SEARCH_REQUEST},
            expected_outputs={},
            config={},
            _outputs=lambda _node_id, _owner: None,
        )


def test_typed_block_rejects_missing_declared_output() -> None:
    graph = GraphBuilder(
        "missing-block-output",
        block_catalog=BlockCatalog.from_blocks(
            [
                {
                    "typeId": "test.answer",
                    "version": 1,
                    "outputs": [{"name": "answer", "type": ANSWER.schema}],
                }
            ]
        ),
    )

    with pytest.raises(ValueError, match="output keys.*missing.*answer"):
        graph.add(
            "answer",
            BoundBlock(
                block_id="test.answer@1",
                inputs={},
                expected_inputs={},
                expected_outputs={"answer": ANSWER},
                config={},
                _outputs=lambda _node_id, _owner: None,
            ),
        )


def test_typed_graph_rejects_incompatible_published_output_at_runtime() -> None:
    graph = GraphBuilder("wrong-output-type")
    query = graph.input("query", SEARCH_REQUEST)
    sources = graph.input("sources", FEDERATED_SOURCES)
    candidate = graph.output("candidate", ANSWER)
    retrieve = graph.add(
        "retrieve",
        RetrieveExecutePlan().bind(query=query, sources=sources),
    )

    with pytest.raises(TypeError, match="graph output 'candidate'.*AnswerValue"):
        graph.publish(candidate, cast(Any, retrieve.result))


def test_typed_graph_rejects_same_marker_with_different_schema() -> None:
    graph = GraphBuilder(
        "wrong-output-schema",
        block_catalog=BlockCatalog.from_blocks(
            [
                {
                    "typeId": "test.answer",
                    "version": 1,
                    "outputs": [{"name": "answer", "type": ANSWER.schema}],
                }
            ]
        ),
    )
    candidate_type: PortType[AnswerValue] = PortType(
        "company.example/Answer@1",
        AnswerValue,
    )
    candidate = graph.output("candidate", candidate_type)
    answer_source = graph.add(
        "answer",
        BoundBlock(
            block_id="test.answer@1",
            inputs={},
            expected_inputs={},
            expected_outputs={"answer": ANSWER},
            config={},
            _outputs=lambda node_id, owner: NodeOutput(
                node_id,
                "answer",
                ANSWER,
                owner,
            ),
        ),
    )

    with pytest.raises(TypeError, match="company.example/Answer@1"):
        graph.publish(candidate, answer_source)


def test_typed_graph_rejects_self_certified_stdlib_output_contract() -> None:
    graph = GraphBuilder("self-certified-block")
    query = graph.input("query", SEARCH_REQUEST)
    sources = graph.input("sources", FEDERATED_SOURCES)

    with pytest.raises(ValueError, match="no catalog output port 'answer'"):
        graph.add(
            "retrieve",
            BoundBlock(
                block_id="retrieve.execute_plan@1",
                inputs={"query": query, "sources": sources},
                expected_inputs={"query": SEARCH_REQUEST, "sources": FEDERATED_SOURCES},
                expected_outputs={"answer": ANSWER},
                config={},
                _outputs=lambda node_id, owner: NodeOutput(
                    node_id,
                    "answer",
                    ANSWER,
                    owner,
                ),
            ),
        )


def test_typed_graph_rejects_forged_node_output_identity() -> None:
    graph = GraphBuilder("forged-output")
    query = graph.input("query", SEARCH_REQUEST)
    sources = graph.input("sources", FEDERATED_SOURCES)
    candidate = graph.output("candidate", ANSWER)
    retrieve = graph.add(
        "retrieve",
        RetrieveExecutePlan().bind(query=query, sources=sources),
    )
    forged = NodeOutput(
        "retrieve",
        "candidate",
        ANSWER,
        retrieve.result._owner,
    )

    with pytest.raises(ValueError, match="was not issued"):
        graph.publish(candidate, forged)


def test_typed_graph_rejects_forged_graph_output_identity() -> None:
    graph = GraphBuilder("forged-graph-output")
    query = graph.input("query", SEARCH_REQUEST)
    sources = graph.input("sources", FEDERATED_SOURCES)
    candidate = graph.output("candidate", ANSWER)
    retrieve = graph.add(
        "retrieve",
        RetrieveExecutePlan().bind(query=query, sources=sources),
    )
    forged = GraphOutput(
        candidate.name,
        RETRIEVAL_RESULT,
        candidate._owner,
    )

    with pytest.raises(ValueError, match="was not issued"):
        graph.publish(cast(Any, forged), retrieve.result)


def test_typed_graph_rejects_forged_input_reference_identity() -> None:
    graph = GraphBuilder("forged-input")
    query = graph.input("query", SEARCH_REQUEST)
    sources = graph.input("sources", FEDERATED_SOURCES)
    forged_query = NodeOutput(
        "missing",
        "query",
        SEARCH_REQUEST,
        query._owner,
    )

    with pytest.raises(ValueError, match="not an issued node output"):
        graph.add(
            "retrieve",
            RetrieveExecutePlan().bind(
                query=cast(Any, forged_query),
                sources=sources,
            ),
        )


def test_typed_stdlib_configs_reject_invalid_literals() -> None:
    with pytest.raises(ValueError, match="failure_mode"):
        RetrieveExecutePlan(failure_mode="continue")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fusion strategy"):
        RetrieveFuse(algorithm="unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="grounding failure policy"):
        AnswerValidateGrounding(  # type: ignore[arg-type]
            on_insufficient_evidence="continue"
        )

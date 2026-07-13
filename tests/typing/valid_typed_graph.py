from typing import assert_type

from graphblocks.stdlib_blocks import (
    ANSWER,
    FEDERATED_SOURCES,
    SEARCH_REQUEST,
    AnswerValidateGrounding,
    ContextBuild,
    RankDocuments,
    RetrievalSourcesValue,
    RetrieveExecutePlan,
    RetrieveFuse,
    SearchRequestValue,
    StructuredGenerate,
)
from graphblocks.typed import GraphBuilder, GraphInput, NodeOutput


graph = GraphBuilder("typing-valid")
query = graph.input("query", SEARCH_REQUEST)
sources = graph.input("sources", FEDERATED_SOURCES)
candidate = graph.output("candidate", ANSWER)

assert_type(query, GraphInput[SearchRequestValue])
retrieve = graph.add(
    "retrieve",
    RetrieveExecutePlan().bind(query=query, sources=sources),
)
assert_type(retrieve.sources, NodeOutput[RetrievalSourcesValue])
fused = graph.add("fuse", RetrieveFuse().bind(sources=retrieve.sources))
ranked = graph.add(
    "rank",
    RankDocuments().bind(query=query, hits=fused.hits),
)
context = graph.add("context", ContextBuild().bind(evidence=ranked.hits))
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
graph.build()

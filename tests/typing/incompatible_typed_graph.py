from graphblocks.stdlib_blocks import (
    ANSWER,
    FEDERATED_SOURCES,
    SEARCH_REQUEST,
    RetrieveExecutePlan,
)
from graphblocks.typed import GraphBuilder


graph = GraphBuilder("typing-invalid")
query = graph.input("query", SEARCH_REQUEST)
sources = graph.input("sources", FEDERATED_SOURCES)
candidate = graph.output("candidate", ANSWER)

RetrieveExecutePlan().bind(query=sources, sources=query)
retrieve = graph.add(
    "retrieve",
    RetrieveExecutePlan().bind(query=query, sources=sources),
)
graph.publish(candidate, retrieve.result)

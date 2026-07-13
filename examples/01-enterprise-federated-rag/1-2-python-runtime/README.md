# 1-2 Python runtime

This variant imports typed standard-library block definitions, wires their
typed ports with `GraphBuilder`, and invokes `InProcessRuntime` directly.

```python
from graphblocks.stdlib_blocks import (
    FEDERATED_SOURCES,
    SEARCH_HITS,
    SEARCH_REQUEST,
    RetrieveExecutePlan,
    RetrieveFuse,
)
from graphblocks.typed import GraphBuilder

graph = GraphBuilder("typed-retrieval")
query = graph.input("query", SEARCH_REQUEST)
sources = graph.input("sources", FEDERATED_SOURCES)
hits = graph.output("hits", SEARCH_HITS)
retrieve = graph.add(
    "retrieve",
    RetrieveExecutePlan(minimum_successful_sources=2, top_k=5).bind(
        query=query,
        sources=sources,
    ),
)
fused = graph.add("fuse", RetrieveFuse().bind(sources=retrieve.sources))
graph.publish(hits, fused.hits)
document = graph.build()
```

`RetrieveExecutePlan` has a concrete configuration dataclass, and `retrieve`
has a concrete output dataclass. Consequently, an IDE or static type checker can
detect misspelled output ports and incompatible connections before execution.
The default builder also treats the built-in catalog as authoritative: it
checks required and declared ports, exact schema-and-marker identity, and port
provenance, rejecting cross-builder or forged references even when static type
checking is skipped.

The builder materializes the same canonical `Graph` mapping used by YAML and
Rust, so the compiler, graph hash, and runtime callable registry remain shared.
`stdlib_registry()` is closed to undeclared handlers, and its runtime rejects a
cataloged handler that returns undeclared keys or omits required outputs. Use
`RuntimeRegistry(allow_untyped=True)` only for explicit custom test or
compatibility handlers without descriptors.

```bash
python examples/01-enterprise-federated-rag/1-2-python-runtime/run.py
```

It does not load the YAML graph or call the shared example integration runner.

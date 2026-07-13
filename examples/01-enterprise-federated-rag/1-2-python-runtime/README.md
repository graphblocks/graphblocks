# 1-2 Python runtime

This variant imports typed standard-library block definitions, wires their
typed ports with `GraphBuilder`, and invokes `InProcessRuntime` directly.

```python
from graphblocks.stdlib_blocks import RetrieveExecutePlan, RetrieveFuse
from graphblocks.typed import GraphBuilder

retrieve = graph.add(
    "retrieve",
    RetrieveExecutePlan(minimum_successful_sources=2, top_k=5).bind(
        query=query,
        sources=sources,
    ),
)
fused = graph.add("fuse", RetrieveFuse().bind(sources=retrieve.sources))
```

`RetrieveExecutePlan` has a concrete configuration dataclass, and `retrieve`
has a concrete output dataclass. Consequently, an IDE or static type checker can
detect misspelled output ports and incompatible connections before execution.
The builder materializes the same canonical `Graph` mapping used by YAML and
Rust, so the compiler, graph hash, and runtime callable registry remain shared.

```bash
python examples/01-enterprise-federated-rag/1-2-python-runtime/run.py
```

It does not load the YAML graph or call the shared example integration runner.

# Enterprise Federated RAG — three runtime styles

This example combines dense and keyword retrieval, fusion, reranking, a bounded
context builder, grounded answer generation, citation validation, and
abstention. Its binding document shows how provider and index choices remain
outside the portable graph.

Example 01 now presents the same deterministic RAG vertical slice through three
runtime entry points:

- [1-1 YAML runtime](1-1-yaml-runtime/README.md) loads `graph.yaml` through the
  `graphblocks run` CLI.
- [1-2 Python runtime](1-2-python-runtime/README.md) constructs the graph as
  Python dictionaries and calls `InProcessRuntime` directly.
- [1-3 Rust runtime](1-3-rust-runtime/README.md) constructs the graph with
  `serde_json::json!` and calls `run_stdlib_graph_with_options_json` directly.

All three execute federated retrieval, reciprocal-rank fusion, deterministic
reranking, bounded context construction, structured answer generation, and
grounding validation. They use the same inputs and must produce the same graph
hash, successful-node order, and normalized cited answer.

Run all variants together with the original production contract acceptance
checks:

```bash
python examples/01-enterprise-federated-rag/run.py
```

The combined runner and 1-3 require Rust 1.94 or newer. The 1-1 and 1-2
variants only require the Python development installation.

Each subdirectory is independently runnable. No variant contacts the production
resources named by the binding in `example.yaml`; all sources and the generated
answer are deterministic fixtures. The root enterprise RAG acceptance
application additionally executes citation and abstention semantic gates for
the full production-oriented graph.

# 1-1 YAML runtime

This variant defines the executable RAG graph in `graph.yaml`, loads
`inputs.json`, and invokes the public `graphblocks run` CLI with the pure-Python
runtime backend.

```bash
python examples/01-enterprise-federated-rag/1-1-yaml-runtime/run.py
```

The graph itself is loaded from YAML by the CLI. Before dispatching a block,
`graphblocks run` compiles it against the closed built-in catalog and rejects an
unregistered block (`GB1022`), unknown or reversed ports, missing required
inputs, nominal type mismatches, and optional outputs connected to required
targets. The catalog-backed runtime also rejects missing required outputs and
undeclared output keys.

The wrapper normalizes and verifies the runtime result for cross-language
parity. `graphblocks validate` and `plan` type-check every descriptor they
discover, but intentionally keep an open catalog so extension graphs can be
inspected before all provider packages are installed; successful execution is
the closed-world check.

# 1-1 YAML runtime

This variant defines the executable RAG graph in `graph.yaml`, loads
`inputs.json`, and invokes the public `graphblocks run` CLI with the pure-Python
runtime backend.

```bash
python examples/01-enterprise-federated-rag/1-1-yaml-runtime/run.py
```

The wrapper only normalizes the runtime result for cross-language parity; the
graph itself is loaded from YAML by the CLI.

# 1-2 Python runtime

This variant declares the graph and deterministic resources as Python values,
registers the standard library, and invokes `InProcessRuntime` directly.

```bash
python examples/01-enterprise-federated-rag/1-2-python-runtime/run.py
```

It does not load the YAML graph or call the shared example integration runner.

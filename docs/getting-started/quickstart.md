# Quickstart

Install the [root development package](installation.md), then validate and plan
one of the checked-in application contracts:

```bash
python -m graphblocks validate examples/01-enterprise-federated-rag/example.yaml
python -m graphblocks plan examples/01-enterprise-federated-rag/example.yaml --expand
python examples/01-enterprise-federated-rag/run.py
```

The example runner also executes deterministic semantic checks with recording
retriever/model fakes. Real network access is blocked and the final JSON line
contains the checks, mocked boundaries, call-input digests, and evidence digest.

Inspect project assets:

```bash
python -m graphblocks plugins list
python -m graphblocks packages doctor --root .
python -m graphblocks schemas manifest schemas
```

Run the reference runtime against a locally compilable graph:

```bash
python -m graphblocks run graph.yaml --input-json '{"message":{"text":"Hello"}}'
```

The separate `graphblocks-testing` workspace package provides the TCK command
and acceptance runner. Contributors working on conformance can invoke its module
from the workspace or install that package explicitly; see
[testing](../development/testing.md).

Next read [graphs and bindings](../concepts/graphs-and-bindings.md) and
[conformance](../development/conformance.md).

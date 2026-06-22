# GraphBlocks

GraphBlocks is a provider-neutral contract toolkit for authoring and validating
GraphBlocks graph, binding, application, and plugin manifest documents.

This repository starts from the upstream v1.0 architecture bundle in
`docs/upstream/GraphBlocks_v1.0_Final`. The first implemented scope follows
`GB-C0-SCHEMA`: parse, migrate, normalize, hash, validate, and inspect static
plugin manifests without importing provider SDKs.

## Install for development

```bash
python -m pip install -e '.[test]'
```

## CLI

```bash
graphblocks validate docs/upstream/GraphBlocks_v1.0_Final/examples/01-enterprise-federated-rag.yaml
graphblocks plan docs/upstream/GraphBlocks_v1.0_Final/examples/01-enterprise-federated-rag.yaml --expand
graphblocks plugins list
```

## Runtime Slice

The package includes an early deterministic in-process runtime for the Phase 1
vertical slice. It executes normalized finite graphs with the bundled stdlib
blocks and records a single-terminal execution journal.

```bash
graphblocks run graph.yaml --input-json '{"message":{"text":"Hello"}}'
```

The package intentionally does not include model provider, parser, database,
cloud, server, TUI, Kubernetes, Terraform, voice, or durable stream SDKs.

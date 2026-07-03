# GraphBlocks

Don't reinvent wheel.

GraphBlocks is a provider-neutral contract layer for AI applications. It gives
teams a shared way to describe graphs, bindings, packages, policies, runtime
behavior, and conformance without forcing one model provider, vector database,
document parser, server framework, or deployment platform.

Use the tools you already trust. Use GraphBlocks to make their contracts
portable, testable, and governable.

This repository starts from the upstream v1.0 architecture bundle in
`docs/upstream/GraphBlocks_v1.0_Final`. The implementation follows the
conformance-profile order in that bundle: schema/compiler contracts first,
then local runtime, governed runtime, application contracts, and optional
adapter packages.

The Rust workspace owns the normative compiler/runtime mechanics:
`graphblocks-schema`, `graphblocks-compiler`, `graphblocks-runtime-core`,
`graphblocks-runtime-seq`, `graphblocks-runtime-durable`, and the single PyO3
binding crate `graphblocks-python`. Python `graphblocks-core` is the
authoring/schema facade and must match the Rust compiler TCK results and
canonical hashes.

## Documentation

- [Documentation overview](docs/README.md)
- [Getting started](docs/getting-started.md)
- [Core concepts](docs/concepts.md)
- [Runtime model](docs/runtime.md)
- [Package model](docs/packages.md)
- [Conformance and TCK](docs/conformance.md)

The upstream architecture bundle is preserved under
`docs/upstream/GraphBlocks_v1.0_Final` as a reference source. The top-level
documentation is the maintainer-facing and user-facing guide for this
implementation.

## Install for development

```bash
python -m pip install -e '.[test]'
```

## CLI

```bash
graphblocks validate docs/upstream/GraphBlocks_v1.0_Final/examples/01-enterprise-federated-rag.yaml
graphblocks plan docs/upstream/GraphBlocks_v1.0_Final/examples/01-enterprise-federated-rag.yaml --expand
graphblocks plugins list
graphblocks packages doctor --root .
graphblocks schemas manifest schemas
```

`graphblocks-testing` also installs a focused TCK inventory helper:

```bash
graphblocks-tck list tck
graphblocks-tck check tck --profiles src/graphblocks/data/conformance-profiles.yaml --profile GB-C3-GOVERNED-RUNTIME
graphblocks-tck run policy tck/policy/cases.json
graphblocks-tck run-all tck
```

## Runtime

The package includes a deterministic in-process Python runtime and Rust runtime
crates for scheduler, lifecycle, cancellation, bounded sequence, output policy,
tool lifecycle, usage, budget, and exhaustion semantics. The Python runtime
wheel delegates native behavior to `crates/graphblocks-python`.

```bash
graphblocks run graph.yaml --input-json '{"message":{"text":"Hello"}}'
graphblocks run graph.yaml --runtime native --input-json '{"message":{"text":"Hello"}}'
```

Optional provider, parser, database, cloud, server, TUI, Kubernetes,
Terraform, voice, and durable stream packages are present as lightweight
contract/adaptor packages. They avoid large default SDK dependencies unless an
integration package explicitly declares an optional extra.

## Conformance

Shared TCK fixtures live under `tck/`:

- `application-events`
- `application-protocol`
- `approval-review`
- `budget-race`
- `compiler`
- `conversation`
- `deployment`
- `documents`
- `durable`
- `exhaustion`
- `orchestration`
- `policy`
- `rag`
- `retry`
- `runtime`
- `schema`
- `sequence`
- `tool-execution`
- `tool-lifecycle`
- `tool-result`
- `usage`
- `voice`

Rust and Python harnesses consume these fixtures where the suite is applicable.
The implemented profile catalog is in `src/graphblocks/data/conformance-profiles.yaml`.

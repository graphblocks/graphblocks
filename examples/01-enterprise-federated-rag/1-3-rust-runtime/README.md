# 1-3 Rust runtime

This variant imports typed stdlib block definitions and wires them with Rust
`Port<T>` values. `GraphBuilder` materializes the same portable Graph document
used by the YAML runtime, while `run_stdlib_graph_with_options` accepts the
typed document and returns a `StdlibRunResult` without a JSON string round trip.

Configuration is also block-specific (`RetrieveFuseConfig`,
`ContextBuildConfig`, and so on). A mismatched connection such as passing a
`Port<SearchHitsValue>` to an input requiring `Port<ContextPackValue>` is a Rust
compile-time error.

`GraphBuilder::new` includes the official stdlib catalog. Private port
constructors prevent fabricating a node output, while `GraphBuilder::add` and
`bind_output` return an error for an unknown block, missing required port,
catalog/type disagreement, cross-builder reference, or invalid output source.
The repository's trybuild fixtures cover both incompatible `Port<T>` wiring and
port/type-identity forgery. Thus static Rust errors and builder-time catalog
errors are separate, complementary checks. Manifest/runtime parity tests also
execute the fixed stdlib handlers against their advertised input and output
aliases.

```bash
cargo run --locked \
  --manifest-path examples/01-enterprise-federated-rag/1-3-rust-runtime/Cargo.toml
```

The combined example runner invokes this Cargo command directly; the Rust
variant has no Python launcher or runtime dependency.

The Rust program shares only the deterministic input fixture with 1-1. JSON
values remain at the payload/wire boundary, but graph structure, block ports,
runtime options, and the result envelope use typed Rust APIs.

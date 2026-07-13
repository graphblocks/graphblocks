# 1-3 Rust runtime

This variant declares the graph with Rust `serde_json::json!` values and calls
`graphblocks_runtime_core::stdlib_runtime::run_stdlib_graph_with_options_json`
directly.

```bash
python examples/01-enterprise-federated-rag/1-3-rust-runtime/run.py
```

Or invoke Cargo directly:

```bash
cargo run --locked \
  --manifest-path examples/01-enterprise-federated-rag/1-3-rust-runtime/Cargo.toml
```

The Rust program shares only the deterministic JSON input fixture with 1-1; it
constructs the graph in Rust and executes without Python.

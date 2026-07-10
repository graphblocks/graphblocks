# Testing

Run the root Python suite after installing the development extra:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest
```

Run Rust formatting, lint, and tests with the workspace toolchain:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace --all-targets
```

Example-local integration tests invoke each example's runner. Documentation
integrity tests verify links and ensure retired bundle artifacts do not become a
second source of truth.

For conformance work, install or expose the workspace
`packages/graphblocks-testing` distribution and use the commands in
[conformance](conformance.md). A green unit suite alone does not establish a
profile claim; required TCK and acceptance evidence must also pass.

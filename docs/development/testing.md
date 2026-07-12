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

Before a release, build the complete three-distribution Python surface and
prove that those artifacts install without an index from the resulting
wheelhouse:

```bash
python -m pip install build hatchling maturin
python tools/verify_wheelhouse.py --wheelhouse dist/wheelhouse
```

The verifier requires the installed CLI's complete schema manifest, including
every entry and digest, to match the checked-in `schemas/` manifest exactly.

The verifier builds exactly the root `graphblocks` project,
`packages/graphblocks-runtime`, and `packages/graphblocks-testing`. It resolves
their external runtime dependencies into the wheelhouse, installs the three
generated wheel artifacts into a fresh environment with `--no-index`, and runs
`pip check`. Catalog package identities are not additional wheel build targets.

The Rust release gate also packages every workspace crate. Path dependencies
therefore declare both a local path and a publishable version, while crate tests
consume fixtures shipped inside the crate archive.

Example-local integration tests invoke each example's runner. Documentation
integrity tests verify links and ensure retired bundle artifacts do not become a
second source of truth.

For conformance work, install or expose the
`packages/graphblocks-testing` distribution and use the commands in
[conformance](conformance.md). A green unit suite alone does not establish a
profile claim; required TCK and acceptance evidence must also pass. TCK reports
are valid claim evidence only when they contain at least one executed case and
bind a suite id, implementation identity and version, and fixture digest. A
native-profile report containing any local fallback is failed evidence.

# Installation

GraphBlocks is currently developed from source. The monorepo contains multiple
Python distributions at different stages of packaging, so this page documents
the verified contributor setup rather than promising a unified published
installation.

## Requirements

- Python 3.11 or newer
- `pip`
- the Rust toolchain selected by `rust-toolchain.toml` for Rust work

## Root authoring package

From the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m graphblocks --help
```

This installs `graphblocks-core`, PyYAML, and pytest in the activated virtual
environment. Use `python -m graphblocks`; the root distribution intentionally does not install a
`graphblocks` console script.

## Optional workspace packages

Provider, parser, server, callback, deployment, telemetry, voice, and durable
stream adapters live under `packages/`. Install only the package and optional
extras required for the integration you are developing. Internal versions and
dependency ranges are still being reconciled for publication, so do not assume
the entire monorepo is installable from a public package index.

Continue with the [quickstart](quickstart.md).

## Python-free native CLI

The Rust workspace provides `graphblocks-native` without a Python runtime
dependency:

```bash
cargo build -p graphblocks-cli-native
target/debug/graphblocks-native run --input-json '{"message":{"text":"hello"}}' < graph.json
```

The native CLI currently accepts one JSON or YAML `Graph` from stdin, or selects
a named `Graph` from a multi-document YAML stream with `--graph NAME`, and
supports the native stdlib block set. Examples that use integration blocks still
run through the Python authoring layer plus deterministic fakes. `graphblocksd`
currently processes worker-control messages and SQLite checkpoint recovery
claims; it does not bind a server socket or expose a `serve` command.

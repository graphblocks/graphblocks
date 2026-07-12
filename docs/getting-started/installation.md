# Installation

GraphBlocks is currently developed from source. This page documents the
verified contributor setup for its three Python distributions rather than
promising availability from a public package index.

## Requirements

- Python 3.11 or newer
- `pip`
- the Rust toolchain selected by `rust-toolchain.toml` for Rust work

## Pure-Python SDK

From the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
graphblocks --help
python -m graphblocks --help
```

This installs the `graphblocks` distribution in editable mode. It contains the
public SDK, built-in block implementations, pure-Python reference runtime, CLI,
and framework-neutral server contracts. The `graphblocks` command and
`python -m graphblocks` expose the same CLI. `GraphBlocksServerApp` handles the
project request/response contract but does not start a network listener or add a
`serve` command.

Extras add concrete install dependencies rather than enabling internal feature
packages:

- `graphblocks[runtime]` adds `graphblocks-runtime`.
- `graphblocks[pdf]` adds `pypdf`.
- `graphblocks[test]` adds pytest for repository development.

The built-ins, CLI, and server contracts are always part of the base
`graphblocks` install.

## Native Python runtime

Install the native extension separately when you need the Rust-backed Python
entry points:

```bash
python -m pip install ./packages/graphblocks-runtime
python -c 'import graphblocks_runtime; graphblocks_runtime.require_native_extension()'
graphblocks run graph.yaml --runtime native --input-json '{"message":{"text":"Hello"}}'
```

`graphblocks-runtime` builds the independent `graphblocks_runtime._native`
module with the selected Rust toolchain. It is not required for the pure-Python
SDK; the root distribution's `runtime` extra is a convenience dependency on
this wheel. For a fresh source checkout that uses the extra, install the local
runtime project first, then install the root project with
`python -m pip install -e '.[runtime,test]'`. This lets pip satisfy the extra
from the locally built runtime rather than expecting a public package index.

## TCK tooling

Install the conformance tools separately when developing or verifying a
profile implementation:

```bash
python -m pip install -e ./packages/graphblocks-testing
graphblocks-tck --help
```

`graphblocks-testing` depends on `graphblocks` and owns the `graphblocks-tck`
command. It is not part of the root `test` extra. Its `runtime` extra adds
`graphblocks-runtime` for native-profile TCK work.

Continue with the [quickstart](quickstart.md).

## Python-free native CLI

The Rust workspace also provides `graphblocks-native`, a standalone executable
with no Python runtime dependency:

```bash
cargo build -p graphblocks-cli-native
target/debug/graphblocks-native run --input-json '{"message":{"text":"hello"}}' < graph.json
```

The native CLI currently accepts one JSON or YAML `Graph` from stdin, or selects
a named `Graph` from a multi-document YAML stream with `--graph NAME`, and
supports the native stdlib block set. Examples that use integration blocks still
run through the Python authoring layer plus deterministic fakes. `graphblocksd`
currently processes worker-control messages and SQLite checkpoint claim
lifecycle commands; it does not bind a server socket or expose a `serve`
command.

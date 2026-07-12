# GraphBlocks

[English](README.md) | [한국어](README.ko.md) | [简体中文](README.zh-CN.md)

> Don't reinvent the wheel.

GraphBlocks is a provider-neutral contract toolkit for portable, testable, and
governable AI applications. It defines typed graphs, runtime behavior,
application protocols, policy and budget boundaries, package metadata, and
conformance profiles without requiring a particular model provider, database,
parser, server framework, or deployment platform.

The project is alpha software. Compatibility is claimed by conformance profile
and executable evidence, not by package or directory presence.

## What is here

- The pure-Python `graphblocks` SDK, including authoring, validation, built-in
  blocks, the reference runtime, CLI, and framework-neutral server contracts.
- The optional native `graphblocks-runtime` Python extension.
- The `graphblocks-testing` distribution and shared TCK fixtures.
- Rust schema, compiler, protocol, and runtime crates.
- Versioned schemas and provider-neutral package catalogs.
- Shared TCK fixtures and executable acceptance applications.

## Development quickstart

Requirements are Python 3.11 or newer and the Rust toolchain selected by
`rust-toolchain.toml`.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m graphblocks validate examples/01-enterprise-federated-rag/example.yaml
python examples/01-enterprise-federated-rag/run.py
python -m pytest
cargo test --workspace --all-targets
```

After virtual-environment activation, the root editable install provides
the `graphblocks` import package, the `graphblocks` command, and
`python -m graphblocks`. Built-in block implementations and the CLI and server
contracts are part of that distribution; they are not separate feature wheels.
Extras add actual install dependencies: `runtime` adds the native bindings,
`pdf` adds `pypdf`, and `test` adds pytest. Install `graphblocks-testing` for
the `graphblocks-tck` command.

The machine-readable package catalog distinguishes release artifacts from
portable component and binding identities. Component entries do not correspond
to separately published Python wheels. The Python release surface consists of
`graphblocks`, `graphblocks-runtime`, and `graphblocks-testing`.

The repository also builds `graphblocks-native`, a Python-free Rust executable
for `validate`, `plan`, and `run`. It accepts JSON or YAML on stdin, can select a
named `Graph` from a multi-document YAML stream, and executes the native stdlib
block set. `graphblocksd` is a worker control-plane command, not yet a listening
HTTP/server process.

## Documentation

- [Documentation map](docs/README.md)
- [Installation](docs/getting-started/installation.md)
- [Quickstart](docs/getting-started/quickstart.md)
- [Architecture](docs/concepts/architecture.md)
- [Living specification](docs/specification/README.md)
- [Conformance](docs/development/conformance.md)
- [Implementation status](docs/project/status.md)
- [Examples](examples/README.md)

## Project and community

GraphBlocks is licensed under [Apache License 2.0](LICENSE). Contributions are
welcome; see [CONTRIBUTING.md](CONTRIBUTING.md),
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), [SECURITY.md](SECURITY.md), and
[GOVERNANCE.md](GOVERNANCE.md).

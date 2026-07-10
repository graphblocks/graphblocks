# GraphBlocks

GraphBlocks is a provider-neutral contract toolkit for portable, testable, and
governable AI applications. It defines typed graphs, runtime behavior,
application protocols, policy and budget boundaries, package metadata, and
conformance profiles without requiring a particular model provider, database,
parser, server framework, or deployment platform.

The project is alpha software. Compatibility is claimed by conformance profile
and executable evidence, not by package or directory presence.

## What is here

- A Python authoring, validation, and reference-runtime implementation.
- Rust schema, compiler, protocol, and runtime crates.
- Versioned schemas and provider-neutral package catalogs.
- Shared TCK fixtures and executable acceptance applications.
- Optional adapter packages for existing provider, storage, policy,
  observability, deployment, voice, and durable-stream ecosystems.

## Development quickstart

Requirements are Python 3.11 or newer and the Rust toolchain selected by
`rust-toolchain.toml`.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m graphblocks validate examples/01-enterprise-federated-rag/example.yaml
python -m pytest
cargo test --workspace --all-targets
```

After virtual-environment activation, the root editable install provides
`python -m graphblocks`. The separately
packaged command-line and testing distributions are developed under `packages/`;
they are not installed by the root test extra.

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

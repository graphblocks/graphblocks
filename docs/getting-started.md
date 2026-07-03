# Getting Started

GraphBlocks is developed as a mixed Python and Rust workspace. The fastest path
is to install the Python package in editable mode, then use the CLI against the
checked-in examples and TCK fixtures.

## Requirements

- Python 3.11 or newer
- Rust toolchain matching the workspace `Cargo.toml`
- `pip`

## Install

```bash
python -m pip install -e '.[test]'
```

This installs the local Python authoring package and test extras. Provider,
parser, database, cloud, server, TUI, Kubernetes, Terraform, voice, and durable
stream dependencies are intentionally not installed by default.

## Validate an Example

```bash
graphblocks validate docs/upstream/GraphBlocks_v1.0_Final/examples/01-enterprise-federated-rag.yaml
graphblocks plan docs/upstream/GraphBlocks_v1.0_Final/examples/01-enterprise-federated-rag.yaml --expand
```

## Inspect Packages and Schemas

```bash
graphblocks plugins list
graphblocks packages doctor --root .
graphblocks schemas manifest schemas
```

## Run TCK Fixtures

```bash
graphblocks-tck list tck
graphblocks-tck run schema tck/schema/cases.json
graphblocks-tck run-all tck
```

For focused profile work, run the profile check first:

```bash
graphblocks-tck check tck --profiles src/graphblocks/data/conformance-profiles.yaml --profile GB-C3-GOVERNED-RUNTIME
```

## Next Steps

Read [Core concepts](concepts.md) before adding a new package or runtime
contract. Read [Conformance and TCK](conformance.md) before claiming support for
a profile.

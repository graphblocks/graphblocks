# Custom Python and Rust blocks

This example executes a two-node graph with real custom implementations:

1. `examples.python.normalize-text@1` runs a Python worker callable.
2. `examples.rust.text-stats@1` runs a Rust worker binary.

Both implementations receive the versioned `WorkerInvokeRequest` contract and
return `WorkerInvokeResult`. The local example adapter validates invocation ID,
node-attempt ID, lease epoch, correlation, and causation before exposing outputs
to the Python `InProcessRuntime` registry. No external API is contacted and no
fixture block replaces either implementation.

## Run

Install the root project and use Rust 1.94 or newer, then run from the repository
root:

```bash
python examples/12-custom-python-rust-blocks/run.py
```

The first run builds the standalone Rust worker in a temporary Cargo target
directory. The JSON result records both worker calls and their canonical request
and result digests.

Run only this integration test with:

```bash
python -m pytest examples/12-custom-python-rust-blocks/test_custom_python_rust_blocks.py
```

The files have separate responsibilities:

- `graphblocks-plugin.yaml` provides static block and port descriptors for
  validation and planning.
- `schemas/` defines the custom values crossing the block boundary.
- `python/custom_block.py` implements the Python worker callable.
- `rust/src/main.rs` implements the Rust stdin/stdout worker.
- `integration.yaml` binds those implementations into this local executable
  example and declares exact expected evidence.

## Current loader boundary

Static plugin discovery does not import executable code. The shared runner
explicitly registers adapters for this example, and it supplies the local Rust
subprocess transport. `graphblocks run` cannot load this custom registry, and
`graphblocks-native` remains limited to its statically linked stdlib blocks.
Production hosts must provide an equivalent authorized registry and worker
transport; the manifest's `implementation` value alone is not an executable
loader.

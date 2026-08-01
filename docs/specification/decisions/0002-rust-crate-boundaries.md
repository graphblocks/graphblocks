# ADR-0002: Rust Crate Boundary Budget

- Status: Accepted
- Date: 2026-07-31
- Decision owners: GraphBlocks maintainers

## Context

The Rust workspace grew before its public Rust API and release trains were
stable. A crate boundary has real costs: coordinated versions, publish order,
feature combinations, dependency review, and a larger compatibility surface.
Those costs are not justified by a package that only re-exports another crate
or by an implementation module with one in-workspace consumer.

Two boundaries did not meet that test:

- `graphblocks-types` contained only a `TypedValue` re-export from
  `graphblocks-schema` and had no consumer.
- `graphblocks-runtime-seq` depended on `graphblocks-runtime-core`; only the
  Python binding consumed it, and its bounded sequence, port-channel, and tool
  queue primitives are part of the local runtime lifecycle.

## Decision

`graphblocks-types` is retired. `TypedValue` remains owned and exported by
`graphblocks-schema`.

`graphblocks-runtime-seq` is absorbed into `graphblocks-runtime-core` as the
`bounded`, `port_channel`, and `tool_queue` modules. Its tests and sequence TCK
fixture move with the implementation. The Python binding consumes those
modules through `graphblocks-runtime-core`.

A Rust crate must now have at least one of these reasons to remain separate:

1. it produces a separately installed binary or language binding;
2. it owns a versioned wire, schema, compiler, or persistence boundary used by
   multiple consumers;
3. it is an optional extension profile with meaningful implementation and test
   weight whose dependencies should be compile-isolated; or
4. it is an explicitly reserved distribution artifact.

The retained boundaries are:

| Crate | Boundary and consumer rationale | Rust API budget |
| --- | --- | --- |
| `graphblocks` | Reserved crates.io name; no implementation contract. | Reserved only; no supported API. |
| `graphblocks-flow` | Optional flow-control extension with its own lease, rate, semaphore, and ticket implementation and tests. | Internal until separately promoted. |
| `graphblocks-telemetry` | Optional observability extension with a distinct implementation and crate-local observability tests. | Internal until separately promoted. |
| `graphblocks-cli-native` | Produces the separately installed `graphblocks-native` executable. | Preview executable; library API is not supported. |
| `graphblocks-schema` | Owns schema identity, typed values, canonical schema behavior, and shared compiler/runtime inputs. | Internal normative implementation. |
| `graphblocks-compiler` | Owns the normative compilation boundary consumed by the native CLI, runtime, and Python binding. | Internal normative implementation. |
| `graphblocks-runtime-core` | Owns local lifecycle and execution primitives shared by bindings and durable/control-plane layers. | Internal normative-target implementation. |
| `graphblocks-runtime-durable` | Owns the durable and checkpoint persistence boundary consumed by the Python binding and control plane. | Internal extension implementation. |
| `graphblocks-python` | Produces the PyO3 native extension installed by the Python runtime wheel. | Binding ABI is release-controlled; Rust API is internal. |
| `graphblocks-protocol` | Owns versioned worker wire contracts consumed by the Python binding and control plane. | Internal versioned protocol implementation. |
| `graphblocks-control-plane` | Produces a reusable control-plane library and the separate one-shot `graphblocks-control` executable. | Internal library and executable. |

All retained implementation crates remain on the coordinated pre-1.0 version
train. Being a workspace member does not create a stable Rust SemVer claim.
Promotion requires an independent consumer review, public API snapshot, package
evidence, and an update to this decision.

## Consequences

- The workspace loses two publishable packages and one dependency edge from the
  Python binding.
- Sequence behavior keeps the same module-level contracts and TCK evidence
  under runtime core.
- Typed-value ownership is unambiguous in the schema crate.
- CI checks that every workspace package appears in this decision and that the
  retired manifests do not return.

## Rejected alternatives

- **Keep compatibility-only crates.** There is no released Rust compatibility
  surface to preserve, and carrying empty package boundaries would make that
  surface harder to define.
- **Merge every extension into runtime core.** Flow control, telemetry, and
  durable storage have distinct dependency or profile boundaries with
  substantial implementation and test weight.

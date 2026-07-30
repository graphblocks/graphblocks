# Runtime

The runtime executes a compiled physical plan while preserving typed values,
dependency readiness, journal ordering, cancellation, outcomes, and admission
boundaries.

Core responsibilities include scheduling, bounded flow, state patches,
compare-and-swap updates, ownership leases and fencing, retry/timeout behavior,
tool admission, output delivery policy, usage reconciliation, budget permits,
and checkpointed async operations.

The execution journal and application event stream are authoritative records.
Client streams, callbacks, and observability exporters are projections. A
projection failure must not rewrite the authoritative outcome.

The base `graphblocks` distribution provides the Python reference runtime and
built-in registry. Authoring and explicit reference-runtime entry points do not
require the native extension. Public Graph compilation is different: it uses
the normative Rust compiler from `graphblocks-runtime` and fails closed when the
binding is unavailable. The Python compiler remains available only through the
explicit `graphblocks.compiler.compile_graph_reference` oracle.

`RuntimeRegistry()` is closed by default: its empty catalog does not accept
arbitrary handlers, duplicate `register` calls fail, and `replace` is required
for an intentional handler replacement. `stdlib_registry()` provides the
built-in catalog. Tests and compatibility adapters may explicitly opt into
`RuntimeRegistry(allow_untyped=True)`, but production extensions should publish
descriptors and construct a catalog-backed registry.

For every cataloged block, the runtime rejects non-mapping results, output keys
not declared by the descriptor, and omitted required outputs. The same contract
applies when resuming a callback. These checks enforce port membership and
requiredness; schema and domain validators remain responsible for the fields
inside each value. See [type safety](type-safety.md).

Long-running accepted runs may detach and later replay from a cursor. Callback
resume requires authentication, schema validation, operation/attempt/provider
identity fences, journal-before-resume ordering, and renewed policy, budget,
release, and ownership admission. `GraphBlocksServerApp` defines a
framework-neutral request/response contract rather than binding a network
socket. Its checkpoint continuation is process-local and is not restart-durable.
The separate preview `DurableAcceptedRunServerApp` stores accepted runs,
cursor events, callback continuation, cancellation, expiration, and
pause/resume controls, fencing state, and completion effects through a durable
repository; the SQLite implementation can recover those transitions after
process restart. Cancellation and expiration are terminal and fence late
workers and callbacks. A paused run retains its exact resume phase and cannot
be claimed. Valid callbacks may still be recorded with their issuance-time
state version while paused, but execution remains gated until an owner-scoped
resume establishes fresh state and fencing authority.

See [async runs and callbacks](../guides/async-runs-and-callbacks.md) and the
normative [runtime specification](../specification/operations/applications-async-callbacks.md).

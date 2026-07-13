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

The base `graphblocks` distribution provides the pure-Python reference runtime
and built-in registry. Native Python entry points are isolated in the optional
`graphblocks-runtime` distribution; the SDK and its built-ins do not require the
native extension.

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

See [async runs and callbacks](../guides/async-runs-and-callbacks.md) and the
normative [runtime specification](../specification/operations/applications-async-callbacks.md).

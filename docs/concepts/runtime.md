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

Long-running accepted runs may detach and later replay from a cursor. Callback
resume requires authentication, schema validation, operation/attempt/provider
identity fences, journal-before-resume ordering, and renewed policy, budget,
release, and ownership admission. The Python reference server's checkpoint
continuation is process-local and is not restart-durable.

See [async runs and callbacks](../guides/async-runs-and-callbacks.md) and the
normative [runtime specification](../specification/operations/applications-async-callbacks.md).

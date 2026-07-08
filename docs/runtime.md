# Runtime Model

The runtime is where GraphBlocks moves from contract documents to executable
behavior. The Rust crates own the normative runtime mechanics; Python provides
the authoring facade, packaging surface, and integration layer.

## Responsibilities

The runtime contract covers:

- scheduling and dependency readiness
- typed send and receive ports
- terminal outcome semantics
- structured cancellation
- bounded sequence execution
- run stores and execution journals
- leases and fencing
- state patches and compare-and-swap boundaries
- policy enforcement points
- tool admission and output delivery policy
- usage and budget reconciliation
- async operations and callback replay

## Local Runtime

The local runtime profile focuses on deterministic execution, cancellation,
bounded flow, journaling, and Python binding behavior. It is the foundation for
higher-level AI application and governed runtime profiles.

## Tool and Output Policy

Tool execution is not authorized by model output alone. GraphBlocks separates
tool definitions from tool bindings and requires runtime admission before a tool
effect executes. Streaming output passes through policy enforcement before
mandatory client delivery.

## Async Runs and Callbacks

Long-running runs can be accepted or backgrounded. The application event stream
is the replayable source of truth; callback subscriptions are delivery
projections. External callbacks are authenticated resume signals for async
operations, not the authoritative record of run correctness.

The runtime event stream rejects duplicate event IDs and non-increasing
sequences within a run before applying response-specific policy cutoff rules.
This keeps replay cursor state authoritative while still allowing different
runs to use independent sequence ranges.

Async operation state records `callback_received_at` separately from
`completed_at`. A callback receipt can move an operation into
`callback_received` and then `resuming`; `completed_at` is reserved for terminal
operation states such as `completed`, `failed`, `cancelled`, or `expired`.
Durable callback receipt replay must verify the stored run, node, attempt, and
provider-operation identity against the operation record before admitting any
duplicate callback or resume decision.
Server callback ingress also treats provider-operation identity as a fence
between receipts for the same operation: once a receipt records a provider
operation, later receipts for that operation must not omit or change it.
Callback helper endpoint references may also pin the provider-operation
identity so resume admission rejects stale provider callbacks even when the
run, node, attempt, and operation ids still match.
The Rust runtime and callback receipt facade reject async callbacks whose
verifier is explicitly `unauthenticated` before normal receipt, artifact
compaction, or pre-operation quarantine can create resumable state.
Callback receipt projection also rejects operation identity drift when a
callback envelope includes `operation_id`; the durable receipt must name the
same operation as the authenticated envelope.
Receipt projection only accepts `ExternalCallbackReceived` envelopes, so
ordinary callback-subscription events cannot become async resume receipts.
Persisted run provenance is also replay-validated without type coercion so
corrupted release or physical-plan identity cannot become authoritative state.

Callback dead-letter records preserve original delivery identity and a
consecutive attempt history starting at attempt `1`. Redrive uses that history
to choose the next pending delivery attempt without creating a duplicate
application event.

## Production Boundaries

Remote workers, deployments, and release bundles add more checks: worker
advertisement, protocol compatibility, package lock identity, remote payload
serialization, artifact references, immutable release digests, and physical plan
hashes.

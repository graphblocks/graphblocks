# Applications, Async Runs, and Callbacks

This contract preserves the durable async and callback semantics that were
previously embedded only in the retired monolithic architecture document.

## Application protocol

An application exposes versioned commands, routes, event streams, and callback
endpoints separately from its graph. Run invocation modes are synchronous,
accepted, or background. Accepted/background invocation MUST return a stable run
identity and an initial replay cursor before the initiating connection is
required to remain open.

Clients MAY attach, detach, replay after a cursor, request cancellation, inspect
status, and register delivery subscriptions. Per-run application event sequence
numbers MUST increase monotonically. Replaying the same event identity and
content is idempotent; a duplicate identity with different content or a
non-increasing new sequence MUST fail.

The `ApplicationEventStream` is authoritative. SSE, WebSocket, long polling,
CLI/TUI attachment, local callbacks, and webhooks are projections. Run
correctness MUST NOT depend on projection delivery success, and exactly-once
callback delivery MUST NOT be promised.

Durable runtime implementations MUST persist the authoritative stream in a
restart-safe store before projecting events to any delivery transport. The
runtime-core reference implementation provides `SqliteApplicationProtocolLog`,
which stores immutable event envelopes, validates decoded rows against their
stored run, sequence, cursor, and event identity, and rebuilds the in-memory
`ApplicationProtocolLog` on reopen so duplicate, cursor, run, and sequence
rules remain identical across process restarts. Cursor replay MUST use the
persisted stream; retained replay that cannot satisfy an old cursor MUST report
the requested cursor, earliest retained cursor, last cursor, and last sequence.

The native stdlib runtime exposes this through the
`applicationEventStorePath` runtime option. When set, graph execution persists a
per-run SQLite application event stream containing `RunStarted` and the terminal
run event before clients attach or replay from that store.
One SQLite store MAY contain multiple run streams. Event sequence and replay
cursor uniqueness are per run; clients MUST supply the run identity when
replaying from a shared store.
Durable attach implementations SHOULD call the same retained cursor replay over
the persisted stream; if the requested cursor has expired, the response SHOULD
include the earliest retained cursor, last cursor, last sequence, and current
run status when available.

Example durable event-stream configuration:

```yaml
runtime:
  applicationEventStream:
    store:
      kind: sqlite
      path: /var/lib/graphblocks/runs/application-events.sqlite

    replay:
      retention: 14d
      retainedEventCount: 10000
      cursorExpiredResponse: include_run_status
```

## Subscriptions and webhook delivery

A callback subscription binds run, event filter, target, delivery policy, and
tenant/principal scope. Webhook targets MUST declare timeout, retry limits,
payload bound, and a signing method. Redirects, DNS resolution, and egress MUST
obey deployment policy.

For hostname targets, the dispatcher MUST resolve every candidate address and
reject the delivery if any candidate violates the egress policy. The transport
MUST connect to one of those validated addresses without resolving the hostname
again; the original hostname remains the HTTP authority and TLS server name.
A default-deny egress policy MUST reject literal and resolved addresses that are
not globally routable, including private, loopback, link-local, shared (CGNAT),
benchmarking, documentation, reserved, unspecified, and multicast ranges. A
deployment MAY permit such a destination only through an explicit override.
A redirect is a new target and MUST be rejected or independently resolved and
validated before following it. Passing URL validation while later reconnecting
by hostname is not a compliant SSRF control.

Secrets MUST be registered by reference and resolved by a deployment-owned
resolver. Protocol responses, journals, evidence, and dead letters MUST NOT
contain raw secret bytes. The reference dispatcher signs a canonical envelope
with HMAC-SHA256 and records secret-free identity, attempt, response class, and
outcome. Missing secrets, signing failure, and transport failure fail closed.
Delivery is at least once. Dead-letter redrive MUST continue the original
consecutive attempt history without duplicating the application event.
The reference daemon exposes `enqueue-callback-delivery`,
`claim-callback-deliveries`, `complete-callback-delivery`,
`move-callback-to-dead-letter`, and `redrive-callback-delivery` as
SQLite-backed callback-delivery queue operations. Enqueue stores a pending
delivery identity, claim moves due deliveries to `delivering` with a lease and
claim generation, and completion records success, duplicate acknowledgement,
target-gone cancellation, rate-limit retry, server-error retry/dead-letter, or
client-error failure through the runtime scheduler. Claim generation and lease
expiration are returned to workers and MUST be presented back on completion, so
recovered or superseded claims cannot overwrite a newer terminal delivery.
Dead-letter movement stores immutable delivery identity, consecutive attempt
history, error reason, and redrive count in the dead-letter store. Redrive MUST
preserve the original delivery identity and idempotency key, append operator
audit fields, increment attempt/redrive counters, and reinsert the delivery as
pending only through the queue's terminal-state redrive transition.

## Async operations and receipt admission

An `AsyncOperation` binds operation, run, node, attempt, provider operation,
idempotency key, expected callback schema, deadline, policy snapshot, release,
and ownership fences. It is committed before provider invocation whenever
possible. Callback and polling results normalize into one result model.

Callback ingestion MUST perform, in order:

1. authenticate the callback and bind its tenant/principal;
2. resolve operation identity and reject attempt/provider-operation drift;
3. enforce replay window, payload size, and expected schema;
4. deduplicate identical delivery and reject conflicting identity reuse;
5. record `ExternalCallbackReceived` in the authoritative journal; and
6. only then evaluate whether execution may resume.

Unauthenticated callbacks MUST NOT create resumable or quarantined state.
Ordinary subscription events MUST NOT be promoted to async receipts. Duplicate
callbacks MUST NOT resume twice. A callback after timeout/cancellation or for a
stale attempt MUST NOT modify the newer or terminal operation.

The reference daemon exposes `register-async-operation`,
`submit-async-callback`, `quarantine-async-callback`,
`accept-quarantined-async-callbacks`, `cancel-async-operation`, and
`expire-async-operation` as SQLite-backed async-operation control-plane
operations. Registration records the operation identity, provider identity,
schema reference, idempotency key, callback-wait state, and timeout or explicit
infinite-wait policy through `SqliteAsyncOperationStore`. Callback submission
reads the payload from standard input, loads the expected callback schema from
`--schema-json`, and submits the receipt to the same store; it returns whether
the receipt was a duplicate and whether the operation should resume. Early
callbacks that arrive before operation commit MAY be quarantined by
`quarantine-async-callback` and later replayed by
`accept-quarantined-async-callbacks` once registration has committed; replay
uses the same schema validation, idempotency, and single-resume behavior as
normal callback admission. Cancellation and expiration transition the operation
to terminal state through the same store so later callbacks are recorded as late
receipts without resuming execution. These commands are control-plane adapters
only: durable acceptance, schema validation, idempotency, early-callback
quarantine, late-callback handling, timeout validation, terminal-state
persistence, and journal-before-resume ordering remain owned by the Rust
runtime store.

## Checkpoint resume

A checkpoint binds run, graph/plan, next work, journal position, operation,
node, attempt, provider operation, policy snapshot, release, deadline, and a
canonical digest. Resume requires all identities to match plus positive policy
re-evaluation, budget reservation, release compatibility, and ownership fence.
`RunResuming` is emitted only when an executor claims the checkpoint. Durable
runtime implementations MUST claim the latest compatible checkpoint with a
lease and fencing epoch before resuming; active claims MAY be renewed without
changing their fencing epoch, but renewal MUST extend rather than shorten the
active lease; stale or expired claims MUST NOT renew or complete resume. Claim
renewal and completion MUST bind the run, checkpoint, worker, lease, and
fencing epoch recorded by the active claim. Restart-durable stores MUST
preserve active claims and the next fencing epoch across coordinator restart.
Control-plane claim, renewal, and completion APIs SHOULD expose active-claim,
missing-checkpoint, stale-fence, and expired-claim failures as structured
machine-readable errors.

If no executor is available, a valid receipt remains recorded and the run may
project a paused callback-delivery state for explicit retry. The Python
reference server retains checkpoints and executors in process; this continuation
does not survive process restart. A restart-durable or remote-worker claim
requires a durable runtime implementation.

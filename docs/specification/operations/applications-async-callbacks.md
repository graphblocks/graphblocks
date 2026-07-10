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

## Subscriptions and webhook delivery

A callback subscription binds run, event filter, target, delivery policy, and
tenant/principal scope. Webhook targets MUST declare timeout, retry limits,
payload bound, and a signing method. Redirects, DNS resolution, and egress MUST
obey deployment policy.

Secrets MUST be registered by reference and resolved by a deployment-owned
resolver. Protocol responses, journals, evidence, and dead letters MUST NOT
contain raw secret bytes. The reference dispatcher signs a canonical envelope
with HMAC-SHA256 and records secret-free identity, attempt, response class, and
outcome. Missing secrets, signing failure, and transport failure fail closed.
Delivery is at least once. Dead-letter redrive MUST continue the original
consecutive attempt history without duplicating the application event.

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

## Checkpoint resume

A checkpoint binds run, graph/plan, next work, journal position, operation,
node, attempt, provider operation, policy snapshot, release, deadline, and a
canonical digest. Resume requires all identities to match plus positive policy
re-evaluation, budget reservation, release compatibility, and ownership fence.
`RunResuming` is emitted only when an executor claims the checkpoint.

If no executor is available, a valid receipt remains recorded and the run may
project a paused callback-delivery state for explicit retry. The Python
reference server retains checkpoints and executors in process; this continuation
does not survive process restart. A restart-durable or remote-worker claim
requires a durable runtime implementation.

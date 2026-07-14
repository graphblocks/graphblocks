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
`GetRunStatus` SHOULD derive `lastCursor` and `lastSequence` from the
authoritative event stream, not from callback delivery state. Implementations
SHOULD read them as one event-stream position and project both fields from that
position so the cursor and sequence refer to the same persisted event.
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
Callback subscription `expires_at` is an exclusive capability boundary; events
whose occurrence time is greater than or equal to `expires_at` MUST NOT schedule
new callback deliveries.
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
Completion authority uses the half-open lease interval from claim acquisition
through, but not including, claim expiration. A completion timestamp before the
claim start or at or after the claim expiration MUST fail, even when recovery
has not yet moved the expired delivery back to pending.
If a worker claims a delivery but cannot load the authoritative application
event, it MUST complete the claim with a terminal projection failure instead of
leaving the delivery in flight.
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
Async operation `expires_at` is an exclusive callback admission deadline;
callbacks received at or after the deadline MUST be rejected before journaling a
resumable receipt. A non-expiration terminal transition is valid only when its
terminal timestamp is strictly earlier than `expires_at`; at the boundary the
operation MUST expire instead.
Callback endpoint `expires_at` is also exclusive and MUST be checked before
authentication creates an `AsyncCallbackSubmission`.

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

Receipt acceptance is fail-closed for execution resumption. The reference
daemon records a valid receipt but returns `shouldResume: false` unless the
submission includes explicit authentication verification plus a policy
decision identifier, budget reservation identifier, compatible release
identifier, and ownership fencing token. `submit-async-callback` supplies this
evidence through `--authentication-verified`,
`--resume-policy-decision-id`, `--resume-budget-reservation-id`,
`--resume-compatible-release-id`, and `--resume-ownership-fence-token`; the
four valued options MUST be supplied together. A successful authorization is
recorded as `CallbackResumeAuthorized` before `shouldResume` becomes true.

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
fencing epoch recorded by the active claim. A recovery claim is usable only
during its half-open activation interval from `claimed_at` through, but not
including, `expires_at`; a timestamp before `claimed_at` MUST fail claim use
without allowing a competing worker to replace the unexpired claim.
Restart-durable stores MUST
preserve active claims and the next fencing epoch across coordinator restart.
Control-plane claim, renewal, and completion APIs SHOULD expose active-claim,
missing-checkpoint, stale-fence, and expired-claim failures as structured
machine-readable errors.

If no executor is available, a valid receipt remains recorded and the run may
project a paused callback-delivery state for explicit retry. The Python
reference server retains checkpoints and executors in process; this continuation
does not survive process restart. A restart-durable or remote-worker claim
requires a durable runtime implementation.

The native stdlib runtime provides a preview, local-filesystem SQLite
continuation through `checkpointStorePath`. Cooperating processes that resume
the same run through the same checkpoint store are serialized by a lock file
adjacent to that store. Every such process MUST resolve the checkpoint path and
adjacent lock to the same shared filesystem and lock inode; separate filesystem
namespaces or aliases are not coordinated. This serialization does not provide
a distributed checkpoint lease/claim service.
A graph that reaches `async.await_callback@1` with
checkpointing enabled returns `waiting_callback`, persists the canonical
checkpoint, and leaves the run and journal nonterminal. `asyncOperationStorePath`
selects the SQLite async-operation receipt store and defaults to the checkpoint
store. These options, `callbackReceipt`, and `callbackAdmissionHmacKey` are
available through the raw native JSON entry point and the Rust
`StdlibRunOptions` builder; the Python wrapper exposes the key as
`callback_admission_hmac_key`. The native CLI accepts the name of an environment
variable containing the key, not the key itself as an argument. A later
invocation with the same graph, inputs, `runId`, store paths, deployment
provenance, shared callback receipt envelope, and separately injected admission
key reloads the checkpoint and may resume it. The
checkpoint digest binds the deployment provenance so a continuation
cannot silently cross release or physical-plan identity. The receipt must carry
matching operation, run, node, attempt,
provider-operation, operation-idempotency, resume-token, and schema identities;
an explicit `schema_validated: true` assertion from a trusted schema validator;
a canonical payload digest; and a
`graphblocks.trusted-callback-resume-admission.v1` decision. That decision binds
non-empty authentication, policy, and budget decision identifiers; the
compatible release digest; run, operation, node, attempt, checkpoint, and
checkpoint-state identities; a positive ownership fencing epoch plus owner,
lease, and fence-token identities; and schema-verification identity to the same
schema, payload digest, and verifier as the receipt. The decision MUST include
an `hmac-sha256` signature made with a deployment-owned key of at least 32
bytes. The runtime verifies that signature over the domain-separated canonical
decision; callback-supplied claims without the separately injected key are not
trusted admission evidence.

These are trusted pre-admission assertions, not authorities implemented by the
native stdlib runtime. Before invoking it, the embedding ingress/coordinator
MUST authenticate the assertion producer, MUST keep and inject the HMAC key
outside the callback envelope, and MUST obtain fresh authentication,
policy, budget, compatible-release, schema-validation, and ownership/lease
decisions from deployment-owned authorities. The native runtime checks the
assertion shape and its structured identity/digest bindings; it does not query
those authorities, validate the callback payload against the referenced schema,
or verify that the asserted lease remains fresh. Consequently this preview is
not a multi-worker production admission boundary.

The external rejection surface is deliberately non-oracular for callback
admission: a denied assertion, unknown coordinator/operation, malformed or
missing trusted evidence (including `schema_validated` other than the boolean
`true`), and callback/admission identity mismatch all return the same
`native async callback rejected` error. They do not mutate a waiting or terminal
coordinator. Distinct runtime errors are reserved for local storage corruption,
I/O failure, or divergence in already-authoritative persisted evidence; callers
MUST NOT expose those operator-facing failures on an unauthenticated callback
endpoint.

The callback coordinator persists five recoverable phases:
`waiting_evidence_pending`, `waiting_callback`,
`callback_accepted`, `terminal_evidence_pending`, and `terminal`. On retry it
reconstructs missing
async-operation, run, or journal evidence from the canonical coordinator record;
accepts only an exact persisted journal prefix bound by position and digest; and
then advances the phase. If callback acceptance commits before the coordinator
advances, retry verifies the persisted receipt and `CallbackResumeAuthorized`
event against the incoming structured admission, repairs `callback_accepted`,
and continues without accepting or journaling the callback a second time. This
recovery retains the persisted payload for both `callback_received` and
`resuming` operation states. A divergent prefix or persisted receipt fails
closed. The runtime appends
`external_callback_received` before `run_resuming`, replays completed node
outputs without re-executing those blocks, and persists the terminal result.
An identical callback idempotency key and payload digest returns the persisted
result without a second resume. If resumed execution itself fails, the
coordinator consumes the checkpoint into a terminal failed result so identical
retries return that result rather than executing again.

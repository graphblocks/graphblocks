# Graph, Compiler, and Runtime

## Graph validation

<a id="GB-GCR-GRAPH-VALIDATION-001"></a>

A graph MUST define unique node and edge identities, resolvable block types,
valid port directions, compatible value types, and all required inputs. The
compiler MUST diagnose unknown endpoints, duplicate identities, invalid
configuration, unsupported cycles, unresolved binding requirements, and target
incompatibility before execution.

<a id="GB-GCR-CLOSED-WORLD-001"></a>

Catalog-backed compilation MUST be closed-world by default. Every executable
node MUST resolve to one descriptor; a missing descriptor MUST fail with
`GB1022`. An implementation MAY expose an explicitly open catalog for discovery
or compatibility workflows, but that result MUST NOT be represented as proof
that unknown blocks are executable.

<a id="GB-GCR-TYPING-001"></a>

Declared root-port types MUST be compared by exact nominal identity for
graph-input-to-block, block-to-block, and block-to-graph-output connections.
`Any` is the only wildcard. Implementations MUST NOT coerce or structurally
equate two different schema IDs. An optional block output MUST NOT feed a
required block input or graph output, and every required block input MUST be
supplied. A nested endpoint MUST name an existing root port; compilation does
not infer a nested field type beyond that root, so payload-schema validation is
a separate boundary.

<a id="GB-GCR-PREDICATES-001"></a>

The compiler MUST evaluate descriptor `requiredWhen` predicates against the
normalized, immutable node configuration in the `initial` phase. A
configuration predicate that evaluates true promotes that source output to
required for type-flow validation. A predicate that is false, refers to a
missing configuration pointer, or is guaranteed only in the `resumed` phase
MUST remain optional during initial compilation. Implementations MUST use exact
JSON equality without scalar coercion, so for example boolean `true`, integer
`1`, and string `"1"` remain distinct.

<a id="GB-GCR-ENDPOINTS-001"></a>

Every edge endpoint MUST contain an owner and port path. `$input` is valid only
as an edge source and `$output` only as an edge target; the opposite directions
MUST fail compilation. Every segment after the root port denotes an object key;
a segment made only of ASCII decimal digits MUST fail compilation with `GB1020`.
List-valued wiring shorthand that lowers to such a segment is therefore not
executable. Ordinary executable graphs MUST be acyclic unless a selected runtime
profile explicitly defines a bounded cycle construct.

<a id="GB-GCR-PLAN-001"></a>

Normalization and expansion MUST be deterministic. A physical plan MUST bind
the normalized graph, resolved blocks and packages, target, policy inputs, and
compiler version into canonical evidence. Identical inputs MUST produce the same
plan hash. Matching input-side and output-side shorthand declarations are two
views of one connection and MUST normalize to one edge. Explicit edges remain
independent declarations; an explicit edge matching shorthand, or another
explicit edge, MUST fail as a duplicate edge identity.

<a id="GB-GCR-SINGLE-WRITER-001"></a>

Each normalized target endpoint MUST have at most one distinct source. Distinct
sources writing the same block input or graph output MUST fail with `GB1007`.
Target paths under the same owner also share a write domain when one path's
segments are a prefix of the other, so independent writers for `node.value` and
`node.value.detail` MUST fail with `GB1007`. Textual prefixes that are not whole
segments, such as `node.value` and `node.value2.detail`, and paths owned by
different nodes do not overlap.
One source MAY fan out to multiple targets, and symmetric input-side and
output-side shorthand remains the single connection described above.

## Execution

### Local scheduling and outcomes

<a id="GB-GCR-SCHEDULING-001"></a>

The runtime MUST schedule a node only after its dependencies and admission
requirements are satisfied. It MUST preserve typed ports, record state
transitions in order, and project exactly one terminal outcome per run.

### Terminal outcome extensions

Terminal success, failure, cancellation, rejection, pause, and exhaustion MUST remain
distinguishable.

### Catalog-backed local invocation

<a id="GB-GCR-CATALOG-RUNTIME-001"></a>

A catalog-backed runtime MUST reject handlers registered under undeclared block
IDs and MUST reject duplicate registration unless the caller explicitly uses a
replacement operation. After a block returns, the runtime MUST reject a
non-mapping result, any output key absent from the descriptor, and omission of
any output required by the descriptor for the node's immutable configuration
and current execution phase.

### Preview callback continuation

Ordinary invocation uses `initial`; an admitted callback continuation uses
`resumed`. Callback resume MUST enforce the same
output contract before resumed outputs become visible to downstream nodes.

### Untyped compatibility mode

<a id="GB-GCR-UNTYPED-COMPAT-001"></a>

An explicit untyped compatibility mode MAY admit handlers without descriptors;
it MUST NOT disable output checks for descriptors that are present.

### Conditional local execution

<a id="GB-GCR-CONDITIONAL-EXECUTION-001"></a>

A node `when` reference is a boolean dependency. The runtime MUST wait for that
dependency, execute the node only when it resolves to `true`, and skip it
without invoking the block when it resolves to `false`. A missing or non-boolean
condition MUST fail closed. The referenced root port MUST exist on a declared
graph input or resolved source block. In particular, a false guard MUST never
allow a state-changing block to commit an effect. Guard resolution gates ordinary input
readiness: a false branch MUST be skippable without waiting for inputs that the
block will never consume. The skip and its reason MUST remain auditable.

### Preview native scheduling

Native scheduling MUST propagate skipped outcomes to dependent ports.

### Failure finalization across checkpoints

Failures after a block returns, including output-path projection and checkpoint
materialization, remain part of node execution. They MUST append a terminal
failure, update durable run state, and release run-scoped leases instead of
escaping with the run still recorded as running.

### Local output projection

<a id="GB-GCR-OUTPUT-PROJECTION-001"></a>

Output projection MUST finish before terminal success is recorded.

### Preview callback failure finalization

The same cleanup rule applies while
projecting a resumed callback, and a failed resume MUST consume its checkpoint
so it cannot be replayed.

### Structured cancellation across adapters

Cancellation MUST be structured and cooperative, with explicit behavior for
in-flight provider calls, tools, children, checkpointing, and cleanup. Timeout
and retry MUST use bounded policies.

`ToolBinding.cancellation` declares an adapter capability, not an outcome:

- `unsupported` means the adapter cannot propagate a cancellation request to
  work that has already started;
- `cooperative` means the adapter can propagate the request and the callee is
  expected to observe it, but the runtime cannot compel observation; and
- `force_terminable` means the adapter owns a runtime-controlled boundary that
  can terminate the executing call. It does not by itself retract an external
  effect that the call already delegated.

Reaching a deadline MAY stop the runtime from accepting a late result without
stopping the underlying call. In particular, `cooperative` cancellation MUST
NOT be represented as a guarantee that execution or side effects cease at the
deadline. A stronger timeout guarantee requires process isolation or another
verified force-termination boundary, plus effect fencing at every authoritative
result and effect commit. Conformance for that stronger guarantee MUST hold an
adapter past its deadline and prove that a late return cannot publish a result,
record success, commit an effect under stale authority, or overwrite the
terminal timeout/cancellation outcome.

### Durable accepted-run HTTP boundary (preview)

`DurableAcceptedRunServerApp` is the repository-authoritative HTTP boundary for
the accepted-run preview. It exposes admission, tenant-and-owner-scoped status,
cursor-bounded event replay, and callback submission. Returning `202` from
`POST /runs` means the admission, immutable owner identity, accepted event, and
reconstructable invocation were committed in `AcceptedRunRepository`; it does
not mean execution has completed.

Callback continuation uses
`POST /runs/{run_id}/callbacks/{operation_id}`. The request is a closed object
containing the persisted checkpoint digest, operation attempt, callback
idempotency key, issuing lease generation and fencing token, expected run state
version, payload, and callback receipt. The server derives tenant and owner
from the authenticated principal, resolves the run before decoding callback
authority, and returns non-disclosing `404` for a foreign run. Issuance,
state-version, lease, fence, payload digest, callback inbox, resume transition,
event append, and dispatch-effect settlement are checked in one repository
transaction. Replaying the same issuance and payload returns the original
acceptance; a stale issuance, conflicting payload, or conflicting version
returns `409`.

Durable cancellation uses `POST /runs/{run_id}/cancel` with the closed
`expectedStateVersion`, `requestId`, and `reason` fields. The repository MUST
recheck tenant and immutable owner in the write transaction. It atomically
records the idempotent control, cancellation event, terminal result, and
completion outbox intent; invalidates the current run lease and fencing token;
and suppresses any undelivered callback-dispatch effect. An identical request
replays the stored acceptance after restart. A conflicting request, late
callback, or worker commit using the invalidated fence MUST be rejected without
publishing another result.

Durable expiration uses `POST /runs/{run_id}/expire` with the same closed
terminal-control fields. It is an explicit owner-scoped control: the caller
supplies the authoritative expiration reason and expected state version. The
repository MUST atomically append `run_expired`, persist the `expired` terminal
result and completion outbox intent, invalidate the current lease and fencing
token, clear any pause marker, and suppress an undelivered callback-dispatch
effect. Identical requests MUST replay after restart; a late callback, worker
commit, foreign owner, or conflicting state version MUST not mutate the
terminal run.

Durable non-terminal suspension uses `POST /runs/{run_id}/pause` and
`POST /runs/{run_id}/resume` with the same closed control fields. Pause MUST
atomically append its control event, preserve the exact resumable phase,
invalidate any active lease and fencing token, and prevent a worker from
claiming the run. Resume MUST clear the pause marker and restore only that
preserved phase with a new state version and fence. If a valid callback arrives
while a waiting run is paused, the callback receipt, inbox entry, event, and
dispatch-effect settlement MUST still commit; the preserved resumable phase
becomes `ready_resume`, but the run remains paused and unclaimable until an
authorized resume. Callback issuance MUST persist the waiting-state version,
and that issuance version remains valid across intervening pause/resume
controls; the repository still compare-and-swaps against the live state in the
acceptance transaction. The `external_callback_received` event reports
`resumeState: ready_resume` rather than claiming that the live run state is
unpaused. Identical pause or resume requests MUST replay their original
acceptance after restart, and foreign tenant or owner controls MUST return a
non-disclosing `404`. This control-plane pause is a non-terminal scheduling
state, distinct from a terminal paused outcome.

Repository implementations claiming this preview contract revision MUST
implement `expire_run`, `pause_run`, and `resume_run`, and control acceptances
MUST identify their `resulting_phase`. Custom repositories targeting the prior
preview contract are not compatible until they implement that capability
boundary.

Callback acceptance only makes the run ready for resume. A worker must claim
the reconstructable work with fresh lease and fencing authority before
execution. Process restart between admission, waiting, callback acceptance,
resume, and terminal publication MUST preserve each observable transition.
The process-local accepted-run mode in `GraphBlocksServerApp` is a bounded
development/reference mode and MUST NOT be described as restart durable or as
a multi-tenant run authority. It is disabled by default: `accepted` and
`background` requests, plus synchronous graphs whose
`async.await_callback@1` node has checkpointing enabled, return `503` with
`server.durable_accepted_run_required`. After rejection, no new run, event,
checkpoint, or admission-ticket state associated with that invocation remains.
Reference-only tests and local development must opt in with
`allow_process_local_accepted_runs_dev=True`; deployed services that accept
resumable runs use `DurableAcceptedRunServerApp`. The reference app has one
fixed `reference_tenant_id`; a single-tenant `StaticBearerAuthHook` may
establish that value at construction,
while custom authentication MUST configure it explicitly. A principal from
another tenant is rejected before resource resolution or authorization. The
effective boundary is fixed at construction and later configuration-field
mutation MUST NOT change it. The explicit
`allow_unsafe_multi_tenant_dev=True` compatibility escape hatch is for legacy
tests only and MUST NOT support a compatibility, production, or security
claim. Multi-tenant accepted/background runs use
`DurableAcceptedRunServerApp`, whose repository binds a tenant-scoped external
run ID to a distinct immutable internal ID.

### Process-isolated worker deadline (preview)

The Python reference package exposes `graphblocks.isolated_worker` as a
preview force-termination boundary. `ProcessWorkerExecutor` starts a fresh
worker process with the portable `spawn` model, sends a bounded canonical
`WorkerInvokeRequest`, and accepts a bounded `WorkerInvokeResult` only after
the worker exits within the configured deadline. On timeout it terminates and,
if necessary, kills and reaps the worker before reporting
`ProcessWorkerDeadlineExceeded`. The deadline begins before canonical request
serialization and process setup; if that preparation consumes the budget, no
child process is started.

`DurableAcceptedRunService` uses this boundary for every claimed graph
execution. Its default importable worker target reconstructs the closed durable
intent-only subset of the preview standard library in the child process. The
current package-owned target descriptor is checked exactly, and its current
built-in handlers construct local values or operation intents rather than
importing provider transports. Default parent admission and child execution
build separate registries from one closed inventory that binds every admitted
block ID to an exact implementation ID and package-owned handler construction
authority. A new preview standard-library block is excluded until that
inventory is explicitly reviewed, and an implementation rebinding fails
closed. Nested dispatch such as `control.map@2` resolves against the same
restricted registry rather than a captured full-registry resolver. A custom
worker target can publish an external effect before it returns or times
out, so the durable service MUST reject custom targets by default. The explicit
`allow_unsafe_custom_worker_dev=True` escape hatch is fixed at construction,
is for tests and local development only, and MUST NOT support a durable,
production, compatibility, or security claim. Mutating that public field after
construction MUST NOT enable the escape hatch. A service that replaces the
registry with one that is not verified as admission-compatible with the closed
durable inventory, or selects a compiler other than the native or deterministic
reference compiler, requires this unsafe mode and a matching importable worker
target. For legacy positional compatibility, an unmodified full preview
registry is accepted for admission only while its block and catalog ID sets
exactly equal the closed inventory; adding a preview block makes that path fail
closed, and callers should use `durable_intent_registry()` directly. The service
MUST NOT silently run a replacement handler in the scheduler process or execute
the default handler under different semantics. Graph-declared `effects`
metadata does not establish external-effect authority. The worker deadline,
termination grace, and publication margin MUST fit inside the run lease. The
scheduler recomputes the available deadline from the actual remaining lease
immediately before process start. A timeout does not fabricate a terminal
result: the run remains `running` under the original claim until that lease
expires, after which a new claim with a higher lease generation and fencing
token may retry it.
By contrast, a deterministic worker request or response byte-limit violation
commits a bounded `failed` result under the same repository fence so oversized
poison work is not reclaimed forever.

The durable request binds tenant, immutable owner, run, state version, event
high watermark, graph hash, lease owner, lease generation, fencing token,
lease expiry, checkpoint digest, and callback-receipt digest into one canonical
authority digest. For resume work, the authoritative accepted callback payload
digest is bound separately from the whole receipt digest. Callback acceptance
MUST reject a receipt whose embedded payload or payload digest differs from the
top-level payload used for inbox idempotency and audit identity. Invocation ID
and node-attempt ID derive from the authority digest. A worker request MUST
derive its receipt from the stored callback acceptance and MUST NOT accept an
alternate caller-supplied receipt. The worker protocol's lease epoch is the
accepted-run fencing token. Checkpoint and callback values cross the process
boundary only through their bounded canonical wire forms.

The parent first validates invocation ID, node-attempt ID, and lease epoch
against the request. It then calls the executor's mandatory
`authority_validator` after the worker exits and immediately before returning
the result. That validator MUST read the current authoritative lease/fence and
raise when the request no longer owns it; returning a boolean decision is
rejected rather than interpreted. The validator MUST be read-only and MUST
apply its own bounded storage timeout; its result is also rejected if the
overall execution deadline has elapsed. The deadline covers process startup,
execution, response transfer, and worker exit. Request and response byte
limits are mandatory and bounded.

Live validation before return narrows the stale-result window but is not an
atomic publication transaction. The repository that records an authoritative
result or outbox effect MUST revalidate the same lease/fence in the transaction
that commits it. A caller MUST NOT treat a successful
`authority_validator` call alone as permission for a later unfenced write.
`graphblocks.isolated_worker_server.AcceptedRunWorkerAuthorityValidator`
provides the reference bridge for durable accepted runs: it maps the worker
request's `lease_epoch` to the accepted-run claim's `fencing_token`, reads the
tenant-scoped run, compares the complete current claim, and rejects authority
at or after lease expiry.

After the isolated executor returns, a durable server MUST construct its
waiting or terminal commit with that same claim and expected state version.
The service and SQLite repository MUST share the exact clock authority. After
acquiring the SQLite write transaction, the repository reads that clock again
and checks the current claim, state version, lease generation, fencing token,
and lease expiry before it records the checkpoint or result, event, and
corresponding outbox intent. The validator and repository commit are both
required: the first rejects authority lost while the worker ran, and the second
closes both the validation-to-publication race and lease expiry while waiting
for the database write lock.

This boundary controls the worker process only. A handler that delegates work
to an untracked child, background service, or provider MUST NOT claim
`force_terminable` without proving that delegated work is also terminated or
fenced. The current preview therefore rejects custom worker targets by default
and does not claim that its generic operation-dispatch outbox is a complete
provider-effect boundary.

A stronger profile that permits a force-terminated worker to request a
state-changing external effect MUST atomically transfer authority from the
current run claim to a closed durable effect intent before any send. The intent
MUST bind the tenant, run, logical effect identity, stable idempotency key,
request digest, provider target and operation, and originating lease generation
and fencing token. The dispatcher MUST atomically acquire the current
independent effect claim before starting a send and MUST acknowledge delivery
only with that same unexpired claim. The logical effect identity and
idempotency key MUST remain stable across every retry or reclaim of the same
logical effect.

The delivery repository MUST own the clock used for claim eligibility and
lease authority. Inside the write transaction it MUST reject a caller-declared
future timestamp, derive the expiry from its own clock and a finite
policy-bounded duration, and use that authoritative time in both selection and
compare-and-swap predicates. Acknowledgement and retry release timestamps are
caller-declared metadata, not proof of live authority: a new transition MUST
also observe repository time before the stored expiry, reject declared future
times, and recheck the lease immediately before commit. Retry availability
MUST NOT precede repository time. While its replay slot remains retained, a
matching replay of an already committed acknowledgement or retry MAY succeed
after expiry so response loss does not turn a completed transaction into a
conflicting mutation.

Each delivery claim MUST persist a repository-issued claim start together with
the effect ID, delivery owner, claim generation, fencing token, and lease
expiry. A new acknowledgement or retry release MUST present that complete
claim identity. Its observation timestamp MUST be no earlier than the claim
start and no later than repository time, while repository time MUST remain
strictly before claim expiry.

After an acknowledgement or retry release commits, the repository MUST retain
a versioned, closed, content-bound replay identity covering the transition
kind, complete claim identity, and every command timestamp until a later claim
or terminal transition supersedes that recovery slot. For an acknowledgement
this includes `delivered_at_unix_ms`; for a retry it includes both
`released_at_unix_ms` and `available_at_unix_ms`. While retained, only the
identical command MAY recover a committed result after response loss or lease
expiry. Changing the owner, generation, fence, claim interval, transition kind,
or any command timestamp MUST fail without mutation. This replay exception
authorizes no new external send.

A storage upgrade that first adopts repository-owned delivery time MUST NOT
retain active claims whose expiry was issued from caller-controlled time. It
MUST transactionally invalidate and requeue those claims while advancing their
generation and fencing token, or fail the migration closed unless every
delivery counter retains enough headroom to issue the requeued claim. Requeued
delivery remains at-least-once and therefore retains the stable
receiver-deduplication requirement. An upgrade that cannot tolerate an
immediate duplicate MUST quiesce legacy dispatchers before opening the database
with the new schema authority.

A storage upgrade that first adopts complete replay identity MUST NOT invent a
claim start, owner, expiry, or command timestamp that the prior schema did not
retain. It MUST invalidate and requeue active claims without a reconstructable
start while advancing their generation and fence, subject to the same counter
headroom requirement. Already delivered or retry-pending effects retain their
authoritative state, but a pre-upgrade command with no complete stored identity
is not replayable after migration. Operators MUST stop legacy dispatchers and
back up the database before upgrade, MUST NOT run mixed-version writers, and
MUST reconcile any provider outcome that cannot tolerate the documented
at-least-once requeue behavior.

If the delivery claim expires before acknowledgement, the outcome is ambiguous
and the dispatcher MUST reconcile only that same provider-correlated intent.
It MAY replay the send only when the provider atomically deduplicates the stable
idempotency key; otherwise it MUST query provider status or obtain confirmed
cancellation. Requesting cancellation is not confirmation. Until terminal
reconciliation, the runtime MUST NOT issue a new mutation for the same logical
effect. A profile MUST reject admission or dispatch when the provider offers no
applicable deduplication, status, or confirmed-cancellation capability, and
GraphBlocks MUST NOT make an exactly-once effect claim without a matching
provider atomic-deduplication boundary.

Provider and tool mutations use a boundary distinct from the generic
operation-dispatch outbox. The closed
`graphblocks.provider-capability-snapshot.v1` contract binds a
deployment-owned adapter identifier and release digest, provider target and
operation, and exact deduplication, status-lookup, and cancellation
capabilities together with its registry-authority digest. A caller declaration
is not capability evidence: admission MUST require an exact-true result from a
deployment-owned capability-authority verifier whose identity matches that
digest. The same registry-authentic snapshot MUST bind the only permitted
reconciliation verifier identifier, release digest, and verification-authority
digest. Admission MUST use that authority as a deployment-owned registry and
resolve the actual registered verifier implementation for the exact snapshot;
a caller-supplied verifier that merely copies the admitted tuple is not an
authenticated implementation. A snapshot whose cancellation capability is
only `request_only` provides no confirmed cancellation recovery path.

Before a first send, the closed `graphblocks.provider-effect-intent.v1`
contract MUST bind the effect kind and identifier, tenant, run, owner,
idempotency key, canonical request and digest, provider target and operation,
adapter identity and release, capability-snapshot digest, any prebound provider
correlation identifier, originating run state version, lease generation,
fencing token, authority digest, optional checkpoint digest, and creation time.
The referenced snapshot MUST match every bound adapter, target, and operation
field. Admission MUST fail closed unless at least one applicable recovery path
exists: atomic replay by the stable idempotency key, definitive status lookup
by that key or an already-bound correlation identifier, or confirmed
cancellation by one of those identities. A correlation-based capability is
not applicable when the correlation identifier was not bound before send.
The intent's tenant, run, owner, state version, lease generation, fencing token,
and checkpoint MUST first match a repository-resolved
`graphblocks.provider-run-authority-snapshot.v1`. Request-supplied authority
values do not satisfy this comparison. The accepted-run repository MUST verify
that live run claim and atomically commit both the immutable provider-effect
intent and a closed `graphblocks.provider-effect-origin-transfer.v1`. The
transfer binds the exact immutable intent content digest, effect, tenant, run,
owner, state version, lease generation, fencing token, checkpoint, source
run-authority digest, repository-authority digest, and the same timestamp used
for intent creation. Its copied source fields MUST reconstruct the exact
run-authority digest, and the intent's origin authority digest MUST equal that
source digest. Later send and retry admission MUST require the transfer's intent
digest to equal the supplied intent's digest and verify the exact stored pair
through the structurally distinct `verify_transferred_origin` authority
operation. It MUST NOT require the original run lease to remain current. A
live-run verifier that lacks that operation is not a transferred-origin
verifier. Successful validation issues
an opaque admission bound to the intent, capability authority, transferred
origin authority and its verifier, admission time, applicable methods, any
prior send attempt, and the repository-issued next attempt identifier, claim
owner, generation, fencing token, expiry, and claim-authority digest. The
admission has no supported authority-rehydration or persistence format. Its
private canonical identity projection and inspectable in-memory fields do not
convey send authority; repository one-shot consumption and fencing remain the
authority boundary.

The generic state transition API MUST NOT enter `send_started`. A send begins
only through that admission and a closed
`graphblocks.provider-effect-send-attempt.v1` binding the effect, intent,
capability, admission, attempt identifier, repository claim owner and
generation, fencing token, and repository-issued start time. After a terminally
safe retry, the new attempt identifier MUST differ and both generation and
fence MUST increase. An attempt that predates admission or omits the admitted
prior-attempt digest MUST fail before provider I/O. Send entry MUST atomically
consume the admission exactly once and return both the repository-timed attempt
installed as active and a closed
`graphblocks.provider-effect-admission-receipt.v1`. That serializable receipt
retains the admission fields and digests, binds the installed attempt digest,
records the repository-issued send and consumption times, and conveys no
permission to begin another send. Rehydrating a receipt MUST revalidate
`admitted <= send-started <= consumed < claim-expiry` and the attempt's start
MUST equal the receipt's start. A stale, expired, or already-consumed admission
MUST fail before provider I/O. Persistence MUST retain the receipt and attempt,
not the send-capable admission. Both persisted records MUST pass their closed,
versioned decoder and exact content revalidation before provider I/O or evidence
application.

Once sending starts, an uncertain result MUST enter `quarantined_unknown`, not
the pending queue. Reconciliation moves it to `reconciling`; an unknown result
returns it to quarantine, and optional manual review remains an unknown state.
Only content-bound `graphblocks.provider-reconciliation-evidence.v1` matching
the complete intent, capability, consumed-admission receipt, and current
send-attempt digests MAY establish
`confirmed_committed`, `confirmed_not_committed`, or `confirmed_cancelled`.
It MUST retain canonical provider evidence and its matching digest, observation
time no earlier than the current send start, and the identifier, release digest,
and authority digest of a deployment-owned verifier. Persisted evidence MUST
pass its closed, versioned decoder and exact canonical body-digest revalidation
before verification or terminal settlement. That verifier MUST return
exact true after authenticating the provider evidence and its normalized
method/outcome mapping. The evidence API MUST resolve the implementation again
through the deployment-owned verifier authority and MUST NOT accept an
independently supplied verifier handle. Merely knowing or copying a digest is
not verification. The resolved verifier triple MUST equal the capability
registry's admitted triple, and the repository claim authority MUST
independently return exact true that this attempt remains active at evidence
application. A historically valid evidence bundle for an inactive attempt
cannot settle the current state.
Evidence based on atomic replay cannot establish non-commit, and a cancellation
request cannot establish confirmed cancellation. Only confirmed non-commit or
confirmed cancellation MAY return the exact, unchanged intent to pending;
confirmed commit is terminal. Evidence from a prior attempt cannot settle a
retried send. Changing any request, identity, provider, adapter, correlation,
origin authority, lease, fence, checkpoint, or creation field creates an
identity conflict rather than a retry.

The core contracts and their state machine perform no provider I/O or
scheduling. The preview SQLite v8 origin-transfer repository provides the first
persistence slice in a provider-specific `provider_effects` projection and
authoritative `provider_effect_events` journal. It shares the accepted-run
database so one `BEGIN IMMEDIATE` transaction can resolve the tenant-scoped run,
recheck owner, state version, checkpoint, lease generation, fence, and
repository-time expiry, and atomically persist the exact intent, capability
snapshot, transfer, and initial event. Exact committed replay MAY return that
stored transfer after the source lease expires; a changed effect identity,
idempotency identity, capability, or repository authority MUST conflict. Every
stored wire record and digest MUST pass its closed decoder and canonical
identity check when read. Tenant, run, and owner scope MUST be part of every
external lookup even when two runs use the same effect identifier.

SQLite v9 adds the durable pre-send claim slice. A claim request is scoped by
tenant and owner principal, while the repository assigns time, a deployment
claim-authority digest, a bounded half-open lease of at most 60 seconds, an
exactly advancing generation and fencing token, and a newly issued send-attempt
identifier that is unique among active pre-send claims. Repository claim calls
replay an existing unexpired claim for that owner before selecting more work;
multiple active claims for the same owner and scope MUST fail closed. Replay
returns the exact active claim for restart or response-loss recovery only after
a second repository-time expiry check. Otherwise only `pending` or expired
`claimed` rows are eligible. Reclaiming an expired claim MUST advance both
counters and install a new claim and attempt identifier in the same
`BEGIN IMMEDIATE` transaction as the state version and authoritative
`send_claimed` or `send_claim_reclaimed` event. The event embeds the complete
closed claim record as well as its canonical digest, so replacement of the
projection does not erase the historical authority record. Each pre-send claim
or release transition MUST validate the exact projection tail and reject events
above its watermark with bounded indexed lookups rather than rescanning the
whole journal. Paged journal reads MUST validate contiguous sequence and state,
plus time, generation, fence, reclaim-expiry, and release binding within the
returned bounded page and its predecessor when one is required.

An exact active claim MAY be released before provider I/O, including after its
lease expires, because release removes authority rather than granting it. The
release competes with reclaim under the same SQLite write lock; if reclaim wins,
the old claim is stale. A successful release atomically returns the projection
to `pending`, retains the generation and fence, clears all active claim fields,
and stores a closed release receipt, including the released generation and
fence, plus a `send_claim_released` event. Repeating the exact release after
commit MUST return the stored result without issuing a new transition or
consulting repository time. A prior release MUST become stale once a new claim
is installed. `send_started` and every later provider-effect state MUST NOT be
automatically selected or returned to `pending` by this pre-send claim
repository.

SQLite v9 does not implement core admission verification or one-shot claim
consumption into `send_started`; SQLite v10 provides those repository
boundaries as described below.

SQLite v10 adds current-claim verification and one-shot local send-entry
authority consumption. The origin-transfer repository and send-claim authority
MUST remain structurally distinct: the former exposes the origin repository
digest, while the latter exposes the deployment claim-authority digest.
Admission verification reads the tenant-, run-, owner-, and effect-scoped
projection, exact-decodes the current claim and latest attempt, validates the
projection tail, and checks repository time twice against the half-open claim
interval. This read does not consume authority. Send entry MUST repeat the
complete intent, capability, origin transfer, claim owner, generation, fence,
expiry, admitted time, attempt identifier, and previous-attempt checks under a
single `BEGIN IMMEDIATE` transaction.

Each successful claim allocation MUST append its exact canonical claim and
attempt identifier to `provider_effect_send_claim_issuances` in the same
transaction as the claim projection and journal event. Issuance rows remain
append-only after release, expiry, reclaim, or consumption, so an attempt
identifier can never be issued again merely because the active projection was
cleared. The claim projection, issuance row, generation, fence, installed state
version, and event sequence MUST agree exactly.

Successful consumption appends exact canonical
`graphblocks.provider-effect-send-attempt.v1` and
`graphblocks.provider-effect-admission-receipt.v1` records to
`provider_effect_send_attempts`, advances the projection from `claimed` to
`send_started`, clears the active pre-send claim fields while retaining its
generation and fence, installs active and latest attempt/receipt digests, and
appends a closed `send_started` event. The event binds the consumed claim
digest, complete attempt and receipt records and their digests, intent, state,
state version, sequence, and repository consumption time. Attempt identifiers,
consumed-claim digests, admission digests, attempt digests, and receipt digests
MUST be one-shot identities. The opaque `ProviderEffectAdmission` MUST NOT be
serialized or stored; only its digest and the non-authoritative receipt fields
are durable. Repository start, consumption, and final pre-commit times MUST be
monotonic and strictly before claim expiry or the entire transition rolls back.

Repeating an already consumed admission MUST fail rather than return the stored
attempt and receipt as if they conveyed fresh send authority. If the local
commit succeeds but its response is lost, a tenant-, run-, owner-, and
effect-scoped observation API MAY exact-decode the persisted active attempt and
receipt. The effect remains conservatively in `send_started`; the observation
does not prove provider I/O occurred and MUST NOT authorize replay. The
structurally distinct claim authority MAY exact-verify that this same persisted
attempt and receipt remain active, including after restart, so the core can
authenticate reconciliation evidence without trusting caller state. This read
does not persist evidence or change state. This v10 slice does not invoke an
adapter, quarantine an ambiguous provider outcome, or enable durable retry.

SQLite v11 adds append-only reconciliation evidence and atomic active-send
settlement. After the core has exact-decoded the evidence, rechecked the active
attempt, and authenticated the capability-bound verifier, settlement MUST
repeat the exact receipt, attempt, evidence, method, observation time, verifier
tuple, and correlation bindings under one `BEGIN IMMEDIATE` transaction. It
MUST append the canonical evidence to
`provider_effect_reconciliation_evidence`, compare-and-swap the current state,
version, journal watermark, and active/latest attempt identities, then append a
closed `reconciliation_evidence_applied` event at the same version. Confirmed
commit, non-commit, or cancellation clears active send authority while
retaining the latest immutable attempt history. An unknown result advances to
`quarantined_unknown` and retains the exact active attempt and receipt for a
future explicit reconciliation transition.

Every projection-tail read MUST cross-check the evidence row, event, canonical
evidence digest, attempt and receipt digests, from/to states, observation and
settlement times, and installed state/event versions. Failure before commit
rolls back all three writes. An exact repeated settlement call MAY return the
already committed result after response loss, but this recovery does not make
the higher-level evidence API accept a now-terminal attempt as active. The v11
repository does not run the deployment verifier itself; that authentication is
the preceding core boundary. It also does not perform adapter I/O, schedule
reconciliation, implement the quarantine-to-reconciling command, or enable
durable retry.

SQLite v12 adds the explicit quarantine/reconciliation/manual-review control
boundary. A control MUST bind tenant, run, owner principal, effect, a stable
control identifier, one of `begin_reconciliation`, `escalate_manual_review`, or
`resume_reconciliation`, and the exact expected state version. The repository
MUST resolve the effect through the complete tenant and owner scope, verify its
projection tail and active attempt/receipt, and apply the closed state-machine
transition with a compare-and-swap on state, version, journal watermark, and
active send digests. The control row, projection update, and closed
`reconciliation_control_applied` event MUST share one `BEGIN IMMEDIATE`
transaction and one installed state/event version. These transitions retain
the active send identity; they never authorize a second send.

An exact control replay MAY return the installed result while it remains the
current projection, including after response loss. Reusing its control identity
with changed scope, transition, or expected version MUST conflict, and a replay
superseded by a later transition MUST be reported as stale rather than
reconstructing current authority from history. The v12 migration creates an
empty control ledger and does not infer controls for existing quarantined
sends. This repository boundary does not authenticate operator policy, schedule
reconciliation workers, invoke provider verification, or implement a retry.

SQLite v13 adds the confirmed-safe same-intent retry boundary. A retry command
MUST bind tenant, run, owner principal, effect, a stable retry identifier, the
complete immutable intent digest, and the exact expected state version. The
repository MUST exact-decode the supplied intent and compare it with the stored
intent before changing state. Only `confirmed_not_committed` and
`confirmed_cancelled` are eligible; committed or unknown outcomes MUST NOT
return to the send queue. The settled attempt and receipt remain immutable
history, while active send authority remains absent.

The canonical retry command, projection compare-and-swap, and closed
`retry_same_intent_applied` event MUST commit in one `BEGIN IMMEDIATE`
transaction and share one installed state/event version. Exact replay MAY
return the current pending result after response loss. Changed command identity
or intent MUST conflict, and a replay superseded by a later claim MUST be stale.
The next claim MUST retain the previous attempt digest and strictly advance the
claim generation, fencing token, and attempt identifier before another send can
begin. The v13 migration creates an empty retry ledger and does not infer retry
authority from existing terminal rows.

Existing operation-dispatch rows MUST NOT be migrated or reinterpreted as
provider-effect intents because they do not contain this authority or evidence.
SQLite v8 creates empty provider-specific tables and does not backfill the
generic outbox. The v9 migration accepts only v8 `pending` rows; it MUST fail
closed rather than invent claim authority for any later state that v8 could not
represent exactly. The v10 migration preserves only v9 `pending` and `claimed`
rows whose send history is still empty, and backfills an issuance row for each
exact active v9 claim. It MUST fail closed rather than invent an attempt or
receipt for `send_started` or later state. Operators upgrading the preview
database MUST prevent mixed-version writers and retain the normal pre-migration
backup. The v11 migration creates an empty evidence ledger and does not invent
outcomes for existing active sends. The v12 migration likewise creates an empty
control ledger, and v13 creates an empty retry ledger. A production profile
still requires real
adapter capability and verifier-registry authorities, adapter I/O,
ambiguous-send reconciliation, kill/restart/fencing tests, and provider-side
idempotency or status/cancellation evidence. Repository snapshots, transfers,
claim fields, and verifier authorities MUST come from trusted service
dependencies and MUST NOT be accepted from request data. Claim consumption,
receipt creation, active attempt installation, and its journal event MUST be
one repository transaction with the corresponding state change. Every closed
record loaded from persistence MUST pass its exact decoder again at the
admission, send, or evidence boundary; an in-memory type check alone is not
rehydration evidence. Durable origin transfer, pre-send claiming, and local
send-entry consumption alone establish neither safe provider delivery nor an
exactly-once provider effect.

The preview executor supplies the killable-process, structural result
validation, and live-revalidation hook of the stronger timeout contract. It
does not by itself prove atomic publication, rollback, or exactly-once effects;
the existing operation-dispatch outbox is partial evidence until a closed
adapter contract satisfies the stronger requirements above.

### Local timeout and retry

<a id="GB-GCR-TIMEOUT-RETRY-001"></a>

A configured node timeout MUST be a positive finite duration and invalid values
MUST be rejected before the node is
scheduled. At its deadline, the in-process runtime exposes cancellation through
the block context; cooperative blocks MUST inspect that token before committing
an effect. This first stable local-runtime contract guarantees deadline
signaling and rejection of stale authoritative commits, not preemptive
termination of arbitrary in-process code or rollback of an external effect. An
adapter claiming stronger post-deadline behavior MUST provide the termination
and effect-fencing boundaries described above. A stale retry, lease holder, or
fencing token MUST NOT mutate a newer attempt.

<a id="GB-GCR-RETRY-LIMIT-001"></a>

Node retry attempts MUST be an integer from 1 through 100. Stable schemas MUST
enforce that maximum, and compilers admitting preview or legacy graph forms
MUST reject a larger value with `GB1008` before execution.

### Bounded work and durable state

Sequences and dynamic task work MUST declare hard bounds. State mutation MUST
use an expected revision or equivalent compare-and-swap fence. Replay MUST be
idempotent for identical authoritative records and reject conflicting identity
reuse.

Persisted run records MUST fail closed when required deployment provenance,
invocation mode, or model-visible tool evidence is missing or malformed. Replay
MUST NOT synthesize defaults for corrupt stored contract fields.

## Python/native boundary

<a id="GB-GCR-LANGUAGE-BOUNDARY-001"></a>

Rust is the normative Graph compiler. Python provides authoring APIs and an
explicit reference oracle; its public compiler entry point invokes the native
compiler and MUST fail closed when that compiler is unavailable rather than
selecting the reference implementation implicitly. Native execution may be
selected only when the compiled plan and required contracts are supported by
the native runtime. The language boundary MUST preserve canonical values,
diagnostics, hashes, journal order, cancellation, and terminal outcome. See
[ADR-0001](../decisions/0001-rust-normative-authority.md) and
[language support](../conformance/language-support.md).

<a id="GB-GCR-LANGUAGE-BOUNDARY-002"></a>

Compiler-backed CLI commands MUST translate an unavailable native compiler to
`GB1056` and exit with status 1 without emitting a traceback. Commands with a
machine-readable output contract MUST return that diagnostic in their JSON
diagnostics array.

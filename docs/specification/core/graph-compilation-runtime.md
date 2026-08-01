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
MUST NOT precede repository time. A matching replay of an already committed
acknowledgement or retry MAY succeed after expiry so response loss does not turn
a completed transaction into a conflicting mutation.

A storage upgrade that first adopts repository-owned delivery time MUST NOT
retain active claims whose expiry was issued from caller-controlled time. It
MUST transactionally invalidate and requeue those claims while advancing their
generation and fencing token, or fail the migration closed unless every
delivery counter retains enough headroom to issue the requeued claim. Requeued
delivery remains at-least-once and therefore retains the stable
receiver-deduplication requirement. An upgrade that cannot tolerate an
immediate duplicate MUST quiesce legacy dispatchers before opening the database
with the new schema authority.

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

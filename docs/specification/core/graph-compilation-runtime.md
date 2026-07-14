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

Each normalized target endpoint MUST have at most one distinct source. Distinct
sources writing the same block input or graph output MUST fail with `GB1007`.
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

### Local timeout and retry

<a id="GB-GCR-TIMEOUT-RETRY-001"></a>

A configured node timeout MUST be a positive finite duration and invalid values
MUST be rejected before the node is
scheduled. At its deadline, the in-process runtime exposes cancellation through
the block context; cooperative blocks MUST inspect that token before committing
an effect. An adapter that cannot cooperate MUST provide its own force-
termination or effect-fencing boundary. A stale retry, lease holder, or fencing
token MUST NOT mutate a newer attempt.

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

Python is the authoring and broad reference implementation. Native execution
may be selected only when the compiled plan and required contracts are
supported by the native runtime. The language boundary MUST preserve canonical
values, diagnostics, hashes, journal order, cancellation, and terminal outcome.
See [language support](../conformance/language-support.md).

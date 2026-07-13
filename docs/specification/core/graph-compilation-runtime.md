# Graph, Compiler, and Runtime

## Graph validation

A graph MUST define unique node and edge identities, resolvable block types,
valid port directions, compatible value types, and all required inputs. The
compiler MUST diagnose unknown endpoints, duplicate identities, invalid
configuration, unsupported cycles, unresolved binding requirements, and target
incompatibility before execution.

Catalog-backed compilation MUST be closed-world by default. Every executable
node MUST resolve to one descriptor; a missing descriptor MUST fail with
`GB1022`. An implementation MAY expose an explicitly open catalog for discovery
or compatibility workflows, but that result MUST NOT be represented as proof
that unknown blocks are executable.

Declared root-port types MUST be compared by exact nominal identity for
graph-input-to-block, block-to-block, and block-to-graph-output connections.
`Any` is the only wildcard. Implementations MUST NOT coerce or structurally
equate two different schema IDs. An optional block output MUST NOT feed a
required block input or graph output, and every required block input MUST be
supplied. A nested endpoint MUST name an existing root port; compilation does
not infer a nested field type beyond that root, so payload-schema validation is
a separate boundary.

Every edge endpoint MUST contain an owner and port path. `$input` is valid only
as an edge source and `$output` only as an edge target; the opposite directions
MUST fail compilation. Ordinary executable graphs MUST be acyclic unless a
selected runtime profile explicitly defines a bounded cycle construct.

Normalization and expansion MUST be deterministic. A physical plan MUST bind
the normalized graph, resolved blocks and packages, target, policy inputs, and
compiler version into canonical evidence. Identical inputs MUST produce the same
plan hash.

## Execution

The runtime MUST schedule a node only after its dependencies and admission
requirements are satisfied. It MUST preserve typed ports, record state
transitions in order, and project exactly one terminal outcome per run. Terminal
success, failure, cancellation, rejection, pause, and exhaustion MUST remain
distinguishable.

A catalog-backed runtime MUST reject handlers registered under undeclared block
IDs and MUST reject duplicate registration unless the caller explicitly uses a
replacement operation. After a block returns, the runtime MUST reject a
non-mapping result, any output key absent from the descriptor, and omission of
any required output. Callback resume MUST enforce the same output contract.
An explicit untyped compatibility mode MAY admit handlers without descriptors;
it MUST NOT disable output checks for descriptors that are present.

A node `when` reference is a boolean dependency. The runtime MUST wait for that
dependency, execute the node only when it resolves to `true`, and skip it
without invoking the block when it resolves to `false`. A missing or non-boolean
condition MUST fail closed. The referenced root port MUST exist on a declared
graph input or resolved source block. In particular, a false guard MUST never allow a
state-changing block to commit an effect. Guard resolution gates ordinary input
readiness: a false branch MUST be skippable without waiting for inputs that the
block will never consume. The skip and its reason MUST remain auditable, and
native scheduling MUST propagate skipped outcomes to dependent ports.

Failures after a block returns, including output-path projection and checkpoint
materialization, remain part of node execution. They MUST append a terminal
failure, update durable run state, and release run-scoped leases instead of
escaping with the run still recorded as running. Output projection MUST finish
before terminal success is recorded. The same cleanup rule applies while
projecting a resumed callback, and a failed resume MUST consume its checkpoint
so it cannot be replayed.

Cancellation MUST be structured and cooperative, with explicit behavior for
in-flight provider calls, tools, children, checkpointing, and cleanup. Timeout
and retry MUST use bounded policies. A configured node timeout MUST be a
positive finite duration and invalid values MUST be rejected before the node is
scheduled. At its deadline, the in-process runtime exposes cancellation through
the block context; cooperative blocks MUST inspect that token before committing
an effect. An adapter that cannot cooperate MUST provide its own force-
termination or effect-fencing boundary. A stale retry, lease holder, or fencing
token MUST NOT mutate a newer attempt.

Sequences and dynamic task work MUST declare hard bounds. State mutation MUST
use an expected revision or equivalent compare-and-swap fence. Replay MUST be
idempotent for identical authoritative records and reject conflicting identity
reuse.

Persisted run records MUST fail closed when required deployment provenance,
invocation mode, or model-visible tool evidence is missing or malformed. Replay
MUST NOT synthesize defaults for corrupt stored contract fields.

## Python/native boundary

Python is the authoring and broad reference implementation. Native execution
may be selected only when the compiled plan and required contracts are
supported by the native runtime. The language boundary MUST preserve canonical
values, diagnostics, hashes, journal order, cancellation, and terminal outcome.
See [language support](../conformance/language-support.md).

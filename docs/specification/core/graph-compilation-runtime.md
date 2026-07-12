# Graph, Compiler, and Runtime

## Graph validation

A graph MUST define unique node and edge identities, resolvable block types,
valid port directions, compatible value types, and all required inputs. The
compiler MUST diagnose unknown endpoints, duplicate identities, invalid
configuration, unsupported cycles, unresolved binding requirements, and target
incompatibility before execution.

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

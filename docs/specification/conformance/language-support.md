# Language Support

This matrix describes the current source-tree implementation. It is not a
release compatibility promise.

| Contract area | Python | Rust |
| --- | --- | --- |
| Schema and canonical authoring utilities | Canonical load/dump/hash, `SchemaId.parse`, resource validation, and resource migration are fail-closed native facades with explicit Python reference oracles; the complete schema TCK remains `referenceOracle` for other schema-facing behavior | Active authority for canonical serialization/hash, SchemaId identity, resource validation, and resource migration through the installed binding |
| Graph compiler and canonical Plan identity | `compile_graph` is a fail-closed native facade; `compile_graph_reference` is the explicit oracle executed beside every installed compiler-TCK case | Normative compiler, exposed through the exact `graphblocks-runtime` wheel and accepted only when its complete Plan contract matches the Python oracle |
| Cross-file YAML composition authoring | Implemented; materializes an expanded Graph | Does not resolve authoring sources; consumes expanded Graph YAML |
| Typed code graph authoring | Implemented and mypy-tested for the stdlib RAG vertical slice; catalog-backed and materializes a portable Graph | Implemented and trybuild-tested for the stdlib RAG vertical slice; catalog-backed and materializes a portable Graph |
| Local runtime, cancellation, tools, budget core | Implemented reference interpreter and exact oracle for every bundled stable C1 suite | Active authority for every bundled stable C1 exact-differential suite; typed ports, outcomes, cancellation, and local flow still block complete C1 authority |
| Documents, RAG, conversation reference APIs | Implemented | Selected core models/TCK behavior |
| Accepted runs and callback resume | Reference server; process-local checkpoint continuation | Preview single-process/single-worker SQLite continuation plus core async/callback records and TCK behavior; consumes trusted pre-admission assertions and does not query policy/budget/schema/lease authorities or verify lease freshness |
| Registered-secret signed webhook dispatch | Implemented in `graphblocks.callbacks` | Implemented in runtime-core with HMAC signing, replay verification, and egress-bound delivery hooks |
| Bounded orchestration | Full acceptance contract, including depth/parallel limits and budget-bound leases | Core task-plan/lease contracts; not full Python parity |
| Workspace governed commit | Implemented | Evaluation primitives only; not full commit contract |
| Release attestation, canary, rollback/drain evidence | Implemented | Deployment primitives; not full named reference contract |
| Telemetry correctness outbox | Implemented in `graphblocks.telemetry` | Observability primitives; not full outbox contract |
| Voice interruption/playback authority | Implemented in `graphblocks.voice` | Implemented in runtime-core and covered by the shared TCK foundation |
| Durable stream extension | Implemented reference contracts | Implemented reference contracts |

Profile claims are determined by applicable fixtures and acceptance evidence,
not this summary alone. Advanced provider-specific voice adapters may still add
their own evidence beyond the shared provider-authority and playback lifecycle
cases.

Authority is phase-scoped. It does not promote every Rust API or every surface
of the native wheel. The accepted transition and its remaining release blockers
are defined by [ADR-0001](../decisions/0001-rust-normative-authority.md).
The stable release matrix is packaged with `graphblocks`; installed TCK runs
read its closed suite claim projection directly and retain the matrix digest,
language, implementation identity, profile role, comparison mode, and the
runner-issued executor proof. Rust compiler evidence is
`exact-native-reference`. Installed platform evidence separately binds a fixed
canonical/identity corpus and the complete shared resource-validation and
migration corpora across public/reference/direct-native paths to the exact
runtime wheel. The complete C0 schema suite remains `reference-only`. C1
evidence is phase-scoped: the bundled runtime suite is
`exact-native-reference`. The `application-events`, `retry`, `sequence`,
`tool-execution`, `tool-lifecycle`, and `tool-result` suites are also
`exact-native-reference`. No stable C1 suite remains `reference-only`.
With a selected native wheel, the `application-events` report executes a
materialized-event stream admission differential:
per-operation accepted/dropped updates, final accepted events, and cutoff
responses must match the Python oracle. The installed private conformance
adapter also executes the raw shared-fixture operations through Rust and exact
compares normalized metadata/payloads, operation-level emission and admission,
accepted events, and structured diagnostics. Numeric `*UnixMs` fields are
authoritative over legacy display timestamps. The shared adversarial corpus
includes boolean numeric coercion and unknown-operation cases with exact code,
message, and path comparison. The release matrix binds the report to the
`rust-application-events-exact-differential` executor and exact runtime wheel,
with Python retained as the reference oracle.
The installed `retry` report executes all four shared fixtures through the Rust
local runtime and exact-compares the closed status, terminal, attempt,
retry-key, and context-key contract with Python. Its evidence is assigned to
`rust-retry-exact-differential` and bound to the same exact runtime wheel.
The installed `sequence` report exact-compares bounded FIFO operation results,
buffer lengths, terminal state, and invalid-capacity creation through
`rust-sequence-exact-differential`, bound to that exact wheel.
The installed `tool-execution` report executes all 19 shared plan fixtures in
Rust and exact-compares creation errors, per-operation ready and state
transition results, policy-stop effects, and final call states with Python.
Its evidence is assigned to `rust-tool-execution-exact-differential` and bound
to that exact wheel.
The installed `tool-lifecycle` report executes all 18 shared argument,
admission-order, approval, and idempotency fixtures in Rust and exact-compares
their closed result flags with Python. Its evidence is assigned to
`rust-tool-lifecycle-exact-differential` and bound to that exact wheel.
The installed `tool-result` report executes all six shared preparation and
stream-state fixtures in Rust and exact-compares their closed result contract
with Python. Its evidence is assigned to
`rust-tool-result-exact-differential` and bound to that exact wheel.
This preserves the boundary around other schema-facing and stable C1 runtime
behavior. C4 production and X3 durable-stream durability are preview promotion
work under `REL-EXTENSION-RUNTIME-AUTHORITY`; their live-authority,
multi-process, fencing, and outbox requirements do not block the first C0/C1
release.

Composition is outside the runtime language boundary. A Python-authored graph
may be materialized with `graphblocks compose` and then compiled or run by Rust
without granting the Rust process access to the source tree. Direct Rust
composition support requires parity on composition fixtures, canonical expanded
values, graph hashes, and deterministic diagnostics before it can be listed as
implemented.

Typed code authoring is intentionally narrower than the complete block catalog.
The current Python and Rust definitions cover the stdlib RAG path demonstrated
by example 01 and preserve the portable Graph as the compiler/runtime boundary.
Python checks schema-and-marker identity, required catalog ports, and reference
provenance in addition to generic static types. Rust uses private `Port<T>`
construction and `PortType::TYPE_REF`, then rechecks catalog identity and port
provenance in `GraphBuilder`. Both materialized documents undergo catalog-backed
compiler validation before execution.

# Language Support

This matrix describes the current source-tree implementation. It is not a
release compatibility promise.

| Contract area | Python | Rust |
| --- | --- | --- |
| Canonical schema/compiler | Implemented and TCK-backed | Implemented and TCK-backed |
| Cross-file YAML composition authoring | Implemented; materializes an expanded Graph | Does not resolve authoring sources; consumes expanded Graph YAML |
| Typed code graph authoring | Implemented and mypy-tested for the stdlib RAG vertical slice; catalog-backed and materializes a portable Graph | Implemented and trybuild-tested for the stdlib RAG vertical slice; catalog-backed and materializes a portable Graph |
| Local runtime, cancellation, tools, budget core | Implemented | Implemented |
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

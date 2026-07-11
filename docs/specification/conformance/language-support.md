# Language Support

This matrix describes the current source-tree implementation. It is not a
release compatibility promise.

| Contract area | Python | Rust |
| --- | --- | --- |
| Canonical schema/compiler | Implemented and TCK-backed | Implemented and TCK-backed |
| Local runtime, cancellation, tools, budget core | Implemented | Implemented |
| Documents, RAG, conversation reference APIs | Implemented | Selected core models/TCK behavior |
| Accepted runs and callback resume | Reference server; process-local checkpoint continuation | Core async/callback records and TCK behavior |
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

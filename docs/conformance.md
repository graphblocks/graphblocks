# Conformance and TCK

GraphBlocks compatibility is profile-based. Do not claim broad compatibility
because a package imports successfully or because a directory exists in the
repository.

## Profiles

The upstream architecture defines these major profiles:

- `GB-C0-SCHEMA`: canonical schemas, graph parsing, normalization, hashing, and
  plugin manifest validation.
- `GB-C1-LOCAL-RUNTIME`: local runtime, scheduler, typed ports, outcomes,
  cancellation, journal, flow, and Python binding behavior.
- `GB-C2-AI-APPLICATION`: document, RAG, conversation, and provider-neutral
  application contracts.
- `GB-C3-GOVERNED-RUNTIME`: policy, usage, budget, permits, exhaustion,
  approval, review, and gate semantics.
- `GB-C4-PRODUCTION`: immutable release, worker protocol, placement, drain,
  deployment revision, audit, SLO, and telemetry contracts.
- `GB-X1-ORCHESTRATION`: bounded task plans, task patches, worker/model pools,
  and task budget delegation.
- `GB-X2-VOICE`: duplex sessions, VAD authority, interruption, and playback
  ledger semantics.
- `GB-X3-DURABLE-STREAM`: unbounded source offsets, watermarks, checkpoints, and
  sink commit semantics.

## TCK Fixtures

Shared fixtures live under `tck/`. Rust and Python harnesses consume the same
fixtures where the suite applies. This keeps schema, compiler, runtime, and
facade behavior aligned.

The `application-events` suite includes authoritative stream invariants:
idempotent exact replay, duplicate event-id conflict rejection, and monotonic
per-run sequence enforcement.

The `durable` suite includes async callback guards for signature/auth failures,
explicitly unauthenticated callbacks, schema failures, stale attempts,
non-`ExternalCallbackReceived` receipt promotion, provider-operation mismatches,
timeout/cancel races, and budget-paused resume.

Common commands:

```bash
graphblocks-tck list tck
graphblocks-tck run schema tck/schema/cases.json
graphblocks-tck run policy tck/policy/cases.json
graphblocks-tck run-all tck
```

Profile inventory checks use the implemented profile catalog:

```bash
graphblocks-tck check tck --profiles src/graphblocks/data/conformance-profiles.yaml --profile GB-C3-GOVERNED-RUNTIME
```

## Acceptance Applications

Profile claims can also require acceptance applications. The manifest in
`acceptance/applications.yaml` maps each required application to a shipped
runnable scenario, profile, and gate list. Implementation-specific runnable
fixtures live under `acceptance/scenarios/`; the checksummed upstream examples
remain design references and are not rewritten to satisfy local compiler rules.

Run the manifest to produce immutable, digest-backed gate evidence:

```bash
graphblocks-tck run-acceptance acceptance/applications.yaml --root . --json
```

The runner dispatches only exact registered gate names. The built-in
`graphblocks validate` and `graphblocks plan --expand` gates invoke the Python
CLI directly with a fixed argument vector; manifest text is never evaluated by
a shell. The coding-agent production application also has four non-overridable
built-ins: accepted invocation handles, cursor replay after detach, authenticated
callback journal-before-resume ordering, and registered-secret signed webhook
delivery. These gates run authenticated server flows and emit canonical,
digest-backed evidence bound to the application and scenario. The signed gate
uses the actual optional callback dispatcher and receiver verifier; install it
with `graphblocks-testing[production]`. Missing optional dependencies or unknown
semantic gates fail closed. Other semantic gates still require an explicitly
registered handler.

The coding-agent fixture proves those four framework semantics against the
scenario contract. Callback schema, idempotency, attempt fencing, resume fences,
checkpointing, and timeout settings are validated and projected from that
scenario into the executable probe; weakening them fails the gate. Dynamic
runtime timestamps are excluded from canonical evidence so repeat executions
remain stable. This does not claim that every application/provider block in the
complete design graph has a local implementation. Structural manifest coverage
alone is not sufficient for a conformance claim or release-candidate gate: every
required application needs a current passing execution report bound to the
manifest digest.

The default runner also executes the shipped AI-application semantic gates for
direct-file analysis, document ingestion, enterprise RAG, and multi-turn chat.
Those probes use real lineage/chunking, local blob persistence, ordered parser
fallback, ACL-filtered retrieval, citation/grounding validation, abstention,
conversation CAS, and draft commit/retraction APIs. The direct-file application
uses `acceptance/scenarios/direct-file-analysis.yaml`; the prior authority-backed
advisory design reference did not declare the generated artifact required by the
gate. Parser candidate fallback records failed locks and the selected fallback
instead of treating a configured candidate list as execution evidence. Each
probe also validates the relevant block identities and dataflow edges; ingestion
declares required ACL propagation through document, chunk, and index stages.

## Claiming Support

When adding a feature, update or add the narrowest applicable TCK fixture. A
feature is not done just because the happy path works; it needs deterministic
behavior for invalid input, boundary cases, replay, cancellation, policy
rejection, or dependency closure where those concerns apply.

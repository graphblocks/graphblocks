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
example, profile, and gate list. Examples that serve as profile evidence should
be declared there instead of remaining only illustrative YAML.

Run the manifest to produce immutable, digest-backed gate evidence:

```bash
graphblocks-tck run-acceptance acceptance/applications.yaml --root . --json
```

The runner dispatches only exact registered gate names. The built-in
`graphblocks validate` and `graphblocks plan --expand` gates invoke the Python
CLI directly with a fixed argument vector; manifest text is never evaluated by
a shell. Semantic gates require an explicitly registered handler and fail closed
when none exists. Structural manifest coverage alone is not sufficient for a
conformance claim or release-candidate gate: every required application needs a
current passing execution report bound to the manifest digest.

## Claiming Support

When adding a feature, update or add the narrowest applicable TCK fixture. A
feature is not done just because the happy path works; it needs deterministic
behavior for invalid input, boundary cases, replay, cancellation, policy
rejection, or dependency closure where those concerns apply.

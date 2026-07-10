# Conformance Profiles

The canonical profile catalog is
`src/graphblocks/data/conformance-profiles.yaml`. Profiles are cumulative only
through their declared `extends` relationships.

- `GB-C0-SCHEMA`: schemas, canonical values, parsing, normalization, hashing,
  plugins, and migration readers.
- `GB-C1-LOCAL-RUNTIME`: scheduling, typed ports, outcomes, cancellation,
  journal, bounded flow, tools, and Python/native boundary behavior.
- `GB-C2-AI-APPLICATION`: documents, retrieval/RAG, conversation, and
  application protocol.
- `GB-C3-GOVERNED-RUNTIME`: policy, usage, budget, permits, exhaustion,
  approval, review, checks, and gates.
- `GB-C4-PRODUCTION`: background runs, callbacks, immutable releases, workers,
  deployment, drain, audit, SLOs, and telemetry projection.
- `GB-X1-ORCHESTRATION`: bounded task plans, patches, pools, and delegated
  task budgets.
- `GB-X2-VOICE`: duplex sessions, VAD authority, interruption, and playback.
- `GB-X3-DURABLE-STREAM`: offsets, watermarks, checkpoint barriers, and sink
  commits.

A claim MUST identify implementation name/version, profile, schema/spec
revision, applicable TCK report, required acceptance report, and known
limitations. Passing a base profile does not imply an extension. A profile whose
catalog status is provisional or experimental MUST retain that qualifier.

Reports become stale when the implementation, manifest, scenario, schemas,
catalog, or required fixture digest changes.

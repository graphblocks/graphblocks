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

## Release tracks and ownership

The conformance catalog defines capability inheritance. The machine-readable
[release matrix](../../project/stable-release-matrix.yaml) separately owns each
profile's release track, claim-owner artifact, distinct implementation and
packaged evidence-producer artifacts, role-scoped active/target/reference
authority, compatibility tier, ancestors, and promotion gate. The package
catalog maps the corresponding core, AI application, governance,
production-platform, orchestration, voice, durable-stream, and integration
component trains.
Every component declares its required profiles; all component dependencies
must resolve within those profiles' transitive ancestor closure. Mixed-profile
components such as governed agents therefore retain an AI owner while naming
governance as an additional required profile; external adapters likewise name
the exact profile they require. The shared CLI
records separate stable core-command and preview production-command surfaces
instead of assigning the whole module one misleading tier.
Document and model-application adapters require C2, executable MCP/OpenAPI tool
bridges require at least C1, and governance, production, voice, and durable
adapters require their corresponding extension profile. A stable integration
claim cannot outrank any of those required profiles.
Authority inheritance follows the exact role maps of `extends` ancestors; it
does not flatten an active Python reference interpreter and a blocked Rust
scheduler target into one active Rust authority.

`GB-C0-SCHEMA` and `GB-C1-LOCAL-RUNTIME` are the complete 1.0 core release
track. A new core profile is eligible only when its semantics are required for
portable execution, can be implemented by at least two independent runtimes,
have a provider-neutral TCK, and do not impose provider, database, or deployment
policy. The `REL-CORE-PROFILE` gate is release-blocking for that track.

All other profiles are independently promoted extensions. Application,
governance, production-platform, orchestration, voice, and durable-stream
capabilities do not become stable because C0 or C1 passes. A child profile
cannot be promoted above an ancestor's tier and must retain every applicable
ancestor promotion gate. Their package presence is not a compatibility claim,
and `REL-EXTENSION-PROFILE` is a profile-identity-bound, currently blocked gate
outside the 1.0 global gate set. External integrations use the separate
integration maturity and real-service promotion policy. Known security defects
in shipped extension code still remain release-blocking.

A claim MUST identify implementation name/version, profile, schema/spec
revision, applicable TCK report, required acceptance report, and known
limitations. Passing a base profile does not imply an extension. A profile whose
catalog status is provisional or experimental MUST retain that qualifier.

Reports become stale when the implementation, manifest, scenario, schemas,
catalog, or required fixture digest changes.

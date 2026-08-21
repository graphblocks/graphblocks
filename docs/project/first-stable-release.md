# First Stable Release Boundary

This document defines the intended compatibility boundary for the first stable
GraphBlocks release. It is a release target, not a statement that the current
source tree is already stable. A row marked **stable** is part of the 1.0
promise only after every release gate in this document passes. The canonical
machine-readable form of these classifications is
[`stable-release-matrix.yaml`](stable-release-matrix.yaml).
Artifact release trains and interoperability contracts are separately recorded
in [`version-compatibility.yaml`](version-compatibility.yaml). A package SemVer
identifies an independently published artifact; it does not imply the schema,
native-binding, worker, application, or durable-checkpoint protocol version.
Only combinations declared by that matrix and accepted by the runtime
handshake are supported. An undeclared or mismatched combination fails closed
before the operation begins.

The boundary deliberately starts with the portable schema/compiler and local
runtime. It does not cancel the work on AI application, governance, production,
or extension profiles. Those areas remain on the stabilization path and can be
promoted independently without weakening the initial core promise.

## Stability tiers

| Tier | Release meaning |
| --- | --- |
| Stable | Covered by the [compatibility policy](../specification/reference/compatibility-policy.md), executable conformance evidence, and the supported-platform matrix. |
| Preview | Shipped for evaluation and subject to release-note-documented changes in a minor release. It is not covered by the stable compatibility window. |
| Internal | Used to build or verify GraphBlocks, but not a supported public API or independently consumable release artifact. |
| Reserved | Holds a package name or future surface. It provides no usable implementation or compatibility promise. |

Repository presence, a passing unit test, a package-catalog entry, or a
`0.1.x` version does not promote an item to stable.

The Python core and testing artifacts use the `1.0.0rc17` core candidate train,
while the native runtime, active Rust workspace crates, and deployment scaffold
remain on their independent `0.1.0` trains. The Cargo and npm `graphblocks`
packages at `0.0.2` reserve names only. These version numbers express artifact
release maturity and publication history, not wire compatibility. The
machine-readable compatibility matrix binds each contract to the exact
artifact ranges allowed to participate in it and to mismatch regression
evidence.

## Artifact matrix

### Python distributions

| Artifact | 1.0 tier | Stable scope or limitation |
| --- | --- | --- |
| `graphblocks` | Stable | Python authoring and schema facade, stable API, explicit reference oracle, built-in C0/C1 blocks, and compiler-backed CLI contracts. Public compilation uses the companion `graphblocks-runtime` Rust binding and has no implicit Python fallback. Modules belonging to C2-C4, X1-X3, external integrations, server/deployment operations, and catalog entries explicitly listed below remain preview even though they ship in this wheel. |
| `graphblocks-testing` | Stable | TCK discovery/execution, C0/C1 fixtures, deterministic report format, and the `graphblocks-tck` command. A TCK report is evidence for the named implementation/profile/digests, not a blanket claim for the whole repository. |
| `graphblocks-runtime` | Stable | Stable scope is limited to the normative compiler binding and the C1 runtime surface that passes the final authority, protocol, supported-wheel, suspension, and differential gates. Other native entry points remain preview unless named by a promoted profile. |

The deliberately small candidate stable Python surface is enumerated in
[`compatibility/stable-python-surface.yaml`](../../compatibility/stable-python-surface.yaml)
and enforced against its exact
[`stable-python-api.json`](../../compatibility/stable-python-api.json) signature
and dataclass-field snapshot. Importing a name from the distribution does not by
itself make that name stable. The `validate`, `plan`, and `run` exit-code and
parsed-JSON cases are likewise enumerated in
[`stable-cli-cases.yaml`](../../compatibility/stable-cli-cases.yaml) and frozen
in
[`stable-cli-contracts.json`](../../compatibility/stable-cli-contracts.json).
These snapshots are candidate-enforced evidence and have been refreshed against
the stable `v1` wire resources. They still require independent compatibility
review before the release gate can be declared passed.

The installed C1 runtime compatibility boundary is separately enumerated in
[`stable-runtime-surface.yaml`](../../compatibility/stable-runtime-surface.yaml)
and frozen in
[`stable-runtime-api.json`](../../compatibility/stable-runtime-api.json). It
contains only native status, fail-closed availability admission, and the
checkpoint-free `run_stdlib_graph(graph, inputs, *, run_id=None)` entry point.
The installed-wheel probe checks those exact signatures, the closed status and
five-field result contracts, `runtime.local.v1`, `py.typed`, smoke execution,
and package/native bytes from the selected wheel. Persistence, checkpoint,
callback, deployment, raw JSON, and test-runtime helpers remain preview.

### Rust and non-Python artifacts

| Artifact | 1.0 tier | Reason |
| --- | --- | --- |
| `graphblocks-schema`, `graphblocks-compiler`, `graphblocks-flow`, `graphblocks-runtime-core`, `graphblocks-runtime-durable`, `graphblocks-protocol`, `graphblocks-telemetry`, `graphblocks-control-plane`, and `graphblocks-python` crates | Internal | Implementation crates used by the native bindings, native CLIs, and conformance work. Their Rust APIs are not yet a public SemVer surface. |
| `graphblocks-native` executable (`graphblocks-cli-native` crate) | Preview | Python-free validate/plan/run is useful, but native block coverage, adapter injection, diagnostics, and differential evidence are not yet at the stable gate. |
| `graphblocks-control` executable | Internal | The binary is delivered by the `graphblocks-control-plane` crate. It is an argv-driven one-shot CLI with command-specific JSON stdin payloads and structured JSON stdout/stderr; it is not a server or daemon. |
| `graphblocks` Rust crate | Reserved | Repository version 0.0.2 emits a build warning and exports only `RESERVED_PACKAGE_NOTICE`; it has no supported Rust API. Registry publication remains gated. |
| `graphblocks` npm package | Reserved | Repository version 0.0.2 throws `ERR_GRAPHBLOCKS_RESERVED_PACKAGE` on import and has no JavaScript/TypeScript API. Publication and registry deprecation evidence remain gated. |
| `graphblocks-deployment-chart` Helm scaffold | Internal | Disabled by default. It contains templates for a user-supplied controller but no controller implementation or OCI image and makes no operator/reconciliation claim. |

Promotion of a Rust crate, native executable, npm API, or Kubernetes operator changes the
release matrix and requires its own public-surface snapshot, packaging gates,
and compatibility evidence. It is not implied by Python 1.0.
The deployment scaffold cannot be promoted to an operator claim until
`REL-KUBERNETES-OPERATOR` has revision-bound controller source and signed image,
reconcile/status convergence, conflict retry, finalizer, leader-election,
envtest, kind install/upgrade, and CRD migration/rollback evidence.
The repository contract for both reserved-name packages is enforced by
`REL-RESERVED-ARTIFACTS`, but it does not change crates.io or npm registry state.
Publishing 0.0.2 and applying a registry-visible npm deprecation message require
separate release authorization and evidence.

## Conformance-profile matrix

Profile stability is implementation-specific. The table describes the
phase-scoped first-stable claim selected by
[ADR-0001](../specification/decisions/0001-rust-normative-authority.md); reports
must identify both the Rust authority and Python facade/reference roles.

| Profile | 1.0 tier | Promotion condition |
| --- | --- | --- |
| `GB-C0-SCHEMA` | Stable | Rust-authoritative Graph compilation and Plan identity behind the Python authoring/schema facade; closed schemas and readers for every claimed stable resource; alpha-to-v1 migrations; closed-world compilation; deterministic registered diagnostics; installed native wheels; and exact C0 differential/TCK evidence. |
| `GB-C1-LOCAL-RUNTIME` | Stable | All C0 gates plus promotion of the Rust runtime target selected by ADR-0001, typed ports, outcomes, cooperative deadline signaling, rejection of stale authoritative commits, journal, bounded flow, tool lifecycle, protocol handshake, and restart-independent correctness evidence. Python remains the explicit reference interpreter. The claim does not promise preemptive termination of arbitrary provider work or rollback of an external effect after a deadline. |
| `GB-C2-AI-APPLICATION` | Preview | Documents, retrieval/RAG, conversation, and application protocol remain on the roadmap until their wire/API and acceptance gates are frozen. |
| `GB-C3-GOVERNED-RUNTIME` | Preview | Policy, usage, budget, permit, approval, review, and workspace contracts require their own stable API and durability/security gates. |
| `GB-C4-PRODUCTION` | Preview | Existing checkpoint, replay, journal-repair, and claim/fence primitives remain preview evidence. Promotion additionally requires live authority and lease-freshness revalidation, independent-process crash recovery, a durable outbox/idempotency boundary for output and effect publication, explicit delivery/effect guarantees, immutable-release evidence, and production adapter verification. |
| `GB-X1-ORCHESTRATION` | Preview (provisional) | Retains the catalog's provisional qualifier until bounded orchestration and delegated-budget parity gates pass. |
| `GB-X2-VOICE` | Preview (experimental) | Retains the catalog's experimental qualifier until transport/provider support and interruption/playback authority gates pass. |
| `GB-X3-DURABLE-STREAM` | Preview (experimental) | Existing source replay, barrier, watermark, window, checkpoint, and sink-commit primitives remain preview evidence. Promotion requires real multi-process restart/crash failpoints, fresh lease and authority checks at commit, durable outbox/idempotency evidence, and an explicit boundary between at-least-once delivery and any exactly-once effect claim. |

The SQLite accepted-run vertical slice now has required independent-process
`SIGKILL` evidence across admission, lease claim, checkpoint, outbox, and
terminal-commit transaction boundaries, including competing-worker fencing and
stale-claim rejection. C4 remains preview because this evidence does not by
itself establish deployment-like Postgres recovery, provider/output effect
guarantees, or the separate X3 streaming state machine.

The release matrix assigns every profile a claim-owner artifact, distinct
implementation and evidence artifacts, role-scoped active/target/reference
authority, compatibility tier, release track, ancestors, and promotion gate.
Only C0 and C1 belong to the release-blocking 1.0 core track. Each preview
profile belongs to a named extension track and must eventually be promoted with
profile-identity-bound evidence through the currently blocked
`REL-EXTENSION-PROFILE` definition. A child cannot outrank an ancestor or omit
its applicable gate, and shipping extension code in a core package does not
widen the stable compatibility claim. The package catalog also binds every
component to required profiles and rejects dependencies outside their
transitive profile ancestry. Mixed modules such as the CLI record stable core
commands and preview production commands as separate profile surfaces.

Passing C0 or C1 does not make a preview profile stable. Conversely, keeping a
profile preview does not remove it from the specification or future roadmap.
Capability-completeness gates that apply only to C4 or X3 do not block the
first stable C0/C1 release while those profiles remain explicitly preview. They
become blocking for a later promotion of the named preview profile.

That sequencing does not waive release-wide defect gates. A known P0 or P1 in
code shipped by the 1.0 release blocks promotion even when the affected
capability remains preview; alternatively, the affected code must be removed
from every release artifact. Compatibility scope and security release scope
are different boundaries.

The executable mapping from every direct C0/C1 capability requirement to its
normative source, implementation, schema, TCK suite, and focused tests is
maintained in `stable-requirements.yaml`; CI rejects drift from the canonical
profile catalog or missing evidence paths.

## Wire-version matrix

| Resource/version | 1.0 tier | Read/write policy |
| --- | --- | --- |
| `graphblocks.ai/v1` `Graph` | Stable candidate | Canonical output and authoring target for C0/C1. The closed schema contains graph interfaces, executable block nodes, edges, typed configuration/resource bindings, conditions, bounded local flow/effects, tool bindings/execution, and output policy. Composition, background execution, events, callbacks, AI-application state, governance, voice, and other preview fields are excluded. Its canonical form, alpha migrations, negative reader tests, and TCK fixtures are candidate-enforced; compatibility and release review remain. |
| `graphblocks.ai/v1` `PluginManifest` | Stable candidate | Stable C0 plugin/catalog resource. Its closed schema, alpha migration, stable-reader validation, and TCK evidence are candidate-enforced; compatibility and release review remain. |
| `graphblocks.ai/v1alpha3` `Graph` | Preview and migration input | C0/C1-compatible documents use the explicit, golden-tested alpha-to-v1 migration. A document containing preview-only fields cannot be represented by v1: public migration fails closed, while preview compilation retains alpha3. Alpha3 is not a stable authoring or output contract. |
| `graphblocks.ai/v1alpha1` and `v1alpha2` `Graph` | Migration-only | Accepted only through explicit, golden-tested migrations. They are not valid 1.0 output or stable authoring targets. |
| `graphblocks.ai/v1alpha1` `Application` and `Binding` | Preview | Belong to the C2+ surface and are not part of the initial stable wire promise. |
| `graphblocks.ai/v1alpha1` `PluginManifest` | Preview and migration input | Accepted through the explicit, golden-tested migration to the stable `v1` resource; it is not a stable authoring or output contract. |
| `graphblocks.ai/composition/v1alpha1` `GraphFragment` and composition block | Preview | Authoring convenience outside the initial stable wire promise. Materialized output must ultimately be a stable `graphblocks.ai/v1` Graph. |
| `graphblocks.voice/v1alpha1` extension | Preview (experimental) | Governed by X2 and not covered by the initial stable promise. |
| Acceptance, deployment, GitOps, policy, and other specialized alpha envelopes | Preview or internal | Stable only when a later profile promotion names the exact resource/version and adds migration and conformance evidence. |

An alpha identifier is never silently reclassified as a stable wire contract.
Promotion creates a non-alpha version and an explicit migration.

## Integration matrix

No external-provider or infrastructure integration is stable in the first
release. Stable C0/C1 behavior uses provider-neutral contracts and deterministic
local implementations.

| Tier | Implementation maturity | Components |
| --- | --- | --- |
| Preview | Contract-only | `graphblocks-pdf`, `graphblocks-qdrant`, `graphblocks-mcp`, `graphblocks-openapi`, `graphblocks-openai`, `graphblocks-haystack`, `graphblocks-policy-opa`, `graphblocks-policy-cedar`, `graphblocks-budget-postgres`, `graphblocks-usage-postgres`, `graphblocks-kubernetes`, `graphblocks-terraform`, `graphblocks-oci`, `graphblocks-gitops`, `graphblocks-otel`, `graphblocks-langfuse`, `graphblocks-prometheus`, `graphblocks-dashboards`, `graphblocks-webrtc`, `graphblocks-websocket-media`, `graphblocks-openai-realtime`, `graphblocks-silero-vad`, `graphblocks-kafka`, `graphblocks-nats`, `graphblocks-sqs`, and `graphblocks-pubsub`. |
| Internal | Test-double | `graphblocks-scripted`, repository fakes, and acceptance harness adapters. |

Preview means that the adapter contract may be exercised and documented. It
does not mean that a real external service, SDK version range, authentication
mode, retry policy, or failure model is supported. Each integration is promoted
separately after real-service tests and an explicit dependency/platform matrix.
The machine-readable
[release matrix](stable-release-matrix.yaml) enforces those fields and currently
contains no `real-adapter` entry. Promotion evidence must bind the exact test,
workflow job, commit, workflow run, uploaded artifact, and attestation.
Signed results must cover the Cartesian product of every claimed authentication
mode and service or SDK version; a successful sample from only part of the
declared support matrix cannot authorize promotion.

## Deep-audit gate

The [deep audit remediation plan](audit-remediation-plan.md) is release
blocking. All P0 and P1 findings in a shipped artifact must be closed with
executable regression evidence before 1.0, even when the affected module or
profile remains preview. Preview limits the compatibility promise; it does not
permit a known authorization bypass, fail-open decoder, unbounded
attacker-controlled execution path, or public panic boundary in a shipped
artifact.

The original report, issue inventory, and evidence bundle digests plus the
99-finding workstream crosswalk are recorded in
[`audit-remediation-map.yaml`](audit-remediation-map.yaml). The evidence bundle
does not contain the audited source revision/archive digest. The repository now
preserves all 13 captured reproduction files at their exact bundle digests,
reconstructs the five output-only or command-wrapper harnesses, and binds all
nine findings to current executable regression selectors through
[`audit-reproduction-manifest.yaml`](../../reproductions/audit-reproduction-manifest.yaml).
CI rejects substituted evidence and reruns the reconstructed harnesses and
selectors. The signed `audit-closure` promotion report additionally binds the
reproduction manifest and checker, validates all captured and reconstructed
file digests from both candidate and final Git objects, and records the exact
selector-source digests. Historical vulnerable behavior still cannot be
independently replayed against an identified audited source; that
source-identity limitation is recorded explicitly and is not inferred from the
captured timestamps. It does not block stable promotion because the project
owns this audit and binds its report, inventory, evidence bundle, remediation
commits, and executable regressions directly into the candidate and final
promotion evidence.

The final admission path requires the project-managed audit closure: exact
artifact digests, all 99 finding identities and remediation commits, captured
and reconstructed reproduction digests, executable selectors, and equality of
the candidate and final closure. A closed audited-source package remains an
optional higher-assurance input. When supplied, its Git or archive identity,
file-level evidence, provenance attestation, and Cosign authority are still
verified fail-closed. The current
[`audit-provenance-trust.yaml`](audit-provenance-trust.yaml) therefore records
that optional independent verification is not configured; it is not a stable
release blocker. RC10 through RC12 remain historical evidence. RC13 binds the
completed remediation set and its retained signed evidence is indexed in the
[`v1.0.0-rc.13` operator ledger](releases/v1.0.0-rc.13.md). It has one complete
signed matrix attestation, zero-blocker status, and the configured project
owner's signed approval under the simplified personal-project policy.

The stable promotion validator treats runtime security as evidence distinct
from supply-chain integrity. Each candidate matrix attestation carries the
closed object-authorization scope plus the digest of
[`stable-security-gates.yaml`](stable-security-gates.yaml). One canonical CI
leg executes that manifest's exact pytest node selectors for authorization,
request, response, schema/regex, YAML-parser, and canonical-number behavior.
The resulting canonical report binds the candidate commit, source digests,
all-pass counts, and raw JUnit digest; candidate attestation freezing parses
and revalidates both retained files before recording `passed`. At least one
complete matrix report must be signed by the candidate CI identity. The final
owner signoff must be dispatched by the configured project owner and bind all
included signed candidate matrix report digests. A generic `approved: true`
report or a green unrelated test suite cannot satisfy either runtime-security
gate.

The profile tables now record the phase-scoped authority accepted by ADR-0001.
Standalone canonical serialization/hash, SchemaId routing, resource
validation, and resource migration are complete: installed platform evidence
compares public facades, explicit Python oracles, and direct native functions
over the fixed utility corpus and complete shared resource corpora, then binds
those results to the exact runtime wheel record. This does not relabel other
behavior in the broader reference-only schema TCK.
`REL-NORMATIVE-AUTHORITY` is now candidate-enforced: conditional execution,
timeout/retry flow, and the retry-attempt boundary complete the local-flow
requirement. Fallible public Rust boundaries, the stable native
runtime API, all stable C1 exact-differential suites, and their installed
artifact identities are now closed. Installed compiler TCK reports execute
the normative facade and bind the
`graphblocks-runtime` implementation version plus the exact platform wheel
record and SHA-256. The evidence runner also compares the installed package and
loaded native-module bytes with that wheel, while release assembly rechecks the
artifact identity against retained artifacts.

The bundled stable C1 runtime suite now follows the same artifact rule. Every
case executes the Rust stdlib scheduler/journal path without fallback, compares
status, outputs, terminal state, and normalized lifecycle order with the Python
oracle, and binds the runtime report to the exact `graphblocks-runtime` wheel.
This promotes only the named local scheduler/journal slice; production
suspension, multi-worker crash recovery, lease/fence, and durable effect
boundaries remain preview promotion work under
`REL-EXTENSION-RUNTIME-AUTHORITY`, which does not block the first C0/C1 release.
`REL-RUNTIME-CORRECTNESS` is likewise candidate-enforced. Installed fresh-process
evidence binds the selected
native wheel member to exact store/journal reopen results and stale-coordinator
patch/status rejection after an expired lease takeover.

The installed `application-events` report additionally runs each
Python-materialized attempted event through the selected Rust wheel and exact
compares the closed accepted/dropped update sequence, final accepted events,
and cutoff-response projection. Its suite claim is
`exact-native-reference`, bound to the selected `graphblocks-runtime` wheel:
the installed Rust boundary also interprets every raw shared-fixture operation
and exact-compares normalized metadata/payloads, per-operation
emission/admission traces, accepted events, and structured diagnostics.
Numeric `*UnixMs` fields are the cross-language time authority. The shared
adversarial corpus includes boolean numeric coercion and unknown-operation
cases, with exact code, message, and path comparison. The authority matrix
assigns this suite to `rust-application-events-exact-differential`, with Python
retained as the reference oracle. The installed `retry` report likewise runs
all seven shared retry/idempotency/cancellation fixtures through the Rust local
runtime and exact-compares status, terminal kind, attempts, retry identities,
node commits, terminal count, and post-terminal immutability with the Python
oracle. The cancellation cases cover pre-start, failed-attempt pre-retry,
successful-attempt pre-commit, and post-terminal boundaries. Its claim is bound
to `rust-retry-exact-differential` and the selected runtime wheel.
The installed `sequence` report now exact-compares bounded FIFO operation
results, buffer lengths, terminal state, and invalid-capacity creation through
`rust-sequence-exact-differential`. The installed `tool-execution` report runs
all 19 shared execution-plan fixtures through Rust and exact-compares creation
errors, per-operation results, and final call states through
`rust-tool-execution-exact-differential`. The installed `tool-lifecycle` report
executes all 18 shared argument, admission-order, approval, and idempotency
fixtures in Rust and exact-compares their closed result flags through
`rust-tool-lifecycle-exact-differential`. The installed `tool-result` report
executes all six shared preparation and stream-state fixtures in Rust and
exact-compares their closed result contracts through
`rust-tool-result-exact-differential`. The installed compiler report covers
nominal identity, requiredness, and nested-root validation; the installed
`typed-ports` report adds exact typed Graph construction, canonical compilation,
typed-boundary rejection, and local-runtime port preservation through
`rust-typed-ports-exact-differential`. The installed `outcome` report
exact-compares all eight closed outcome.v1 variants, readiness resolution, and
invalid/duplicate identity handling, plus eight local-terminal cases covering
six distinct terminal states, exact terminal payloads, and exactly-one terminal
enforcement. Two additional cases execute the native graph bridge to prove
that projected outputs commit before `run_succeeded` and that projection
failure emits one `run_failed` terminal with no partial outputs. This evidence
runs through `rust-outcome-exact-differential`, bound to the same runtime wheel.
All stable C1 suites now have exact native/reference coverage. The comparison
begins only after the shared canonical JSON envelope admission; invalid
envelopes fail closed before either evaluator. The outcome and cooperative
cancellation requirements are now Rust-authoritative; local flow remains the
only direct C1 requirement-authority work. Restart, lease, and fencing
correctness remains outside the cancellation promotion.

## Release gates

The first stable release is blocked until all of these statements are evidenced
from the exact release artifacts:

1. The stable API/signature, CLI JSON/exit-code, schema, canonical-byte/hash,
   and [diagnostic-code](../specification/reference/diagnostic-codes.yaml)
   snapshots are complete and enforced in CI.
2. Closed `graphblocks.ai/v1` Graph and PluginManifest schemas exist; all stable
   readers validate them; alpha-to-v1 migrations have positive and negative
   golden tests; and compilers reject unknown blocks by default.
3. C0 and C1 trace every normative requirement to the implementation selected
   by the final authority matrix and to schema, TCK, differential, and
   acceptance evidence.
4. The Rust normative-authority transition ADR, artifact/profile/language
   matrices, native-first Python binding, explicit reference oracle with no
   implicit fallback, protocol handshake, and phase-level differential evidence
   are complete.
5. Wheels and sdists are built once, installed into clean supported
   environments, and used for TCK execution. Reports bind implementation,
   implementation-artifact, schema, fixture, profile-catalog, and
   acceptance-manifest digests.
6. Supported Python/platform combinations pass install, type, and runtime
   tests, plus either upgrade testing or the closed first-stable upgrade
   exemption applicable only to `v1.0.0`. The exact matrix is published in the
   release notes.
7. All 99 baseline IDs map to an owning workstream; the source/evidence
   provenance and live issue inventory are digest-bound; all nine reproduced
   findings have executable regression harnesses; the inventory has zero open
   P0/P1; there are no unresolved critical/high stable-scope defects or
   unexplained flakes; the project owner approves the exact candidate after
   its route manifest, selector manifest, source digests, and all-pass JUnit
   result are validated automatically; adversarial request, response, schema,
   YAML-parser, and canonical resource-budget tests pass with the same
   candidate-bound evidence; and at least one release-candidate matrix run is
   clean on supported Python and pinned Rust.
8. Artifacts carry checksums, an SBOM, provenance, and signatures; publishing,
   rollback, and yank procedures have been rehearsed.
9. The configured project owner signs approval of the exact candidate and all
   included matrix attestation digests.
10. The required `macos-15` arm64 native-wheel smoke matrix passes on Python
   3.11 and 3.12. It builds and installs the exact base and native wheels in an
   isolated environment, executes the native compiler binding, validates the
   runner, architecture, ABI, module origin, and wheel digests, and retains the
   evidence record. This does not expand the supported-platform matrix without
   a separate release-tooling and evidence change.

The stable tag must not be cut by waiving a missing gate. A capability gate may
be removed only by changing the stable scope in this document and explaining
the user impact in the release notes. An audit finding leaves the release gate
only after verified closure or after the affected code is removed from every
1.0 release artifact.

### Supply-chain gate status

This section describes artifact and promotion integrity only. Passing it does
not satisfy the separate API, runtime-security, durability, or adapter axes in
the [generated status projection](status.md#readiness-by-independent-axis).

Installed-artifact CI covers Python 3.11 and 3.12 on Ubuntu and Windows. Each
combination uses the pinned Rust 1.94.0 toolchain to build its wheelhouse once,
installs it into a clean offline environment, runs the installed TCK and
acceptance gates, and retains digest-bound evidence. The platform builder runs
`rustc --version`, parses the reported version, fails if it is not 1.94.0, and
retains the exact observed output in platform evidence. Third-party dependency
wheels are kept in an install-only cache and never enter the first-party publish
set. The retained platform input contains only the three first-party wheels,
the three matching sdists, the exact TCK and acceptance reports, a platform
identity manifest, and its CycloneDX SBOM. Because `v1.0.0` has no previous
stable artifact, upgrade-from-previous-stable is explicitly not applicable for
this first release. Final promotion evidence must encode the closed
`first-stable-release` exemption; later stable release contracts must replace
that exemption with an installed upgrade result from the immediately previous
stable version.

The code-enforced candidate path aggregates the exact first-party artifacts from
all four supported platform jobs. Identically named universal wheels must be
byte-identical; platform-specific native wheels must match the recorded Python
and operating-system target. Missing platforms, unexpected distributions,
dependency wheels, and conflicting duplicate filenames fail closed. The one
self-contained bundle retains every platform's evidence and contains a canonical
`SHA256SUMS`, a reproducible aggregate CycloneDX 1.6 SBOM, an in-toto/SLSA
provenance statement, and a deterministic publish/rollback/yank rehearsal.
Assembly is available only from a clean checkout whose observed HEAD equals the
declared commit. The bundle records both that commit and its Git tree id.

The SBOM carries a dedicated component for every published wheel filename and
SHA-256 digest, in addition to its distribution/version identity. Platform
validation requires exactly one installed-version component and one exact
CycloneDX dependency row for each of `graphblocks`, `graphblocks-runtime`, and
`graphblocks-testing`; each row's edges must equal that package's declared
direct runtime dependencies. It also records the exact installed runtime
distribution closure and requires every member of that closure in the SBOM.
Aggregation preserves the complete dependency graph.
Provenance
binds the Git commit, the exact artifact union, all platform TCK/acceptance and
identity digests, the aggregate SBOM, the four build environments, and pinned
`pip==25.1.1`, `build==1.5.1`, `hatchling==1.31.0`, `maturin==1.14.1`, Rust,
`cyclonedx-bom==7.3.0`, and Cosign tool identities. Standalone verification
uses the immutable in-bundle `release-expectations.json` snapshot for the TCK
suite, ordered-case, fixture, implementation, and version expectations and the
acceptance manifest/scenario/gate expectations. That snapshot is bound to the
source commit and tree, listed in the signed manifest, and bound again by SLSA
provenance; verification never substitutes expectations from its live checkout.
Each platform identity also records the exact CPython patch version, platform
string, hosted-runner image identity, and complete resolved Python distribution
closure used by the build, so a transitive tool or runner-image change cannot
reuse the same provenance identity.
It uses one descriptor-backed snapshot per regular file and rejects symlinked,
missing, unexpected, or digest-mismatched manifest, signature, artifact,
evidence, expectation, and metadata files.

Release-candidate refs need no promotion record and continue to produce a
`candidate` manifest. Final `v1.0.0` assembly instead fails before creating a
bundle unless `--promotion-evidence` names an explicit regular, non-symlink
JSON file. The record must use canonical JSON and carry a self-verifying
`contentDigest`. Its `auditClosure` claim and signed `audit-closure` report bind
the immutable issue inventory, live status overlay, remediation map, and
checker by exact source digest. The validator re-runs the closure contract from
both the candidate and final Git objects, requires identical claims, verifies
every fix commit and regression path against the candidate history, and rejects
any open P0/P1 finding. The closed contract binds all of the following:

- the exact final ref and `1.0.0` version, with the enclosing release manifest
  separately binding the final Git commit and tree;
- a canonical prior `v1.0.0-rc.N` ref, its distinct ancestor commit, and that
  candidate's manifest digest;
- the explicit `v1.0.0` first-stable upgrade exemption, which is the only
  accepted substitute for an upgrade-from-previous-stable result;
- the exact Git name/status diff from that candidate to the final commit,
  including a lowercase SHA-256 digest and a sorted closed change list;
- at least one successful, complete attestation covering the
  exact supported operating-system/Python matrix and the same candidate;
- one successful signed run for every `real-adapter` evidence recipe, binding
  the candidate commit, concrete Actions run attempt, test and workflow,
  authentication, service or SDK version, retry/failure model, result digest,
  and uploaded report, with the runs collectively covering every declared
  authentication/version pair; this list is empty while no real adapter is
  claimed;
- a digest-bound audit inventory proving zero open P0/P1 findings across all
  code shipped in the 1.0 release artifacts, zero unresolved critical/high
  stable-scope defects, and zero unexplained flakes;
- a signed approval from the configured project owner that binds the exact
  candidate and every included matrix attestation digest.

Every candidate, matrix run, integration run, audit inventory, stable-scope
defect audit, and owner-signoff report must
be referenced by a canonical lowercase SHA-256 digest and an adjacent Sigstore
bundle. The record must give the exact safe relative paths, file hashes,
signature hashes, certificate identities, and issuer. Before 1.0 promotion,
the assembler must resolve regular non-symlink files, reject unreferenced or
substituted reports, verify every signature, and validate each report's content
against the corresponding promotion claim. Matrix attestations must use
the `graphblocks/graphblocks` CI workflow identity at the candidate tag; the
deterministic candidate-manifest report and later operator reports use the
dedicated promotion-report workflow identity at that same tag. A real-service
report must instead be signed by the exact integration workflow named by its
catalogued recipe at that candidate tag. Identities selected only by the
evidence are never accepted as signature authorities. Each matrix and
integration report binds the canonical GitHub Actions run/attempt URL that
produced it. The signed owner payload binds the configured project owner and
the included matrix digests. The assembler validates the complete record
against the clean, full-history final checkout. Only
release documentation other than `stable-release-matrix.yaml` and
`stable-security-gates.yaml`, the two Python package manifests, the public
version constant, and the two version-bearing testing compatibility snapshots
may differ from the candidate. Both release-authority YAML files are immutable
between RC and final. Non-documentation files must be exact
`1.0.0rc.N`-to-`1.0.0` replacements, apart from the optional packaging
classifier promotion to Production/Stable; implementation, schema, TCK, and
normative-specification changes require a new RC and owner signoff.
The immutable matrix therefore remains the candidate-time authority snapshot:
its maturity policy, candidate artifact train, and readiness fields describe
the promoted RC, while the final package metadata, project status, and security
policy documents may record the completed stable release.
This whole-matrix immutability also prevents the final release from adding or
widening an adapter, readiness, approval, or test-gate claim after signed runs
were collected.

The promotion record binds the exact final ref and version without embedding
the final commit or tree. This avoids an impossible self-reference when the
checked-in record is itself part of that final tree. The release manifest binds
the final commit and tree, copies the validated record to
`promotion-evidence.json`, lists its exact file record and content digest, and
binds the same record in provenance. Standalone verification repeats the
record's closed structural and semantic checks, re-resolves and verifies every
retained signed report, and recomputes the candidate ancestry and exact Git
source diff from a full-history checkout. It never disables the source-diff
check merely because the bundle was already assembled. Internal consistency
checks reject partial candidate, source-diff, report, or matrix-run
substitution, and the final Sigstore signature freezes the validated result.

The promotion record binds operational reports by digest and path rather than
embedding them in one JSON object. Assembly copies the canonical report files
and their Sigstore bundles into `promotion-reports/`, lists every file in the
manifest metadata closure, and retains them for independent verification.
Promotion evidence therefore must not contain secrets or sensitive report
contents that cannot be published with the release bundle.

Passing promotion evidence does not make an unsigned artifact stable. Its
manifest readiness is `promotion-authorized-signature-required`, and the only
remaining external gate is the pinned keyless signing identity. A successful
signature-aware public bundle verification is required for a final stable
claim; the assembler alone may invoke the explicit private structural check
before the final manifest signature exists. Public verification of an unsigned
final bundle fails closed, and a manifest that self-declares `stable` is
rejected. RC manifests remain
`candidate` with all promotion gates outstanding.

Only the canonical `graphblocks/graphblocks` CI workflow on `v1.0.0` or an
explicit `v1.0.0-rc.N` tag, where `N` is a canonical positive integer, may
enter the signing job. A prior job with no token permissions validates that
exact ref grammar and exports the admitted ref; values such as `rc.0`,
`rc.01`, and `rc.foo` fail before any signing job can start.

Bundle assembly, source checkout, PyPI installation, project execution, and
unsigned verification occur in a separate job with no OIDC permission. That
job uploads one exact frozen unsigned artifact. The dependent signing job has
only `id-token: write`: it does not check out source, install through `pip`, or
run project code. Every action in that trust boundary is pinned to a full
commit id, and the Cosign installer is additionally pinned to Cosign 3.0.6.
The job downloads the exact named unsigned artifact, keyless-signs the fixed
`release-manifest.json` path, and invokes Cosign directly to verify the fixed
in-bundle `release-manifest.sigstore.json` against the GitHub Actions issuer and
the canonical repository, workflow, and ref identity before uploading the
signed bundle.

The unsigned assembly job observes and parses `cosign version`, fails unless it
reports the pinned 3.0.6 release, uses that binary to verify every promotion
report signature, and records the exact output in the manifest and provenance.
The signing boundary installs that same release through the commit-pinned
installer. Public verification of a final bundle requires Cosign both for the
retained promotion reports and for the final in-bundle manifest signature; the
executing binary's observed identity must equal the recorded identity.

Release operators must create RC refs through
`.github/workflows/cut-release-candidate.yml`, dispatched from `main` with a
canonical positive candidate number and the exact lowercase 40-character
commit SHA to promote. The read-only admission job accepts only a successful
`main` push run of `.github/workflows/ci.yml` for that SHA whose `Required
gates` job also concluded successfully. Only the dependent tag-creation job
receives `contents: write`, and it creates a new `v1.0.0-rc.N` ref without
moving an existing tag. Direct local `git tag` and `git push --tags` operations
are outside the release process because they do not prove that the tagged SHA
passed the aggregate gate.

For an RC tag, the no-OIDC release-evidence job runs only after the Python,
installed-artifact, example, and Rust jobs succeed. It emits exactly one
canonical matrix attestation for that workflow attempt, binding the candidate
ref and commit, the deterministic candidate-manifest digest, the closed
four-combination matrix, and the canonical
`https://github.com/graphblocks/graphblocks/actions/runs/<run>/attempts/<attempt>`
identity. Before freezing, it downloads the canonical Ubuntu/Python 3.11
security-gate artifact and independently verifies its canonical result, exact
selector manifest, candidate/source digests, all-pass counts, and JUnit digest.
The artifact name is scoped to `github.run_attempt` and must match the attempt
component of the attested run URL. Every other intermediate CI and manual
promotion-report artifact is attempt-scoped as well, so a rerun cannot collide
with or silently reuse an earlier attempt. A separate `id-token: write`-only
job signs and directly verifies that fixed report under the CI workflow
identity. Re-running the complete candidate workflow can produce another
distinct attempt attestation; the manual report workflow cannot create or sign
matrix claims.

The deterministic candidate-manifest and post-candidate operational reports
are signed by manually dispatching
`.github/workflows/promotion-reports.yml` with the exact candidate tag as the
workflow ref, a closed report type, and the public JSON report content. A first
job with only `contents: read` checks out that candidate, admits only canonical
`v1.0.0-rc.N` refs, validates the report against its type and candidate binding,
canonicalizes it, and uploads one fixed `report.json`. The dependent signing job
has only `id-token: write`; it does not check out source, install packages, or
run project code. It downloads that exact frozen artifact, signs and directly
verifies the fixed paths with Cosign 3.0.6, and retains the report and adjacent
Sigstore bundle for assembly. The workflow must be present before the candidate
tag is cut so that the candidate manifest, defect audit, and owner signoff can
be signed without moving or rebuilding the candidate tag.

Branch pushes and pull requests exercise the validator tests and
four-combination installed-artifact matrix without receiving an OIDC signing
token; they do not claim that the external signature gate passed. RC manifests
therefore remain `candidate` and record the signature as
`external-gate-pending`.

CI uses the fixed repository path
`docs/project/releases/v1.0.0-promotion-evidence.json` only for the final tag.
No record exists there yet and this document does not fabricate one, so a final
tag currently fails closed. A release operator must place verifiable evidence
at that path only after a clean candidate matrix, the defect/flake audit, and
the configured project owner's approval, using retained CI outputs for the
matrix attestation and the candidate-tag promotion-report workflow outputs for
the remaining report/signature pairs. The deterministic in-bundle dry run
continues to validate the publish/rollback/yank/restore state machine without
performing registry mutations.

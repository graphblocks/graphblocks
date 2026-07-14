# First Stable Release Boundary

This document defines the intended compatibility boundary for the first stable
GraphBlocks release. It is a release target, not a statement that the current
source tree is already stable. A row marked **stable** is part of the 1.0
promise only after every release gate in this document passes. The canonical
machine-readable form of these classifications is
[`stable-release-matrix.yaml`](stable-release-matrix.yaml).

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

## Artifact matrix

### Python distributions

| Artifact | 1.0 tier | Stable scope or limitation |
| --- | --- | --- |
| `graphblocks` | Stable | Pure-Python C0/C1 SDK, canonicalization, closed-world validation and planning, local runtime, built-in C0/C1 blocks, and the corresponding `graphblocks validate`, `plan`, and local `run` CLI contracts. Modules belonging to C2-C4, X1-X3, external integrations, server/deployment operations, and catalog entries explicitly listed below remain preview even though they ship in this wheel. |
| `graphblocks-testing` | Stable | TCK discovery/execution, C0/C1 fixtures, deterministic report format, and the `graphblocks-tck` command. A TCK report is evidence for the named implementation/profile/digests, not a blanket claim for the whole repository. |
| `graphblocks-runtime` | Preview | Optional PyO3 native bindings. It remains preview until native execution, suspension behavior, supported-wheel coverage, and Python/native differential gates pass. The pure-Python implementation can claim C1 without implying a native C1 claim. |

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

### Rust and non-Python artifacts

| Artifact | 1.0 tier | Reason |
| --- | --- | --- |
| `graphblocks-schema`, `graphblocks-types`, `graphblocks-compiler`, `graphblocks-flow`, `graphblocks-runtime-core`, `graphblocks-runtime-seq`, `graphblocks-runtime-durable`, `graphblocks-protocol`, `graphblocks-telemetry`, and `graphblocks-python` crates | Internal | Implementation crates used by the native bindings, native CLI, and conformance work. Their Rust APIs are not yet a public SemVer surface. |
| `graphblocks-native` executable (`graphblocks-cli-native` crate) | Preview | Python-free validate/plan/run is useful, but native block coverage, adapter injection, diagnostics, and differential evidence are not yet at the stable gate. |
| `graphblocksd` crate and executable | Internal | It is a one-shot worker/checkpoint control-plane tool, not the production daemon its name may imply. |
| `graphblocks` Rust crate | Reserved | Name-reservation crate with no supported implementation. |
| `graphblocks` npm package | Reserved | Name-reservation package with no JavaScript/TypeScript API. |
| `graphblocks-operator` Helm/OCI artifact | Internal | Templates exist, but there is no supported reconciliation controller or deployment lifecycle yet. |

Promotion of a Rust crate, native executable, npm API, or operator changes the
release matrix and requires its own public-surface snapshot, packaging gates,
and compatibility evidence. It is not implied by Python 1.0.

## Conformance-profile matrix

Profile stability is implementation-specific. The table describes the first
stable pure-Python claim; other implementations must publish their own reports.

| Profile | 1.0 tier | Promotion condition |
| --- | --- | --- |
| `GB-C0-SCHEMA` | Stable | Closed schemas and readers for every claimed stable resource, `graphblocks.ai/v1` Graph output, alpha-to-v1 migrations, closed-world compilation by default, deterministic registered diagnostics, and current C0 TCK evidence. |
| `GB-C1-LOCAL-RUNTIME` | Stable | All C0 gates plus local scheduling, typed ports, outcomes, cancellation, journal, bounded flow, tool lifecycle, and restart-independent local correctness evidence. This first claim applies to the pure-Python runtime only. |
| `GB-C2-AI-APPLICATION` | Preview | Documents, retrieval/RAG, conversation, and application protocol remain on the roadmap until their wire/API and acceptance gates are frozen. |
| `GB-C3-GOVERNED-RUNTIME` | Preview | Policy, usage, budget, permit, approval, review, and workspace contracts require their own stable API and durability/security gates. |
| `GB-C4-PRODUCTION` | Preview | Requires restart-durable accepted runs, authenticated and idempotent resume, worker crash recovery and fencing, immutable-release evidence, and production adapter verification. |
| `GB-X1-ORCHESTRATION` | Preview (provisional) | Retains the catalog's provisional qualifier until bounded orchestration and delegated-budget parity gates pass. |
| `GB-X2-VOICE` | Preview (experimental) | Retains the catalog's experimental qualifier until transport/provider support and interruption/playback authority gates pass. |
| `GB-X3-DURABLE-STREAM` | Preview (experimental) | Retains the catalog's experimental qualifier until restart, replay, barrier, watermark, and sink-commit gates pass. |

Passing C0 or C1 does not make a preview profile stable. Conversely, keeping a
profile preview does not remove it from the specification or future roadmap.
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

| Tier | Components |
| --- | --- |
| Preview | `graphblocks-pdf`, `graphblocks-qdrant`, `graphblocks-mcp`, `graphblocks-openapi`, `graphblocks-openai`, `graphblocks-haystack`, `graphblocks-policy-opa`, `graphblocks-policy-cedar`, `graphblocks-budget-postgres`, `graphblocks-usage-postgres`, `graphblocks-kubernetes`, `graphblocks-terraform`, `graphblocks-oci`, `graphblocks-gitops`, `graphblocks-otel`, `graphblocks-langfuse`, `graphblocks-prometheus`, `graphblocks-dashboards`, `graphblocks-webrtc`, `graphblocks-websocket-media`, `graphblocks-openai-realtime`, `graphblocks-silero-vad`, `graphblocks-kafka`, `graphblocks-nats`, `graphblocks-sqs`, and `graphblocks-pubsub`. |
| Internal | `graphblocks-scripted`, repository fakes, acceptance harness adapters, and any adapter that has contract/unit evidence only and is not named in an installed-artifact integration matrix. |

Preview means that the adapter contract may be exercised and documented. It
does not mean that a real external service, SDK version range, authentication
mode, retry policy, or failure model is supported. Each integration is promoted
separately after real-service tests and an explicit dependency/platform matrix.

## Release gates

The first stable release is blocked until all of these statements are evidenced
from the exact release artifacts:

1. The stable API/signature, CLI JSON/exit-code, schema, canonical-byte/hash,
   and [diagnostic-code](../specification/reference/diagnostic-codes.yaml)
   snapshots are complete and enforced in CI.
2. Closed `graphblocks.ai/v1` Graph and PluginManifest schemas exist; all stable
   readers validate them; alpha-to-v1 migrations have positive and negative
   golden tests; and compilers reject unknown blocks by default.
3. C0 and pure-Python C1 trace every normative requirement to a schema or
   implementation check and to TCK/acceptance evidence.
4. Wheels and sdists are built once, installed into clean supported
   environments, and used for TCK execution. Reports bind implementation,
   schema, fixture, profile-catalog, and acceptance-manifest digests.
5. Supported Python/platform combinations pass install, upgrade, type, and
   runtime tests. The exact matrix is published in the release notes.
6. There are no unresolved critical or high-severity correctness or security
   defects in the stable scope, no unexplained flakes, and three consecutive
   clean release-candidate matrix runs.
7. Artifacts carry checksums, an SBOM, provenance, and signatures; publishing,
   rollback, and yank procedures have been rehearsed.
8. The unchanged release candidate completes a two-to-four-week soak in at
   least two non-trivial applications and passes independent API/security
   review.

The stable tag must not be cut by waiving a missing gate. A gate may be removed
only by changing the stable scope in this document and explaining the user
impact in the release notes.

### Supply-chain gate status

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
validation requires all direct runtime dependencies and their CycloneDX
relationships, records the exact installed runtime distribution closure, and
requires every member of that closure in the SBOM. Aggregation preserves the
complete dependency graph.
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
`contentDigest`. Its closed contract binds all of the following:

- the exact final ref and `1.0.0` version, with the enclosing release manifest
  separately binding the final Git commit and tree;
- a canonical prior `v1.0.0-rc.N` ref, its distinct ancestor commit, and that
  candidate's manifest digest;
- the explicit `v1.0.0` first-stable upgrade exemption, which is the only
  accepted substitute for an upgrade-from-previous-stable result;
- the exact Git name/status diff from that candidate to the final commit,
  including a lowercase SHA-256 digest and a sorted closed change list;
- at least three distinct, successful, complete attestations covering the
  exact supported operating-system/Python matrix and the same candidate;
- a soak of at least 14 days in at least two distinct applications explicitly
  attested as non-trivial;
- approved API and security reports from distinct reviewer identities;
- zero unresolved critical/high stable-scope defects and zero unexplained
  flakes;
- an attestation that the exact final ref is protected; and
- an authorized real staging rehearsal in which publish, rollback, yank, and
  restore each succeeded.

Every candidate, run, application, review, ref-protection, and rehearsal report
is referenced by a canonical lowercase SHA-256 digest. The assembler validates
the complete record against the clean, full-history final checkout. Only
release documentation, the two Python package manifests, the public version
constant, and the two version-bearing testing compatibility snapshots may
differ from the candidate. Non-documentation files must be exact
`1.0.0rc.N`-to-`1.0.0` replacements, apart from the optional packaging
classifier promotion to Production/Stable; implementation, schema, TCK, and
normative-specification changes require a new RC and soak.

The promotion record binds the exact final ref and version without embedding
the final commit or tree. This avoids an impossible self-reference when the
checked-in record is itself part of that final tree. The release manifest binds
the final commit and tree, copies the validated record to
`promotion-evidence.json`, lists its exact file record and content digest, and
binds the same record in provenance. Standalone verification repeats the
record's closed structural and semantic checks and includes it in the exact
file closure; clean-checkout assembly is the step that recomputes the Git
ancestry and source diff. Internal consistency checks reject partial candidate,
source-diff, or matrix-run substitution, and the final Sigstore signature
freezes the validated result.

The compact promotion record binds operational reports by digest rather than
embedding them. The validator can prove that the record is complete,
internally consistent, and signed with the release, but it cannot independently
prove the truth of an external review or staging service. The authorized
release operator must verify those reports before admitting the record; their
distinct digests make that decision auditable without copying sensitive report
contents into the public bundle.

Passing promotion evidence does not make an unsigned artifact stable. Its
manifest readiness is `promotion-authorized-signature-required`, and the only
remaining external gate is the pinned keyless signing identity. A successful
signature-aware bundle verification is required for a final stable claim; a
manifest that self-declares `stable` is rejected. RC manifests remain
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
reports the pinned 3.0.6 release, and records the exact output in the manifest
and provenance. The signing boundary installs that same release through the
commit-pinned installer. Cosign is not required for unsigned standalone bundle
inspection; project-level signature verification still requires the executing
binary's observed identity to equal the signed identity.

Branch pushes and pull requests exercise the validator tests and
four-combination installed-artifact matrix without receiving an OIDC signing
token; they do not claim that the external signature gate passed. RC manifests
therefore remain `candidate` and record the signature as
`external-gate-pending`.

CI uses the fixed repository path
`docs/project/releases/v1.0.0-promotion-evidence.json` only for the final tag.
No record exists there yet and this document does not fabricate one, so a final
tag currently fails closed. A release operator must place independently
verifiable evidence at that path only after the real soak, reviews, protected
ref observation, clean matrices, defect/flake audit, and authorized staged
publish/rollback/yank/restore rehearsal have occurred. The deterministic
in-bundle dry run remains useful candidate evidence, but it cannot satisfy or
replace that real staged-rehearsal attestation.

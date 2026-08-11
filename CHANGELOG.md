# Changelog

Notable project changes are documented here. GraphBlocks follows semantic
versioning for the explicitly listed stable surfaces; the current series is
preparing the first stable 1.0 release.

## 1.0.0rc9 - Unreleased

### Added

- Candidate-stable C0 schema/compiler and C1 local-runtime boundaries, closed
  `graphblocks.ai/v1` Graph and PluginManifest schemas, compatibility snapshots,
  numeric diagnostics, bundled stable TCK fixtures, and release evidence gates.
- Deterministic local YAML composition with typed `GraphFragment` slots,
  imported bindings, materialized output, and bounded filesystem access.
- Living English specification organized by contract domain.
- Open-source contribution, governance, conduct, and security policies.
- Executable acceptance coverage for ten applications and 42 gates.

### Changed

- Made preview durable accepted-run services reject custom process worker
  targets by default. Test and local-development users must now opt in with
  `allow_unsafe_custom_worker_dev=True`, which is ineligible for durable,
  production, compatibility, or security claims.
- Pinned the package-owned durable parent and child to a closed intent-only
  block, implementation, and handler-construction inventory. New preview
  handlers and same-ID aggregate handler substitutions are not consumed,
  implementation-ID rebindings fail closed, and nested `control.map@2`
  resolution cannot escape the restricted registry. Explicit callers should
  migrate from the full preview registry to `durable_intent_registry()`.
- Made the SQLite effect outbox own the authoritative transaction clock for
  claim eligibility, lease expiry, acknowledgements, and retry release. Effect
  leases are policy-bounded, future claim and observation timestamps fail
  closed, live state transitions are rechecked before commit, and matching
  committed replays remain recoverable after lease expiry. The v6 storage
  migration requeues every legacy caller-timed active effect claim while
  advancing its generation and fence, and fails closed unless the attempt,
  generation, and fence counters retain enough headroom for a new claim.
  Operators should quiesce v5 dispatchers before migration; otherwise the
  documented at-least-once receiver-deduplication requirement applies.
- Strengthened the preview SQLite effect-outbox replay boundary. New claims now
  persist repository-issued start and expiry times, while a versioned canonical
  command envelope and digest bind the complete claim plus every
  acknowledgement or retry timestamp. Backdated new transitions and altered
  post-commit replays fail closed; until a later claim or terminal transition
  supersedes its replay slot, an identical committed command remains recoverable
  after response loss and lease expiry. The v7 migration requeues active v6
  claims whose start cannot be reconstructed and treats pre-v7 settled command
  identities as unverifiable instead of guessing them. Before upgrade,
  operators must quiesce v6 dispatchers, back up the database, prevent
  mixed-version writers, and reconcile ambiguous provider sends as specified in
  the runtime upgrade contract. This preserves at-least-once semantics and does
  not resolve an ambiguous provider send or establish an exactly-once effect
  claim.
- Added separate closed provider-effect intent, live run-authority, durable
  origin-transfer, deployment capability, fenced send-attempt,
  reconciliation-evidence, and fail-closed state-machine contracts. Send entry
  now requires an opaque admission with no supported authority-rehydration
  format from a deployment-owned capability verifier and structurally distinct
  repository-owned transferred-origin and claim authorities. The transfer
  content-binds the exact immutable intent and a self-consistent live-run
  snapshot. The admission fixes the next claim identity and must be atomically
  consumed once into a repository-timed attempt plus a serializable,
  send-authority-free receipt that revalidates the full claim interval.
  Restored intent, capability, transfer, attempt, receipt, and evidence records
  are exact-decoded again at their authority boundary. Admission and evidence
  verification resolve the exact capability-bound verifier through a
  deployment-owned registry, so a caller cannot substitute a verifier that
  merely copies the admitted identity.
  Response ambiguity remains quarantined until canonical provider evidence is
  authenticated and the repository confirms the exact attempt is still active.
  Prior-attempt evidence cannot settle a retry, and only an unchanged intent can
  retry after confirmed non-commit or cancellation. This contract-only preview
  adds no provider I/O and establishes no exactly-once claim without real
  adapter and repository evidence.
- Added the first provider-specific durable storage slice in accepted-run
  SQLite schema v8. `SQLiteProviderEffectRepository` atomically rechecks the
  tenant-scoped live run owner, state version, checkpoint, lease generation,
  fence, and repository-time expiry before storing exact canonical intent,
  capability, origin-transfer, and initial journal records. Exact committed
  replays survive source-lease expiry and response loss; divergent effect or
  idempotency reuse, stale authority, cross-tenant or cross-owner lookup, and
  corrupted stored wire identities fail closed. The migration creates empty
  `provider_effects` and `provider_effect_events` tables and never reinterprets
  generic operation outbox rows. Pre-send claim persistence is added below;
  send-attempt consumption, receipt, evidence, adapter I/O, and reconciliation
  persistence remain subsequent preview work.
- Added durable provider-effect pre-send claiming in accepted-run SQLite schema
  v9. Claim selection is tenant-and-owner scoped, uses repository time and a
  bounded half-open lease, advances generation and fencing tokens exactly, and
  reclaims only expired `claimed` work while never auto-selecting
  `send_started`. Active same-owner claims and committed pre-send releases replay
  exactly after response loss. A release is serialized against reclaim, safely
  removes authority even after expiry, and records a closed pre-send release
  record. Canonical claim and release records are embedded in the authoritative
  event journal, active send-attempt identifiers are unique among active claims,
  stale authority fails closed, and every projection transition is committed
  atomically with its event. Indexed point and range lookups keep
  projection-tail validation bounded, while paged journal reads validate local
  sequence, state, generation, fence, reclaim-expiry, and release binding.
  Durable claim consumption into `send_started`, core admission verification,
  provider I/O, receipts, evidence, and reconciliation remain subsequent
  preview work.
- Added one-shot provider-effect send-entry consumption in accepted-run SQLite
  schema v10. A structurally distinct claim-authority facade exact-verifies the
  current scoped claim, then atomically installs a repository-timed closed send
  attempt and admission receipt, advances the projection to `send_started`, and
  appends their consumed-claim-bound event. An append-only claim-issuance
  registry prevents attempt identifiers from being reused after release,
  reclaim, or consumption. The opaque admission is never persisted, consumed
  admissions cannot be replayed as fresh send authority, and scoped restart
  recovery exposes only the exact persisted attempt and receipt. The claim
  authority can also exact-verify that those records remain the repository's
  active send for core evidence authentication, without persisting evidence or
  changing state. Failpoints, half-open expiry, concurrent release/consume,
  immutable identity reuse, canonical corruption, and v9 migration rollback are
  covered. This local authority boundary performs no provider I/O and does not
  establish exactly-once delivery, outcome evidence, reconciliation, or durable
  retry.
- Added atomic provider-effect evidence settlement in accepted-run SQLite
  schema v11. Exact canonical reconciliation evidence is append-only and bound
  to the current persisted attempt and admission receipt. Settlement rechecks
  the active projection under the SQLite write lock, advances state and journal
  sequence together, clears active send authority for confirmed terminal
  outcomes, and retains it for quarantined unknown outcomes. Projection-tail
  reads cross-check the evidence row, event, attempt, receipt, state, and
  installed versions; interrupted writes roll back, while an exact settlement
  call can recover a committed result after response loss. This boundary does
  not invoke or authenticate provider I/O itself, schedule reconciliation, or
  establish exactly-once delivery or durable retry.
- Added idempotent provider-effect reconciliation controls in accepted-run
  SQLite schema v12. Tenant-, run-, owner-, and effect-scoped control records
  bind a caller control identity, exact expected state version, and one closed
  quarantine/reconciliation/manual-review transition. Each control is committed
  with the projection CAS and authoritative event, retains the active attempt
  and receipt, replays exactly after response loss while still current, and
  rejects stale versions or changed control identities. This provides durable
  operator state control but does not schedule workers, authenticate operator
  policy, call providers, or implement retry.
- Added confirmed-safe same-intent retry in accepted-run SQLite schema v13.
  A tenant- and owner-scoped retry command binds its idempotency identity,
  immutable intent digest, exact expected state version, and previous settled
  attempt. Only confirmed non-commit or confirmed cancellation can return to
  `pending`; the command, projection CAS, and authoritative event commit
  together and replay exactly after response loss while still current. The next
  claim must retain the prior attempt digest while advancing generation, fence,
  and attempt identity. Interrupted retries roll back without exposing pending
  work. This boundary does not retry a provider call by itself.
- Added a bounded CommonMark and GitHub-heading integrity checker, a closed
  generated-documentation registry with content-bound results, and an always-run
  fast documentation workflow plus named stable release gate.
- Split release readiness into generated supply-chain, API, runtime-security,
  durability, and adapter axes, and made stable promotion bind actor-identified
  object-authorization review plus manifest-selected, JUnit-bound adversarial
  resource evidence while keeping release-authority manifests immutable and
  rerun artifacts attempt-scoped.
- Replaced the normal-looking APIs of the reserved Rust and npm `graphblocks`
  artifacts with explicit reserved-name metadata, a Rust build warning and
  notice-only surface, and an npm import error backed by package smoke gates.
- Renamed the unsupported `graphblocks-operator` Helm/OCI claim to the internal
  `graphblocks-deployment-chart` scaffold, disabled all resources by default,
  removed the nonexistent default controller image and arguments, and added a
  blocked evidence gate before any future operator or reconciliation claim.
- Renamed the one-shot Rust control-plane executable from daemon-shaped
  `graphblocksd` to `graphblocks-control`, and bound its manifest, usage output,
  release metadata, and documentation to an argv-driven command lifecycle with
  command-specific JSON stdin payloads, structured JSON stdout/stderr, and no
  server listener, `serve` command, or supervisor claim.
- Split the machine-readable package and profile release model into a C0/C1
  core train plus independently owned AI, governance, production,
  orchestration, voice, durable-stream, and integration tracks, with exact
  ancestor, authority-role, artifact, tier, component-dependency closure, and
  promotion-gate checks.
- Classified every shipped integration as contract-only or test-double and
  added a machine-enforced promotion gate for authentication, supported
  versions, their complete Cartesian support matrix, retry/failure ownership,
  and revision/run-bound real-service evidence signed by the referenced
  integration workflow.
- Replaced the stable local journal's frozen-but-mutating dataclass contract
  with an explicitly mutable, thread-safe writer and immutable point-in-time
  snapshots while preserving its `(run_id)` constructor and C1 event surface.
- Made block catalogs closed by default; validated recursive descriptor type
  expressions and exact booleans; enforced nominal graph-interface and block
  port types and optional-to-required boundaries; enforced Python runtime output
  contracts and Rust stdlib manifest parity; and added mypy and Rust
  compile-fail evidence against mismatched or forged typed ports.
- Made graph execution honor boolean `when` dependencies without dispatching
  false branches or waiting for their unused inputs, propagated auditable native
  skip outcomes, terminalized projection/checkpoint/resume failures, and
  rejected cycles, malformed endpoints, and reversed pseudo-node directions at
  compile time in Python and Rust, while preserving the tightly bounded
  checkpointed realtime voice tool-feedback profile.
- Aligned native stdlib execution with the advertised portable block contracts:
  typed retrieval, context, and grounding aliases now execute, structured
  requests drive ranking, metric constraints fail closed, review digests are
  emitted, and all result-bundle evidence participates in its canonical digest.
- Made resolved-tool expiration exclusive at admission and at every Python
  adapter invocation, completed PEP 503 handling for all artifact references,
  disambiguated exact component requests, and prevented package/composition
  source swaps from escaping their roots or blocking on raced special files.
- Consolidated the Python release surface into `graphblocks`,
  `graphblocks-runtime`, and `graphblocks-testing`; built-in and integration
  catalog components now map to those artifacts instead of requiring separate
  feature wheels.
- Replaced the mutable architecture bundle with explicit documentation
  authorities, implementation status, and roadmap documents.
- Made shipped catalogs under `src/graphblocks/data/` the canonical catalogs.
- Hardened release and conformance gates: release bundles reject non-finite
  numbers, native TCK fallback cannot satisfy a native claim, and TCK reports
  bind suite, implementation version, and fixture digest evidence.
- Made approval expiration consistently exclusive, rejected malformed or
  non-positive node deadlines before execution, and exposed elapsed deadlines
  through the cooperative block cancellation token.
- Made Rust policy evaluation return typed validation errors for malformed
  public requests instead of panicking at mandatory enforcement points.
- Canonicalized Python artifact identities according to PEP 503 across catalog,
  lock, wheel-matrix, and installed-wheel verification, and derived wheelhouse
  build targets from the canonical catalog.
- Made manifest-root validation portable to platforms without descriptor-
  relative `open`, added Windows Python coverage, and made CI test every Rust
  crate from its packaged archive with byte-verified crate-local TCK fixtures.
- Aligned first-party Python dependency constraints with the `0.1` release
  train, added an offline wheelhouse install gate, and made Rust workspace
  crates packageable with versioned path dependencies and bundled schema TCK
  fixtures.
- Made Helm service-account identity consistent across the operator Deployment,
  ServiceAccount, and RBAC binding, and promoted formatting, strict all-target
  lint/tests, and package verification to CI release gates.
- Bound budget-permit spending to its source budgets, enforced permit expiry on
  every settlement path, rejected unsafe idempotency keys, and made SQLite
  callback claims and async-operation mutations transactional across workers.
- Pinned webhook connections to policy-validated DNS results, closed frozen
  mapping mutation escapes, and aligned Python/Rust canonical number bytes for
  large integers and floating-point exponents.
- Kept bearer credentials on their original HTTP origin, normalized urllib
  errors, and closed Python client responses on every result path.
- Brought the PyO3 application-protocol bridge up to runtime-core event and
  metadata parity, rejected stale provider interruption decisions, and made
  callback resumption fail closed until all resume gates pass.
- Cross-checked persisted checkpoint payload identity against indexed SQLite
  fields, enforced event-time-only window inputs and watermarks, honored full
  PEP 440 Python constraints, and made bundled schemas available to the
  installed CLI.
- Added fallible SQLite async-operation reads and made the daemon preserve
  storage and decoding failures instead of misreporting corrupt state as a
  missing operation.
- Aligned Python event-time windows with the durable contract by rejecting
  missing event timestamps, ignoring processing-time watermarks, and preserving
  monotonic event-time watermarks.
- Canonicalized blocked Python dependency names according to PEP 503 so dotted,
  underscored, repeated-separator, and mixed-case spellings cannot bypass
  vulnerability policy.
- Confined catalog artifact manifests to the declared package root, rejecting
  absolute paths, traversal that escapes the root, unsafe symlink swaps,
  malformed paths, and duplicate canonical manifest aliases during
  wheel-matrix or doctor validation.
- Fenced SQLite run mutations against concurrent terminal transitions so stale
  state, tool-evidence, or status writers cannot erase an authoritative run
  outcome.
- Rejected impossible calendar dates in Rust RAG freshness metadata, matching
  Python ISO-datetime validation while preserving valid Gregorian leap days.
- Bound policy snapshots to the bundle set declared by their profile, rejecting
  missing, ambiguous, or duplicate references and excluding unrelated bundles
  from effective-policy identity.
- Made zero-length byte-range reads consistent across local and S3-compatible
  blob stores without emitting an invalid HTTP Range request.
- Rejected malformed `project.requires-python` constraints during wheel-matrix
  construction instead of emitting empty, falsely unsupported build targets.
- Rejected malformed fractional seconds in Rust RAG freshness timestamps while
  retaining valid fractional ISO datetimes.
- Reported malformed requested Python matrix versions at their indexed input
  path instead of mislabeling package metadata as unsupported.
- Made the offline wheelhouse release gate compare the complete installed schema
  manifest with the checked-in schemas, rejecting omissions and malformed output.
- Implemented observable accumulating event-time windows in Python and Rust with
  on-time/final pane revisions, deadline-bound lateness, and shared TCK coverage.
- Rejected reviewer credentials before their issuance time in Python and Rust,
  making the authorization interval issuance-inclusive and expiry-exclusive.
- Confined local blob sidecar metadata beneath the configured storage root and
  rejected symlink escapes before writing blob content.
- Made local blob reads reject malformed sidecars and content that no longer
  matches its recorded checksum or size.
- Aligned native local blob pagination with canonical decimal cursors and made
  maximum-value cursors overflow-safe.
- Preserved the authorized workspace commit identity through compare-and-swap
  materialization instead of replacing it with a generated snapshot identity.
- Enforced valid positive lease intervals in Python and Rust and rejected lease
  authority before acquisition as well as at or after expiry.
- Retained governed-trial lease evidence in workspace commit requests and
  revalidated required lease kinds at the immediate commit time.
- Bound native workspace commits to matching head, base, candidate, and gate
  identities instead of accepting digest-only cross-workspace substitutions.
- Retained required gate-check and review-scope obligations in workspace commit
  requests and rejected stripped governance evidence at commit time.
- Made native workspace trials require an explicit mutation decision and a gate
  that binds every required check before issuing a commit request.
- Made the native workspace head require an explicit mutation decision and gate
  for every commit instead of treating absent proof as allowed.

### Removed

- Duplicated monolithic specification, mutable checksum manifest, historical
  review reports, and the bundled binary archive.

## 0.1.0 - Development baseline

- Initial Python and Rust contract implementations, schema set, TCK fixtures,
  package manifests, and acceptance applications.

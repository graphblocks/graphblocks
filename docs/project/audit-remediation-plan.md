# Deep Audit Remediation Plan

This plan turns the GraphBlocks deep-audit findings into release-blocking work.
It supplements the machine-readable
[stable release matrix](stable-release-matrix.yaml) and the
[first stable release boundary](first-stable-release.md); it does not redefine a
preview surface as stable or close an issue merely because the issue appears
here.

## Baseline and tracking rules

The audit baseline contains 99 findings:

| Severity | Findings | Planning rule |
| --- | ---: | --- |
| P0 | 4 | Security freeze; close before other feature work resumes. |
| P1 | 23 | Close before a 1.0 or production-readiness claim. |
| P2 | 64 | Triage into the stabilization milestones after the P0/P1 design boundaries are fixed. |
| P3 | 8 | Schedule with the documentation, naming, and developer-experience work that owns the affected surface. |

Nine findings were reproduced dynamically and 68 were confirmed directly from
code. The original artifacts are now digest-bound in the machine-readable
[remediation map](audit-remediation-map.yaml):

| Artifact | SHA-256 |
| --- | --- |
| Korean report | `5ad9b3dcb77b387e1a5d6b41bdb65b9c609c18526d0f1ec712dcb102bfba9f2c` |
| Issue inventory | `9f98ebde8dc981b0eaee8ed795e04306ac67f707223cfe844e2365561db7eb44` |
| Evidence bundle | `75cf142766d1e8cbf8c89c1157727d8a4ca0e945b15e9157c8977f1c6a02fe06` |

The unchanged [issue inventory](audit-issues.json) remains the issue-level
authority for severity, impact, recommendation, and original evidence status.
Its digest-bound [live status overlay](audit-issue-status.yaml) binds resolved
findings to ancestor fix commits and executable regression paths. The
`tools/check_audit_inventory.py` gate verifies the original digest, all 99
identities and severity counts, complete remediation-map coverage, evidence
paths, commit ancestry, and zero open P0/P1 findings. P2 and P3 remain open by
default until individually added to the overlay.

The evidence bundle intentionally omits the audited source archive and contains
no Git metadata. Its source commit, tree, and archive digest therefore remain
unknown and must not be inferred from the audit date. Obtaining one of those
source identities and creating a file-level evidence manifest with commands,
tool versions, environment, and digests is a release-evidence blocker.

A finding moves to resolved only when:

1. its fix is merged;
2. the original reproduction is a regression test or an equally strong
   executable check;
3. the relevant cross-runtime, adversarial, restart, or concurrency matrix
   passes; and
4. the release matrix or profile evidence is updated when the finding changes a
   compatibility or production claim.

The current generated count is 79 resolved findings: all 27 P0/P1 findings,
the first 11 P2 findings, GB-ARCH-013, and GB-ARCH-015 through GB-ARCH-017.
GB-COR-005 through GB-COR-009 are also resolved. There are zero open P0/P1,
12 open P2, and 8 open P3; GB-COR-010 through GB-COR-014, GB-DOC-006, GB-INP-006,
GB-INP-007, GB-INP-010, GB-PERF-004, and GB-PERF-005 are resolved. GB-ARCH-012
remains open pending shared-primitives consolidation. GB-PERF-006 is resolved
with tenant/owner-scoped indexed run-list cursor pagination and a 10,000-run
bounded-projection regression.
GB-QA-001 is resolved by expanding strict mypy coverage from 2 to 14
production modules and enforcing non-regression budgets for both strict module
count and the 145 remaining type-ignore comments.
GB-QA-002 is resolved by requiring explicit mypy error codes on every
type-ignore comment, enforcing zero uncoded ignores, and capping total
production ignore debt at 145.
GB-QA-005 is resolved with a pinned, required Ruff job that lints the entire
repository and enforces an initial seven-module production format baseline;
intentional example bootstrap imports have a narrow E402 exception.
GB-QA-006 is resolved by required branch-coverage floors for canonical (80%),
server (80%), and policy (65%), plus 90% changed-line branch coverage across
those security-critical modules with XML and JUnit evidence retained by CI.
GB-QA-007 is resolved by required, pinned pip-audit and cargo-audit dependency
scans plus CodeQL security-extended analysis. Any dependency exception is a
closed, machine-readable entry with package, reason, HTTPS evidence, and an
enforced expiry date; audit outputs and the active exception set are retained.
GB-QA-009 is resolved by a required Loom 0.7.2 checkpoint-recovery model that
exhaustively explores initial claim races, renew-versus-takeover, and
complete-versus-takeover interleavings while asserting monotonic fencing and
stale-owner rejection. Its dedicated CI log is retained with Rust diagnostics.
GB-QA-010 is resolved by a required `macos-15` arm64 matrix for Python 3.11 and
3.12 that builds both distributions, installs the exact wheels into an isolated
environment, executes the native compiler binding, verifies the runner,
architecture, Python ABI, module origin, and wheel digests, and retains the
closed evidence record. This smoke gate does not expand the supported platform
matrix.
GB-QA-012 is resolved by an always-run push/PR quick-feedback job that checks
repository lint and the progressive format baseline, static stable API
compatibility, strict typing ownership, the live audit inventory, and a
bounded native-free core smoke. It retains logs and JUnit evidence, has a
five-minute hard timeout, completed its first green CI execution in 28 seconds,
and does not use path filters; the full platform, native, Rust, security, and
artifact matrix continues in parallel and remains required.
GB-QA-013 is resolved by a pip-tools 7.6.0 development constraints lock with
28 exact direct and transitive pins. The checker rejects URL, extra,
non-exact, duplicate, and OS/architecture-specific entries, covers the base and
test dependency declarations, uses checkout-relative generation inputs, emits
a useful drift diff, and regenerates byte-for-byte in the quick gate. Every
Ubuntu/Windows and Python 3.11/3.12 test installation consumes the same lock;
all four clean-runner constraint installation steps have passed.
GB-QA-014 is resolved by a machine-readable artifact/protocol compatibility
matrix that separates package SemVer from schema, native-binding, worker,
application, and durable-checkpoint contract versions. It binds every artifact
train to its source manifest, declares supported combinations, points to
fail-closed mismatch regressions, and corrects the package catalog so its
constraints accept the versions it actually publishes. The matrix is enforced
by the always-run quick feedback gate, whose first matrix-aware execution passed
in 32 seconds.
GB-QA-015 is resolved by retaining the strict stable-symbol gate while adding
strict mypy debt budgets for every shipped Python package. The gate caps debt
module, diagnostic, type-ignore, uncoded-ignore, and preview root-alias counts;
it emits a deterministic per-module strict/debt JSON report that both quick and
full Python CI retain. The first clean quick execution reproduced the committed
core/runtime/testing diagnostic ceilings and completed in 47 seconds.
GB-QA-016 is resolved by a route-manifest-generated authorization harness. It
executes the complete two-principal by two-tenant identity product across every
protected HTTP, SSE, and WebSocket endpoint; object-scoped routes run in both
accepted and running states, non-owner combinations require a non-disclosing
404, and run listing proves exact owner-and-tenant isolation. The meta-test
requires every path-scoped protected route to declare a supported resource
resolver, while principal-scoped routes are included automatically. The same
three tests are bound to the stable security-gate manifest and always-run quick
smoke. The first clean matrix-aware quick execution ran 91 tests and completed
in 52 seconds (CI run 31241231744, job 93062485078).
GB-QA-017 is resolved for the durable SQLite accepted-run slice by a required
POSIX process-crash gate. One recovery subprocess forks and kills isolated
workers with `SIGKILL` at all 15 declared admission, claim, checkpoint, outbox,
event, state-update, and post-commit failpoints. A reopened repository proves
pre-commit rollback, post-commit replay, and exactly one checkpoint/outbox
record. A separate two-worker process race proves one lease winner, monotonic
generation/fencing on expiry, and rejection of the stale winner. Ubuntu Python
3.11 CI retains dedicated JUnit and logs; its first execution passed in about
two seconds (CI run 31241802106, job 93064041399). This closes the recorded
testing gap without promoting the wider C4 production or X3 durable-stream
profiles.
GB-RUST-002 is resolved by making `clippy::expect_used` a workspace-level deny
for production targets while binding the 119 existing calls in 16 source files
to a closed, Rust-1.94-specific per-file budget. Only those files carry a
machine-checked exception marker; a forced-warning Clippy pass observes even
the allowed calls and rejects a new file, any per-file increase, or total
growth. Test, example, and benchmark targets receive the narrower command-line
allow. CI retains the deterministic JSON report and separate production/test
Clippy logs. All three gates passed in CI run 31242631454, Rust job 93066047306.
GB-SEC-010 is resolved by replacing exception-derived HTTP payloads with a
closed public envelope containing a stable `errorCode`, fixed safe message,
and response/header correlation ID. A bounded internal `ServerErrorAuditEvent`
retains the route, operation, exception type, and size-limited detail; invalid
correlation factories fall back to a server UUID, unprintable or noncanonical
exception details are normalized or hashed, and audit-sink failure cannot
alter the public response. Background execution and callback-resume failure
records use the same non-disclosing contract. The dedicated regression injects
sensitive compiler, persistence, operation-dispatch, and unprintable failures,
while the security-critical CI selector enforces at least 90% changed-line
branch coverage for this server change. The clean Python 3.11 execution,
including the full suite and coverage gate, passed in CI run 31245606181, job
93073622856.
GB-DOC-001 is resolved by removing the stale 2,700/2,625 Python-test totals
from the status authorities and making revision-bound CI/JUnit evidence the
only test-count authority. The always-run documentation checker now rejects a
numeric test-count claim in either `status.md` or `remaining-work.md`, so the
same drift cannot return through prose. The focused reproduction and the full
link/generated-facts checker passed in Documentation run 31246430572, job
93075701399.
GB-DOC-002 is resolved by replacing the obsolete future-promotion wording with
the actual remaining release boundary: the closed v1 Graph and PluginManifest
resources are candidate-enforced, while their compatibility promise still
depends on every applicable release gate. An executable documentation contract
now binds both statements to the two checked-in closed schemas, their exact
wire declarations, and the `REL-WIRE-V1` and `REL-CLOSED-SCHEMA` gates. The
clean documentation gate passed in run 31246722408, job 93076456592.
GB-DOC-005 is resolved by separating the project phase, distribution metadata,
profile compatibility, artifact readiness, and security-support promise. The
release matrix now owns the pre-1.0 RC project phase and the artifact-specific
Beta/Beta/Alpha classifiers; `SECURITY.md` states the same project phase while
retaining current-branch-only support and no production security-boundary
claim. A regression reads all three package manifests and rejects drift from
that policy. The clean documentation gate passed in run 31247119804, job
93077455406.
This closes the recorded code-and-regression count; it does not replace the
independent review, candidate attestation, platform, or soak gates.

## Release posture

Until all P0 findings have closure evidence:

- protect or withdraw any externally exposed server deployment;
- pause new server, governance, durable, and external-adapter surface growth;
- do not describe the server as a secure multi-tenant boundary;
- do not describe contract-only integrations as production adapters; and
- do not cut a stable release.

The 1.0 gate is stricter than the compatibility tier of an individual module:
every P0 and P1 in a shipped artifact must be resolved, including findings in a
preview module that ships inside the `graphblocks` wheel. A preview
compatibility label does not make a known authorization bypass or fail-open
decoder acceptable to ship.

The following existing assets remain invariants throughout the remediation:

- versioned closed schemas and explicit migrations;
- canonical serialization, identity, and hashing;
- a shared Python/Rust TCK and profile-specific compatibility claims;
- an authoritative event journal distinct from projections;
- runtime admission for policy, budget, approval, and effects;
- installed-wheel, SBOM, Sigstore, and exact-green-SHA release evidence; and
- webhook URL, DNS, egress, and address-pinning validation.

## Phase 0: security freeze

Target window: days 0-7. These items are ordered by exploitability and shared
design dependency, not by source-file location.

| Work item and covered findings | Required outcome | Minimum closure evidence |
| --- | --- | --- |
| SEC-01 Protected-route fail-closed behavior (`GB-SEC-003`) | The application enforces `auth_required`. A manifest containing protected routes cannot be constructed without an authenticator unless `allow_unauthenticated_dev=True` is explicit. Public-only and authenticated manifests retain `/health`; custom hooks cannot redefine public/protected semantics. | Protected manifest without authenticator fails construction; public-only and authenticated `/health` return 200; protected routes deny unauthenticated access; only explicit development mode permits it. |
| SEC-02 Tenant-owned identity (`GB-SEC-001`, `GB-SEC-006`, `GB-COR-006`) | Store immutable internal ID, tenant-scoped external run ID, tenant, owner, creation authorization context, state/version, and fence. Admission-ticket subjects and signatures bind `{tenant_id, principal_id}`. The same external ID may exist in two tenants; a duplicate inside one tenant conflicts. | Tenant-isolated identical IDs succeed, same-tenant duplicate returns 409, immutable ownership survives restart, and identical principal IDs in different tenants cannot share tickets. |
| SEC-03 Object authorization (`GB-SEC-001`, `GB-SEC-002`, `GB-SEC-004`, `GB-SEC-008`, `GB-QA-016`) | Enforce `authenticate -> tenant-scoped resolve -> object authorize -> service transaction -> audit`. A common `ResourceOwner`/`Action` policy covers list, status, attach, events, WebSocket, SSE, detach, cancel, pause, resume, expire, subscription, acknowledgement, callback, redrive, and dead-letter. Direct handler principal comparison is forbidden. | A generated two-principal/two-tenant matrix covers owner/non-owner, accepted/running state, every read/control/stream path, non-disclosing 404 policy, and concurrent same-transaction owner/version/fence revalidation. A route-manifest meta-test fails for any protected route without owner resolution and authorization coverage. |
| SEC-04 Callback and delivery lifecycle (`GB-SEC-007`, `GB-COR-009`) | Resolve delivery, subscription, registration, tenant, and owner atomically; enforce a symmetric delivery state machine and operation idempotency contract. Missing delivery returns 404 and impossible transition returns 409. | Missing, foreign, completed, cancelled, duplicate, conflicting-payload, concurrent, stale-lease, and stale-fence redrive/dead-letter cases. |
| SEC-05 Exact external-input decoding (`GB-POL-001`, `GB-INP-010`) | Validate every external YAML/JSON command input against a closed schema and replace `str`, `int`, `tuple`, and `dict` coercions with path-aware exact codecs. | `actions`, `resources`, and `selectors` reject object, scalar, bool, null, duplicate key, unknown field, missing identifier, and `None`; generated type-confusion cases cover every CLI subcommand. |
| SEC-06 Immediate resource ceilings (`GB-INP-001`–`004`, `GB-INP-006`, `GB-INP-007`, `GB-SEC-011`, `GB-SEC-012`) | Apply adapter wire caps before parsing and defense-in-depth route caps with 413. Client reads enforce `Content-Length`, chunk/decompressed-byte caps, total cancellation deadline, and truncated error bodies. YAML/directory loaders stream under document/file/byte/node, root-containment, and symlink budgets. IDs and reasons have separate byte, control-character, and Unicode-normalization policies. Untrusted regex is denied by default and may be admitted only by a non-backtracking engine or isolated deadline boundary. | Content-length mismatch, infinite chunking, compressed bomb, slow/error stream, deep/large JSON, header bomb, file bomb, symlink loop/escape, NUL/CRLF/bidi/normalization, catastrophic regex, and canonical integer boundary corpora. |
| SEC-07 Evidence reconstruction and disclosure (all nine reproduced findings) | Reconstruct missing harnesses for policy CLI, canonical bigint/Decimal, journal append, and nonexistent delivery; convert every reproduced issue into before/after regression evidence. Create an evidence manifest binding the eventual source identity, commands, environment, tool versions, and file digests. Determine whether external users require a coordinated advisory. | Nine issue-to-fixture mappings are executable on supported Python and Rust environments; missing, stale, substituted, unresolved, or output-only evidence fails promotion. |
| SEC-08 Authentication failure and audit contract (`GB-SEC-005`, `GB-SEC-009`, `GB-SEC-010`) | Return `401` plus `WWW-Authenticate` for absent/invalid authentication, `403` for an authenticated denial, and explicit `404` only for resource hiding. Public errors use stable codes, safe messages, and correlation IDs. Auth-provider timeout, exception, and invalid decisions emit structured internal audit containing route, request/correlation ID, exception class, and safe principal hint. | Status/reason/header matrix plus injected provider failures and sensitive exception strings; public responses do not leak internals and audit records remain correlated. |

Phase 0 exits only with zero open P0 findings, passing adversarial regressions,
and a documented authorization policy for existence disclosure. Handler-local
owner checks are not sufficient closure for SEC-02 or SEC-03.

## Phase 1: storage, durability, and resource model

Target window: weeks 2-4. This phase closes the P1 failure modes that cannot be
made reliable inside process-local dictionaries or monolithic handlers.

1. Define `RunRepository`, `EventJournal`, `WorkQueue`, `CheckpointStore`,
   `CallbackInbox`, `CallbackOutbox`, `DeliveryRepository`, and
   `LeaseRepository` interfaces. Each write transaction binds tenant, resource,
   owner, state/version, lease generation, fencing token, and idempotency
   identity.
2. Deliver one durable accepted-run vertical slice across process restart,
   callback continuation, control state, and output/effect publication before
   generalizing other routes.
3. Add cursor pagination, retention, compaction, and explicit quotas for runs,
   events, callbacks, controls, subscriptions, acknowledgements, and delivery
   results. Default run responses are summary-only and enforce `maxEvents` and
   `maxBytes`; full history uses cursor continuation or streams. In-memory
   repositories remain bounded test/development implementations.
4. Implement atomic outbox claim, lease, renewal, fence, completion, and retry
   semantics so concurrent publishers cannot claim the same work without an
   explicit idempotent duplicate contract.
5. Define delivery lifecycle and operation idempotency keys; same key and
   payload returns the same outcome, a conflicting payload returns 409, and
   missing or illegal transitions never record a successful no-op.
6. Replace long-lived, thread-affine SQLite connections and whole-ledger
   snapshot rewrites with either a per-operation/thread pool or serialized DB
   actor, explicit WAL and `busy_timeout`, transaction policy, versioned
   transactional migrations, and row-level or append-oriented mutations.
   Validate thread/process contention, close-during-call, concurrent open, and
   crash-safe migration replay.
7. Reserve tenant-scoped run and idempotency identities atomically before
   compilation, generate missing resource IDs with UUIDv7/ULID rather than
   fixed defaults, and keep resource IDs separate from idempotency keys.
   Detach/ack uniqueness checks and appends occur in one transaction.
8. Remove tuple-copy journal append behavior and replace Decimal placeholder
   replacement with a single-pass canonical encoder. Separate the mutable
   journal from immutable snapshots and add integer, node, depth, string,
   field, allocation, and total-work limits.
9. Introduce one `SchemaExecutionPolicy` for external MCP schemas, plugin
   configuration schemas, OpenAPI-derived schemas, and future schema entry
   points. It bounds schema bytes, nodes, depth, patterns, references, and
   validation work. Remote references and patterns default to disabled; a
   pattern requires a non-backtracking engine or an isolated killable deadline,
   not merely a length check. Cache validated schemas by canonical digest in a
   bounded LRU and retain immutable parsed discovery schemas.
10. Change public Rust canonical and typed-value APIs from panicking `expect`
    paths to `Result`-returning APIs with stable diagnostics. Deny new
    `expect_used` in non-test targets and retain a reviewed baseline for any
    temporary exception.
11. State timeout semantics precisely. Cooperative in-process deadlines must
    not be represented as preemptive termination; production isolation requires
    a killable worker/process boundary, provider cancellation, worker recovery,
    and fenced effect publication. Tests cover an infinite loop, unresponsive
    provider, cancellation-ignoring tool, and stale commit after reclamation.
12. Add explicit `start`, `drain`, and `close` lifecycle and ownership rules for
    executors and repositories. Separate monotonic in-process deadlines,
    authority wall-clock leases with skew policy, and audit wall timestamps;
    test clock rollback/forward, graceful/forced drain, double close, and submit
    during shutdown.

The `GB-COR-010` server-lifecycle portion is resolved by explicit
`running -> draining -> closed` admission, monotonic drain deadlines, graceful
waiting for admitted requests and accepted runs, forced cancellation on timeout,
idempotent close, and opt-in executor ownership. Health remains observable
during shutdown, already-admitted requests may finish scheduling their work,
and direct submit after drain fails closed. Thirteen lifecycle regressions are
included in the security-critical changed-line branch-coverage gate.
`GB-COR-013` is resolved by distinct process-monotonic deadlines,
skew-checked authority-wall epoch milliseconds for admission and leases, and
timezone-aware audit wall timestamps with no scheduling authority. The
authority clock detects rollback, forward skew, and monotonic rollback; small
allowed rollback is clamped. The in-memory lease pool consumes that authority
for omitted observations, while explicit inputs are treated as already
repository-authorized. The new clock module is strict-mypy-owned with complete
branch coverage, and server clock changes meet 100% changed-line branch
coverage.
`GB-COR-014` is resolved by declaring every public server field that is frozen
to `MappingProxyType` as a read-only `Mapping`, including route parameters,
request/auth metadata, response headers, bearer principals, and health-check
details. Existing runtime immutability checks remain green, while a dedicated
mypy fixture rejects all 13 indexed mutation attempts and compatibility
snapshots remain unchanged.

Phase 1 exits when every related P1 has executable closure evidence, the durable
vertical slice passes multi-process crash/restart tests, and resource-budget
benchmarks fail closed at configured ceilings.

## Phase 2: implementation boundaries

Target window: months 1-2. Refactoring follows the security and repository
contracts so moving code cannot preserve the current authority mistakes behind
new module names.

### Release scope

`GB-ARCH-014` now has executable closure evidence: the target 1.0 release
contains only the portable C0 schema/compiler contract and C1 local-runtime
contract. AI application, governance, production platform, orchestration,
voice, and durable stream are closed, independently promoted extension tracks
whose package presence cannot expand the stable claim. Every profile declares
one claim-owner artifact, separate implementation and evidence artifacts,
role-scoped authority, compatibility tier, ancestors, and a promotion gate.
The release matrix and conformance catalog must contain the same closed profile
set, and the regression rejects ownership, track, tier, gate, or inheritance
drift.

### Server

Split routing, request limits, authentication, authorization, error mapping,
route handlers, services, and repositories. Route modules resolve typed
requests; services own domain transactions; repositories own tenant-scoped
atomicity. The existing monolithic `GraphBlocksServerApp.handle` is retired
incrementally behind route-level characterization tests. A typed `ServerLimits`
contract covers body/header bytes and counts, connection and request
concurrency, per-tenant rate, and idle/total deadlines; adapter conformance
tests prove the limit is enforced before application parsing.

`GB-ARCH-005` now has executable closure evidence: `handle` is a bounded
49-line request pipeline that performs request limits, authentication,
tenant-scoped resource authorization, and registry dispatch in that order.
Default route operations, minimum authentication requirements, resource path
policies, and typed handler keys are checked as complete sets. An AST gate caps
the entry point at 70 lines and four conditionals and rejects operation-specific
branching there. Further route/service module extraction may continue without
reopening this request-pipeline boundary.

### CLI and external codecs

Give each command a `register` and `run` boundary with injected dependencies.
Centralize typed input codecs, closed-schema validation, filesystem budgets,
error-to-exit-code mapping, and JSON/text formatting. Command modules must not
define their own permissive coercion rules.

`GB-ARCH-006` now has executable closure evidence: parser construction is
separate from execution, all 29 parser paths are enumerated, exactly 22 leaf
paths resolve through a tuple-keyed callable registry, and the seven group-only
paths remain explicit help boundaries. `_main` is a 20-line registry dispatcher
with a 30-line/four-conditional AST budget and no command-name branches.
Command handlers retain their existing output and exception contracts while
shared release loading returns a typed result instead of cross-command locals.

### Compiler and conformance kit

Separate decode, migration, closed-schema validation, normalization, catalog
resolution, type checking, lowering, and canonical evidence into immutable
phase results with collected diagnostics. Split the conformance package into
models, fixtures, reports, acceptance, and profile-specific runners; replace
the durable mega-dispatch with typed case handlers. Generate the stdlib registry,
documentation, and TCK inventory from one machine-readable manifest. Keep one
authoritative durable fixture or enforce a digest-bound generated mirror.
Consolidate repeated wire, validation, immutability, and SQLite primitives
behind a shared corpus.

`GB-ARCH-007` now has executable closure evidence: the Python reference
compiler entry point is a 22-line orchestrator over eight ordered phases with
frozen phase-result envelopes and tuple-owned diagnostics. Validation is split
into envelope, graph-contract, output-policy, and tool-binding passes; normalized
analysis is split into edge, catalog, and dependency passes with frozen edge
sets and dependency snapshots between passes. AST budgets cap the public
orchestrator at 30 lines, validation passes at 500 lines, and topology passes at
350 lines. Golden phase, pass-order, diagnostic-order, shared TCK, and
Python/Rust differential tests preserve plan identity and diagnostic ownership.

`GB-ARCH-008` now has executable closure evidence: the existing closed
`builtin-plugin.yaml` is the block/implementation authority for Python stdlib
wiring, while stable membership and the `control.map` descriptor overlay remain
single-sourced in `builtin_block_catalog(profile="stable")`. The former
1,420-line registry builder is a bounded manifest dispatcher with no embedded
block IDs or handler definitions; implementation functions live behind
implementation-ID maps, and `control.map` receives a registry-local late-bound
resolver. Exact manifest↔catalog↔handler completeness tests fail closed on
missing or duplicate bindings. One generator emits both the linked Markdown
inventory and resolved stable/preview contracts in `tck/stdlib/inventory.json`;
a clean-generation gate and profile-parity test prevent descriptor, docs, and
TCK drift.

`GB-ARCH-009` now has executable closure evidence: the former 20,246-line
`graphblocks_testing.__init__` is a 250-line export-only compatibility facade
with no function or class definitions and the same ordered 87-name public
surface. Cases, reports, fixture loading, conformance profiles, acceptance
contracts/execution, release gates, runners, and CLI composition have explicit
owner modules with no child import of the root facade. Stable class and pickle
identity remains `graphblocks_testing`, including the private frozen-evidence
aliases needed to read earlier report pickles. Facade dependency forwarding
preserves the existing compiler and installed-wheel override seams. Exact
export, owner identity, pickle round-trip, stable API/CLI snapshot, fixture
digest, packaged-fixture, and acceptance regressions enforce the split.

`GB-ARCH-010` now has executable closure evidence: the former 6,169-line
`TckRunner._run_durable_case` is a two-line delegate to a bounded shared
dispatcher. A closed lightweight contract owns all 15 durable kinds; immutable
registries map each kind to exactly one frozen per-kind decoder and one handler
without making fixture loading import the handler implementation. The 331
canonical fixtures must cover exactly that contract, and AST budgets cap the
delegate, dispatcher, and individual handlers. Unknown kinds fail before
expected-diagnostic reconciliation, preventing an unsupported kind from being
accepted by naming `DurableKindUnknown` as an expected diagnostic. A pinned
full-suite report digest preserves ordered diagnostics and observations for all
331 cases across the extraction.

`GB-ARCH-011` now has executable closure evidence:
`tck/durable/cases.json` is the named canonical source for both runtimes. The
Rust crate keeps a generated byte-for-byte mirror so its packaged tests remain
self-contained, while a checked-in manifest binds the source and mirror paths,
byte counts, and SHA-256 digests. A repository-root-independent generator owns
both outputs; its `--check` mode runs in the Python test gate, and mutation
regressions prove that stale mirror content is detected and repaired. Rust
tests document the generated ownership next to the compile-time fixture
inclusion.

### Python API

Reduce the root export surface to the reviewed stable API, move preview
capabilities under explicit namespaces, replace internal `from graphblocks
import ...` imports with leaf-module imports, and remove import cycles. Enforce
that no root export exists outside the reviewed snapshot; optional preview
warnings remain inside explicit namespaces. Enforce base-install dependency/API
budgets, module-level preview typing/debt budgets, import-time, resident-memory,
and loaded-module limits in CI.

`GB-ARCH-019` now has executable closure evidence: importing the package root
loads only the facade, version, lazy-export helper, and compatibility map. The
606 historical root bindings remain runtime-compatible through exact lazy
resolution and an independently generated name-to-owner snapshot, while only
the reviewed C0/C1 `__all__` is publicly discoverable before explicit access.
`compatibility/python-package-boundaries.yaml` classifies every unlisted public
leaf module and integration as preview, requires preview typing to use the
defining leaf namespace, freezes the base and optional dependency sets, and
establishes three-run cold-import and stable-symbol first-access ceilings. The
Python CI suite rejects alias replacement, duplication, missing owner modules,
dependency or extra growth, preview discovery from a cold root, and time, RSS,
total-module, GraphBlocks-module, or root-attribute budget regressions. The
installed-artifact gate additionally creates a separate environment containing
only the built `graphblocks` wheel and its base dependency closure, runs
`pip check`, compares every base and extra PEP 508 requirement in wheel
metadata, verifies the exact root and canonical module allowlists, and resolves
every stable root export without the runtime or testing distributions. Direct
use of `referencing` is now declared instead of relying on jsonschema's
transitive dependency.

### Rust authority and crate graph

Adopt the following target authority model:

```text
Rust normative
  schema reader and migration semantics
  normalized IR, canonical serialization, and canonical hash
  diagnostic registry and physical plan
  runtime protocol and production scheduler

Python
  typed authoring and ergonomic builders
  schema facade and native compiler/runtime binding
  deterministic reference interpreter and TCK oracle
```

[ADR-0001](../specification/decisions/0001-rust-normative-authority.md)
accepts the target Rust authority and the public Python compiler now dispatches
to it without an implicit reference fallback. The first-stable matrix records
phase-scoped C0/C1 implementation roles rather than a blanket
`python-reference` identity. Resolving `GB-ARCH-001` and `GB-ARCH-002` remains a
1.0 blocker until standalone canonical/schema routing and production scheduler
authority are complete. The native binding now exposes a separate versioned,
closed capability handshake; the Python wrapper rejects incompatible protocol,
version, and required-capability contracts before native invocation, and the
installed-wheel gate compares that contract with distribution metadata. During
the transition, Python/Rust differential tests gate every normative phase.

`GB-ARCH-020` now has executable decision evidence without introducing a
second authority source. The accepted stable release matrix is included
verbatim in the base wheel. Its closed `tckClaimValidation` projection assigns
every C0/C1 suite exactly one closed executor, declaring profile, and authority
role. Executor definitions bind implementation, language, reference
implementation, comparison mode, and allowed suites. Each runner emits its own
execution claim; installed TCK execution reads the packaged matrix and rejects
missing, extra, relabeled, or executor-mismatched suites before binding the
matrix digest, resolved authority claim, and executor proof into `tck.json` and
platform evidence. The compiler executor is `exact-native-reference`: each case
runs the selected Rust wheel and Python oracle and compares the complete Plan
contract before the Rust result is accepted. Schema and C1 runtime claims remain
`reference-only`, preserving the explicit transition blockers for standalone
Rust schema/canonical routing and the production scheduler.

The reusable `graphblocks-control-plane` library boundary is extracted:
`graphblocks-python` and the `graphblocks-control` binary now depend on that
library, so the binding no longer depends on an executable package.
[ADR-0002](../specification/decisions/0002-rust-crate-boundaries.md) defines
the crate-boundary budget: the compatibility-only `graphblocks-types` crate is
retired, bounded sequence primitives are absorbed into runtime core, and every
remaining workspace crate records its consumer, artifact, or compile-isolation
reason before any public Rust API promotion.

### Product and profile boundary

Keep the portable core limited to schema, compiler, runtime core, protocol, and
testing. AI application, governance, durable/remote workers, voice, deployment,
observability, and external integrations advance as separately gated extension
profiles. A feature enters core only when it is required for portable execution
semantics, is implementable by two independent runtimes, has a
provider-neutral TCK, and does not impose a provider or deployment policy.

Domain examples remain examples. Reusable patterns such as bounded delegation,
authority-backed evidence, human approval, workspace transactions, retrieval
provenance, background callbacks, and provider-authoritative playback may be
promoted; legal, research, coding, or voice examples do not become separate
product packages by default. Example metadata declares required profiles,
mocked versus real integrations, threat model, and non-goals.

Explicit non-goals include a hosted orchestrator, full API gateway, secret
manager, generic ETL platform, and full Kubernetes operator. Proposals that
cross those boundaries require a core-inclusion ADR rather than inheriting scope
from an example or adapter.

Every integration promotion record must state contract-only versus real-adapter
maturity, supported authentication modes, SDK/service versions, real-service
evidence, retry/failure model, and promotion gate. The one-shot control-plane
command is named `graphblocks-control`; it must not claim a listener, `serve`
command, or daemon lifecycle. The Kubernetes artifact is named
`graphblocks-deployment-chart`, is disabled by default, and contains no bundled
controller or OCI image. It may claim operator maturity only after the blocked
`REL-KUBERNETES-OPERATOR` gate receives revision-bound reconciliation, status,
upgrade, finalizer, leader-election, envtest, and kind evidence. Reserved
Rust/npm artifacts expose no usable API: the Rust crate emits an install/build
notice and exports only that notice, while npm import throws a dedicated
reserved-package error. `REL-RESERVED-ARTIFACTS` remains blocked until the
marker releases are published and the npm registry deprecation message is
verified.

### Generated project facts and naming

Do not introduce another manually maintained authority. Generate project facts
from the existing owners: the stable release matrix for release tier and
support, the conformance catalog for profile definitions, package metadata for
interpreter constraints, and commit-bound CI evidence for test counts and TCK
digests. Produce status tables, README facts, compatibility matrices, badges,
and release evidence as digest-bound projections and fail CI on drift.

Align `SECURITY.md`, package classifiers, Python version wording, roadmap state,
and status wording through those projections. Describe `graphblocks-control`,
the deployment scaffold, and contract-only integrations by their implemented
behavior rather than by aspirational product names. Generate evidence-bound risk status,
owner, resolved version, and digest; reopen a resolved risk when its required
evidence disappears or changes. The status projection reports supply chain,
API, runtime security, durability, and adapter maturity as separate axes.

`GB-DOC-007` now has executable closure evidence. The stable release matrix owns
five closed readiness axes with explicit 1.0 claim and tag-blocking semantics,
and a checked generator projects them into the status page. Supply-chain
readiness cannot alter runtime-security, durability, or adapter claims. Final
promotion additionally requires CI-signed candidate reports that bind the
closed authorization and adversarial-resource evidence to a candidate-bound
selector manifest, exact test-source digests, all-pass counts, and retained
JUnit digest, plus an actor-bound independent security review covering every
retained matrix report digest. The durability axis separately records that C1
local-runtime authority still blocks 1.0 even though C4/X3 production
durability remains preview and outside the stable claim. Candidate reruns use
attempt-scoped artifacts, and the release matrix plus security-gate manifest
cannot change between the reviewed RC and final tag.

### Quality debt and developer loop

Roll strict typing out package by package, require an error code and reason for
every `type: ignore`, and enforce no-new-ignore and no-new-preview-debt budgets.
Inventory broad exception catches, rethrow cancellation and fatal errors, and
replace external/persisted-data assertions with explicit errors that behave the
same under `python -O`. Add non-test Rust `expect_used` denial.

Split a two-to-three-minute quick PR gate from the full required matrix without
allowing path filters to skip release-critical dependents. Generate the
dependency map used by those filters. Pin a reproducible development
constraints/lock input and verify clean installation and lock regeneration.

Phase 2 exits only when its P1 findings—`GB-API-001`, `GB-ARCH-001`, and
`GB-ARCH-002`—have executable closure evidence, the accepted authority decision
is reflected consistently in the release/profile matrices, and every remaining
P2/P3 has an owner, milestone, and acceptance contract in the remediation map.

## Complete inventory coverage

The [remediation map](audit-remediation-map.yaml) assigns every baseline
finding to exactly one primary workstream. Several fixes affect multiple
profiles or release gates, but primary ownership is unique so no finding can be
silently omitted or counted closed twice.

| Workstream | Phase | Findings | Primary outcome |
| --- | --- | ---: | --- |
| `M0-AUTHORIZATION` | Security freeze | 11 | Tenant identity, object authorization, authentication/error/audit contract |
| `M0-INPUT-RESOURCE` | Security freeze | 11 | Exact codecs and bounded wire/schema/filesystem/server inputs |
| `M1-DURABLE-SERVER` | Storage/resource model | 18 | Durable repositories, lifecycle, idempotency, migration, clocks, pagination |
| `M1-CANONICAL-JOURNAL` | Storage/resource model | 6 | Linear bounded canonical/journal behavior and fallible Rust APIs |
| `M2-PYTHON-API` | Implementation boundaries | 7 | Stable facade, preview isolation, imports, typing and base-install budgets |
| `M2-NORMATIVE-AUTHORITY` | Implementation boundaries | 8 | Rust authority transition, differential evidence, crate/fixture/version boundary |
| `M2-MODULE-BOUNDARIES` | Implementation boundaries | 7 | Server/CLI/registry/TCK/shared-primitive separation and mutation coverage |
| `M2-PRODUCT-BOUNDARY` | Implementation boundaries | 7 | Core/non-goal, integration maturity, naming and example policy |
| `M2-SCHEMA-CACHING` | Implementation boundaries | 2 | Bounded validator and parsed discovery-schema caches |
| `QG-QUALITY-AND-CI` | Cross-cutting | 15 | Fuzz, typing/exception/assert debt, lint, coverage, SAST, model and platform gates |
| `QG-DOCUMENTATION-FACTS` | Cross-cutting | 7 | Generated facts, evidence-bound risks, maturity/readiness and claim automation |

CI validates the unchanged inventory digest and severity counts, unique
workstream ownership, ancestor fix commits, executable regression paths, and
the zero-open P0/P1 release threshold. The closure overlay is deliberately
separate from the immutable baseline so status changes cannot rewrite the
auditor's finding content.

## Evidence and performance seed matrix

The evidence bundle provides useful adversarial seeds, not portable hard caps:

| Seed | Audit observation | Required regression contract |
| --- | --- | --- |
| Catastrophic regex | input length 18: 0.009s; 26: 2.412s | Default rejection or bounded non-backtracking/isolated execution; never calibrate only by pattern length. |
| Canonical bigint | 10k digits: load/dump 0.0015/0.0043s; 300k: 0.886/3.507s | Stable digit/token ceiling and diagnostic; boundary, ceiling+1, time and allocation checks. |
| Decimal canonicalization | 1k values: 0.0138s; 16k: 2.777s | Single-pass slope/complexity and allocation budget through at least 16k values. |
| Journal append | 1k: 0.021s; 16k: 1.256s | Append scaling through 64k plus allocation and immutable-snapshot checks. |
| Import | baseline 0.02s/7.6MB/24 modules; `graphblocks` 1.51s/59.7MB/388 modules; leaf canonical import is effectively identical | Clean-process time, RSS, GraphBlocks-module and total-module budgets for root and leaf imports. |

Before turning these observations into thresholds, record the runner,
interpreter/toolchain, warmup, repetitions, variance allowance, input fixture,
absolute cap, and slope/complexity rule. Also seed:

- one million runs/events for heap plateau and lookup SLO;
- 10, 10k, and 1M runs for cursor-page latency and response caps;
- 10k repeated schema validations for cache behavior and eviction;
- 100 concurrent requests for one tenant/run ID with exactly one compile;
- every append, checkpoint, outbox, and lease atomic boundary with a
  deterministic pre/post-write kill point; and
- client/server attacks covering content-length mismatch, infinite chunking,
  decompression bombs, slow streams, oversized error bodies, deep JSON, header
  bombs, and concurrency/rate limits.

The audit ran Python tests on unsupported Python 3.13 and could not run Rust.
The repository reproduction gate now preserves the captured files, executes the
five reconstructed harnesses, and runs all nine current regression selectors on
the canonical Python 3.11 CI leg. Candidate-bound full gates must still run on
Python 3.11/3.12 and pinned Rust 1.94 while retaining exact commands, versions,
environment identity, and results.

## Required CI and review gates

The stabilization work adds the following gates before 1.0:

1. a complete multi-tenant authorization matrix that automatically includes
   every new protected route;
2. Hypothesis plus Rust fuzz/proptest seed-corpus PR smoke and scheduled
   long-running fuzz for panic-free, bounded-resource, and cross-runtime
   equivalence properties;
3. Python/Rust canonical and compiler phase differential tests;
4. request, response, schema, and canonical resource-budget tests;
5. import time, RSS, and loaded-module budgets;
6. canonical, journal, budget, and compiler performance regressions;
7. multi-process crash, restart, lease, and fencing tests;
8. stable/security-critical branch coverage plus changed-lines coverage policy;
9. Ruff formatting/linting, progressive strict typing, no-new-ignore/debt,
   exception-boundary inventory, and optimized-mode validation;
10. Python and Rust dependency vulnerability scanning;
11. CodeQL or an equivalent reviewed static-analysis gate;
12. macOS and native-wheel smoke coverage;
13. documentation link/anchor, evidence-bound risk, and generated-facts drift
    checks with a fast docs-only job;
14. Miri, Loom, or an equivalent targeted Rust state-machine validation where
    the model is applicable;
15. a dependency-aware quick/full CI split plus reproducible development
    constraints lock; and
16. handler-level mutation coverage and surviving-mutant evidence for stable
    compiler, policy, and canonical boundaries.

Each gate needs a documented budget, deterministic failure output, and a named
release-matrix requirement. Merely adding a tool without an enforced threshold
does not close a finding.

`GB-QA-011` is enforced by the named `REL-DOCS-INTEGRITY` gate. A bounded
checker discovers living Markdown while pruning build and dependency trees,
uses a CommonMark AST and upstream GitHub-compatible slugger, and validates
undefined references, repository containment, exact-case local paths, heading
and source-line fragments, and explicit anchor uniqueness. One closed generated
documentation registry owns every generator command, primary source, and output;
the checker runs every entry in `--check` mode and binds checker, document, and
projection content digests into its result. An always-run five-minute workflow
avoids both path-filter omissions and skipped required-check states. External
URLs are counted but intentionally not fetched; the gate claims deterministic
local integrity, not third-party availability.

The `GB-QA-008` implementation is specified in
[security-fuzzing.md](security-fuzzing.md). Normal pull-request CI runs the
Hypothesis and proptest properties, while the dedicated security-fuzz workflow
runs a bounded cargo-fuzz seed-corpus mutation smoke on relevant changes and a
30-minute weekly or manually dispatched campaign. This is implemented gate
evidence, not proof that the exact future release candidate completed its
scheduled campaign; `REL-AUDIT-REMEDIATION` remains blocked until
candidate-bound execution evidence and the other exit criteria are complete.

## 1.0 exit criteria

The stable tag remains blocked until all existing release gates and all of the
following audit gates pass:

- zero open P0 and P1 findings;
- all 99 immutable baseline IDs map to one workstream, and the imported live
  inventory remains digest-bound to the signed `audit-closure` promotion
  report together with the exact captured/reconstructed reproduction files and
  current selector sources;
- the audited source commit/tree or source-archive digest and a file-level
  evidence manifest are present;
- the two-principal/two-tenant authorization matrix is green;
- adversarial JSON, YAML, schema, regex, and canonical-number cases are green;
- Python/Rust differential evidence is digest-bound to the release candidate;
- the Rust normative-authority ADR and transition evidence satisfy
  `REL-NORMATIVE-AUTHORITY`;
- multi-process crash/restart/lease/fence tests are green;
- output/effect outbox and idempotency guarantees have executable evidence;
- performance, memory, and import budgets are enforced;
- dependency and static security scans have no unaccepted high-impact finding;
- a separately defined macOS and native-wheel smoke gate passes; expanding the
  official supported-platform matrix requires an explicit release-tooling and
  evidence decision;
- independent API and security reviews approve the unchanged release
  candidate; and
- the reconstructed audit reproductions and applicable full gates pass on
  Python 3.11/3.12 and pinned Rust 1.94.

The exact release candidate must still satisfy the installed-artifact,
supply-chain, signed-evidence, protected-ref, and soak requirements in the
[first stable release boundary](first-stable-release.md). Audit remediation
adds to those gates; it does not replace them.

## P2 and P3 scheduling

Every P2 and P3 is assigned in the remediation map. Security-adjacent P2 items
needed to make a P0/P1 fix sound—tenant-bound admission tickets, atomic
detach/ack, streaming/file budgets, safe errors and identifiers, versioned
migrations, shutdown/clock boundaries, and Rust panic linting—move with Phase 0
or Phase 1 rather than waiting for broad refactoring.

The remaining P2 work follows the Phase 2 workstream order. P3 naming,
documentation, reserved-artifact, non-goal, example-metadata, docs-check, and
mutation-testing items ship with the owning product/quality/generated-facts
workstream so they cannot drift independently again.

Review this plan after each security-freeze merge, weekly through Phase 1, and
at every release-candidate decision. Update status from evidence; do not reduce
severity to meet a date.

# Risk Register

This file tracks Graphblocks issues that need follow-up beyond one isolated implementation batch.

## Tests

### [TEST-001] Rust voice TCK does not enforce provider interruption authority

**Severity**: Medium
**Status**: Resolved
**Location**: `crates/graphblocks-runtime-core/src/voice.rs`, `crates/graphblocks-runtime-core/tests/voice_tck.rs`, `tck/voice/cases.json`

**Description**:

- The shared voice fixture carries provider authority and provider decision fields.
- The Python voice implementation and acceptance runner require those fields and prove that local VAD is advisory.
- The Rust runtime-core classifier and TCK runner still derive interruption directly from local VAD and ignore the provider fields.

**Impact**:

- A passing Rust voice TCK is not evidence of provider-authoritative interruption semantics.
- Cross-language voice conformance remains incomplete even though the Python acceptance application is executable and green.

**Plan**:

- Add provider interruption decision contracts to Rust runtime-core.
- Add discriminating shared cases for local speech without confirmation, provider interrupt during local silence, and wrong authority/session.
- Make both Python and Rust TCK runners consume the same provider-bound fixture evidence.

**Resolution**:

- Rust now consumes provider interruption decisions, rejects future or stale
  authority evidence, and only interrupts playback active at the decision
  timestamp. Shared and Rust-specific voice tests cover those cases.

## Correctness

### [BUG-001] Python budget permits pool authority across unrelated reservations and expose an expiry-bypassing spend path

**Severity**: High
**Status**: Resolved
**Location**: `src/graphblocks/budget.py`, `tests/test_budget_permit.py`, `tck/budget/`

**Description**:

- `issue_permit` sums every referenced reservation into one permit-wide amount pool.
- `commit_with_permit` can spend that combined pool against any one referenced reservation, including a reservation owned by a different budget.
- The primary `commit_with_permit` and `release_with_permit` methods do not enforce `expires_at`; only the alternate `*_at` methods do.

**Impact**:

- Authority reserved by one budget can be converted into overdraft on another budget.
- An expired permit remains usable through a public settlement API.

**Plan**:

- Bind authorized amounts to reservation and budget identities instead of only to a permit-wide total.
- Make expiry validation mandatory on every permit-authorized mutation through an injected clock or required evaluation timestamp.
- Add in-memory, persisted-ledger, and shared TCK cases for cross-budget spending and expired permits.

**Resolution**:

- Permits bind amounts to reservation and budget identities, every authorized
  mutation enforces expiry, and the in-memory, SQLite, and shared race/TCK
  suites cover cross-budget and expired-permit behavior.

### [BUG-002] Canonical JSON numbers diverge between Python and Rust

**Severity**: High
**Status**: Resolved
**Location**: `src/graphblocks/canonical.py`, `crates/graphblocks-compiler/src/canonical.rs`, `tck/schema/typed-values.json`

**Description**:

- Python renders `1e-7` as `1e-07`, while Rust renders it as `1e-7`.
- Python preserves the integer `100000000000000000000`, while the default Rust `serde_json` representation renders the same JSON input as `1e+20`.
- Current shared canonical-value fixtures cover ordinary integers but not exponent formatting or large integers.

**Impact**:

- Plan hashes, tool argument digests, signatures, replay identities, and other canonical SHA-256 values can differ across language boundaries.
- Rust does not preserve the integer/floating-point distinction required by the canonical data-model specification for sufficiently large integers.

**Plan**:

- Define one normative numeric canonicalization and supported numeric domain.
- Preserve or reject numbers outside that domain consistently in both implementations.
- Add shared fixtures for exponent boundaries, large integers, signed zero, and precision limits.

**Resolution**:

- Python and Rust use the same supported numeric domain and canonical exponent
  form, with shared fixtures covering exponent boundaries, large integers,
  signed zero, and precision limits.

## Reliability

### [BUG-003] Durable SQLite stores use non-atomic read-modify-replace and callback claims

**Severity**: High
**Status**: Resolved
**Location**: `crates/graphblocks-runtime-core/src/async_operation.rs`, `crates/graphblocks-runtime-core/src/callback_delivery.rs`

**Description**:

- Several `SqliteAsyncOperationStore` mutations load the whole database, release the connection lock, mutate an in-memory snapshot, then delete and reinsert all rows.
- The callback queue selects due rows and later writes `Delivering` with an unconditional upsert rather than an atomic claim or version check.

**Impact**:

- Concurrent mutations can delete another writer's operation, receipt, event, or quarantine record.
- Concurrent callback workers can deliver the same event and regress a terminal delivery to stale pending, delivering, or failed state.

**Plan**:

- Replace snapshot rewrites with row-level transactional mutations and explicit compare-and-set transitions.
- Add deterministic two-writer tests using both one shared handle and separately opened handles.
- Assert terminal-state monotonicity and exactly one transport call per successful queue claim.

**Resolution**:

- SQLite mutations use coherent transactions and compare-and-set claims.
  Separate-handle concurrency tests cover operation preservation, terminal
  monotonicity, and single successful delivery claims.

## Security

### [SEC-001] Webhook DNS policy checks are not bound to the connected address

**Severity**: High
**Status**: Resolved
**Location**: `src/graphblocks/url_validation.py`, `src/graphblocks/server.py`, `crates/graphblocks-runtime-core/src/callback_delivery.rs`, `crates/graphblocksd/src/lib.rs`

**Description**:

- The Python default policy accepts syntactically valid hostnames without resolving their A/AAAA records.
- Rust validates resolved addresses, then discards them; the standard HTTP client connects by hostname and performs a second DNS resolution.

**Impact**:

- A hostname that resolves to loopback, private, link-local, or metadata infrastructure can bypass the Python check.
- DNS rebinding between Rust policy evaluation and connection can redirect an approved webhook to an internal service.

**Plan**:

- Inject resolution into the egress policy, validate every address, and connect only to a validated `SocketAddr` while preserving the hostname for HTTP Host and TLS SNI.
- Define redirect revalidation and address-family behavior.
- Add scripted DNS-rebinding and private-resolution integration tests in both language paths.

**Resolution**:

- Egress policy validates resolved addresses and the standard clients connect
  to the validated address. Redirect and private-address tests cover both
  language paths.

## Open follow-up

### [API-001] SQLite async-operation convenience reads hide storage failures

**Severity**: Medium
**Status**: Resolved
**Location**: `crates/graphblocks-runtime-core/src/async_operation.rs`

**Description**:

- `quarantined_callback_count`, `events_for_operation`, and `operation_state`
  convert `load_memory_store` failures to `0`, an empty list, or `None`.
- Callers cannot distinguish absent data from corruption, lock, or decode
  failures.

**Plan**:

- Add fallible read APIs and migrate control-plane callers to them before
  deciding whether the infallible convenience methods can be deprecated.
- Add corrupt-row and locked-database regressions for each read path.

**Resolution**:

- Added `try_quarantined_callback_count`, `try_events_for_operation`, and
  `try_operation_state` so reliability-sensitive callers can preserve storage
  and decoding failures while compatibility shims retain their prior defaults.
- Migrated the daemon's terminal-operation response path to the fallible state
  read and added corrupt-row regressions proving storage failures remain
  machine-readable instead of becoming `OperationNotFound`.

### [ARCH-001] Accumulating windows have no defined observable semantics

**Severity**: Medium
**Status**: Open
**Location**: `crates/graphblocks-runtime-durable/src/lib.rs`, `src/graphblocks/durable.py`, `tck/durable/cases.json`

**Description**:

- Both language APIs accept `accumulating`, but the current tumbling-window
  accumulator emits only once at final closure, so the mode cannot affect an
  output.
- The shared TCK covers only `discarding` and does not define early triggers,
  pane replacement, or retraction behavior.

**Plan**:

- Specify trigger and pane-update semantics before adding runtime behavior.
- Add discriminating shared fixtures, then implement the same behavior in
  Python and Rust.

# Changelog

Notable project changes are documented here. GraphBlocks follows semantic
versioning once public compatibility guarantees begin; the current series is
pre-release alpha software.

## Unreleased

### Added

- Living English specification organized by contract domain.
- Open-source contribution, governance, conduct, and security policies.
- Executable acceptance coverage for ten applications and 42 gates.

### Changed

- Replaced the mutable architecture bundle with explicit documentation
  authorities, implementation status, and roadmap documents.
- Made shipped catalogs under `src/graphblocks/data/` the canonical catalogs.
- Hardened release and conformance gates: release bundles reject non-finite
  numbers, native TCK fallback cannot satisfy a native claim, and TCK reports
  bind suite, implementation version, and fixture digest evidence.
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

### Removed

- Duplicated monolithic specification, mutable checksum manifest, historical
  review reports, and the bundled binary archive.

## 0.1.0 - Development baseline

- Initial Python and Rust contract implementations, schema set, TCK fixtures,
  package manifests, and acceptance applications.

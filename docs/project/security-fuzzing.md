# Security fuzzing gate

This gate is the executable implementation evidence for `GB-QA-008`. It covers
the parser, canonical JSON, schema identity, and typed-value boundaries with
three complementary layers:

- Hypothesis properties in `tests/test_canonical_properties.py`, executed by
  the normal Python pull-request matrix;
- proptest properties in
  `crates/graphblocks-schema/tests/canonical_properties.rs`, executed by the
  normal Rust pull-request matrix; and
- a libFuzzer target in `fuzz/fuzz_targets/canonical_json.rs`, executed by the
  dedicated security-fuzz workflow.

The workflow pins `nightly-2026-04-22`, cargo-fuzz 0.13.2, and
`libfuzzer-sys` 0.4.13. The fuzz crate has its own lockfile and workspace so it
does not weaken the stable Rust 1.94 contract used by normal builds and release
artifacts. CI runs a locked Cargo metadata preflight and verifies that
cargo-fuzz did not rewrite the lockfile.

## Campaign budgets

| Campaign | Mutation budget | Input ceiling | Per-input timeout | RSS ceiling | Job ceiling |
| --- | ---: | ---: | ---: | ---: | ---: |
| Pull request and main push | 10,000 executions | 16,384 bytes | 10 seconds | 2,048 MiB | 45 minutes |
| Weekly schedule and manual dispatch | 30 minutes | 16,384 bytes | 10 seconds | 2,048 MiB | 45 minutes |

The first libFuzzer corpus directory is an ephemeral writable directory under
`dist/fuzz`; the checked-in seed directory is read-only input. This prevents a
CI run from silently rewriting the authoritative seed corpus. Generated corpus
entries, toolchain identity, and crash or timeout artifacts are retained for 30
days. Pull-request and push runs use event-scoped cancellation groups, while
scheduled and manual campaigns cannot be canceled by those shorter runs.

The compact seed modes deliberately reach the audited boundaries without
checking huge generated files into Git:

- `R` passes arbitrary UTF-8 text to canonical parsing and round-trip checks;
- `K` constructs duplicate object keys and requires the exact duplicate-key
  error;
- `D` synthesizes nesting around the canonical depth ceiling, requiring
  acceptance through the ceiling and the exact bounded error above it;
- `I` synthesizes integers around the 10,000-digit ceiling with the same
  accept-or-bounded-error oracle; and
- `S` exercises schema identity parsing.

Every successfully parsed value must encode, parse, and encode idempotently and
must enter `TypedValue::from_schema` without panicking. Invalid inputs may
return typed errors except where a boundary mode requires a specific outcome;
an unwind, sanitizer failure, timeout, or invariant mismatch fails the job and
leaves a reproducible artifact.

## Local commands

Install the pinned fuzz driver once:

```text
cargo +nightly-2026-04-22 install cargo-fuzz --version 0.13.2 --locked
```

Replay the checked-in corpus and run the pull-request mutation budget:

```text
mkdir -p dist/fuzz/corpus dist/fuzz/artifacts
cargo +nightly-2026-04-22 fuzz run canonical_json \
  dist/fuzz/corpus fuzz/corpus/canonical_json -- \
  -runs=10000 -max_len=16384 -timeout=10 -rss_limit_mb=2048 \
  -artifact_prefix=dist/fuzz/artifacts/
```

The scheduled workflow replaces the execution count with
`-max_total_time=1800`.

This closes the missing-gate implementation described by `GB-QA-008`; it does
not by itself satisfy every audit promotion requirement. In particular,
candidate-bound scheduled-run artifacts and the separate Python/Rust
differential evidence must still be collected before
`REL-AUDIT-REMEDIATION` can become ready.

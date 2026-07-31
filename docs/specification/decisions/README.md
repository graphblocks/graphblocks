# Architecture Decisions

The living architecture follows these durable decisions:

- Keep graph contracts provider-neutral and select concrete tools through
  versioned bindings and packages.
- Separate graph, application, release, and deployment resources.
- Make canonical records and event streams authoritative; callbacks and
  telemetry are projections.
- Treat policy, budget, approval, review, leases, and fencing as runtime
  admission boundaries.
- Bound dynamic work through explicit sequence, task, retry, time, resource,
  and checkpoint limits.
- Claim compatibility by profile with shared TCK and acceptance evidence.
- Assign portable compiler and execution authority by phase. Rust is the
  normative compiler and target production execution authority; Python is the
  authoring facade and explicit reference oracle. See
  [ADR-0001](0001-rust-normative-authority.md).
- Keep a Rust crate only for an independently justified artifact, consumer,
  protocol, persistence, extension-isolation, or reservation boundary. See
  [ADR-0002](0002-rust-crate-boundaries.md).

New decisions that alter a public contract should be added as numbered ADRs
with context, decision, consequences, migration, and conformance impact. Git
history retains the retired bundle's draft and legacy decision logs.

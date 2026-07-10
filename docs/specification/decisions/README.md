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
- Keep Python and Rust parity explicit rather than declaring one implementation
  normative where support differs.

New decisions that alter a public contract should be added as numbered ADRs
with context, decision, consequences, migration, and conformance impact. Git
history retains the retired bundle's draft and legacy decision logs.

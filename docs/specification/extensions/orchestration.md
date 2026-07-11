# Bounded Orchestration Extension

A `TaskPlan` contains versioned `TaskStep` records and explicit
`TaskPlanLimits`. Limits bound step count, dependency count, description size,
nesting depth, and maximum parallel tasks. Priorities are `optional`, `normal`,
`required`, `verification`, and `finalization`.

Dependencies MUST resolve, the plan MUST remain acyclic, and
`execution_layers()` MUST produce deterministic ready layers within the declared
parallel width. Context access is allow-listed; a child MUST NOT read undeclared
parent, sibling, workspace, secret, or artifact context.

A `TaskPlanPatch` binds `base_plan_id` and `base_revision`. Applying it uses
compare-and-swap, rejects duplicate steps or identity mismatch, and revalidates
dependencies, cycles, depth, width, and other limits before publishing the next
revision.

`TaskExecutionContract` uses an `each_task` checkpoint and `per_task` budget
reservation. A child permit MUST be active, held by the expected task, covered
by a parent reservation, no larger than the parent amount, and no later than the
parent expiry. Budget pressure may cancel optional/normal work while preserving
required, verification, and declared finalization work within its continuation
permit.

`LeasePool.acquire_with_budget_permit()` MUST verify holder identity, active
permit, covered reservation, capacity, and expiry. Lease expiry cannot exceed
permit or reservation expiry, and lease metadata records permit/reservation
evidence. A lease interval MUST use valid timestamps with
`acquired_at < expires_at`; a grant is active only throughout the
acquisition-inclusive, expiry-exclusive interval. Stale, future, or malformed
lease or permit identity MUST NOT authorize a task.

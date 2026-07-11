# Policy, Budget, Usage, and Evaluation

Policy decisions are immutable evidence bound to subject, action, resource,
principal, policy snapshot, and relevant input digest. Mandatory enforcement
points fail closed on unavailable or malformed decisions. Advisory results MUST
not be presented as authorization.

Usage entries record observed consumption independently of estimates. Budget
reservations and permits authorize bounded work before admission; reconciliation
commits actual usage and releases unused capacity. A child permit MUST not exceed
its parent's amounts or expiry. Concurrent reservations MUST preserve hierarchy
limits and reject stale or duplicate commits.
Every permit-authorized commit or release MUST be evaluated at an explicit time,
MUST fail after permit expiry, and MUST spend or release only the reservation
identity covered by that permit.

Exhaustion policy defines denial of new work, behavior for in-flight work,
bounded continuation, output disposition, effect atomicity, and the terminal or
paused state. Cleanup is not authority for new provider calls or effects.

Approval authorizes a proposed action. Review evaluates an immutable candidate
or result. Checks produce typed evidence; a gate combines required checks and
must bind the same subject. Changes to the subject invalidate reviews and gate
evidence. Evaluation records MUST retain release, plan, policy, dataset, and
implementation provenance needed to reproduce the decision.

Canonical example profiles are in `profiles/policy-profiles.yaml`.

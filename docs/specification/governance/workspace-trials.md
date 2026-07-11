# Workspace Trials and Governed Commits

A workspace trial evaluates a candidate mutation without authorizing it to
replace the base workspace. The trial plan binds trial identity, base revision
and digest, candidate digest, required checks, gate, review scopes, lease kinds,
and mutation policy.

Commit authorization requires all of the following:

- every required check exists, passes, and binds the candidate;
- the gate passes, binds the candidate, and includes every required check;
- the mutation policy decision allows that candidate;
- every required lease is active and owned by `trial:{trial_id}`;
- every required review is accepted, in scope, and valid for the unchanged
  candidate; and
- the request binds its commit identity, expected base revision, and base and
  candidate digests, plus the required check IDs, review scopes, and exact
  lease evidence active during the trial.

A native trial MUST NOT issue a commit request until a mutation decision is
present and its passing gate includes every required check ID. Missing policy
or gate-binding evidence is a trial failure, not a deferred commit-time default.

The workspace store MUST re-evaluate revision, base digest, candidate identity,
gate, review, lease, and materialized candidate digest immediately before its
compare-and-swap commit. Any mismatch rejects the commit without partially
applying the candidate. Review or gate evidence from an earlier candidate MUST
NOT authorize a changed candidate. A successful commit MUST retain the commit
identity authorized by its request.
The final commit boundary MUST require an explicit allowed mutation decision
and passing gate even for requests constructed outside a trial helper; missing
proof MUST NOT default to authorization.
The workspace head, change-set base, and change-set candidate MUST all name the
same workspace; matching digests cannot substitute for workspace identity. A
passing gate MUST bind the complete candidate reference, not merely report a
passing decision.
Removing a required check from the gate or removing the last valid review for a
required scope MUST invalidate the request at commit time.
Lease-bearing requests MUST be committed with an explicit evaluation time. A
required lease that is not yet acquired, has expired, belongs to another trial,
or was not retained by the request MUST reject the commit.

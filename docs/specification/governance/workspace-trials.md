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
- the request binds the expected base revision plus base and candidate digests.

The workspace store MUST re-evaluate revision, base digest, candidate identity,
gate, review, lease, and materialized candidate digest immediately before its
compare-and-swap commit. Any mismatch rejects the commit without partially
applying the candidate. Review or gate evidence from an earlier candidate MUST
NOT authorize a changed candidate.

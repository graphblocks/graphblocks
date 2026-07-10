# Release and Deployment

A release bundle binds graph, physical plan, packages and lock, schemas, policy,
prompts, indexes, and other declared artifacts. Artifact digests are immutable.
Bundle builders MUST reject NaN and positive or negative infinity before
writing an archive; every emitted `release.json` MUST be strict JSON that the
bundle verifier can parse.
`ReleaseBundle.attestation_digest()` excludes signatures and computed bundle
identity while binding the release and artifact set.

The current reference `ReleaseAttestation` verifier supports canonical
HMAC-SHA256 evidence, explicit signer trust, subject binding, and attestation
digest binding. This is a reference contract, not a recommendation that shared
secret signing is sufficient for every production environment. Unknown signers,
malformed digests, subject mismatch, and signature mismatch fail closed.

A deployment revision maps one release to targets, worker constraints, package
locks, routes, scaling, callback ingress, observability, and rollout policy. A
production run MUST record graph hash, release digest, deployment revision,
physical-plan hash, and verified signature evidence without coercing malformed
persisted identities.

Canary thresholds declare a metric and minimum or maximum allowed regression.
Evaluation MUST fail closed for missing candidate/baseline values, zero baseline
where a ratio is required, non-finite values, duplicate metrics, or missing
thresholds. The canonical evaluation evidence and digest bind all inputs and
per-threshold results.

Rollback evidence is authorized only after an aborted rollout whose policy
allows automatic rollback. Drain behavior MUST distinguish new requests,
existing requests, conversations, durable jobs, and realtime sessions. New work
may be rejected while bounded existing work drains; ownership and checkpoint
fences continue to apply throughout rollback.

# Deployment Guide

A GraphBlocks release freezes graph, binding, package, policy, prompt, schema,
and index identities. A deployment maps that release to targets and produces a
deployment revision and physical plan.

For production evidence, build or load a deployment plan before starting the
run. The run path verifies canonical digest forms, graph identity, release
digest, deployment revision, and the physical-plan content digest before
execution. The signature digest records a signature verified by the release
workflow; it is not a replacement for signature verification.

```bash
python -m graphblocks deploy plan deployment.yaml --revision revision-1 --json > deployment-plan.json
python -m graphblocks run graph.yaml \
  --deployment-plan deployment-plan.json \
  --release-signature-digest sha256:... \
  --run-store runs.sqlite3
```

Rollout automation should evaluate declared canary thresholds, fail closed on
missing or non-finite measurements, authorize rollback only for an aborted
rollout that permits it, and apply workload-specific drain decisions.

See [release and deployment semantics](../specification/operations/release-deployment.md).

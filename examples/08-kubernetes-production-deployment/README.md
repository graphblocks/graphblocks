# Kubernetes Production Deployment

This pair of resources defines an immutable graph release and a Kubernetes
deployment with execution groups, placement, canary quality/policy thresholds,
automatic rollback policy, and workload-aware drain behavior.

```bash
python examples/08-kubernetes-production-deployment/run.py
```

Validation does not access a cluster. The production acceptance application
executes release-attestation, canary, rollback, and drain semantic gates.

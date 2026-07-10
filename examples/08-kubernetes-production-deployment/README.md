# Kubernetes Production Deployment

This pair of resources defines an immutable graph release and a Kubernetes
deployment with execution groups, placement, canary quality/policy thresholds,
automatic rollback policy, and workload-aware drain behavior.

```bash
python examples/08-kubernetes-production-deployment/run.py
```

The runner uses fake signing, metric, and deployment boundaries to execute
release-attestation, canary, rollback, and drain gates without accessing a
cluster.

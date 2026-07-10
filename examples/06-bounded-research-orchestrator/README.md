# Bounded Research Orchestrator

This graph resolves a worker pool, creates and validates a bounded task plan,
executes with per-task checkpoints and budget reservations, applies a replan
patch through compare-and-swap, and requires independent verification.

```bash
python examples/06-bounded-research-orchestrator/run.py
```

The runner executes semantic integration gates for task-plan limits, mocked
worker-pool resolution, delegated budget authority, and patch revision fencing.

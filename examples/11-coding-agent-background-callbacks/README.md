# Coding Agent with Background Callbacks

This application and graph define accepted background invocation, cursor replay,
a registered-secret signed webhook subscription, a checkpointed external CI
operation, journal-before-resume ordering, review, and compare-and-swap commit.

```bash
python examples/11-coding-agent-background-callbacks/run.py
```

The script runs authenticated server flows, callback resume, replay, and signed
delivery through fake CI, secret-resolver, and webhook transports. It sends no
webhook and starts no external CI job.

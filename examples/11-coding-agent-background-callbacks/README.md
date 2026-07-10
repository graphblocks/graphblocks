# Coding Agent with Background Callbacks

This application and graph define accepted background invocation, cursor replay,
a registered-secret signed webhook subscription, a checkpointed external CI
operation, journal-before-resume ordering, review, and compare-and-swap commit.

```bash
python examples/11-coding-agent-background-callbacks/run.py
```

The script validates identities and callback fences without sending a webhook or
starting CI. The production acceptance application performs authenticated server
flows and signed-delivery verification.

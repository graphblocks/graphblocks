# Verified RTL Workspace Trial

This application-local RTL fixture models a governed candidate mutation. It
reserves EDA leases, evaluates the candidate in a trial workspace, invalidates
review on subject change, combines checks in a gate, and commits through
compare-and-swap.

```bash
python examples/07-verified-rtl-workspace-trial/run.py
```

No EDA tool runs during validation. The governed-runtime acceptance application
executes the corresponding lease, review, and commit-authorization probes.

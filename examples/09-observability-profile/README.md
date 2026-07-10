# Observability Profile

This profile maps runtime events to OpenTelemetry and Langfuse projections while
keeping execution journal, audit, usage, and budget ledgers authoritative. Its
backpressure policy explicitly prevents exporter outage from changing run
correctness.

```bash
python examples/09-observability-profile/run.py
```

Recording OTel/Langfuse exporters exercise projection identity, failure/retry,
and authoritative-state immutability without exporting telemetry.

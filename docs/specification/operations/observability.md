# Observability and Telemetry

Execution journal, audit log, usage ledger, and budget ledger are authoritative
records. Traces, logs, metrics, dashboards, OpenTelemetry spans, and Langfuse
generations are projections. Export failure MUST have no effect on run outcome,
policy, usage, or budget correctness.

A telemetry correctness snapshot contains exactly the four authoritative record
families above and binds their canonical state. A `TelemetryExportOutbox` record
identity maps to one immutable canonical observation; conflicting reuse MUST
fail. Delivery status is tracked independently per exporter.

A failed exporter attempt leaves records pending and reports `run_impact: none`.
Retry delivers a pending record once to that exporter; a redundant retry sends
an empty batch. An exporter that mutates the authoritative snapshot or claims a
different canonical observation MUST raise a correctness violation.

Provider requests and generations require stable deduplication/link identities
so GraphBlocks, provider SDK instrumentation, and exporter SDKs do not count one
call multiple times. Sensitive values, prompts, tool arguments, and artifacts
MUST follow policy before export. Exporters receive references or redacted
projections when raw content is not authorized.

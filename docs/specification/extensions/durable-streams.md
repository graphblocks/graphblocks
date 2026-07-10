# Durable Streams Extension

A durable stream source emits records with stable source, partition, and offset
identity. Offsets are monotonic within a partition. Duplicate identical records
may replay; conflicting reuse of an offset MUST fail.

Watermarks describe event-time progress and MUST NOT move backwards. Operators
with state participate in checkpoint barriers. A completed checkpoint binds
source offsets, watermarks, operator state, physical plan, release, and ownership
fence before it becomes eligible for recovery.

Sink effects use prepare/commit/abort semantics or an equivalent idempotent
protocol. A sink commit MUST be bound to a completed checkpoint; failover MUST
not commit the same logical output twice. A stale coordinator or fencing token
cannot advance source ownership, checkpoint state, or sink commit.

Backpressure, retry, and resource limits MUST be explicit and bounded.
Cancellation defines whether the runtime drains to a checkpoint, aborts prepared
sinks, or terminates immediately. Durable recovery restores only compatible
release and physical-plan identities.

# Async Runs and Callbacks

Use an accepted or background invocation when a run should outlive the request
that starts it. The response returns a run identity and replay position; clients
can detach, attach again with a cursor, request cancellation, or register a
delivery projection.

Treat the application event stream as the source of truth. A webhook, local
callback, SSE connection, or WebSocket is a delivery target and may retry or
fail independently. Consumers must handle at-least-once delivery and reject
conflicting reuse of event or delivery identities.

External systems resume an `AsyncOperation` through an authenticated callback
or a poll result normalized into the same result model. Register secrets by
reference. The deployment-owned resolver supplies secret bytes to the webhook
dispatcher; API responses, journals, and dead-letter records retain only
secret-free identity and outcome evidence.

Callback acceptance is not execution resumption. The runtime records the valid
receipt durably first, then rechecks operation, attempt, provider operation,
deadline, policy, budget, release, and ownership fences before a worker claims
the checkpoint. Duplicate callbacks do not resume twice, and stale callbacks
cannot modify a newer attempt.

See the normative [application, async-run, and callback contract](../specification/operations/applications-async-callbacks.md).

# Admission Tickets and Overload Queues

Admission tickets let an accepted or background invocation return immediately
when a rate limit or concurrency permit is unavailable. The client receives a
stable ticket instead of holding a connection or waiting on a semaphore.

Admission is an application-boundary capability, not a graph node. A graph
MUST NOT start executing merely because its invocation was parsed or queued.
The authoritative run stream MUST emit `RunStarted` only after a worker owns a
current admission claim.

## Queue contract

An admission queue combines:

- a maximum number of concurrently claimed runs;
- a fixed-window admission-rate budget;
- a bounded FIFO pending queue; and
- a queued-ticket time to live.

An invocation supplies a run identity, an owner-scoped idempotency key, and a
positive number of admission units. Reusing the same owner and idempotency key
with the same run and units MUST return the original ticket without consuming
capacity again. Reusing it with different values MUST fail as a conflict.

When capacity and rate budget are available, the ticket begins as `admitted`.
Otherwise it begins as `queued` and the server returns HTTP 202 immediately.
If the bounded pending queue is full, the server rejects the invocation rather
than creating an untracked run. Rate budget is consumed on admission, not on
ticket creation. A concurrency slot is released only after the worker exits;
publishing cancellation while a worker is still unwinding MUST NOT promote a
replacement early.

The state machine is:

```text
queued -> admitted -> running -> completed
   |          |          |-----> failed
   |          |          |-----> cancelled
   |          |-----> cancelled
   |-----> cancelled
   |-----> expired
```

Queued tickets are promoted in sequence order. A queued ticket expires at its
TTL and MUST never execute. An admitted or running claim uses an internal,
monotonically increasing fencing token bound to the server-side owner identity.
Workers MUST present the current owner identity and fence when starting,
mutating, or completing work; stale or forged-owner claims fail closed.
The current owner MAY renew an active claim lease without changing the fencing
token, but renewal MUST extend the existing expiration and MUST fail after the
claim expires or another owner acquires a newer fence.
Fencing and owner data are server-internal and MUST NOT be exposed as client
capabilities.

## HTTP projection

Ticketed admission is opt-in. Without an admission queue, accepted/background
invocation retains the ordinary application protocol behavior. Synchronous
invocation is not ticketed.

A ticketed response adds `admissionTicket` to the normal 202 run handle:

```json
{
  "ok": true,
  "runId": "run-42",
  "status": "accepted",
  "initialCursor": "run-42:0",
  "eventStream": "/runs/run-42/events",
  "websocket": "/runs/run-42/ws",
  "cancel": "/runs/run-42/cancel",
  "admissionTicket": {
    "ticketId": "interactive-ticket-000042",
    "runId": "run-42",
    "limiterId": "interactive",
    "state": "queued",
    "units": 1,
    "sequence": 42,
    "stateVersion": 1,
    "issuedAtUnixMs": 1783641600000,
    "expiresAtUnixMs": 1783641660000,
    "queuePosition": 3,
    "retryAfterMs": 850,
    "startedAtUnixMs": null,
    "completedAtUnixMs": null
  }
}
```

`queuePosition` and `retryAfterMs` are snapshots, not reservations. Clients
MUST refresh ticket or run status before presenting them as current. Before
claim, the event stream is valid and empty at cursor 0. Run status projects
`state: queued` or `state: admitted`, `startedAt: null`, and a `waitingOn`
entry of kind `admission`. Cancellation of a queued ticket records
`RunCancelled` at sequence 1 and does not synthesize `RunStarted`.

## Session and screen projection

A session or screen is a projection of the ticket and run stream, not the
admission mechanism. A ticket screen SHOULD show ticket identity, run state,
limiter, queue position, retry delay, refresh, and cancel. It MAY transition to
the ordinary run session after `RunStarted`. Closing the screen MUST NOT cancel
the ticket unless the client explicitly sends cancellation.

The Python TUI package exposes `admission_ticket_screen(ticket)` for this pure
projection. It performs no network request and opens no session, so HTTP, CLI,
TUI, and other clients can share the same server-side admission semantics.

## Reference implementation and durability

The Python `AdmissionTicketQueue` and Rust `graphblocks-flow::ticket` queue are
thread-safe, process-local reference implementations. The Python server can
run a maintenance pass with `promote_admission_tickets()` and dispatch admitted
runs through its accepted-run executor. Deterministic clocks are injectable for
tests; maintenance never sleeps while holding the queue lock.

These reference queues do not survive process restart. A deployment claiming
restart-durable tickets MUST atomically persist the ticket, pending run
envelope, event stream position, idempotency record, and claim fence. Restoring
only the ticket is insufficient because a promoted worker would not have the
graph or authoritative run state needed to resume safely.

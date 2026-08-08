# Architecture

GraphBlocks separates portable contracts from the tools that implement them.

```text
authoring -> schema validation -> normalization/compiler -> physical plan
                                                        -> runtime/journal
application protocol <-> event stream/callback projections -> integrations
policy + budget + usage + review --------------------------^          |
release + deployment + observability ---------------------------------+
```

Graphs describe bounded typed computation. Bindings select providers, local
functions, worker targets, MCP or OpenAPI operations, and adapters. Applications
expose commands and event streams independently of graph authoring. Releases
freeze compatible artifacts; deployments place a release on real targets.

Policy, usage, budget, approvals, reviews, and leases are runtime admission
boundaries, not prompt hints. Observability exports projections of authoritative
records and must not determine run correctness.

The `graphblocks` distribution is the broadest Python surface today. It
includes the authoring SDK, built-in block registry, explicit reference
compiler/runtime, CLI, and framework-neutral server request/response contracts.
Its public compiler delegates to the separately installed
`graphblocks-runtime` Rust binding and fails closed when that binding is not
available. Rust is the normative Graph compiler and the selected target for
production execution authority; the remaining runtime transition is
release-blocking.
See [ADR-0001](../specification/decisions/0001-rust-normative-authority.md)
and the explicit differences in
[language support](../specification/conformance/language-support.md).

## Product boundary and core inclusion

GraphBlocks is a portable execution-contract toolkit. It does not attempt to
own the complete operational product around that contract. Explicit non-goals
are:

- a hosted orchestrator;
- a full API gateway;
- a secret manager;
- a generic ETL platform; and
- a full Kubernetes operator.

External systems provide hosting, credential custody, edge routing, general
data movement, and cluster reconciliation. They connect through versioned
bindings, framework-neutral request/response contracts, and independently
promoted extension profiles. An adapter may translate those contracts, but its
presence does not move the external system's lifecycle, availability, or
security policy into GraphBlocks core.

A proposal may enter core only when its ADR demonstrates all four conditions:

1. the capability is required for portable execution semantics;
2. two independent runtimes can implement it;
3. a provider-neutral TCK can verify it; and
4. it imposes no provider, database, server-framework, or deployment policy.

If any condition is not met, the proposal belongs in an extension profile,
adapter, example, or external project. New contract decisions use the
[ADR template](../specification/decisions/template.md), whose core-inclusion
section is mandatory even when the result is “not core.”

## Network adapter resource boundary

`graphblocks.server_adapter.ServerLimits` is the mandatory contract between an
HTTP framework and `GraphBlocksServerApp`. A conforming adapter applies these
limits before constructing or buffering a complete `ServerRequest`:

- raw header count and encoded header bytes;
- declared and streamed request-body bytes;
- concurrent requests held for the full request lifetime;
- a bounded per-tenant request window, including bounded tenant-bucket state;
- body idle timeout and total request deadline.

`ServerAdapterIngress` is the framework-neutral reference boundary and
conformance fixture. It accepts raw header pairs so duplicate names, header
bombs, ambiguous `Content-Length`/`Transfer-Encoding`, and oversized declared
bodies are rejected before body reads. It wraps chunked reads with cumulative
byte and deadline checks, then delegates to `GraphBlocksServerApp.handle_stream`;
the app's route-specific body caps remain an independent defense-in-depth
layer. Adapter rejections use 431 for header limits, 413 for body limits, 429
for a tenant rate limit, 503 for concurrency or rate-state exhaustion, and 408
for ingress deadline violations.

Production adapters must additionally configure their HTTP server's socket or
task cancellation at the same idle and total deadlines. The reference boundary
detects deadline overruns at read and completion boundaries, but cannot
preempt an arbitrary blocking framework callback or in-process handler by
itself. Tenant keys supplied to the ingress boundary must come from trusted
host/authentication routing metadata, never an unverified client header.

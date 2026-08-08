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

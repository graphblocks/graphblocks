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

The `graphblocks` distribution is the broadest reference surface today. It is
pure Python and includes the SDK, built-in block registry, reference runtime,
CLI, and framework-neutral server request/response contracts. The separately
installed `graphblocks-runtime` distribution exposes native Rust bindings. Rust
shares schema, compiler, core runtime, durable, and protocol contracts, with
explicit differences listed in
[language support](../specification/conformance/language-support.md).

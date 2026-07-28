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

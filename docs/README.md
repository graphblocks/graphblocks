# GraphBlocks Documentation

Don't reinvent wheel.

GraphBlocks is a contract toolkit for AI applications that already depend on
real providers, parsers, retrievers, databases, runtimes, and deployment
systems. It does not try to replace those tools. It defines the portable
contracts around them: graph shape, typed values, bindings, package metadata,
runtime behavior, policy, usage, budget, observability, and conformance.

## Start Here

- [Getting started](getting-started.md): install the development package and run
  the first validation commands.
- [Core concepts](concepts.md): understand graphs, bindings, applications,
  releases, deployments, policies, and packages.
- [Runtime model](runtime.md): see how GraphBlocks separates authoring,
  compilation, execution, journaling, cancellation, and callbacks.
- [Package model](packages.md): learn how the default package stays small while
  integrations remain optional.
- [Conformance and TCK](conformance.md): understand profile claims and the test
  fixtures that back them.

## Repository Map

```text
crates/      Rust compiler, runtime, protocol, telemetry, and native binding crates
packages/    Python distributions for foundation packages and optional adapters
schemas/     Versioned schema contracts
tck/         Shared conformance fixtures
acceptance/  Acceptance application inventory
deployment/  Deployment target contracts
docs/        User and maintainer documentation
```

The original v1.0 architecture bundle is kept unchanged in
`docs/upstream/GraphBlocks_v1.0_Final`. Treat it as the reference design record,
not as the day-to-day documentation entry point.

## Project Status

GraphBlocks is alpha software. The repository contains broad contract coverage
and many focused runtime slices, but compatibility should be claimed by
conformance profile, not by repository presence alone. See
[Conformance and TCK](conformance.md) before describing a package as compatible
with a given profile.

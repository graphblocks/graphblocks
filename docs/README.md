# GraphBlocks Documentation

This is the documentation entry point for the living GraphBlocks project. The
old mutable architecture bundle has been retired; active documents are
organized by audience and authority.

## Learn and use

- [Installation](getting-started/installation.md)
- [Quickstart](getting-started/quickstart.md)
- [Architecture](concepts/architecture.md)
- [Graphs and bindings](concepts/graphs-and-bindings.md)
- [Runtime](concepts/runtime.md)
- [Packages](concepts/packages.md)
- [Async runs and callbacks guide](guides/async-runs-and-callbacks.md)
- [Deployment guide](guides/deployment.md)

## Implement and verify

- [Specification index](specification/README.md)
- [Conformance and TCK](development/conformance.md)
- [Testing](development/testing.md)
- [Language support](specification/conformance/language-support.md)
- [Implementation status](project/status.md)
- [Roadmap](project/roadmap.md)

## Document authority

When sources disagree, use this order:

1. `schemas/` defines normative wire shapes.
2. `docs/specification/` defines normative semantics.
3. `tck/` and `acceptance/` define executable conformance.
4. `src/graphblocks/data/` defines the shipped artifact and component catalogs;
   component identities are not Python distribution names.
5. Guides and examples are non-normative.

The implementation may support only part of a normative contract. Conformance
claims must cite a profile and passing evidence; see
[language support](specification/conformance/language-support.md).

## Repository map

```text
acceptance/  Executable application manifest and scenarios
crates/      Rust schemas, compiler, runtimes, protocols, and bindings
deployment/  Deployment target contracts
docs/        Guides, specification, development, and project status
packages/    Auxiliary release roots; Python wheels are native runtime and TCK
profiles/    Project-level policy profile assets
schemas/     Versioned wire-shape contracts
src/         Python authoring and reference implementation
tck/         Shared conformance fixtures
```

The Python distribution surface is deliberately smaller than the package
catalog: `graphblocks` contains the pure-Python SDK, built-ins, CLI, and server
contracts; `graphblocks-runtime` contains native bindings; and
`graphblocks-testing` contains the TCK tooling.

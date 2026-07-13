# Packages

In GraphBlocks contracts, a package is a logical capability and ownership
identity. Package metadata can declare dependencies, schema assets, blocks,
bindings, and conformance claims. Static discovery must not import a provider or
other heavy third-party SDK.

These identities are not one-to-one with Python distributions. The supported
Python distribution surface is:

- `graphblocks`: the pure-Python SDK, built-in implementations, reference
  runtime, CLI, and framework-neutral server contracts.
- `graphblocks-runtime`: the optional native runtime bindings.
- `graphblocks-testing`: the TCK library and `graphblocks-tck` command.

The catalog also describes the non-Python `graphblocks-operator` release
artifact. It does not count as a fourth Python distribution.

Built-in and integration identities remain independently discoverable in the
catalog so graphs and locks can refer to stable capabilities. They are shipped
as part of `graphblocks`, not as dozens of separately installed feature wheels.

An optional extra is appropriate only when it adds a real install dependency.
For example, `graphblocks[runtime]` adds the separately built native extension,
`graphblocks[pdf]` adds `pypdf`, and `graphblocks[test]` adds pytest. Extras do
not select catalog identities or move built-ins, CLI commands, or server
contracts into separate feature wheels. The native bindings and TCK remain
explicit distributions because they have distinct build and release contracts.

Package-lock and doctor checks validate catalog dependency closure and direct
references. A package identity may claim only profiles backed by applicable TCK
and acceptance evidence.

Python distribution names use PEP 503 identity rules throughout this surface.
For example, `example-wheel`, `example_wheel`, and `example.wheel` identify the
same distribution and cannot appear as separate artifacts, lock artifact
identities, or wheel targets. Component lock entries are logical capability
identities instead: their spelling remains exact. If a requested value is both
an exact component name and an alias of an artifact, the exact component match
takes precedence.

`graphblocks packages doctor --root` and `graphblocks packages wheel-matrix
--root` treat the supplied root as a security boundary. Catalog manifests must
stay within it; absolute paths, `..` escapes, and symlinks resolving outside the
root are rejected. The reader uses descriptor-relative traversal where the
platform provides it and a fail-closed component/file-identity check otherwise,
including Windows reparse-point rejection. Final file opens are non-blocking so
a raced FIFO or other special file is rejected rather than stalling validation.

The canonical machine-readable package catalog is
`src/graphblocks/data/package-catalog.yaml`. See the normative
[packages and plugins specification](../specification/core/packages-and-plugins.md).

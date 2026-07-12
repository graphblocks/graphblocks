# Implementation Status

GraphBlocks is source-tree alpha software. The Python implementation provides
the broad reference surface, and Rust implements canonical schema/compiler,
core runtime, protocol, durable, and selected extension contracts.

The Python release surface is consolidated into three distributions:
`graphblocks` for the pure-Python SDK, built-ins, CLI, and server contracts;
`graphblocks-runtime` for native bindings; and `graphblocks-testing` for the
TCK. Package catalog component entries remain capability and binding identities,
not separately published wheels.

The consolidated checkout is verified by 2,457 Python tests, the complete Rust
workspace formatting/strict-Clippy/test gates, all 42 acceptance gates, and a
fresh no-index wheelhouse install of the three Python distributions followed by
`pip check`.

Current executable coverage includes all 42 gates in the ten-application
acceptance manifest: documents, parser fallback, ACL propagation, RAG citations
and abstention, conversation CAS/drafts, accepted runs and signed callbacks,
bounded orchestration, governed workspace commits, release/canary/rollback,
provider-authoritative voice behavior, and telemetry outage correctness.

Many integrations are still lightweight contracts, built-ins, and test doubles
rather than production-ready external adapters. Optional extras add concrete
dependencies such as the native runtime, `pypdf`, or pytest; they do not change
the three-distribution release boundary.

Python-only advanced reference contracts are listed in
[language support](../specification/conformance/language-support.md).

`graphblocks-native` can validate, plan, and execute a single JSON or YAML graph
without Python, and can select a named graph from multi-document YAML, using the
Rust stdlib runtime. It does not yet inject arbitrary integration adapters.
Despite its name,
`graphblocksd` is currently a one-shot worker control-plane CLI for worker
admission and SQLite checkpoint claim lifecycle operations rather than a
long-running HTTP server.

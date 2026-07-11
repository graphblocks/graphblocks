# Implementation Status

GraphBlocks is source-tree alpha software. The Python implementation provides
the broad reference surface, and Rust implements canonical schema/compiler,
core runtime, protocol, durable, and selected extension contracts.

Current executable coverage includes all 42 gates in the ten-application
acceptance manifest: documents, parser fallback, ACL propagation, RAG citations
and abstention, conversation CAS/drafts, accepted runs and signed callbacks,
bounded orchestration, governed workspace commits, release/canary/rollback,
provider-authoritative voice behavior, and telemetry outage correctness.

This does not mean every package manifest is a published or production-ready
adapter. Many integrations are lightweight contracts, package boundaries, and
test doubles. The monorepo's `0.1.0` package versions and some historical
inter-package `~=1.0` constraints still need publication reconciliation.

Python-only advanced reference contracts are listed in
[language support](../specification/conformance/language-support.md).

`graphblocks-native` can validate, plan, and execute a single JSON graph without
Python, using the Rust stdlib runtime. It does not yet load YAML/multi-document
examples or inject arbitrary integration adapters. Despite its name,
`graphblocksd` is currently a one-shot worker control-plane CLI rather than a
long-running HTTP server.

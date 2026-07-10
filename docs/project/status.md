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

Known parity limitation: Rust voice coverage does not yet match the Python
provider-confirmation and playback acknowledgement acceptance contract. Other
Python-only advanced reference contracts are listed in
[language support](../specification/conformance/language-support.md).

# GraphBlocks Living Specification

Status: alpha, version `v1alpha1`.

This specification defines portable semantics for GraphBlocks implementations.
The words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are to
be interpreted as described by RFC 2119 and RFC 8174 when capitalized.

Wire shapes in `schemas/` take precedence over prose. The TCK and acceptance
applications determine executable conformance. An implementation may implement
a subset identified by a profile; repository presence is not a compatibility
claim.

## Core contracts

- [Canonical data model](core/canonical-data-model.md)
- [Graph, compiler, and runtime](core/graph-compilation-runtime.md)
- [Packages and plugins](core/packages-and-plugins.md)
- [Tools and output policy](core/tools-and-output-policy.md)

## AI application contracts

- [Documents and retrieval](ai/documents-and-retrieval.md)
- [Conversations and tools](ai/conversations-and-tools.md)

## Governance

- [Policy, budget, usage, and evaluation](governance/policy-budget-evaluation.md)
- [Workspace trials and governed commits](governance/workspace-trials.md)

## Operations

- [Applications, async runs, and callbacks](operations/applications-async-callbacks.md)
- [Admission tickets and overload queues](operations/admission-tickets.md)
- [Release and deployment](operations/release-deployment.md)
- [Observability and telemetry](operations/observability.md)

## Extensions

- [Bounded orchestration](extensions/orchestration.md)
- [Realtime voice](extensions/realtime-voice.md)
- [Durable streams](extensions/durable-streams.md)

## Conformance and reference

- [Conformance profiles](conformance/profiles.md)
- [Acceptance applications](conformance/acceptance-applications.md)
- [Language support](conformance/language-support.md)
- [Versioning and diagnostics](reference/versioning-and-diagnostics.md)
- [Architecture decisions](decisions/README.md)

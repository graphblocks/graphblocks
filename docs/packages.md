# Package Model

GraphBlocks uses small packages with explicit responsibilities. The default
installation should be useful for authoring, validation, local runtime work, and
provider-neutral contracts without pulling in every integration SDK.

## Default Install

The default metapackage is intended to depend on foundation packages such as:

- `graphblocks-core`
- `graphblocks-runtime`
- `graphblocks-stdlib`
- `graphblocks-documents`
- `graphblocks-rag`
- `graphblocks-conversation`
- `graphblocks-policy`
- `graphblocks-budget`
- `graphblocks-usage`
- `graphblocks-cli`

It should not include model provider SDKs, vector database clients, parser SDKs,
OCR engines, web servers, Kubernetes clients, Terraform tooling, voice stacks,
or external policy engines unless an explicit integration package asks for them.

## Foundation Packages

Foundation packages define provider-neutral contracts and local development
implementations. They should be safe to install in ordinary development
environments.

## Extension Packages

First-party extensions add capabilities such as agents, evaluation,
orchestration, review, workspace behavior, server adapters, deployment tooling,
observability exporters, dashboards, and TUI surfaces. They are versioned and
claimed separately from the foundation release train.

## Integration Packages

Integration packages connect GraphBlocks contracts to existing tools. Examples
include provider adapters, parser adapters, vector database adapters, external
policy engines, durable ledger backends, and observability exporters.

This is where "Don't reinvent wheel." matters most: integrations should wrap
good existing tools behind explicit contracts instead of duplicating their
domains inside GraphBlocks.

## Package Hygiene

Package metadata should support static discovery without importing heavy SDKs.
Package locks and doctor checks should reject forbidden dependency closure
problems, including transitive dependencies and direct-reference dependency
forms that bypass normal naming.

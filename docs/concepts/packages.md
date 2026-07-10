# Packages

GraphBlocks packages declare ownership, dependencies, capabilities, schema
assets, blocks, and conformance claims. Static metadata discovery must not import
heavy provider SDKs.

Foundation packages define provider-neutral contracts and local development
implementations. Extension packages add orchestration, evaluation, workspace,
server, deployment, observability, TUI, voice, or durable behavior. Integration
packages connect those contracts to existing providers, parsers, databases,
policy engines, transports, and cloud systems.

The default authoring package intentionally avoids optional SDKs. Package-lock
and doctor checks validate dependency closure, including transitive and direct
reference forms. A package may claim only profiles backed by applicable TCK and
acceptance evidence.

The canonical machine-readable package catalog is
`src/graphblocks/data/package-catalog.yaml`. See the normative
[packages and plugins specification](../specification/core/packages-and-plugins.md).

# Core Concepts

GraphBlocks is built around explicit contracts. The goal is to keep application
behavior portable while letting teams choose the implementation tools behind
each boundary.

## Graph

A graph describes typed nodes, ports, values, and edges. It is static enough to
compile, normalize, hash, and test. Dynamic work belongs in bounded runtime
constructs such as sequences, task plans, async operations, or callbacks.

## Binding

A binding connects abstract graph blocks to concrete implementations. This is
where a provider, local function, worker target, MCP server, OpenAPI operation,
or adapter package can be selected without changing the graph contract.

## Application

An application exposes commands, routes, event streams, callback endpoints, and
client-facing protocol behavior. It is separate from the graph so the same graph
can run behind different surfaces.

## Release

A release freezes a compatible set of graph, binding, package, policy, prompt,
schema, and index artifacts. Production runs should record release identity and
plan hashes in provenance.

## Deployment

A deployment maps a release to real targets: local processes, worker pools,
Kubernetes workloads, cloud resources, or other infrastructure. Deployment
contracts are deliberately separate from graph authoring.

## Policy, Usage, and Budget

GraphBlocks treats policy, usage, and budget as runtime-plane concerns. They are
not prompt hints and they are not optional observers. The runtime contract
models policy enforcement points, usage reconciliation, budget permits,
exhaustion behavior, approvals, reviews, and audit records.

## Package

Packages declare what they own and what they depend on. The default package is
provider-neutral and avoids heavy optional SDKs. Integrations should be explicit
packages with explicit extras or dependencies.

## Conformance Profile

A conformance profile is the smallest useful compatibility claim. A package can
support schema contracts without supporting production deployment, voice, or
durable stream semantics. Profile claims must be backed by TCK fixtures.

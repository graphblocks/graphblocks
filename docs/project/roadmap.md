# Roadmap

The [first stable release boundary](first-stable-release.md) makes C0 and the
pure-Python C1 implementation the initial stable compatibility promise. C2-C4,
native execution, the operator, and X1-X3 remain active stabilization tracks;
the boundary sequences their promotion rather than removing them from scope.

Near-term priorities are:

1. Stabilize metadata, compatibility policy, and release automation for the
   consolidated `graphblocks`, `graphblocks-runtime`, and `graphblocks-testing`
   artifacts.
2. Close Python/Rust parity gaps for orchestration limits, governed workspace
   commits, deployment evidence, telemetry outbox behavior, webhook delivery,
   and provider-authoritative voice.
3. Complete end-to-end restart-durable accepted-run recovery and deployment-like
   remote-worker coverage on top of the existing fenced checkpoint claim,
   renewal, and completion primitives.
4. Promote the closed core Graph and PluginManifest wire contracts to
   `graphblocks.ai/v1`, with explicit migrations from the alpha resources.
5. Freeze registered diagnostics, the stable Python/CLI surface, compatibility
   policy, and installed-artifact release evidence for 1.0.
6. Expand adapter integration tests while keeping external SDKs behind explicit
   dependency extras and out of the default `graphblocks` install.

Roadmap items are non-normative. A feature becomes supported only when its
specification, implementation, fixtures, and required acceptance evidence agree.

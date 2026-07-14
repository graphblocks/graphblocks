# Remaining Work

This note tracks follow-up work after the MVP implementation and package
consolidation. It distinguishes verified MVP evidence from later production
integration work.

## MVP verification completed

- The built-in modules and provider adapters are consolidated under
  `graphblocks`; the retired feature distributions have been removed.
- The release boundary is exactly `graphblocks`, `graphblocks-runtime`, and
  `graphblocks-testing`. The operator remains a non-Python catalog artifact.
- The complete Python suite passes with 2,625 tests.
- Rust formatting, strict workspace Clippy, and all workspace/all-target tests
  pass.
- All ten acceptance applications and 42 declared gates pass.
- A fresh wheelhouse builds and installs exactly the three Python distributions
  without an index, and the installed environment passes `pip check` and schema
  verification.
- Status, installation, package-model, testing, and changelog documentation now
  describe the consolidated boundary.

## After MVP

The machine-readable [stable release matrix](stable-release-matrix.yaml) is the
authority for the 1.0 boundary and unmet gates. Immediate stable-core work is:

1. Close and promote the Graph and PluginManifest resources to
   `graphblocks.ai/v1`, retain golden-tested alpha migrations, and make all
   stable readers and compilers fail closed.
2. Finish the stable Python/CLI surface and diagnostic registries, then enforce
   API, CLI, canonical-byte/hash, schema, and diagnostic snapshots.
3. Run digest-bound C0/C1 conformance from installed artifacts across the
   supported platform matrix and build the signed release evidence described in
   the [release boundary](first-stable-release.md).

The continuing stabilization tracks are:

1. Replace remaining lightweight adapter contracts or test doubles with
   production external integrations only where required by a release target.
2. Extend `graphblocks-native` adapter injection beyond the current stdlib
   runtime path.
3. Decide whether `graphblocksd` should remain a one-shot control-plane CLI for
   alpha or become a long-running HTTP service in the next milestone.
4. Expand restart-durable accepted-run recovery and remote-worker claim coverage
   in deployment-like tests.

These later tracks promote C2-C4, native, operator, and X1-X3 surfaces; their
preview classification is sequencing, not abandonment.

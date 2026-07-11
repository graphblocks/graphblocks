# Remaining Work

This note tracks work that remains after the MVP implementation pass. These
items are release and integration follow-ups, not known blockers in the Rust
runtime-core callback slice verified in `1363a3b`.

## Before declaring the MVP done

1. Integrate and commit the outstanding Python/package-layout work from the
   final implementation pass.
2. Run the full repository verification matrix after that integration lands:
   Rust workspace checks, Python tests, package-layout tests, acceptance
   manifests, wheel/build checks, and CLI smoke tests.
3. Confirm the consolidated distribution boundary is stable:
   `graphblocks`, `graphblocks-runtime`, and `graphblocks-testing`.
4. Refresh release evidence: status docs, compatibility notes, changelog or
   release notes, and any generated package metadata.

## After MVP

1. Replace remaining lightweight adapter contracts or test doubles with
   production external integrations only where required by a release target.
2. Extend `graphblocks-native` adapter injection beyond the current stdlib
   runtime path.
3. Decide whether `graphblocksd` should remain a one-shot control-plane CLI for
   alpha or become a long-running HTTP service in the next milestone.
4. Expand restart-durable accepted-run recovery and remote-worker claim coverage
   in deployment-like tests.

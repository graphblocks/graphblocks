# Conformance and TCK

Shared fixtures live under `tck/`. Use the separately packaged
`graphblocks-testing` tools when working on profile inventory, fixtures, and
acceptance applications:

```bash
graphblocks-tck list tck
graphblocks-tck check tck \
  --profiles src/graphblocks/data/conformance-profiles.yaml \
  --profile GB-C3-GOVERNED-RUNTIME
graphblocks-tck run-all tck
graphblocks-tck run-acceptance acceptance/applications.yaml --root . --json
```

The last command executes all ten applications and 42 declared gates through
the exact-name built-ins in `graphblocks`. It emits digest-bound evidence and
fails closed for unknown gates, missing optional install dependencies,
malformed scenarios, or stale identity. The
`graphblocks-testing[runtime]` extra adds the actual `graphblocks-runtime`
dependency for native-profile work; it does not assemble catalog identities or
historical feature wheels.

Add the narrowest applicable positive and negative fixture for a semantic
change. Include replay, cancellation, invalid identity, policy rejection,
boundary, and dependency-closure cases where relevant. Update the canonical
profile catalog only when the implementation and required evidence are ready.

See the normative [profile](../specification/conformance/profiles.md) and
[acceptance](../specification/conformance/acceptance-applications.md) contracts.

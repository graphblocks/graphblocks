# ADR-0001: Rust Normative Compiler and Target Execution Authority

- Status: Accepted
- Date: 2026-07-28
- Decision owners: GraphBlocks maintainers
- Release gate: `REL-NORMATIVE-AUTHORITY`

## Context

GraphBlocks has independent Python and Rust implementations of graph migration,
normalization, validation, diagnostics, canonical plan identity, and runtime
behavior. Shared TCK fixtures reduce drift, but they cannot make two evolving
implementations equally authoritative. Differences in diagnostic rendering,
catalog selection, numeric bounds, and invalid-schema handling demonstrated
that a dual-authority model creates permanent synchronization cost and an
ambiguous production contract.

Python remains the strongest authoring environment and the broadest executable
reference. Rust provides the bounded, fallible implementation boundary needed
for a portable compiler and production execution plane. Authority therefore
must be assigned by phase instead of inferred from package names or from which
implementation currently covers more optional features.

## Decision

Rust is the selected normative authority for portable graph compilation and the
target normative authority for canonical identity, runtime protocol, and the
production scheduler. Python is the authoring facade, schema-facing SDK,
deterministic reference compiler/interpreter, and TCK oracle.

The transition is phase-scoped:

| Boundary | Selected authority | Current state |
| --- | --- | --- |
| Graph decode, migration, normalization, catalog resolution, type checking, lowering, diagnostics, plan serialization, and plan hash | Rust | Enforced by the public Python `compile_graph` entry point through `graphblocks-runtime` |
| Python graph builders, typed authoring, YAML composition, and ergonomic schema APIs | Python | Supported authoring surface; materializes portable resources for the normative compiler |
| Reference compiler and local reference interpreter | Python | Explicit oracle through `graphblocks.compiler.compile_graph_reference` and reference-runtime/TCK imports; never an implicit production fallback |
| Standalone canonical/schema utility authority | Rust | Selected, but remaining public-facade routing and installed differential evidence still block the release gate |
| Runtime protocol and production scheduler | Rust | Selected, but scheduler, suspension, crash/restart, fencing, and protocol-handshake evidence still block the release gate |
| AI application, governance, durable stream, voice, deployment, observability, and integrations | Profile-specific | No authority or stability is implied until the named extension profile passes its own gates |

The Python API follows these rules:

1. `graphblocks.compile_graph` and `graphblocks.compiler.compile_graph` invoke
   the Rust compiler through the native binding.
2. If the binding is absent, incomplete, or reports that its extension is not
   available, compilation fails closed with
   `NativeCompilerUnavailableError`.
3. The public compiler does not silently call the Python implementation.
4. A caller that intentionally needs the deterministic reference oracle must
   call `graphblocks.compiler.compile_graph_reference` explicitly.
5. Authoring and reference-runtime workflows may be installed without the
   native wheel, but normative compilation and compiler-backed CLI commands
   require `graphblocks-runtime`, normally through `graphblocks[runtime]`.

Selecting an authority is not itself a compatibility promotion. The native
binding artifact and C0/C1 claims remain blocked until their artifact,
supported-wheel, protocol, differential, and release-evidence gates pass.
Rust implementation crates remain internal APIs unless separately promoted.

## Consequences

- Plan hashes and compiler diagnostics have one production source of truth.
- Python/Rust differential tests become migration and regression evidence, not
  evidence for two coequal production implementations.
- Missing native support is visible at the call boundary instead of changing
  semantics by environment.
- The base Python distribution can still serve authoring and explicit reference
  use cases, while users of the public compiler must install a supported native
  wheel.
- A Rust compiler regression cannot be masked by a Python fallback; release
  gates must catch it.
- Python reference behavior remains valuable and supported for TCK development,
  portability checks, and diagnosis, but does not authorize a production
  compatibility claim.

## Migration

1. Route the public Python compiler through a strict native `Plan` bridge and
   retain `compile_graph_reference` as an explicit oracle. This step is
   implemented.
2. Preserve exact Python/Rust compiler parity for shared cases, including full
   diagnostic tuples, resource limits, malformed schemas, and canonical
   numeric boundaries. Compiler TCK differential coverage is implemented and
   remains a required gate.
3. Complete standalone canonical/schema authority routing, binding protocol and
   capability negotiation, supported native wheels, and installed-artifact
   evidence.
4. Move the production scheduler and durable authority checks behind the Rust
   runtime protocol while retaining the Python local runtime as a reference
   interpreter.
5. Extract the reusable control-plane library so language bindings do not
   depend on the daemon binary/control-plane layer. This step is implemented:
   `graphblocks-python` and the `graphblocksd` binary both consume the
   `graphblocks-control-plane` library target.

Until steps 3 and 4 are complete, `REL-NORMATIVE-AUTHORITY` remains blocked and
the project must not describe the entire native runtime or C1/C4 production
plane as stable.

## Conformance impact

- Compiler conformance compares Python reference and Rust results across the
  complete shared compiler TCK, including code, severity, path, message,
  normalized graph, and hash.
- Public-entry-point tests prove native dispatch and absence of implicit
  reference fallback.
- Installed-wheel tests must execute the same native compiler artifact that is
  named in release evidence.
- C0/C1 reports must identify the phase-scoped implementation roles rather than
  use the ambiguous implementation name `python-reference` for the whole
  profile.
- Any future authority change requires a superseding ADR and corresponding
  artifact, profile, language-support, traceability, and release-gate updates.

## Rejected alternatives

- **Keep Python normative.** This preserves the duplicate Rust compiler as a
  permanently secondary implementation and does not establish the selected
  production execution plane.
- **Keep Python and Rust coequal.** TCK coverage reduces but cannot eliminate
  ambiguity in new corner cases, diagnostic order, resource ceilings, or plan
  identity.
- **Use an implicit Python fallback.** The same program would produce authority
  decisions from different implementations depending on installation state,
  so missing native support would fail open at the implementation boundary.

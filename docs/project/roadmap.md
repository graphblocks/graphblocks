# Roadmap

GraphBlocks is under a security and stabilization freeze following a 99-finding
deep audit (4 P0, 23 P1, 64 P2, and 8 P3). The detailed sequencing, acceptance
evidence, and 1.0 exit conditions are in the
[deep audit remediation plan](audit-remediation-plan.md). The machine-readable
[stable release matrix](stable-release-matrix.yaml) remains the authority for
release readiness, and the
[remediation map](audit-remediation-map.yaml) assigns all 99 original finding
IDs to one primary workstream.

Work proceeds in this order:

1. **Days 0-7 — security freeze.** Make protected routes fail closed; persist
   immutable tenant and owner identity; enforce object authorization on all
   read, control, callback, subscription, acknowledgement, and delivery paths;
   replace permissive policy coercion with exact decoding; and impose request,
   response, schema, regex, and canonical-number limits. Exit with zero open
   P0 findings and the original reproductions in regression coverage.
2. **Weeks 2-4 — storage and resource model.** Introduce tenant-scoped
   repositories and atomic owner/version/lease/fence/idempotency transactions;
   deliver restart-durable accepted runs; add retention and pagination; make
   outbox claims atomic; bound journal, budget, schema, and canonical work; and
   return Rust errors instead of panicking at public boundaries.
3. **Months 1-2 — implementation boundaries.** Split server middleware, routes,
   services, and repositories; split CLI commands and exact codecs; make
   compiler phases and TCK runners independently testable; reduce the Python
   root surface and import cost; complete the accepted, 1.0-blocking transition
   through the native-first compiler, canonical/SchemaId, and resource
   validation/migration authority slices, then complete the remaining
   production execution authority; and correct the Rust control-plane
   dependency direction.
4. **Stable-candidate closure.** Reach zero open P0/P1 findings, enforce the
   authorization/adversarial/differential/resource/performance/restart/security
   matrices, complete the separately defined macOS and native-wheel smoke gate,
   reconstruct all nine reproduced findings, bind the source/evidence
   provenance and live inventory, rerun on supported Python and pinned Rust, and
   pass an independent API and security review on the unchanged release
   candidate.

The closed `graphblocks.ai/v1` Graph and PluginManifest resources and their
alpha migrations are already candidate-enforced; they are no longer future
promotion work. The compatibility promise remains blocked until every
applicable audit and release gate passes.

The target architecture keeps schema, compiler, runtime core, protocol, and
testing portable. AI application, governance, durable execution, voice,
deployment, observability, and external integrations advance as separately
verified extension profiles. Domain examples remain examples unless a repeated,
provider-neutral pattern earns core or profile status.

Roadmap items are non-normative. A feature becomes supported only when its
specification, implementation, fixtures, and required acceptance evidence
agree.

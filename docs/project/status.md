# Implementation Status

GraphBlocks is pre-1.0 release-candidate software. Python provides the broad
authoring and explicit reference surface. Rust is the normative Graph compiler
and the selected target for production execution authority; the compiler
transition is implemented, while canonical-facade, scheduler,
protocol-handshake, and durable execution evidence still block the complete
authority gate.

The 99-finding deep-audit baseline includes 4 P0 and 23 P1 findings. Closure
evidence has not yet satisfied the release matrix, so security remediation now
precedes feature growth and 1.0 remains blocked. In particular, the current
server must not be represented as a production-ready multi-tenant authority
boundary. See the [deep audit remediation plan](audit-remediation-plan.md).

The intended 1.0 scope and its unmet gates are recorded in the
[first stable release boundary](first-stable-release.md). The closed
`graphblocks.ai/v1` core wire, explicit alpha migrations, compatibility
snapshots, and candidate enforcement are implemented. Independent review,
release-candidate soak, first-stable upgrade-exemption evidence, protected-ref
signing, and authorized publish/rollback rehearsal remain before the stable
tag.

The Python release surface is consolidated into three distributions:
`graphblocks` for the Python authoring/reference SDK, built-ins, CLI, and server
contracts; `graphblocks-runtime` for the normative compiler binding and native
runtime; and `graphblocks-testing` for the TCK. Package catalog component
entries remain capability and binding identities, not separately published
wheels.

Revision-specific test, wheelhouse, and release-gate results are commit-bound CI
facts and are not maintained as prose on this page. The remediation plan
requires generating status projections from the existing release matrix,
catalogs, package metadata, and digest-bound CI evidence.

The ten-application acceptance manifest declares 42 gates covering documents,
parser fallback, ACL propagation, RAG citations and abstention, conversation
CAS/drafts, accepted runs and signed callbacks, bounded orchestration, governed
workspace commits, release/canary/rollback, provider-authoritative voice
behavior, and telemetry outage correctness. CI evidence, rather than this page,
records whether they pass for a revision.

Every currently catalogued external integration is classified as
`contract-only`; scripted providers, repository fakes, and acceptance harness
adapters are `test-double`. There are no `real-adapter` claims. The
[release matrix](stable-release-matrix.yaml) records authentication, supported
service or SDK versions, retry/failure ownership, real-service evidence, and
the promotion gate for every entry. A real adapter additionally requires
revision/run-bound test output and an integration-workflow Sigstore
attestation for every claimed authentication/version pair, consumed by final
promotion. Optional extras add concrete
dependencies such as the native binding, `pypdf`, or pytest; they do not change
an integration's maturity or the three-distribution release boundary. The
native extra is optional for authoring/reference use but required by the public
normative compiler.

Python-only advanced reference contracts are listed in
[language support](../specification/conformance/language-support.md).

`graphblocks-native` can validate, plan, and execute a single JSON or YAML graph
without Python, and can select a named graph from multi-document YAML, using the
Rust stdlib runtime. It does not yet inject arbitrary integration adapters.
The reusable `graphblocks-control-plane` library is consumed by both the Python
binding and the `graphblocks-control` binary, so the binding does not depend on
an executable package. `graphblocks-control` is deliberately a one-shot
local-process CLI for worker admission and SQLite-backed run, callback,
delivery, and checkpoint lifecycle operations. Requests use argv options; only
`admit-worker-message`, `submit-async-callback`, and
`quarantine-async-callback` read JSON payloads from stdin. Success is JSON on
stdout and errors are JSON on stderr. It is not a long-running HTTP server and
has no `serve` command or network listener.

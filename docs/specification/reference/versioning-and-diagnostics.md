# Versioning and Diagnostics

The project-wide stable-release rules are defined in the
[compatibility and deprecation policy](compatibility-policy.md). The canonical
machine-readable allocation list is
[`diagnostic-codes.yaml`](diagnostic-codes.yaml). This page defines the resource
and diagnostic semantics that implementations must preserve.

## Resource versions

<a id="GB-VER-RESOURCE-VERSIONS-001"></a>

GraphBlocks resources use explicit API versions. The candidate-stable C0/C1
`Graph` and `PluginManifest` resources use `graphblocks.ai/v1`. Preview graph
features remain on `graphblocks.ai/v1alpha3`; application, binding, and other
specialized manifests use the version declared by their matching schema or
specification, including `graphblocks.ai/v1alpha1` contracts. Readers MUST
reject unknown `apiVersion`/`kind` pairs unless an explicit migration exists.

## Compatibility changes

Breaking wire or semantic changes require a new API version or artifact major
version with a documented migration. Before 1.0, a change to an alpha resource
still requires a migration when the repository has emitted or persisted the old
form. Additive optional fields must define defaults and canonicalization
behavior. Removing or changing a TCK invariant is a compatibility change even
if a schema remains valid.

## Alpha migration boundary

<a id="GB-VER-ALPHA-MIGRATION-001"></a>

The existing alpha resources are a conformance and migration base, not the 1.0
stable wire promise. An alpha Graph migrates to `graphblocks.ai/v1` only when
every field has a representation in the closed C0/C1 schema. Preview-only
fields make the public migration fail with `GB0002`; preview compilation may
continue under the original alpha version, but MUST NOT relabel that graph as
stable. The [first stable release
boundary](../../project/first-stable-release.md) defines the exact promise.

## Diagnostic contracts

Diagnostics contain a stable code, severity, message, and path where
applicable. Codes are machine contracts; messages may improve without changing
meaning. Validation and planning collect independent diagnostics in
deterministic order. Execution errors also retain run/node/attempt and causal
identity where available.

New public diagnostics must allocate an entry in the registry in the same
change that emits them. An entry records its owner, profile, tier, default
severity, and one invariant-level meaning. Codes are never reused. Preview
named diagnostics that do not match `GB` plus four digits must receive a numeric
allocation before their emitting surface can become stable.

<a id="GB-VER-DIAGNOSTIC-SEVERITY-001"></a>

Implementations MUST NOT downgrade an authorization, integrity, schema,
identity, or conformance failure into an informational diagnostic. JSON output
must remain suitable for automation and must not contain secrets.

# Versioning and Diagnostics

GraphBlocks resources use explicit API versions. The current `Graph` resource is
`graphblocks.ai/v1alpha3`; application, binding, plugin, and specialized
manifests use the version declared by their matching schema or specification,
including `graphblocks.ai/v1alpha1` contracts. Readers MUST reject unknown
`apiVersion`/`kind` pairs unless an explicit migration exists.

Breaking wire or semantic changes require a new API version, profile revision,
or pre-1.0 release change with a documented migration. Additive optional fields
must define defaults and canonicalization behavior. Removing or changing a TCK
invariant is a compatibility change even if a schema remains valid.

Diagnostics contain a stable code, severity, message, and path where
applicable. Codes are machine contracts; messages may improve without changing
meaning. Validation and planning collect independent diagnostics in
deterministic order. Execution errors also retain run/node/attempt and causal
identity where available.

Implementations MUST NOT downgrade an authorization, integrity, schema,
identity, or conformance failure into an informational diagnostic. JSON output
must remain suitable for automation and must not contain secrets.

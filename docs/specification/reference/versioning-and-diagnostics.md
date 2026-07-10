# Versioning and Diagnostics

GraphBlocks resources use explicit API versions. The current alpha resource
family is `graphblocks.ai/v1alpha1`; specialized manifests use their documented
versioned namespaces. Readers MUST reject unknown versions unless an explicit
migration exists.

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

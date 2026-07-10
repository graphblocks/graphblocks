# Canonical Data Model

GraphBlocks resources use a versioned envelope with `apiVersion`, `kind`,
`metadata`, and `spec`. Required fields and resource-specific shapes are defined
by the matching schema under `schemas/`.

## Values and identity

An implementation MUST preserve the distinction among null, boolean, integer,
floating-point, string, bytes/artifact reference, list, and mapping values.
Schema validation MUST reject values that cannot be represented by the declared
port or field type.

Canonical JSON MUST be deterministic across mapping order and presentation
format. Identity digests MUST use the canonical byte representation and name
their algorithm; GraphBlocks SHA-256 identities use the `sha256:<hex>` form.
Digest inputs MUST exclude fields explicitly declared as signatures or computed
identities and MUST NOT silently coerce malformed persisted data.

## Versions and migration

Readers MUST reject unsupported `apiVersion`/`kind` pairs with a stable
diagnostic. A migration MUST be explicit, deterministic, and preserve source
identity and diagnostic evidence. Loading a legacy version MUST NOT silently
claim current-version conformance.

Normalization may fill defined defaults, canonicalize unordered sets, and
remove presentation-only differences. It MUST NOT invent provider choices,
permissions, budget authority, or deployment identity.

## Records

Authoritative records such as journal events, usage entries, budget entries,
application events, release evidence, and callback receipts MUST have immutable
identity semantics. Reusing an identity for different canonical content MUST
fail. Replay of identical content MAY be idempotent.

Related fixtures live in `tck/schema/` and compiler fixtures under
`tck/compiler/`.

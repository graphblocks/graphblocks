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

Canonical JSON numbers MUST retain their JSON type across implementations.
Integers are emitted as their exact base-10 digits, including values outside a
64-bit range, and MUST NOT be converted to exponential floating-point form.
Floating-point and arbitrary-precision decimal values MUST be finite. Values
representable as binary64 use their shortest round-tripping decimal spelling;
integral floating-point values retain a decimal marker (`1.0`). Larger decimal
exponents use a normalized coefficient (`10e399` becomes `1e+400`). Scientific
notation uses a lowercase `e`, an explicit sign, and at least two exponent
digits (`1e-07`, `1e+16`). Implementations MUST exercise the shared numeric
cases in `tck/schema/typed-values.json` before using canonical bytes as an
identity or signature input. JSON readers on identity-bearing boundaries MAY
narrow a finite decimal token to binary64 only when doing so provably leaves
its canonical spelling unchanged; otherwise they MUST preserve the exact
decimal. Parsing `1e400` MUST make `1e+400` available to canonicalization rather
than infinity, and `9007199254740992.0` and `9007199254740993.0` MUST retain
distinct canonical identities.

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

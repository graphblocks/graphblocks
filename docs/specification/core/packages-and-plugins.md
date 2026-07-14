# Packages and Plugins

## Static discovery

<a id="GB-PKG-DISCOVERY-001"></a>

A package manifest declares package identity, version, owned schemas and block
types, capabilities, dependencies, optional integrations, and conformance
claims. Discovery MUST be possible from static metadata without importing
provider SDKs or executing package code.

## Catalog artifacts

A catalog component is a logical capability, ownership, dependency, and import
identity used by graphs and locks. A catalog artifact is a separately built and
released deliverable. Multiple components MAY map to one artifact, but every
component MUST name exactly one cataloged artifact; artifact dependencies MUST
refer to artifacts rather than component identities.

## Block descriptors

<a id="GB-PKG-DESCRIPTORS-001"></a>

Block descriptors MUST use stable type and version identities, declare typed
ports and configuration schemas, and identify required capabilities. Duplicate
block ownership or incompatible descriptors MUST fail registry construction.

<a id="GB-PKG-BLOCK-VERSIONS-001"></a>

Block versions MUST be positive canonical integers. Catalog construction MUST
reject duplicate block IDs and duplicate input, output, or resource-slot names;
malformed input/output/resource collections; and `required` or `optional`
values that are not booleans.

An output port MAY declare `requiredWhen` to promote a globally optional output
to required for a particular immutable node configuration or execution phase.
`requiredWhen` is valid only on outputs. The closed predicate language is:

- `configEquals: {pointer, value}` for exact JSON-value equality at an RFC 6901
  JSON Pointer in the node's configuration; a missing pointer evaluates false;
- `phase: initial | resumed` for the current execution phase;
- `all` and `any`, each containing one to sixteen predicates; and
- `not`, containing one predicate.

<a id="GB-PKG-PREDICATES-001"></a>

A predicate MUST contain exactly one operator, MUST nest no more than sixteen
levels, and a JSON Pointer MUST contain no more than 512 characters. Descriptor
validation MUST reject unknown operators, malformed pointers, invalid phases,
empty boolean operands, excessive operands or nesting, and non-JSON equality
values. Predicate evaluation MUST NOT read inputs, outputs, bindings, mutable
runtime state, environment variables, time, or external services.

## Requiredness and resume consistency

An output is required when its `required` value (which defaults to `true`) is
true or its `requiredWhen` predicate evaluates true. `requiredWhen` therefore
only promotes requiredness; it never makes an otherwise-required output
optional. Node configuration used by the predicate MUST be frozen before
compilation and MUST be identical to the configuration used during execution
and resume contract checks.

## Port types and catalog closure

<a id="GB-PKG-PORT-TYPES-001"></a>

Value-port type references MUST be non-empty and whitespace-free. They MUST be
one of `Any`, `Boolean`, `Bytes`, `Integer`, `Number`, `Null`, or `String`; a
recursively valid `List<T>`, `Map<K,V>`, or `Optional<T>` expression; or a
canonical versioned schema ID. Resource-slot types MUST be canonical schema IDs
or dot-separated opaque identities such as `haystack.component`. Catalogs MUST
be immutable after construction and closed to unknown block IDs by default. An
open catalog MUST require an explicit compatibility or discovery opt-in.

## Package resolution and artifact integrity

Package resolution MUST evaluate complete dependency closure. Doctor and lock
operations MUST detect incompatible versions, missing packages, forbidden
default dependencies, transitive violations, and direct-reference forms that
bypass normal package-name checks. A lock MUST bind exact package and artifact
identities used to compile or deploy a release.

Catalog artifact manifest references MUST be relative paths and MUST remain
beneath the validation or build root after resolving parent segments and
symbolic links. Doctor and wheel-matrix operations MUST reject absolute or
escaped manifest references before reading package metadata. They MUST report
malformed paths separately, MUST reject multiple artifacts whose references
resolve to the same manifest, and MUST read the resolved in-root file without
following a later path or symbolic-link replacement. Final opens MUST NOT block
on a raced FIFO or other special-file replacement.

Python distribution identities MUST be compared using PEP 503 canonical names.
Catalog artifacts, locks, wheel matrices, and installed-artifact verification
MUST reject dotted, underscored, repeated-separator, or case variants that
canonicalize to the same distribution identity.

Canonical comparison applies to every artifact reference, including artifact
dependency edges, component-to-artifact mappings, default artifact selections,
requested artifact selections, and lock artifact fields. Component names are
logical capability identities rather than Python distribution names and retain
their exact spelling. When one requested value is both an exact component name
and a canonical alias of an artifact, the exact component match takes
precedence; one request MUST NOT implicitly select both identities.

## Plugin authority and failures

<a id="GB-PKG-PLUGIN-AUTHORITY-001"></a>

Plugins execute with explicitly granted capabilities. Loading a plugin MUST NOT
grant filesystem, network, secret, tool, deployment, or policy authority merely
because the package is installed. Plugin failures MUST produce diagnostics and
must not corrupt the registry's canonical view.

The canonical catalogs are `src/graphblocks/data/package-catalog.yaml` and
`src/graphblocks/data/conformance-profiles.yaml`.

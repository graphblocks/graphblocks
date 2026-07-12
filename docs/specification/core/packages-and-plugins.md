# Packages and Plugins

A package manifest declares package identity, version, owned schemas and block
types, capabilities, dependencies, optional integrations, and conformance
claims. Discovery MUST be possible from static metadata without importing
provider SDKs or executing package code.

Block descriptors MUST use stable type and version identities, declare typed
ports and configuration schemas, and identify required capabilities. Duplicate
block ownership or incompatible descriptors MUST fail registry construction.

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
following a later path or symbolic-link replacement.

Plugins execute with explicitly granted capabilities. Loading a plugin MUST NOT
grant filesystem, network, secret, tool, deployment, or policy authority merely
because the package is installed. Plugin failures MUST produce diagnostics and
must not corrupt the registry's canonical view.

The canonical catalogs are `src/graphblocks/data/package-catalog.yaml` and
`src/graphblocks/data/conformance-profiles.yaml`.

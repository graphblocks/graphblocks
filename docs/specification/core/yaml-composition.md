# Deterministic YAML Composition

YAML composition is an authoring operation that materializes one ordinary
`Graph` before compilation. It lets an author import local fragment files and
fill typed placeholder nodes without making file access or template evaluation
part of graph execution.

Composition uses `graphblocks.ai/composition/v1alpha1`. The words **MUST**,
**MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** have the meaning defined by
the living specification.

## Authoring boundary

Composition MUST finish before authoritative graph migration, compilation, or
hashing. Its authoring implementation MAY lower ordinary
`inputs` and `outputs` shorthand into an edge intermediate representation while
splicing fragment boundaries and then normalize the expanded graph; that
lowering is not a graph identity boundary.
A compiler that receives an unresolved `spec.composition` member or a node with
`slot` MUST reject it with `GB1052`. A runtime MUST NOT read
composition sources. A composed root MUST already use
`graphblocks.ai/v1alpha3`; composition does not migrate legacy graph versions.

After expansion, the implementation MUST apply the ordinary graph migration
boundary. A stable-representable result materializes as a regular
`graphblocks.ai/v1` `Graph`; a result containing preview-only graph fields
remains a regular `graphblocks.ai/v1alpha3` `Graph`. `GraphFragment` is an
authoring resource and MUST NOT appear in a physical plan or runtime document
stream.

Version 1 supports only:

- exact local-file imports;
- named `GraphFragment` resources and imported `Binding` resources; and
- typed graph slots filled by one named fragment.

It does not support arbitrary mapping or list merges, JSON Pointer includes,
scalar substitution, parameters, conditional imports, environment expansion,
Jinja or another template language, globs, remote URLs, registries, or command
execution.

## Graph composition declaration

A graph opts in with `spec.composition`:

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: assistant
spec:
  composition:
    apiVersion: graphblocks.ai/composition/v1alpha1
    imports:
      rag:
        path: ./fragments/retrieval.yaml
    slots:
      retriever:
        interface:
          inputs:
            query: company/Query@1
          outputs:
            documents: company/Documents@1
        fill:
          fragment: rag/retrieval

  interface:
    inputs:
      query: company/Query@1
    outputs:
      answer: company/Answer@1

  nodes:
    retrieve:
      slot: retriever
    answer:
      block: model.generate@1

  edges:
    - from: $input.query
      to: retrieve.query
    - from: retrieve.documents
      to: answer.context
    - from: answer.message
      to: $output.answer
```

`composition.apiVersion` is required and MUST be exactly
`graphblocks.ai/composition/v1alpha1`. Import aliases, slot names, and fragment
names, as well as fragment node and interface port names, MUST match
`^[A-Za-z][A-Za-z0-9_-]*$`. An import alias is local to its owning graph.

`imports` is a mapping from an alias to an object containing exactly one `path`.
An imported YAML stream may contain only `GraphFragment` and `Binding`
documents. Imported fragments enter the alias-local symbol table and are
removed after expansion. Imported bindings are emitted exactly once as ordinary
documents in the composed output. An imported `Graph`, another composition
root, or any other resource kind MUST be rejected.

Entry-stream documents retain their order, with each composed root replaced by
its expanded `Graph`. Imported bindings are appended in canonical
`(apiVersion, kind, metadata.name, content hash)` order. Changing YAML mapping
insertion order therefore MUST NOT change the composed document stream. A
composition report that records source byte digests may change when source
formatting changes; it is non-authoritative provenance rather than graph
identity.

A fragment reference has the form `<alias>/<fragment-name>` and resolves the
fragment whose `metadata.name` matches in that imported YAML stream. An alias,
fragment name, slot name, or emitted binding identity MUST resolve exactly once.
Two sources that declare the same binding identity are a collision even when
their content is equal; composition MUST NOT choose one by source order.

`slots` is a mapping from slot name to a contract and fill. The contract has
`interface.inputs` and `interface.outputs` mappings from port names to canonical
schema IDs. `fill.fragment` identifies the imported fragment. The slot and
fragment interfaces MUST match exactly, including port names, directions, and
schema IDs. Composition MUST NOT infer a provider, default fragment, schema
conversion, or missing fill.

## Graph fragments

A fragment is a YAML document with this envelope:

```yaml
apiVersion: graphblocks.ai/composition/v1alpha1
kind: GraphFragment
metadata:
  name: retrieval
spec:
  interface:
    inputs:
      query: company/Query@1
    outputs:
      documents: company/Documents@1
  nodes:
    embed:
      block: embedding.generate@1
    search:
      block: vector.search@1
  edges:
    - from: $input.query
      to: embed.text
    - from: embed.embedding
      to: search.vector
    - from: search.documents
      to: $output.documents
```

The normative wire shape is
[`graph-fragment.schema.json`](../../../schemas/graphblocks.ai/composition/v1alpha1/graph-fragment.schema.json).
A fragment MUST contain `spec.interface` and `spec.nodes`; `spec.edges` is
optional. Node `inputs` and `outputs` shorthand has the same edge meaning as it
does in a graph.

Within a fragment, `$input.<port>` may be used only as a source and
`$output.<port>` only as a target. The port MUST be declared by the fragment
interface. An ordinary endpoint MUST name a node inside the fragment. Fragment
v1 does not expose `$state`, `$context`, or `$execution`; a reusable fragment
must receive such data through a declared input instead.

Each fragment output MUST have exactly one internal producer. A fragment input
MUST have at least one internal consumer and may fan out. Fragment documents
MUST NOT declare graph-wide execution, state, policy, or binding merge fields.
Bindings remain ordinary node binding references or imported `Binding`
documents; importing a binding never merges it into a fragment.

Only the root `Graph` may declare `spec.composition`. Nested composition in a
`GraphFragment` is not part of v1, and a fragment therefore cannot contain a
slot placeholder. An imported stream that attempts to introduce another
composition root MUST be rejected, including when it imports the entry source
itself. This keeps the first composition version single-level and makes
expansion and cycle behavior identical across implementations.

## Slot placeholders and wiring

A graph node with `slot` is a placeholder instance. It MUST name a declared
slot and MUST NOT also contain `block`, `config`, `bindings`, `connection`,
`flow`, `effects`, or fragment overrides. It MAY contain only `slot` plus the
ordinary `inputs` and `outputs` wiring shorthand.

The placeholder behaves as a virtual node exposing the slot interface. Explicit
edges may connect to it, so both forms below are equivalent:

```yaml
nodes:
  retrieve:
    slot: retriever
    inputs:
      query: $input.query
    outputs:
      documents: answer.context
```

```yaml
nodes:
  retrieve:
    slot: retriever
edges:
  - from: $input.query
    to: retrieve.query
  - from: retrieve.documents
    to: answer.context
```

Every slot input MUST have exactly one external producer. A slot output may
have zero or more external consumers. Unknown ports, reversed directions,
duplicate input producers, absent required input wiring, and references to a
placeholder outside its declared interface MUST fail composition.

## Deterministic expansion

An implementation MUST perform these operations deterministically:

1. Parse every source into the canonical JSON value domain and validate the
   composition envelopes.
2. Resolve import aliases in lexicographic order and YAML documents in stream
   order.
3. Build the alias-local fragment table and reject ambiguous identities.
4. Lower graph and fragment `inputs` and `outputs` shorthand to an edge
   intermediate representation.
5. For each placeholder, copy the selected fragment's ordinary nodes and
   rewrite an internal node name as
   `<placeholder-name>__<fragment-node-name>`.
6. Replace fragment `$input` and `$output` boundaries with the corresponding
   external placeholder edges.
7. Rewrite endpoint-bearing `edges`, node `inputs`, and node `outputs`. A
   fragment node's `when` reference may name an internal node and receives the
   same instance prefix. Arbitrary strings in `config`, prompts, metadata, or
   bindings MUST NOT be rewritten.
8. Remove the placeholder, `spec.composition`, and all `GraphFragment`
   documents, then pass the expanded graph through ordinary graph migration and
   normalization. Preserve `v1alpha3` only when preview-only fields prevent
   stable migration.

For example, placeholder `retrieve` containing fragment nodes `embed` and
`search` produces `retrieve__embed` and `retrieve__search`. The example above
materializes these edges:

```yaml
- from: $input.query
  to: retrieve__embed.text
- from: retrieve__embed.embedding
  to: retrieve__search.vector
- from: retrieve__search.documents
  to: answer.context
```

Generated names are semantic node identities. If a generated name collides
with a root node or another generated node, composition MUST fail; an
implementation MUST NOT silently rename, overwrite, or merge either node.
Duplicate resource identities and duplicate edges that violate the graph
contract likewise fail closed.

## Source and import security

The composition root is an explicit filesystem trust boundary. By default it
is the directory containing the entry file. A caller MAY select a containing
directory with `--root`; the entry file itself MUST be inside that root.

Every import path MUST:

- be a non-empty, literal, relative POSIX path;
- identify one exact YAML file;
- remain within the composition root after canonical resolution; and
- resolve to a regular file.

Absolute paths, URI schemes, backslashes, `..` segments, globs, environment or
home-directory interpolation, and network access are forbidden. The root,
entry, and every imported path component MUST NOT be a symbolic link. An
implementation MUST check the resolved target against the resolved root and
fail closed on an import cycle, missing file, special file, or path race it can
detect.

Validation and reading MUST be one security boundary. Implementations SHOULD
use descriptor-relative traversal with no-follow semantics where the platform
provides it. A portable fallback MUST reject symbolic links or reparse points,
verify component and file identities before opening, and recheck them after the
read so a validated entry or import cannot be replaced with an out-of-root
target during composition. The final source open MUST be non-blocking and MUST
reject a raced FIFO or other special-file replacement without waiting for a
writer or device.

The YAML reader MUST be safe and bounded. It MUST reject unknown or executable
tags, duplicate mapping keys, recursive aliases, cyclic value graphs,
non-canonical map keys, and values outside the canonical JSON domain. YAML
anchors and merge keys MAY be accepted only when they resolve to an acyclic,
bounded canonical value. Implementations MUST impose common conformance limits
on source bytes, import depth, source and document counts, parsed value depth,
and expanded node and edge counts. Exceeding a limit MUST fail before
compilation rather than produce a partial graph.

Composition MUST NOT read environment variables, credentials, secrets, clocks,
randomness, process output, or network state. A string such as a `secret://`
reference remains an uninterpreted graph value for the later binding/runtime
boundary.

## Identity and provenance

The authoritative graph identity is:

```text
canonical_hash(normalize_graph(expanded_graph))
```

Import paths, aliases, source ordering, YAML formatting, source byte digests,
slot declarations, and fragment metadata MUST NOT be added to the graph hash.
Fragment semantics are already represented by the expanded nodes and edges.
Consequently, moving a fragment file or changing its import alias leaves the
graph hash unchanged when the normalized expanded graph is unchanged.

An implementation MAY emit a non-authoritative composition report containing
the composition version, root-relative logical source paths and byte digests,
placeholder-to-fragment mappings, and expanded-node origins. Such a report is
useful for audit and source diagnostics but is not part of graph identity.
Absolute host paths and source bytes containing secrets MUST NOT be placed in
portable plan evidence.

## Diagnostics

Composition diagnostics MUST be stable and SHOULD use these identifiers:

- `CompositionInvalidYaml`
- `CompositionDuplicateKey`
- `CompositionUnsupportedVersion`
- `CompositionInvalidImport`
- `CompositionImportOutsideRoot`
- `CompositionSymlinkRejected`
- `CompositionImportCycle`
- `CompositionDuplicateAlias`
- `CompositionUnsupportedKind`
- `CompositionDuplicateIdentity`
- `CompositionUnknownFragment`
- `CompositionUnknownSlot`
- `CompositionUnfilledSlot`
- `CompositionInterfaceMismatch`
- `CompositionInvalidWiring`
- `CompositionNodeCollision`
- `CompositionLimitExceeded`
- `CompositionOutputError`
- `GB1052`

A diagnostic SHOULD identify the root-relative logical source, YAML document
index, JSONPath, and import or placeholder expansion stack. It MUST NOT depend
on the process working directory or expose an absolute host path. Multiple
diagnostics MUST have a deterministic order.

## Materialization and language boundary

The portable materialization command is:

```console
graphblocks compose ENTRY --output expanded.yaml [--root ROOT]
```

`--report REPORT.json` MAY be used to write the non-authoritative source and
instance evidence separately from the materialized YAML.

The output MUST be sufficient for validation, planning, hashing, and execution
without access to any composition source. `validate`, `plan`, and `run` MAY
compose an authoring entry automatically, but they MUST apply the same rules as
the explicit command.

The current direct authoring implementation is Python. Rust does not resolve
composition sources and consumes the expanded YAML emitted by Python. Direct
Rust composition support must not be claimed until it passes the same
composition fixtures and produces byte-equivalent canonical expanded values,
hashes, and diagnostics.

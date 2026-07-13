# Type Safety

GraphBlocks checks type contracts at catalog load, graph authoring, graph
compilation, and runtime execution. These checks use nominal type references:
`graphblocks.ai/Answer@1` and `graphblocks.ai/Text@1` are different even when
their current JSON shapes overlap.

## Catalog boundary

A `BlockCatalog` is immutable and closed by default. Its descriptors reject
duplicate block, input, output, and resource-slot names; non-positive block
versions; malformed collection shapes; and non-boolean `required` or
`optional` flags.

Value-port type references are non-empty and whitespace-free. They may use the
primitives `Any`, `Boolean`, `Bytes`, `Integer`, `Number`, `Null`, and `String`;
recursive `List<T>`, `Map<K,V>`, and `Optional<T>` expressions; or a canonical
versioned schema ID. Resource slots use either a canonical schema ID or a
dot-separated opaque identity such as `haystack.component`.

`allow_unknown_blocks=True` is an explicit open-catalog escape hatch. It is
useful while inspecting graphs whose extension packages are not installed, but
it does not prove that those blocks can be executed. CLI `validate` and `plan`
type-check every discovered descriptor while retaining this extension-friendly
open behavior. A catalog-backed runtime or release check is the closed-world
boundary that proves every executable block is registered.

## Compiler boundary

With a catalog, the compiler checks all of the following before execution:

- every block is declared unless the catalog is explicitly open;
- every edge has a valid source and target direction;
- graph-interface and block root ports exist;
- graph-input-to-block, block-to-block, and block-to-graph-output types have
  the same nominal identity, with `Any` as the only wildcard;
- an optional block output does not feed a required block input or graph
  output; and
- every required block input is supplied.

Nested references such as `node.result.items` validate the declared root port
(`result`) but do not infer a type for `items`. Payload-field validation remains
the responsibility of the referenced schema or domain validator. The compiler
does not coerce one nominal type into another.

Common diagnostics are:

| Code | Meaning |
| --- | --- |
| `GB1013` | Unknown target/input-side port or graph output |
| `GB1014` | Unknown source/output-side port or graph input |
| `GB1015` | Optional output connected to a required target |
| `GB1018` | Incompatible nominal port types |
| `GB1022` | Block missing from a closed catalog |

## Typed authoring

Python's `GraphBuilder` uses the built-in catalog by default. `PortType[T]`
binds a schema ID to a marker class, so mypy checks generic connections while
the builder also checks schema-and-marker identity, required catalog ports,
cross-builder references, and forged references at construction time.

Rust records the same contract in `Port<T>` and `PortType::TYPE_REF`. Port
constructors are private, incompatible `Port<T>` connections fail to compile,
and `GraphBuilder::add` and `bind_output` recheck the authoritative catalog and
port provenance through `Result`. The repository keeps mypy negative fixtures
for Python and trybuild compile-fail fixtures for Rust.

Both builders materialize an ordinary portable `Graph`; the compiler validates
that document again. Typed code authoring therefore adds an earlier feedback
layer without creating a language-specific runtime format.

## Runtime boundary

`RuntimeRegistry()` starts with an empty closed catalog. Registration of an
undeclared block or duplicate handler fails. Use `replace` for an intentional
handler replacement. `stdlib_registry()` supplies the authoritative built-in
catalog.

`RuntimeRegistry(allow_untyped=True)` is the explicit compatibility and test
escape hatch for custom handlers without descriptors. Prefer a real descriptor
and catalog for production code. When a descriptor is present, the runtime
still rejects non-mapping results, undeclared output keys, and missing required
outputs during normal execution and callback resume.

These output checks validate the block's port contract. They do not replace
schema validation of every field inside a returned JSON value.

See the normative [graph, compiler, and runtime contract](../specification/core/graph-compilation-runtime.md),
[package and descriptor contract](../specification/core/packages-and-plugins.md),
and [language support matrix](../specification/conformance/language-support.md).

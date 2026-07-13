# Graphs and Bindings

A `Graph` is a versioned document containing metadata, typed inputs and outputs,
nodes, edges, and configuration. Graph identity is derived from normalized
canonical content, not YAML formatting or mapping order.

Each node references a versioned block type. Ports define the values a block may
receive and emit. An edge connects compatible ports; the compiler rejects
unknown endpoints, duplicate identities, invalid direction, type mismatch, and
cycles where the selected runtime profile does not permit them.

Port types are nominal: declared schema IDs must match exactly, with `Any` as
the only wildcard. The compiler also checks graph-interface ports, prevents an
optional block output from feeding a required target, and rejects blocks absent
from a closed catalog. Nested paths validate their declared root port without
inferring nested field types. See [type safety](type-safety.md) for the catalog,
authoring, compiler, and runtime layers.

A binding maps an abstract block contract to an implementation. Bindings may
select a provider model, parser, vector store, local function, remote worker,
tool operation, or package adapter. Runtime plans record the resolved binding
and compatibility evidence so a graph can remain portable without hiding its
physical execution identity.

Dynamic work is represented by bounded constructs such as sequence limits,
task-plan limits, async-operation deadlines, or durable-stream checkpoints. A
graph must not smuggle an unbounded scheduler into ordinary node configuration.

For authoring, a graph may import local `GraphFragment` documents and fill a
typed slot placeholder. Composition is a deterministic preprocessing boundary:
it resolves only explicitly named local files, expands each fragment into
ordinary prefixed nodes and edges, and removes all authoring directives before
compilation. The graph hash is derived from that normalized expanded graph, so
moving an imported file without changing the result does not change graph
identity. Environment interpolation, remote imports, and arbitrary YAML subtree
merges are not composition features.

See the normative [canonical data model](../specification/core/canonical-data-model.md)
and [graph, compiler, and runtime contract](../specification/core/graph-compilation-runtime.md).
The authoring rules are defined by
[deterministic YAML composition](../specification/core/yaml-composition.md).

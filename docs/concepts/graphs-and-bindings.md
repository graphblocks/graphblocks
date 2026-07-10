# Graphs and Bindings

A `Graph` is a versioned document containing metadata, typed inputs and outputs,
nodes, edges, and configuration. Graph identity is derived from normalized
canonical content, not YAML formatting or mapping order.

Each node references a versioned block type. Ports define the values a block may
receive and emit. An edge connects compatible ports; the compiler rejects
unknown endpoints, duplicate identities, invalid direction, type mismatch, and
cycles where the selected runtime profile does not permit them.

A binding maps an abstract block contract to an implementation. Bindings may
select a provider model, parser, vector store, local function, remote worker,
tool operation, or package adapter. Runtime plans record the resolved binding
and compatibility evidence so a graph can remain portable without hiding its
physical execution identity.

Dynamic work is represented by bounded constructs such as sequence limits,
task-plan limits, async-operation deadlines, or durable-stream checkpoints. A
graph must not smuggle an unbounded scheduler into ordinary node configuration.

See the normative [canonical data model](../specification/core/canonical-data-model.md)
and [graph, compiler, and runtime contract](../specification/core/graph-compilation-runtime.md).

use graphblocks_compiler::canonical::{canonical_hash, canonical_json};
use graphblocks_compiler::graph::{GRAPH_API_VERSION, normalize_graph};
use serde_json::json;

#[test]
fn normalize_graph_expands_input_and_output_shorthand() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "input-shorthand"},
        "spec": {
            "interface": {"inputs": {"message": "graphblocks.ai/Text@1"}},
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "inputs": {
                        "message": "$input.message",
                        "context": {"current": "lookup.value"}
                    },
                    "outputs": {"value": "$output.result"}
                },
                "lookup": {"block": "memory.lookup@1"}
            }
        }
    });

    let normalized = normalize_graph(&graph);

    assert_eq!(
        canonical_json(&normalized["spec"]["nodes"]["render"]),
        r#"{"block":"prompt.render@1"}"#
    );
    assert_eq!(
        canonical_json(&normalized["spec"]["edges"]),
        r#"[{"from":"$input.message","to":"render.message"},{"from":"lookup.value","to":"render.context.current"},{"from":"render.value","to":"$output.result"}]"#
    );
}

#[test]
fn normalize_graph_sorts_nodes_and_edges_for_stable_hashes() {
    let left = json!({
        "kind": "Graph",
        "apiVersion": GRAPH_API_VERSION,
        "metadata": {"name": "ordered"},
        "spec": {
            "nodes": {
                "b": {"block": "text.join@1", "config": {"second": 2, "first": 1}},
                "a": {"block": "text.literal@1"}
            },
            "edges": [
                {"to": "b.value", "from": "a.value"},
                {"to": "$output.result", "from": "b.value"}
            ],
            "interface": {"outputs": {"result": "graphblocks.ai/Text@1"}}
        }
    });
    let right = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "spec": {
            "interface": {"outputs": {"result": "graphblocks.ai/Text@1"}},
            "edges": [
                {"from": "b.value", "to": "$output.result"},
                {"from": "a.value", "to": "b.value"}
            ],
            "nodes": {
                "a": {"block": "text.literal@1"},
                "b": {"config": {"first": 1, "second": 2}, "block": "text.join@1"}
            }
        },
        "metadata": {"name": "ordered"}
    });

    let normalized_hash = canonical_hash(&normalize_graph(&left));

    assert_eq!(normalized_hash, canonical_hash(&normalize_graph(&right)));
    assert_eq!(
        normalized_hash,
        "sha256:fce14a3da5ee7f2b579494d34f3320758f5ff3204713d803c73fdcbccb162027"
    );
}

#[test]
fn normalize_graph_rewrites_connection_shorthand_to_default_binding() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "connection-shorthand"},
        "spec": {
            "nodes": {
                "model": {
                    "block": "model.generate@1",
                    "connection": "openai-main"
                }
            }
        }
    });

    let normalized = normalize_graph(&graph);

    assert_eq!(
        canonical_json(&normalized["spec"]["nodes"]["model"]),
        r#"{"bindings":{"default":"openai-main"},"block":"model.generate@1"}"#
    );
}

#[test]
fn normalize_graph_deduplicates_explicit_and_synthesized_edges() {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "deduplicated-edges"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"}
                }
            },
            "edges": [
                {"from": "$input.message", "to": "render.message"},
                {"from": "render.prompt", "to": "$output.prompt"}
            ]
        }
    });

    let normalized = normalize_graph(&graph);

    assert_eq!(
        normalized["spec"]["edges"],
        json!([
            {"from": "$input.message", "to": "render.message"},
            {"from": "render.prompt", "to": "$output.prompt"}
        ])
    );
}

#[test]
fn normalize_graph_leaves_non_graph_documents_unchanged() {
    let document = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Application",
        "metadata": {"name": "app"}
    });

    assert_eq!(normalize_graph(&document), document);
}

#[test]
fn normalize_graph_migrates_only_explicit_alpha_versions() {
    let legacy = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {
            "name": "legacy",
            "annotations": {"graphblocks.ai/migratedFrom": "untrusted"}
        },
        "spec": {"nodes": {}}
    });
    let future = json!({
        "apiVersion": "graphblocks.ai/v2",
        "kind": "Graph",
        "metadata": {"name": "future"},
        "spec": {"nodes": {}}
    });

    let migrated = normalize_graph(&legacy);
    assert_eq!(migrated["apiVersion"], GRAPH_API_VERSION);
    assert_eq!(
        migrated["metadata"]["annotations"]["graphblocks.ai/migratedFrom"],
        "graphblocks.ai/v1alpha3",
    );
    assert_eq!(normalize_graph(&future)["apiVersion"], "graphblocks.ai/v2");
}

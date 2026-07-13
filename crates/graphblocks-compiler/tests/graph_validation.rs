use graphblocks_compiler::compiler::{BlockCatalog, compile_graph, compile_graph_with_catalog};
use graphblocks_compiler::diagnostics::Severity;
use graphblocks_compiler::graph::GRAPH_API_VERSION;
use serde_json::json;

fn voice_feedback_graph() -> serde_json::Value {
    json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "duplex-voice-feedback"},
        "spec": {
            "extensions": ["graphblocks.voice/v1alpha1"],
            "execution": {
                "lifetime": "session",
                "interaction": "duplex",
                "durability": "checkpointed"
            },
            "voice": {"pipeline": {"kind": "realtime"}},
            "nodes": {
                "session": {"block": "realtime.session@1"},
                "tools": {"block": "tools.dispatch@1"}
            },
            "edges": [
                {"from": "session.toolCalls", "to": "tools.calls"},
                {"from": "tools.results", "to": "session.toolResults"}
            ]
        }
    })
}

#[test]
fn compile_graph_rejects_missing_endpoint_ports_and_invalid_pseudo_node_directions() {
    let cases = [
        (
            json!({"from": "source", "to": "sink.value"}),
            "edge from endpoint must include a port path",
            "$.spec.edges[0].from",
        ),
        (
            json!({"from": "source.value", "to": "sink"}),
            "edge to endpoint must include a port path",
            "$.spec.edges[0].to",
        ),
        (
            json!({"from": "$output.value", "to": "sink.value"}),
            "$output cannot be used as an edge source",
            "$.spec.edges[0].from",
        ),
        (
            json!({"from": "source.value", "to": "$input.value"}),
            "$input cannot be used as an edge target",
            "$.spec.edges[0].to",
        ),
    ];

    for (edge, expected_message, expected_path) in cases {
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": "invalid-endpoint"},
            "spec": {
                "nodes": {
                    "source": {"block": "test.source@1"},
                    "sink": {"block": "test.sink@1"}
                },
                "edges": [edge]
            }
        });

        let plan = compile_graph(&graph);
        let errors = plan
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .collect::<Vec<_>>();

        assert_eq!(errors.len(), 1);
        assert_eq!(errors[0].code, "GB1020");
        assert_eq!(errors[0].message, expected_message);
        assert_eq!(errors[0].path, expected_path);
    }
}

#[test]
fn compile_graph_rejects_edge_and_guard_dependency_cycles() {
    let graphs = [
        json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": "edge-cycle"},
            "spec": {
                "nodes": {
                    "a": {"block": "test.node@1"},
                    "b": {"block": "test.node@1"}
                },
                "edges": [
                    {"from": "a.value", "to": "b.value"},
                    {"from": "b.value", "to": "a.value"}
                ]
            }
        }),
        json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": "guard-cycle"},
            "spec": {
                "nodes": {
                    "a": {"block": "test.node@1", "when": "b.enabled"},
                    "b": {"block": "test.node@1", "when": "a.enabled"}
                }
            }
        }),
    ];

    for graph in graphs {
        let plan = compile_graph(&graph);
        let errors = plan
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .collect::<Vec<_>>();

        assert_eq!(errors.len(), 1);
        assert_eq!(errors[0].code, "GB1021");
        assert_eq!(
            errors[0].message,
            "graph dependency cycle detected: a -> b -> a"
        );
        assert_eq!(errors[0].path, "$.spec");
    }
}

#[test]
fn compile_graph_allows_exact_checkpointed_duplex_voice_feedback_cycle() {
    let plan = compile_graph(&voice_feedback_graph());

    assert!(
        plan.diagnostics
            .iter()
            .all(|diagnostic| diagnostic.code != "GB1021")
    );
}

#[test]
fn compile_graph_rejects_voice_feedback_without_the_exact_runtime_profile() {
    let mut invalid_profiles = Vec::new();
    for (pointer, value) in [
        ("/spec/extensions", json!([])),
        ("/spec/execution/lifetime", json!("job")),
        ("/spec/execution/interaction", json!("incremental")),
        ("/spec/execution/durability", json!("ephemeral")),
        ("/spec/voice/pipeline/kind", json!("batch")),
        ("/spec/nodes/session/block", json!("test.session@1")),
    ] {
        let mut graph = voice_feedback_graph();
        *graph
            .pointer_mut(pointer)
            .expect("voice test path must exist") = value;
        invalid_profiles.push(graph);
    }

    for graph in invalid_profiles {
        let plan = compile_graph(&graph);
        assert!(
            plan.diagnostics
                .iter()
                .any(|diagnostic| diagnostic.code == "GB1021")
        );
    }
}

#[test]
fn compile_graph_rejects_other_or_guard_cycles_in_a_duplex_voice_graph() {
    let mut unrelated_cycle = voice_feedback_graph();
    unrelated_cycle["spec"]["nodes"]["a"] = json!({"block": "test.node@1"});
    unrelated_cycle["spec"]["nodes"]["b"] = json!({"block": "test.node@1"});
    unrelated_cycle["spec"]["edges"]
        .as_array_mut()
        .expect("voice graph edges must be an array")
        .extend([
            json!({"from": "a.value", "to": "b.value"}),
            json!({"from": "b.value", "to": "a.value"}),
        ]);

    let mut guard_cycle = voice_feedback_graph();
    guard_cycle["spec"]["nodes"]["session"]["when"] = json!("tools.enabled");

    for graph in [unrelated_cycle, guard_cycle] {
        let plan = compile_graph(&graph);
        assert!(
            plan.diagnostics
                .iter()
                .any(|diagnostic| diagnostic.code == "GB1021")
        );
    }
}

#[test]
fn compile_graph_rejects_malformed_and_output_when_references() {
    let cases = [
        (
            json!("$input"),
            "node when reference must include a port path",
        ),
        (
            json!("$output.enabled"),
            "$output cannot be used as a when source",
        ),
        (json!(false), "node when reference must be a string"),
    ];

    for (when, expected_message) in cases {
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": "invalid-when-reference"},
            "spec": {
                "nodes": {
                    "branch": {"block": "test.branch@1", "when": when}
                }
            }
        });

        let plan = compile_graph(&graph);
        let errors = plan
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .collect::<Vec<_>>();

        assert_eq!(errors.len(), 1);
        assert_eq!(errors[0].code, "GB1020");
        assert_eq!(errors[0].message, expected_message);
        assert_eq!(errors[0].path, "$.spec.nodes.branch.when");
    }
}

#[test]
fn compile_graph_rejects_unknown_interface_input_used_by_when() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unknown-interface-when-port"},
        "spec": {
            "interface": {
                "inputs": {"enabled": "graphblocks.ai/Flag@1"}
            },
            "nodes": {
                "branch": {
                    "block": "test.branch@1",
                    "when": "$input.missing"
                }
            }
        }
    });

    let plan = compile_graph(&graph);
    let errors = plan
        .diagnostics
        .iter()
        .filter(|diagnostic| diagnostic.severity == Severity::Error)
        .collect::<Vec<_>>();

    assert_eq!(errors.len(), 1);
    assert_eq!(errors[0].code, "GB1014");
    assert_eq!(
        errors[0].message,
        "graph interface has no input port \"missing\""
    );
    assert_eq!(errors[0].path, "$.spec.nodes.branch.when");
}

#[test]
fn compile_graph_rejects_unknown_block_output_used_by_when() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "test.source",
            "version": 1,
            "outputs": [
                {"name": "enabled", "type": "graphblocks.ai/Flag@1"}
            ]
        },
        {"typeId": "test.branch", "version": 1}
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unknown-block-when-port"},
        "spec": {
            "nodes": {
                "source": {"block": "test.source@1"},
                "branch": {
                    "block": "test.branch@1",
                    "when": "source.missing"
                }
            }
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);
    let errors = plan
        .diagnostics
        .iter()
        .filter(|diagnostic| diagnostic.severity == Severity::Error)
        .collect::<Vec<_>>();

    assert_eq!(errors.len(), 1);
    assert_eq!(errors[0].code, "GB1014");
    assert_eq!(
        errors[0].message,
        "block test.source@1 has no output port \"missing\""
    );
    assert_eq!(errors[0].path, "$.spec.nodes.branch.when");
    Ok(())
}

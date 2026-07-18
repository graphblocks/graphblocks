use graphblocks_runtime_core::stdlib_runtime::run_stdlib_graph_json;
use serde_json::{Value, json};

#[test]
fn guarded_node_waits_for_true_node_output_before_execution() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-true-when-guard"},
        "spec": {
            "interface": {
                "inputs": {
                    "checks": "graphblocks.ai/Any@1",
                    "message": "graphblocks.ai/Message@1"
                },
                "outputs": {"prompt": "graphblocks.ai/Prompt@1"}
            },
            "nodes": {
                "aBranch": {
                    "block": "prompt.render@1",
                    "config": {"template": "ran"},
                    "when": "zCondition.passed",
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"}
                },
                "zCondition": {
                    "block": "gate.evaluate@1",
                    "inputs": {"checks": "$input.checks"}
                }
            }
        }
    });

    let result = run_graph(
        &graph,
        &json!({
            "checks": [{"checkId": "required", "status": "passed"}],
            "message": {"text": "ignored"}
        }),
    )?;

    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["outputs"], json!({"prompt": "ran"}));
    assert_eq!(started_nodes(&result), vec!["zCondition", "aBranch"]);
    Ok(())
}

#[test]
fn false_guard_skips_block_without_failing_an_independent_run() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-false-when-guard"},
        "spec": {
            "interface": {
                "inputs": {
                    "checks": "graphblocks.ai/Any@1",
                    "message": "graphblocks.ai/Message@1"
                }
            },
            "nodes": {
                "aBranch": {
                    "block": "prompt.render@1",
                    "config": {"template": "{missing}"},
                    "when": "zCondition.passed",
                    "inputs": {"message": "$input.message"}
                },
                "zCondition": {
                    "block": "gate.evaluate@1",
                    "inputs": {"checks": "$input.checks"}
                }
            }
        }
    });

    let result = run_graph(
        &graph,
        &json!({
            "checks": [{"checkId": "required", "status": "failed"}],
            "message": {"text": "ignored"}
        }),
    )?;

    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["outputs"], json!({}));
    assert!(!journal_has_node_kind(&result, "node_failed", "aBranch"));
    Ok(())
}

#[test]
fn false_guard_skips_without_waiting_for_unrelated_inputs_and_omits_outputs() -> Result<(), String>
{
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-false-when-missing-input"},
        "spec": {
            "interface": {
                "inputs": {
                    "checks": "graphblocks.ai/Any@1",
                    "message": "graphblocks.ai/Message@1"
                },
                "outputs": {"prompt": "graphblocks.ai/Prompt@1"}
            },
            "nodes": {
                "aBranch": {
                    "block": "prompt.render@1",
                    "config": {"template": "must not run"},
                    "when": "zCondition.passed",
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"}
                },
                "zCondition": {
                    "block": "gate.evaluate@1",
                    "inputs": {"checks": "$input.checks"}
                }
            }
        }
    });

    let result = run_graph(
        &graph,
        &json!({"checks": [{"checkId": "required", "status": "failed"}]}),
    )?;

    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["outputs"], json!({}));
    let completion = journal_node_record(&result, "node_completed", "aBranch")
        .ok_or_else(|| "missing guarded node completion".to_owned())?;
    assert_eq!(completion.pointer("/payload/skipped"), Some(&json!(true)));
    assert_eq!(
        completion.pointer("/payload/reason"),
        Some(&json!("condition_false"))
    );
    Ok(())
}

#[test]
fn consumers_of_condition_skipped_values_cascade_skip_without_failing() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-cascade-skip"},
        "spec": {
            "interface": {
                "inputs": {
                    "checks": "graphblocks.ai/Any@1",
                    "message": "graphblocks.ai/Message@1"
                }
            },
            "nodes": {
                "branch": {
                    "block": "prompt.render@1",
                    "config": {"template": "must not run"},
                    "when": "condition.passed",
                    "inputs": {"message": "$input.message"}
                },
                "consumer": {
                    "block": "model.generate@1",
                    "config": {"response": "must not run"},
                    "inputs": {"prompt": "branch.prompt"}
                },
                "condition": {
                    "block": "gate.evaluate@1",
                    "inputs": {"checks": "$input.checks"}
                }
            }
        }
    });

    let result = run_graph(
        &graph,
        &json!({
            "checks": [{"checkId": "required", "status": "failed"}],
            "message": {"text": "ignored"}
        }),
    )?;

    assert_eq!(result["status"], "succeeded", "{result:#}");
    for node_id in ["branch", "consumer"] {
        let completion = journal_node_record(&result, "node_completed", node_id)
            .ok_or_else(|| format!("missing {node_id} completion"))?;
        assert_eq!(completion.pointer("/payload/skipped"), Some(&json!(true)));
    }
    Ok(())
}

#[test]
fn a_skipped_condition_source_cascades_to_the_guarded_node() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-skipped-guard-source"},
        "spec": {
            "interface": {"inputs": {"checks": "graphblocks.ai/Any@1"}},
            "nodes": {
                "aCondition": {
                    "block": "gate.evaluate@1",
                    "inputs": {"checks": "$input.checks"}
                },
                "bSkipped": {
                    "block": "gate.evaluate@1",
                    "when": "aCondition.passed",
                    "inputs": {"checks": "$input.checks"}
                },
                "cGuarded": {
                    "block": "gate.evaluate@1",
                    "when": "bSkipped.passed",
                    "inputs": {"checks": "$input.checks"}
                }
            }
        }
    });

    let result = run_graph(
        &graph,
        &json!({"checks": [{"checkId": "required", "status": "failed"}]}),
    )?;

    assert_eq!(result["status"], "succeeded", "{result:#}");
    for node_id in ["bSkipped", "cGuarded"] {
        let completion = journal_node_record(&result, "node_completed", node_id)
            .ok_or_else(|| format!("missing {node_id} completion"))?;
        assert_eq!(completion.pointer("/payload/skipped"), Some(&json!(true)));
    }
    Ok(())
}

#[test]
fn output_projection_error_is_a_terminal_run_failure() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-output-projection-failure"},
        "spec": {
            "interface": {
                "inputs": {"message": "graphblocks.ai/Message@1"},
                "outputs": {"value": "graphblocks.ai/Any@1"}
            },
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "rendered"},
                    "inputs": {"message": "$input.message"}
                }
            },
            "edges": [
                {"from": "render.prompt.missing", "to": "$output.value"}
            ]
        }
    });

    let result = run_graph(&graph, &json!({"message": {"text": "ignored"}}))?;

    assert_eq!(result["status"], "failed");
    assert_eq!(result["outputs"], json!({}));
    assert!(journal_has_node_kind(&result, "node_failed", "render"));
    assert_eq!(
        result["journal"]
            .as_array()
            .and_then(|records| records.last())
            .and_then(|record| record["kind"].as_str()),
        Some("run_failed")
    );
    Ok(())
}

#[test]
fn non_boolean_guard_fails_closed() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-invalid-when-value"},
        "spec": {
            "interface": {
                "inputs": {
                    "guard": "graphblocks.ai/Any@1",
                    "message": "graphblocks.ai/Message@1"
                }
            },
            "nodes": {
                "branch": {
                    "block": "prompt.render@1",
                    "config": {"template": "must not run"},
                    "when": "$input.guard",
                    "inputs": {"message": "$input.message"}
                }
            }
        }
    });

    let result = run_graph(
        &graph,
        &json!({"guard": "true", "message": {"text": "ignored"}}),
    )?;

    assert_eq!(result["status"], "failed");
    let failure = journal_node_record(&result, "node_failed", "branch")
        .ok_or_else(|| "missing guarded node failure".to_owned())?;
    assert!(
        failure
            .pointer("/payload/message")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .contains("boolean")
    );
    Ok(())
}

#[test]
fn malformed_guard_reference_is_rejected() {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-malformed-when-reference"},
        "spec": {
            "interface": {"inputs": {"message": "graphblocks.ai/Message@1"}},
            "nodes": {
                "branch": {
                    "block": "prompt.render@1",
                    "config": {"template": "must not run"},
                    "when": "$input",
                    "inputs": {"message": "$input.message"}
                }
            }
        }
    });

    let error = run_stdlib_graph_json(
        &graph.to_string(),
        &json!({"message": {"text": "ignored"}}).to_string(),
    )
    .expect_err("a guard without a port must fail closed");

    assert_eq!(error.to_string(), "graph did not compile: GB1020");
}

#[test]
fn commit_turn_projects_its_candidate_as_a_required_result() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-commit-turn-result"},
        "spec": {
            "interface": {
                "inputs": {
                    "transaction": "graphblocks.ai/ConversationTransaction@1",
                    "candidate": "graphblocks.ai/TurnCandidate@1"
                },
                "outputs": {"result": "graphblocks.ai/TurnCandidate@1"}
            },
            "nodes": {
                "commit": {
                    "block": "conversation.commit_turn@1",
                    "inputs": {
                        "transaction": "$input.transaction",
                        "candidate": "$input.candidate"
                    },
                    "outputs": {"result": "$output.result"}
                }
            }
        }
    });
    let candidate = json!({"text": "grounded answer", "citations": ["source-1"]});

    let result = run_graph(
        &graph,
        &json!({
            "transaction": {"conversationId": "conversation-1", "turnId": "turn-1"},
            "candidate": candidate
        }),
    )?;

    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["outputs"]["result"], candidate);
    Ok(())
}

fn run_graph(graph: &Value, inputs: &Value) -> Result<Value, String> {
    let result = run_stdlib_graph_json(&graph.to_string(), &inputs.to_string())
        .map_err(|error| error.to_string())?;
    serde_json::from_str(&result).map_err(|error| error.to_string())
}

fn started_nodes(result: &Value) -> Vec<&str> {
    result["journal"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|record| record["kind"] == "node_started")
        .filter_map(|record| record["nodeId"].as_str())
        .collect()
}

fn journal_has_node_kind(result: &Value, kind: &str, node_id: &str) -> bool {
    journal_node_record(result, kind, node_id).is_some()
}

fn journal_node_record<'a>(result: &'a Value, kind: &str, node_id: &str) -> Option<&'a Value> {
    result["journal"].as_array()?.iter().find(|record| {
        record["kind"].as_str() == Some(kind) && record["nodeId"].as_str() == Some(node_id)
    })
}

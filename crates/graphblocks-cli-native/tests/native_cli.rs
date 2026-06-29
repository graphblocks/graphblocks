use std::io::Write;
use std::process::{Command, Stdio};

use graphblocks_cli_native::{NativeCliMode, run_compiler_workflow, run_stdlib_workflow};
use graphblocks_compiler::diagnostics::Severity;
use graphblocks_compiler::graph::GRAPH_API_VERSION;
use serde_json::{Value, json};

#[test]
fn native_validate_reports_ok_and_plan_hash_without_expanded_plan() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "ordered"},
        "spec": {
            "nodes": {
                "b": {"block": "text.join@1", "config": {"second": 2, "first": 1}},
                "a": {"block": "text.literal@1"}
            },
            "edges": [
                {"to": "b.value", "from": "a.value"},
                {"to": "$output.result", "from": "b.value"}
            ]
        }
    });

    let report = run_compiler_workflow(&graph, NativeCliMode::Validate);

    assert!(report.ok);
    assert_eq!(
        report.graph_hash.as_deref(),
        Some("sha256:0b2636678ee1af1446500624da2f5db0dab238aceb858058a6f3b60f9e06f3a8")
    );
    assert_eq!(report.normalized, None);
    assert!(report.diagnostics.is_empty());
}

#[test]
fn native_plan_can_include_normalized_graph_document() {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha2",
        "kind": "Graph",
        "metadata": {"name": "legacy"},
        "spec": {"nodes": {}}
    });

    let report = run_compiler_workflow(&graph, NativeCliMode::Plan { expand: true });

    assert!(report.ok);
    assert_eq!(
        report
            .normalized
            .as_ref()
            .and_then(|value| value.pointer("/metadata/annotations/graphblocks.ai~1migratedFrom"))
            .and_then(serde_json::Value::as_str),
        Some("graphblocks.ai/v1alpha2")
    );
}

#[test]
fn native_validate_returns_structured_diagnostics() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {},
        "spec": {"nodes": {}}
    });

    let report = run_compiler_workflow(&graph, NativeCliMode::Validate);

    assert!(!report.ok);
    assert_eq!(report.graph_hash, None);
    assert_eq!(report.normalized, None);
    assert_eq!(report.diagnostics[0].code, "GB0003");
    assert_eq!(report.diagnostics[0].severity, Severity::Error);
}

#[test]
fn native_run_executes_stdlib_graph_with_inputs() {
    let graph = prompt_graph("Native {message.text}");

    let report = run_stdlib_workflow(&graph, &json!({"message": {"text": "ok"}}));

    assert!(report.ok);
    assert_eq!(
        report
            .result
            .as_ref()
            .and_then(|result| result.pointer("/outputs/prompt"))
            .and_then(Value::as_str),
        Some("Native ok"),
    );
    assert_eq!(report.error, None);
}

#[test]
fn native_run_reports_failed_runtime_status_as_not_ok() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "native-run-fails"},
        "spec": {
            "nodes": {
                "missing": {"block": "missing.block@1"}
            }
        }
    });

    let report = run_stdlib_workflow(&graph, &json!({}));

    assert!(!report.ok);
    assert_eq!(
        report
            .result
            .as_ref()
            .and_then(|result| result.get("status"))
            .and_then(Value::as_str),
        Some("failed"),
    );
    assert_eq!(report.error, None);
}

#[test]
fn native_binary_run_accepts_input_json() -> Result<(), Box<dyn std::error::Error>> {
    let mut child = Command::new(env!("CARGO_BIN_EXE_graphblocks-native"))
        .args(["run", "--input-json", r#"{"message":{"text":"ok"}}"#])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()?;
    let stdin = child
        .stdin
        .as_mut()
        .ok_or("native binary stdin pipe was not available")?;
    stdin.write_all(serde_json::to_string(&prompt_graph("CLI {message.text}"))?.as_bytes())?;

    let output = child.wait_with_output()?;
    assert!(output.status.success());
    let payload = serde_json::from_slice::<Value>(&output.stdout)?;

    assert_eq!(
        payload.pointer("/status").and_then(Value::as_str),
        Some("succeeded"),
    );
    assert_eq!(
        payload.pointer("/outputs/prompt").and_then(Value::as_str),
        Some("CLI ok"),
    );
    Ok(())
}

fn prompt_graph(template: &str) -> Value {
    json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "native-run"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": template},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"}
                }
            }
        }
    })
}

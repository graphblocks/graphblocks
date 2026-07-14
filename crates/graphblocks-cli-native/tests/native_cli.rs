use std::io::Write;
use std::process::{Command, Stdio};

use graphblocks_cli_native::{
    NativeCliMode, NativeDocumentError, load_graph_document, load_single_graph_document,
    run_compiler_workflow, run_stdlib_workflow,
};
use graphblocks_compiler::diagnostics::Severity;
use graphblocks_compiler::graph::GRAPH_API_VERSION;
use serde_json::{Value, json};

#[test]
fn native_validate_reports_ok_and_plan_hash_without_expanded_plan() {
    let graph = prompt_graph("Native {message.text}");

    let report = run_compiler_workflow(&graph, NativeCliMode::Validate);

    assert!(report.ok);
    assert_eq!(
        report.graph_hash.as_deref(),
        Some("sha256:eeb398e0d56800354c0741291c973f76caced54fd6b02bb2de00730fbdce8fa9")
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
fn native_loader_accepts_single_yaml_graph_document() {
    let document = load_single_graph_document(
        r#"
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: yaml-native
spec:
  nodes:
    render:
      block: prompt.render@1
      config:
        template: YAML {message.text}
      inputs:
        message: $input.message
      outputs:
        prompt: $output.prompt
"#,
    )
    .expect("single YAML graph should load");

    let report = run_compiler_workflow(&document, NativeCliMode::Validate);

    assert!(report.ok);
    assert_eq!(
        document
            .pointer("/metadata/name")
            .and_then(serde_json::Value::as_str),
        Some("yaml-native")
    );
}

#[test]
fn native_loader_preserves_strict_json_numbers_without_yaml_coercion() {
    let document = load_single_graph_document(
        r#"{
            "apiVersion":"graphblocks.ai/v1",
            "kind":"Graph",
            "metadata":{"name":"exact-json-numbers"},
            "spec":{"nodes":{"test":{"block":"test.node@1","config":{
                "huge":1e400,
                "precise":1.2345678901234567890123456789
            }}}}
        }"#,
    )
    .expect("strict JSON graph should load");

    assert_eq!(
        document
            .pointer("/spec/nodes/test/config/huge")
            .map(Value::to_string),
        Some("1e+400".to_owned())
    );
    assert_eq!(
        document
            .pointer("/spec/nodes/test/config/precise")
            .map(Value::to_string),
        Some("1.2345678901234567890123456789".to_owned())
    );

    let error = load_single_graph_document(r#"{"kind":"Graph",}"#)
        .expect_err("malformed JSON must not be reinterpreted as YAML");
    assert!(matches!(error, NativeDocumentError::ParseFailed { .. }));
}

#[test]
fn native_loader_rejects_multi_document_yaml_without_graph_selection() {
    let error = load_single_graph_document(
        r#"
---
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: first
spec:
  nodes: {}
---
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: second
spec:
  nodes: {}
"#,
    )
    .expect_err("multi-document YAML requires explicit graph selection");

    assert_eq!(error, NativeDocumentError::MultipleDocuments { count: 2 });
}

#[test]
fn native_loader_selects_named_graph_from_multi_document_yaml() {
    let document = load_graph_document(
        r#"
---
apiVersion: graphblocks.ai/v1alpha3
kind: PolicyProfile
metadata:
  name: support-policy
spec: {}
---
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: first
spec:
  nodes: {}
---
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: selected
spec:
  nodes:
    render:
      block: prompt.render@1
"#,
        Some("selected"),
    )
    .expect("named graph should load");

    assert_eq!(
        document
            .pointer("/metadata/name")
            .and_then(serde_json::Value::as_str),
        Some("selected")
    );
}

#[test]
fn native_loader_reports_missing_named_graph() {
    let error = load_graph_document(
        r#"
---
apiVersion: graphblocks.ai/v1alpha3
kind: PolicyProfile
metadata:
  name: support-policy
spec: {}
---
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: available
spec:
  nodes: {}
"#,
        Some("missing"),
    )
    .expect_err("missing graph selector should fail");

    assert_eq!(
        error,
        NativeDocumentError::GraphNotFound {
            name: "missing".to_owned(),
        }
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
fn native_validate_rejects_graph_interface_to_stdlib_port_type_mismatch() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "interface-type-mismatch"},
        "spec": {
            "interface": {
                "inputs": {"message": "graphblocks.ai/Text@1"}
            },
            "nodes": {
                "context": {"block": "context.build@1"}
            },
            "edges": [
                {"from": "$input.message", "to": "context.currentMessage"}
            ]
        }
    });

    let report = run_compiler_workflow(&graph, NativeCliMode::Validate);

    assert!(!report.ok);
    assert!(report.diagnostics.iter().any(|item| item.code == "GB1018"));
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
    let graph = prompt_graph("Native {message.text}");

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

#[test]
fn native_binary_validate_accepts_yaml_stdin() -> Result<(), Box<dyn std::error::Error>> {
    let mut child = Command::new(env!("CARGO_BIN_EXE_graphblocks-native"))
        .arg("validate")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()?;
    let stdin = child
        .stdin
        .as_mut()
        .ok_or("native binary stdin pipe was not available")?;
    stdin.write_all(
        br#"
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: yaml-cli
spec:
  nodes: {}
"#,
    )?;

    let output = child.wait_with_output()?;
    assert!(output.status.success());
    let payload = serde_json::from_slice::<Value>(&output.stdout)?;

    assert_eq!(payload.pointer("/ok").and_then(Value::as_bool), Some(true));
    assert!(
        payload
            .pointer("/graphHash")
            .and_then(Value::as_str)
            .is_some()
    );
    Ok(())
}

#[test]
fn native_binary_validate_selects_named_yaml_graph() -> Result<(), Box<dyn std::error::Error>> {
    let mut child = Command::new(env!("CARGO_BIN_EXE_graphblocks-native"))
        .args(["validate", "--graph", "selected"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()?;
    let stdin = child
        .stdin
        .as_mut()
        .ok_or("native binary stdin pipe was not available")?;
    stdin.write_all(
        br#"
---
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: first
spec:
  nodes: {}
---
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: selected
spec:
  nodes: {}
"#,
    )?;

    let output = child.wait_with_output()?;
    assert!(output.status.success());
    let payload = serde_json::from_slice::<Value>(&output.stdout)?;

    assert_eq!(payload.pointer("/ok").and_then(Value::as_bool), Some(true));
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

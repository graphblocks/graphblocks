use graphblocks_runtime_core::stdlib_runtime::run_stdlib_graph_json;
use serde_json::{Value, json};

#[test]
fn rust_stdlib_runtime_executes_prompt_render_graph() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-prompt-render"},
        "spec": {
            "interface": {
                "inputs": {"message": "graphblocks.ai/Text@1"},
                "outputs": {"prompt": "graphblocks.ai/Text@1"}
            },
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Test {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"}
                }
            }
        }
    });
    let result = run_graph(&graph, &json!({"message": {"text": "ok"}}))?;

    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["outputs"], json!({"prompt": "Test ok"}));
    Ok(())
}

#[test]
fn rust_stdlib_runtime_matches_shared_runtime_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("../../../tck/runtime/cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "runtime TCK root must be an array".to_owned())?;

    for case in cases {
        let case_name = case
            .get("name")
            .and_then(Value::as_str)
            .ok_or_else(|| "runtime TCK case missing name".to_owned())?;
        let document = case
            .get("document")
            .ok_or_else(|| format!("runtime TCK case {case_name} missing document"))?;
        let expected = case
            .get("expected")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("runtime TCK case {case_name} missing expected object"))?;
        let inputs = case.get("inputs").cloned().unwrap_or_else(|| json!({}));
        let result = run_graph(document, &inputs)?;
        let terminal_kind = result
            .get("journal")
            .and_then(Value::as_array)
            .and_then(|journal| {
                journal
                    .iter()
                    .rev()
                    .find(|record| record.get("terminal").and_then(Value::as_bool) == Some(true))
            })
            .and_then(|record| record.get("kind"))
            .and_then(Value::as_str);

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            expected.get("status").and_then(Value::as_str),
            "runtime TCK case {case_name} status mismatch",
        );
        assert_eq!(
            terminal_kind,
            expected.get("terminal_kind").and_then(Value::as_str),
            "runtime TCK case {case_name} terminal kind mismatch",
        );
        assert_eq!(
            result.get("outputs"),
            expected.get("outputs"),
            "runtime TCK case {case_name} outputs mismatch",
        );
    }

    Ok(())
}

fn run_graph(graph: &Value, inputs: &Value) -> Result<Value, String> {
    let graph_json = serde_json::to_string(graph).map_err(|error| error.to_string())?;
    let inputs_json = serde_json::to_string(inputs).map_err(|error| error.to_string())?;
    let result_json =
        run_stdlib_graph_json(&graph_json, &inputs_json).map_err(|error| error.to_string())?;
    serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())
}

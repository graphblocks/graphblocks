use serde_json::{Value, json};

pub(crate) fn evaluate_case(case: &Value) -> Result<Value, String> {
    let case_object = case
        .as_object()
        .ok_or_else(|| "tool-execution TCK case must be an object".to_owned())?;
    let case_name = case_object
        .get("name")
        .and_then(Value::as_str)
        .ok_or_else(|| "tool-execution TCK case requires name".to_owned())?;
    if case_object.get("kind").and_then(Value::as_str) != Some("execution_plan") {
        return Err(format!(
            "tool-execution TCK case {case_name} requires kind execution_plan"
        ));
    }
    let operations = case_object
        .get("operations")
        .cloned()
        .unwrap_or_else(|| json!([]));
    if !operations.is_array() {
        return Err(format!(
            "tool-execution TCK case {case_name} operations must be an array"
        ));
    }
    let case_json = serde_json::to_string(case)
        .map_err(|error| format!("tool-execution TCK case {case_name}: {error}"))?;
    let operations_json = serde_json::to_string(&operations)
        .map_err(|error| format!("tool-execution TCK case {case_name}: {error}"))?;

    let result = match super::evaluate_tool_execution_plan_json(&case_json, &operations_json) {
        Ok(result_json) => serde_json::from_str::<Value>(&result_json)
            .map_err(|error| format!("tool-execution TCK case {case_name}: {error}"))?,
        Err(error) => {
            let error_text = error.to_string();
            let creation_error = [
                "unsafe_parallel_effects",
                "effect_conflict",
                "duplicate_dependency",
                "parallelism_exhausted",
                "dependencies_not_ready",
                "tool_call_not_pending",
                "tool_call_not_running",
                "tool_execution_plan_error",
            ]
            .into_iter()
            .find(|code| error_text.contains(&format!("plan error {code}:")))
            .ok_or_else(|| {
                format!(
                    "tool-execution TCK case {case_name} failed without a stable creation error: {error_text}"
                )
            })?;
            return Ok(json!({
                "creationError": creation_error,
                "operations": [],
            }));
        }
    };
    let result_object = result.as_object().ok_or_else(|| {
        format!("tool-execution TCK case {case_name} native result must be an object")
    })?;
    let raw_operation_results = result_object
        .get("operations")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            format!("tool-execution TCK case {case_name} native result requires operations")
        })?;
    let mut operation_results = Vec::with_capacity(raw_operation_results.len());
    for (operation_index, raw_result) in raw_operation_results.iter().enumerate() {
        let raw_result = raw_result.as_object().ok_or_else(|| {
            format!(
                "tool-execution TCK case {case_name} native operation {operation_index} must be an object"
            )
        })?;
        let op = raw_result.get("op").and_then(Value::as_str).ok_or_else(|| {
            format!(
                "tool-execution TCK case {case_name} native operation {operation_index} requires op"
            )
        })?;
        let index = raw_result
            .get("index")
            .and_then(Value::as_u64)
            .ok_or_else(|| {
                format!(
                    "tool-execution TCK case {case_name} native operation {operation_index} requires index"
                )
            })?;
        match op {
            "ready" => operation_results.push(json!({
                "index": index,
                "op": op,
                "ready": raw_result.get("ready").cloned().unwrap_or_else(|| json!([])),
            })),
            "policy_stop" => operation_results.push(json!({
                "index": index,
                "op": op,
                "affected": raw_result.get("affected").cloned().unwrap_or_else(|| json!([])),
            })),
            "start" | "complete" | "fail" | "deny" | "expire" | "cancel" | "policy_stopped" => {
                operation_results.push(json!({
                    "index": index,
                    "op": op,
                    "error": raw_result.get("error").cloned().unwrap_or(Value::Null),
                }))
            }
            _ => {
                return Err(format!(
                    "tool-execution TCK case {case_name} native operation {operation_index} has unknown op {op}"
                ));
            }
        }
    }
    let states = result_object.get("states").cloned().ok_or_else(|| {
        format!("tool-execution TCK case {case_name} native result requires states")
    })?;
    if !states.is_object() {
        return Err(format!(
            "tool-execution TCK case {case_name} native states must be an object"
        ));
    }
    Ok(json!({
        "creationError": Value::Null,
        "operations": operation_results,
        "states": states,
    }))
}

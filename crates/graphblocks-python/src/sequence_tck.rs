use graphblocks_runtime_core::bounded::{SequenceError, SequenceState, bounded_sequence};
use serde_json::{Value, json};

pub(crate) fn evaluate_case(case: &Value) -> Result<Value, String> {
    let case_object = case
        .as_object()
        .ok_or_else(|| "sequence TCK case must be an object".to_owned())?;
    let case_name = case_object
        .get("name")
        .and_then(Value::as_str)
        .ok_or_else(|| "sequence TCK case requires name".to_owned())?;
    let capacity = case_object
        .get("capacity")
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("sequence TCK case {case_name} requires capacity"))?
        .try_into()
        .map_err(|_| format!("sequence TCK case {case_name} capacity is too large"))?;

    let (sender, receiver) = match bounded_sequence::<String>(capacity) {
        Ok(sequence) => sequence,
        Err(error) => {
            let creation_error = match error {
                SequenceError::InvalidCapacity => "invalid_capacity",
                SequenceError::Full { .. }
                | SequenceError::Closed { .. }
                | SequenceError::AlreadyTerminal { .. } => "unexpected_runtime_error",
            };
            return Ok(json!({"creation_error": creation_error}));
        }
    };
    let operations = case_object
        .get("operations")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("sequence TCK case {case_name} requires operations"))?;
    let state_name = |state: &SequenceState| match state {
        SequenceState::Open => "open",
        SequenceState::Completed => "completed",
        SequenceState::Failed(_) => "failed",
        SequenceState::Cancelled(_) => "cancelled",
    };
    let mut operation_results = Vec::with_capacity(operations.len());
    for (operation_index, operation) in operations.iter().enumerate() {
        let operation = operation.as_object().ok_or_else(|| {
            format!("sequence TCK case {case_name} operation {operation_index} must be an object")
        })?;
        let op = operation.get("op").and_then(Value::as_str).ok_or_else(|| {
            format!("sequence TCK case {case_name} operation {operation_index} requires op")
        })?;
        match op {
            "send" => {
                let value = operation
                    .get("value")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        format!(
                            "sequence TCK case {case_name} operation {operation_index} requires string value"
                        )
                    })?;
                let result = match sender.try_send(value.to_owned()) {
                    Ok(()) => "ok".to_owned(),
                    Err(SequenceError::Full { .. }) => "full".to_owned(),
                    Err(SequenceError::Closed { state }) => {
                        format!("closed_{}", state_name(&state))
                    }
                    Err(SequenceError::AlreadyTerminal { state }) => {
                        format!("already_terminal_{}", state_name(&state))
                    }
                    Err(SequenceError::InvalidCapacity) => "invalid_capacity".to_owned(),
                };
                operation_results.push(json!({
                    "op": "send",
                    "result": result,
                    "len": receiver.len(),
                }));
            }
            "recv" => {
                operation_results.push(json!({
                    "op": "recv",
                    "value": receiver.try_recv(),
                    "len": receiver.len(),
                }));
            }
            "complete" => {
                let result = match sender.complete() {
                    Ok(()) => "ok".to_owned(),
                    Err(SequenceError::Full { .. }) => "full".to_owned(),
                    Err(SequenceError::Closed { state }) => {
                        format!("closed_{}", state_name(&state))
                    }
                    Err(SequenceError::AlreadyTerminal { state }) => {
                        format!("already_terminal_{}", state_name(&state))
                    }
                    Err(SequenceError::InvalidCapacity) => "invalid_capacity".to_owned(),
                };
                operation_results.push(json!({
                    "op": "complete",
                    "result": result,
                    "len": receiver.len(),
                }));
            }
            _ => {
                return Err(format!(
                    "sequence TCK case {case_name} operation {operation_index} has unknown op {op}"
                ));
            }
        }
    }

    Ok(json!({
        "state": state_name(&receiver.state()),
        "len": receiver.len(),
        "operationResults": operation_results,
    }))
}

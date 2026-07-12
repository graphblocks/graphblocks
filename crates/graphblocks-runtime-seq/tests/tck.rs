use graphblocks_runtime_seq::bounded::{SequenceError, SequenceState, bounded_sequence};
use serde_json::Value;

#[test]
fn rust_sequence_runtime_matches_shared_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("fixtures/sequence-cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "sequence TCK root must be an array".to_owned())?;

    for case in cases {
        run_case(case)?;
    }

    Ok(())
}

fn run_case(case: &Value) -> Result<(), String> {
    let name = required_str(case, "name", "sequence TCK case")?;
    let capacity = required_u64(case, "capacity", name)? as usize;
    let expected = case
        .get("expected")
        .and_then(Value::as_object)
        .ok_or_else(|| format!("sequence TCK case {name} is missing expected result"))?;

    match bounded_sequence::<String>(capacity) {
        Ok((sender, receiver)) => {
            if let Some(error) = expected.get("creation_error").and_then(Value::as_str) {
                return Err(format!(
                    "sequence TCK case {name} expected creation error {error}, but sequence was created"
                ));
            }

            let operations = case
                .get("operations")
                .and_then(Value::as_array)
                .ok_or_else(|| format!("sequence TCK case {name} is missing operations"))?;
            for operation in operations {
                let op = required_str(operation, "op", name)?;
                match op {
                    "send" => {
                        let value = required_str(operation, "value", name)?;
                        let expected_result = required_str(operation, "expect", name)?;
                        let observed = send_result_name(sender.try_send(value.to_owned()));
                        assert_eq!(observed, expected_result, "{name}");
                    }
                    "recv" => {
                        let expected_value = operation.get("value").and_then(Value::as_str);
                        assert_eq!(receiver.try_recv().as_deref(), expected_value, "{name}");
                    }
                    "complete" => {
                        let expected_result = required_str(operation, "expect", name)?;
                        let observed = send_result_name(sender.complete());
                        assert_eq!(observed, expected_result, "{name}");
                    }
                    other => {
                        return Err(format!(
                            "sequence TCK case {name} has unknown operation {other}"
                        ));
                    }
                }
                if let Some(expected_len) = operation.get("len").and_then(Value::as_u64) {
                    assert_eq!(receiver.len(), expected_len as usize, "{name}");
                    assert_eq!(sender.len(), expected_len as usize, "{name}");
                }
            }

            if let Some(expected_state) = expected.get("state").and_then(Value::as_str) {
                assert_eq!(state_name(&receiver.state()), expected_state, "{name}");
                assert_eq!(state_name(&sender.state()), expected_state, "{name}");
            }
        }
        Err(error) => {
            let expected_error = expected
                .get("creation_error")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    format!("sequence TCK case {name} failed to create sequence: {error:?}")
                })?;
            assert_eq!(creation_error_name(&error), expected_error, "{name}");
        }
    }

    Ok(())
}

fn required_str<'a>(value: &'a Value, key: &str, owner: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{owner} is missing string field {key}"))
}

fn required_u64(value: &Value, key: &str, owner: &str) -> Result<u64, String> {
    value
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("{owner} is missing integer field {key}"))
}

fn send_result_name(result: Result<(), SequenceError>) -> String {
    match result {
        Ok(()) => "ok".to_owned(),
        Err(SequenceError::Full { .. }) => "full".to_owned(),
        Err(SequenceError::Closed { state }) => format!("closed_{}", state_name(&state)),
        Err(SequenceError::AlreadyTerminal { state }) => {
            format!("already_terminal_{}", state_name(&state))
        }
        Err(SequenceError::InvalidCapacity) => "invalid_capacity".to_owned(),
    }
}

fn creation_error_name(error: &SequenceError) -> &'static str {
    match error {
        SequenceError::InvalidCapacity => "invalid_capacity",
        SequenceError::Full { .. }
        | SequenceError::Closed { .. }
        | SequenceError::AlreadyTerminal { .. } => "unexpected_runtime_error",
    }
}

fn state_name(state: &SequenceState) -> &'static str {
    match state {
        SequenceState::Open => "open",
        SequenceState::Completed => "completed",
        SequenceState::Failed(_) => "failed",
        SequenceState::Cancelled(_) => "cancelled",
    }
}

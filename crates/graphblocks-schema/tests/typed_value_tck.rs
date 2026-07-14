use graphblocks_schema::{TypedValue, TypedValueError};
use serde_json::Value;

#[test]
fn rust_typed_values_match_shared_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("fixtures/typed-values.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "typed value schema TCK root must be an array".to_owned())?;

    for case in cases {
        let name = required_str(case, "name", "typed value schema TCK case")?;
        let schema = required_str(case, "schema", name)?;
        let value = case
            .get("value")
            .cloned()
            .ok_or_else(|| format!("typed value schema TCK case {name} missing value"))?;
        let expected = case
            .get("expected")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("typed value schema TCK case {name} missing expected"))?;

        match TypedValue::new(schema, value) {
            Ok(typed_value) => {
                assert!(
                    !expected.contains_key("error"),
                    "{name}: unexpectedly accepted invalid typed value"
                );
                assert_eq!(
                    expected.get("canonical_value"),
                    Some(&typed_value.canonical_value()),
                    "{name}",
                );
                assert_eq!(
                    expected.get("canonical_json").and_then(Value::as_str),
                    Some(typed_value.canonical_json().as_str()),
                    "{name}",
                );
            }
            Err(error) => {
                assert_eq!(
                    expected.get("error").and_then(Value::as_str),
                    Some(schema_error_name(&error)),
                    "{name}: {error}"
                );
            }
        }
    }

    Ok(())
}

fn schema_error_name(error: &TypedValueError) -> &'static str {
    match error {
        TypedValueError::SchemaId(_) => "SchemaIdError",
        TypedValueError::CanonicalJson(_) => "CanonicalJsonError",
    }
}

fn required_str<'a>(value: &'a Value, key: &str, owner: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{owner} is missing string field {key}"))
}

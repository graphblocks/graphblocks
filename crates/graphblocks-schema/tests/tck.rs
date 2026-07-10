use graphblocks_schema::SchemaId;
use serde_json::Value;

#[test]
fn rust_schema_matches_shared_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("fixtures/cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "schema TCK root must be an array".to_owned())?;

    for case in cases {
        let name = required_str(case, "name", "schema TCK case")?;
        let schema_id = required_str(case, "schema_id", name)?;
        let expected = case
            .get("expected")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("schema TCK case {name} is missing expected result"))?;
        let observed = SchemaId::parse(schema_id);
        match observed {
            Ok(schema_id) => {
                assert_eq!(
                    expected.get("valid").and_then(Value::as_bool),
                    Some(true),
                    "{name}"
                );
                assert_eq!(
                    expected.get("canonical").and_then(Value::as_str),
                    Some(schema_id.as_str()),
                    "{name}"
                );
                assert_eq!(
                    expected.get("name").and_then(Value::as_str),
                    Some(schema_id.name()),
                    "{name}"
                );
                assert_eq!(
                    expected.get("major_version").and_then(Value::as_u64),
                    Some(schema_id.major_version() as u64),
                    "{name}"
                );
            }
            Err(error) => {
                assert_eq!(
                    expected.get("valid").and_then(Value::as_bool),
                    Some(false),
                    "{name}"
                );
                assert_eq!(
                    expected.get("error").and_then(Value::as_str),
                    Some("SchemaIdError"),
                    "{name}: {error}"
                );
            }
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

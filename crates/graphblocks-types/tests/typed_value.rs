use graphblocks_schema::SchemaIdError;
use graphblocks_types::TypedValue;
use serde_json::Value;
use serde_json::json;

#[test]
fn typed_value_preserves_schema_id_and_round_trips_json() -> Result<(), Box<dyn std::error::Error>>
{
    let value = TypedValue::new("schemas/Message@1", json!({"text": "hello"}))?;

    assert_eq!(value.schema_id().as_str(), "schemas/Message@1");
    assert_eq!(value.value(), &json!({"text": "hello"}));

    let encoded = serde_json::to_value(&value)?;
    assert_eq!(
        encoded,
        json!({
            "schema": "schemas/Message@1",
            "value": {"text": "hello"},
        }),
    );

    let decoded: TypedValue = serde_json::from_value(encoded)?;
    assert_eq!(decoded, value);
    Ok(())
}

#[test]
fn typed_value_rejects_invalid_schema_id() {
    assert_eq!(
        TypedValue::new("schemas/Message", json!({}))
            .expect_err("schema id without a version must be rejected"),
        SchemaIdError::MissingVersion,
    );
}

#[test]
fn typed_value_deserialization_rejects_unknown_envelope_fields() {
    let error = serde_json::from_value::<TypedValue>(json!({
        "schema": "schemas/Message@1",
        "value": {"text": "hello"},
        "unexpected": true,
    }))
    .expect_err("unknown typed-value envelope fields must fail closed");

    assert!(error.to_string().contains("unknown field"));
}

#[test]
fn typed_value_matches_shared_tck_cases() -> Result<(), Box<dyn std::error::Error>> {
    let cases: Vec<Value> = serde_json::from_str(include_str!("fixtures/typed-values.json"))?;

    for case in cases {
        let name = case
            .get("name")
            .and_then(Value::as_str)
            .expect("case has a name");
        let schema = case
            .get("schema")
            .and_then(Value::as_str)
            .expect("case has a schema");
        let value = case.get("value").cloned().expect("case has a value");
        let expected = case
            .get("expected")
            .and_then(Value::as_object)
            .expect("case has expected result");

        let observed = TypedValue::new(schema, value);
        if expected.contains_key("error") {
            assert!(observed.is_err(), "{name}");
            continue;
        }

        let observed = observed?;
        let expected_canonical_value = expected
            .get("canonical_value")
            .expect("expected canonical value");
        assert_eq!(
            &observed.canonical_value(),
            expected_canonical_value,
            "{name}"
        );
        assert_eq!(
            observed.to_canonical_json(),
            expected
                .get("canonical_json")
                .and_then(Value::as_str)
                .expect("expected canonical json"),
            "{name}"
        );
    }

    Ok(())
}

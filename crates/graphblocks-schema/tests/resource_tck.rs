use graphblocks_schema::{
    RESOURCE_SCHEMA_PATHS, ResourceValidationError, resource_schema_errors, resource_schema_path,
    validate_resource,
};
use serde_json::{Value, json};
use std::error::Error;
use std::{fs, path::Path};

const RESOURCE_CASES: &str = include_str!("fixtures/resources.json");

#[test]
fn rust_resource_validator_accepts_shared_positive_tck_cases() -> Result<(), Box<dyn Error>> {
    for case in resource_cases()? {
        let expected = required_object(&case, "expected")?;
        if expected.get("valid").and_then(Value::as_bool) != Some(true) {
            continue;
        }

        let name = required_str(&case, "name")?;
        let document = case
            .get("document")
            .ok_or_else(|| format!("{name} is missing document"))?;
        let errors = resource_schema_errors(document)?;

        assert!(errors.is_empty(), "{name}: {errors:?}");
        validate_resource(document).map_err(|error| format!("{name}: {error}"))?;
    }
    Ok(())
}

#[test]
fn rust_resource_validator_matches_shared_negative_tck_cases() -> Result<(), Box<dyn Error>> {
    for case in resource_cases()? {
        let expected = required_object(&case, "expected")?;
        if expected.get("valid").and_then(Value::as_bool) != Some(false) {
            continue;
        }

        let name = required_str(&case, "name")?;
        let document = case
            .get("document")
            .ok_or_else(|| format!("{name} is missing document"))?;
        let errors = resource_schema_errors(document)?;
        let actual = errors
            .iter()
            .map(|error| {
                json!({
                    "code": error.code.as_str(),
                    "path": error.path.as_str(),
                    "keyword": error.keyword.as_str(),
                })
            })
            .collect::<Vec<_>>();

        assert_eq!(
            actual,
            expected
                .get("errors")
                .and_then(Value::as_array)
                .ok_or_else(|| format!("{name} is missing expected errors"))?
                .to_owned(),
            "{name}",
        );
        let validation_error = match validate_resource(document) {
            Ok(()) => return Err(format!("{name}: negative resource TCK case passed").into()),
            Err(error) => error,
        };
        assert_eq!(validation_error.violations(), errors, "{name}");
    }
    Ok(())
}

#[test]
fn resource_type_selection_is_exact_and_deterministic() {
    for descriptor in RESOURCE_SCHEMA_PATHS {
        assert_eq!(
            resource_schema_path(descriptor.api_version, descriptor.kind),
            Some(descriptor.path),
        );
    }

    assert_eq!(
        resource_schema_path("graphblocks.ai/v1alpha3", "Binding"),
        None
    );
    assert_eq!(resource_schema_path("graphblocks.ai/v9", "Graph"), None);
    assert_eq!(
        resource_schema_path("graphblocks.ai/v1alpha3", "graph"),
        None
    );
}

#[test]
fn envelope_errors_have_stable_field_order() -> Result<(), Box<dyn Error>> {
    let errors = resource_schema_errors(&json!({"kind": 42, "apiVersion": null}))?;
    assert_eq!(
        errors
            .iter()
            .map(|error| (
                error.code.as_str(),
                error.path.as_str(),
                error.keyword.as_str()
            ))
            .collect::<Vec<_>>(),
        vec![
            ("GB0012", "$.apiVersion", "type"),
            ("GB0012", "$.kind", "type"),
        ],
    );
    Ok(())
}

#[test]
fn embedded_schema_assets_are_byte_identical_to_workspace_schemas() -> Result<(), Box<dyn Error>> {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    let workspace_schemas = manifest.join("../../schemas");
    if !workspace_schemas.is_dir() {
        return Ok(());
    }

    for descriptor in RESOURCE_SCHEMA_PATHS {
        assert_eq!(
            fs::read(manifest.join("schemas").join(descriptor.path))?,
            fs::read(workspace_schemas.join(descriptor.path))?,
            "{}",
            descriptor.path,
        );
    }
    Ok(())
}

#[test]
fn validation_error_exposes_normalized_violations() {
    let document = json!({
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "Binding",
        "metadata": {"name": "missing-resources"},
        "spec": {},
    });
    let result = validate_resource(&document);
    assert!(result.is_err(), "invalid binding must fail");
    let Err(error) = result else {
        return;
    };

    assert!(matches!(error, ResourceValidationError::Violations(_)));
    assert_eq!(
        error
            .violations()
            .iter()
            .map(|violation| (violation.path.as_str(), violation.keyword.as_str()))
            .collect::<Vec<_>>(),
        vec![("$.spec", "required")],
    );
}

fn resource_cases() -> Result<Vec<Value>, Box<dyn Error>> {
    let cases = serde_json::from_str::<Value>(RESOURCE_CASES)?;
    cases
        .as_array()
        .cloned()
        .ok_or_else(|| "resource schema TCK root must be an array".into())
}

fn required_object<'a>(
    value: &'a Value,
    key: &str,
) -> Result<&'a serde_json::Map<String, Value>, String> {
    value
        .get(key)
        .and_then(Value::as_object)
        .ok_or_else(|| format!("resource TCK case is missing object field {key}"))
}

fn required_str<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("resource TCK case is missing string field {key}"))
}

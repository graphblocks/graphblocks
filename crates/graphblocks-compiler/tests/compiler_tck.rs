use graphblocks_compiler::compiler::{compile_graph, compile_graph_with_catalog, BlockCatalog};
use graphblocks_compiler::diagnostics::Severity;
use serde_json::Value;

#[test]
fn rust_compiler_matches_shared_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("../../../tck/compiler/cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "compiler TCK root must be an array".to_owned())?;

    for case in cases {
        let name = required_str(case, "name")?;
        let document = case
            .get("document")
            .ok_or_else(|| format!("compiler TCK case {name} missing document"))?;
        let plan = if let Some(block_catalog) = case.get("block_catalog") {
            let catalog = BlockCatalog::from_blocks(block_catalog).map_err(|error| {
                format!("compiler TCK case {name} invalid block catalog: {error}")
            })?;
            compile_graph_with_catalog(document, &catalog)
        } else {
            compile_graph(document)
        };
        let expected = case
            .get("expected")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("compiler TCK case {name} missing expected"))?;
        assert_eq!(
            plan.graph_hash,
            required_map_str(expected, "graph_hash")?,
            "{name}",
        );
        assert_eq!(
            diagnostic_codes(&plan.diagnostics, Severity::Error),
            string_array(expected, "error_codes")?,
            "{name}",
        );
        assert_eq!(
            diagnostic_codes(&plan.diagnostics, Severity::Warning),
            optional_string_array(expected, "warning_codes")?,
            "{name}",
        );
    }

    Ok(())
}

fn diagnostic_codes(
    diagnostics: &[graphblocks_compiler::diagnostics::Diagnostic],
    severity: Severity,
) -> Vec<String> {
    diagnostics
        .iter()
        .filter(|diagnostic| diagnostic.severity == severity)
        .map(|diagnostic| diagnostic.code.clone())
        .collect()
}

fn required_str<'a>(value: &'a Value, field: &str) -> Result<&'a str, String> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing string field {field}"))
}

fn required_map_str<'a>(
    value: &'a serde_json::Map<String, Value>,
    field: &str,
) -> Result<&'a str, String> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing string field {field}"))
}

fn string_array(
    value: &serde_json::Map<String, Value>,
    field: &str,
) -> Result<Vec<String>, String> {
    value
        .get(field)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("missing array field {field}"))?
        .iter()
        .map(|item| {
            item.as_str()
                .map(str::to_owned)
                .ok_or_else(|| format!("field {field} must contain only strings"))
        })
        .collect()
}

fn optional_string_array(
    value: &serde_json::Map<String, Value>,
    field: &str,
) -> Result<Vec<String>, String> {
    match value.get(field) {
        Some(_) => string_array(value, field),
        None => Ok(Vec::new()),
    }
}

use graphblocks_compiler::compiler::{BlockCatalog, compile_graph, compile_graph_with_catalog};
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
        let name = case
            .get("name")
            .and_then(Value::as_str)
            .ok_or_else(|| "compiler TCK case is missing name".to_owned())?;
        let document = case
            .get("document")
            .ok_or_else(|| format!("compiler TCK case {name} is missing document"))?;
        let expected = case
            .get("expected")
            .ok_or_else(|| format!("compiler TCK case {name} is missing expected result"))?;
        let expected_hash = expected
            .get("graph_hash")
            .and_then(Value::as_str)
            .ok_or_else(|| format!("compiler TCK case {name} is missing expected graph_hash"))?;
        let expected_error_codes = expected
            .get("error_codes")
            .and_then(Value::as_array)
            .ok_or_else(|| format!("compiler TCK case {name} is missing expected error_codes"))?
            .iter()
            .map(|code| {
                code.as_str()
                    .ok_or_else(|| format!("compiler TCK case {name} has a non-string error code"))
            })
            .collect::<Result<Vec<_>, _>>()?;

        let plan = if let Some(block_catalog) = case.get("block_catalog") {
            let block_catalog = BlockCatalog::from_blocks(block_catalog)?;
            compile_graph_with_catalog(document, &block_catalog)
        } else {
            compile_graph(document)
        };
        let actual_error_codes = plan
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>();

        assert_eq!(plan.graph_hash, expected_hash, "{name}");
        assert_eq!(actual_error_codes, expected_error_codes, "{name}");
    }

    Ok(())
}

use std::collections::BTreeMap;

use graphblocks_compiler::compiler::compile_graph_with_catalog;
use graphblocks_compiler::diagnostics::Severity;
use graphblocks_runtime_core::stdlib_blocks::{
    ModelGenerate, ModelGenerateConfig, ModelGenerateInputs, ModelResponseValue, PromptValue,
    stable_stdlib_block_catalog,
};
use graphblocks_runtime_core::stdlib_runtime::{StdlibRunOptions, run_stdlib_graph_with_options};
use graphblocks_runtime_core::typed_graph::{
    Block, GraphBuilder, GraphDocument, GraphValue, NodeConfig, NodeInputReference, NodeInputs,
    NodeOutputFactory, NodeOutputs, Port, PortType, TypedBlockDescriptor, TypedGraphError,
    TypedPortDescriptor,
};
use serde_json::{Map, Value, json};

const CONTRACT_VERSION: &str = "graphblocks.typed-ports.tck.v1";

pub(crate) fn evaluate_case(case: &Value) -> Result<Value, String> {
    let case = case
        .as_object()
        .ok_or_else(|| "typed-ports TCK case must be an object".to_owned())?;
    if case.len() != 3
        || !case.contains_key("name")
        || !case.contains_key("scenario")
        || !case.contains_key("expected")
    {
        return Err(
            "typed-ports TCK case must contain exactly name, scenario, and expected".to_owned(),
        );
    }
    let case_name = required_str(case, "name")?;
    let scenario = required_str(case, "scenario")?;
    if !case.get("expected").is_some_and(Value::is_object) {
        return Err(format!(
            "typed-ports TCK case {case_name} expected must be an object"
        ));
    }

    match scenario {
        "compile_stdlib_model_generate" => evaluate_compile_scenario(scenario),
        "run_stdlib_model_generate" => evaluate_runtime_scenario(scenario),
        "reject_cross_builder_port" => evaluate_cross_builder_scenario(case_name, scenario),
        "reject_noncanonical_schema" => evaluate_noncanonical_schema_scenario(case_name, scenario),
        "reject_catalog_type_mismatch" => {
            evaluate_catalog_type_mismatch_scenario(case_name, scenario)
        }
        other => Err(format!(
            "typed-ports TCK case {case_name} has unknown scenario {other:?}"
        )),
    }
}

fn evaluate_compile_scenario(scenario: &str) -> Result<Value, String> {
    let graph = build_model_generate_graph()?;
    let catalog = stable_stdlib_block_catalog()
        .map_err(|error| format!("stable stdlib catalog is invalid: {error}"))?;
    let plan = compile_graph_with_catalog(graph.as_value(), &catalog);
    let diagnostics = plan
        .diagnostics
        .iter()
        .map(|diagnostic| {
            json!({
                "code": diagnostic.code,
                "message": diagnostic.message,
                "path": diagnostic.path,
                "severity": severity_name(diagnostic.severity),
            })
        })
        .collect::<Vec<_>>();
    let plan_ok = plan.ok();
    let graph_hash = plan.graph_hash;
    let normalized = plan.normalized;

    Ok(json!({
        "contractVersion": CONTRACT_VERSION,
        "ok": plan_ok,
        "scenario": scenario,
        "graph": normalized,
        "plan": {
            "ok": plan_ok,
            "graphHash": graph_hash,
            "diagnostics": diagnostics,
            "graph": normalized,
        },
    }))
}

fn evaluate_runtime_scenario(scenario: &str) -> Result<Value, String> {
    let graph = build_model_generate_graph()?;
    let catalog = stable_stdlib_block_catalog()
        .map_err(|error| format!("stable stdlib catalog is invalid: {error}"))?;
    let plan = compile_graph_with_catalog(graph.as_value(), &catalog);
    if !plan.ok() {
        return Err("typed stdlib model.generate graph did not compile".to_owned());
    }
    let interface = plan
        .normalized
        .pointer("/spec/interface")
        .and_then(Value::as_object)
        .ok_or_else(|| "compiled typed graph has no interface object".to_owned())?;
    let input_types = interface
        .get("inputs")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| "compiled typed graph has no input type map".to_owned())?;
    let output_types = interface
        .get("outputs")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| "compiled typed graph has no output type map".to_owned())?;
    let inputs = json!({"prompt": "ignored by scripted response"});
    let result = run_stdlib_graph_with_options(
        &graph,
        &inputs,
        &StdlibRunOptions::default().with_run_id("typed-ports-run-1"),
    )
    .map_err(|error| format!("typed stdlib runtime failed: {error}"))?;
    let journal_kinds = result
        .journal
        .iter()
        .enumerate()
        .map(|(index, record)| {
            record
                .get("kind")
                .and_then(Value::as_str)
                .map(|kind| {
                    if kind == "node_completed" {
                        "node_succeeded".to_owned()
                    } else {
                        kind.to_owned()
                    }
                })
                .ok_or_else(|| format!("typed runtime journal record {index} has no kind"))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let terminal_kind = result
        .journal
        .iter()
        .rev()
        .find_map(|record| {
            let kind = record.get("kind").and_then(Value::as_str)?;
            (record.get("terminal").and_then(Value::as_bool) == Some(true)
                || kind.starts_with("run_"))
            .then(|| kind.to_owned())
        })
        .ok_or_else(|| "typed runtime journal has no terminal record".to_owned())?;

    Ok(json!({
        "contractVersion": CONTRACT_VERSION,
        "ok": true,
        "scenario": scenario,
        "runId": result.run_id,
        "graphHash": result.graph_hash,
        "inputTypes": input_types,
        "inputs": inputs,
        "outputTypes": output_types,
        "outputs": result.outputs,
        "status": result.status.as_str(),
        "journalKinds": journal_kinds,
        "terminalKind": terminal_kind,
    }))
}

fn evaluate_cross_builder_scenario(case_name: &str, scenario: &str) -> Result<Value, String> {
    let result = (|| {
        let mut first = GraphBuilder::new("typed-ports-first")?;
        let foreign_prompt = first.input::<PromptValue>("prompt")?;
        let mut second = GraphBuilder::new("typed-ports-second")?;
        second
            .add(
                "generate",
                ModelGenerate::new(ModelGenerateConfig::new(json!("unused"))),
                ModelGenerateInputs {
                    prompt: foreign_prompt,
                },
            )
            .map(|_| ())
    })();
    rejected_contract(case_name, scenario, result)
}

fn evaluate_noncanonical_schema_scenario(case_name: &str, scenario: &str) -> Result<Value, String> {
    let result = (|| {
        let mut graph = GraphBuilder::new("typed-ports-invalid-schema")?;
        graph.input::<NonCanonicalPrompt>("prompt").map(|_| ())
    })();
    rejected_contract(case_name, scenario, result)
}

fn evaluate_catalog_type_mismatch_scenario(
    case_name: &str,
    scenario: &str,
) -> Result<Value, String> {
    let result = (|| {
        let descriptor = TypedBlockDescriptor::new(
            CatalogMismatch::ID,
            std::iter::empty(),
            [TypedPortDescriptor::required::<PromptValue>("response")],
        )?;
        let mut graph =
            GraphBuilder::with_custom_blocks("typed-ports-catalog-mismatch", [descriptor])?;
        graph
            .add("mismatch", CatalogMismatch, EmptyInputs)
            .map(|_| ())
    })();
    rejected_contract(case_name, scenario, result)
}

fn rejected_contract(
    case_name: &str,
    scenario: &str,
    result: Result<(), TypedGraphError>,
) -> Result<Value, String> {
    let error = result.map_or_else(Ok, |_| {
        Err(format!(
            "typed-ports TCK case {case_name} unexpectedly accepted scenario {scenario}"
        ))
    })?;
    Ok(json!({
        "contractVersion": CONTRACT_VERSION,
        "ok": false,
        "scenario": scenario,
        "errorCategory": typed_graph_error_category(&error),
    }))
}

fn build_model_generate_graph() -> Result<GraphDocument, String> {
    let mut graph = GraphBuilder::new("typed-model-generate")
        .map_err(|error| format!("failed to create typed graph: {error}"))?;
    let prompt = graph
        .input::<PromptValue>("prompt")
        .map_err(|error| format!("failed to add typed graph input: {error}"))?;
    let generated = graph
        .add(
            "generate",
            ModelGenerate::new(ModelGenerateConfig::new(json!("typed response"))),
            ModelGenerateInputs { prompt },
        )
        .map_err(|error| format!("failed to add typed stdlib block: {error}"))?;
    graph
        .bind_output::<ModelResponseValue>("response", &generated.response)
        .map_err(|error| format!("failed to bind typed graph output: {error}"))?;
    Ok(graph.build())
}

fn typed_graph_error_category(error: &TypedGraphError) -> &'static str {
    match error {
        TypedGraphError::EmptyName { .. } => "empty_name",
        TypedGraphError::DuplicateName { .. } => "duplicate_name",
        TypedGraphError::InvalidOutputSource { .. } => "invalid_output_source",
        TypedGraphError::UnknownOutputNode { .. } => "unknown_output_node",
        TypedGraphError::DuplicateOutputPort { .. } => "duplicate_output_port",
        TypedGraphError::CrossBuilderPort { .. } => "cross_builder_port",
        TypedGraphError::InvalidSchema { .. } => "invalid_schema",
        TypedGraphError::InvalidCatalog { .. } => "invalid_catalog",
        TypedGraphError::DuplicateBlockDescriptor { .. } => "duplicate_block_descriptor",
        TypedGraphError::UnknownBlock { .. } => "unknown_block",
        TypedGraphError::UntrustedStdlibBlock { .. } => "untrusted_stdlib_block",
        TypedGraphError::UnknownBlockPort { .. } => "unknown_block_port",
        TypedGraphError::MissingRequiredBlockInput { .. } => "missing_required_block_input",
        TypedGraphError::MissingRequiredBlockOutput { .. } => "missing_required_block_output",
        TypedGraphError::BlockPortTypeMismatch { .. } => "block_port_type_mismatch",
        TypedGraphError::UnknownInputSource { .. } => "unknown_input_source",
    }
}

fn severity_name(severity: Severity) -> &'static str {
    match severity {
        Severity::Error => "error",
        Severity::Warning => "warning",
        Severity::Info => "info",
    }
}

fn required_str<'a>(object: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.trim() == *value)
        .ok_or_else(|| format!("typed-ports TCK case requires canonical string field {key}"))
}

struct NonCanonicalPrompt;

impl PortType for NonCanonicalPrompt {
    const TYPE_REF: &'static str = "graphblocks.ai/Prompt";
}

impl GraphValue for NonCanonicalPrompt {}

struct EmptyConfig;

impl NodeConfig for EmptyConfig {
    fn to_config_object(&self) -> Map<String, Value> {
        Map::new()
    }
}

struct EmptyInputs;

impl NodeInputs for EmptyInputs {
    fn into_node_inputs(self) -> BTreeMap<String, NodeInputReference> {
        BTreeMap::new()
    }
}

struct CatalogMismatch;

struct CatalogMismatchOutputs {
    _response: Port<ModelResponseValue>,
}

impl NodeOutputs for CatalogMismatchOutputs {
    fn port_types() -> BTreeMap<String, &'static str> {
        BTreeMap::from([("response".to_owned(), ModelResponseValue::TYPE_REF)])
    }

    fn from_factory(factory: &NodeOutputFactory<'_>) -> Result<Self, TypedGraphError> {
        Ok(Self {
            _response: factory.port("response")?,
        })
    }
}

impl Block for CatalogMismatch {
    const ID: &'static str = "test.catalog_mismatch@1";

    type Inputs = EmptyInputs;
    type Config = EmptyConfig;
    type Outputs = CatalogMismatchOutputs;

    fn config(&self) -> &Self::Config {
        &EmptyConfig
    }
}

#[cfg(test)]
mod tests {
    use serde_json::Value;

    #[test]
    fn evaluator_matches_every_local_fixture_exactly() -> Result<(), String> {
        pyo3::Python::initialize();
        let local = serde_json::from_str::<Value>(include_str!("fixtures/typed-ports-cases.json"))
            .map_err(|error| error.to_string())?;
        let cases = local
            .as_array()
            .ok_or_else(|| "typed-ports TCK fixture must be an array".to_owned())?;
        if cases.len() != 5 {
            return Err(format!(
                "typed-ports TCK fixture must contain 5 cases, found {}",
                cases.len()
            ));
        }

        for case in cases {
            let input = serde_json::to_string(case).map_err(|error| error.to_string())?;
            let output = crate::evaluate_typed_ports_tck_case_json(&input)
                .map_err(|error| error.to_string())?;
            let actual =
                serde_json::from_str::<Value>(&output).map_err(|error| error.to_string())?;
            let expected = case
                .get("expected")
                .ok_or_else(|| "typed-ports TCK case requires expected".to_owned())?;
            if &actual != expected {
                let name = case
                    .get("name")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "typed-ports TCK case name is invalid".to_owned())?;
                return Err(format!(
                    "typed-ports TCK case {name} mismatch: expected {expected}, got {actual}"
                ));
            }
        }
        Ok(())
    }
}

use std::collections::BTreeMap;

use graphblocks_compiler::compiler::compile_graph;
use graphblocks_compiler::diagnostics::Severity;
use graphblocks_runtime_core::outcome::{BlockError, ErrorCategory, Outcome};
use graphblocks_runtime_core::readiness::{InputDependency, PortRef};
use graphblocks_runtime_core::scheduler::{ScheduledNode, StartedNode};
use graphblocks_runtime_core::test_runtime::{InProcessTestRuntime, NodeExecutor, TestRunStatus};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use serde_json::{Value, json};

#[pyfunction]
fn binding_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pyfunction]
fn compile_graph_json(document_json: &str) -> PyResult<String> {
    let document = serde_json::from_str::<Value>(document_json)
        .map_err(|error| PyValueError::new_err(format!("invalid graph document JSON: {error}")))?;
    let plan = compile_graph(&document);
    let diagnostics = plan
        .diagnostics
        .iter()
        .map(|diagnostic| {
            let severity = match diagnostic.severity {
                Severity::Error => "error",
                Severity::Warning => "warning",
                Severity::Info => "info",
            };
            json!({
                "code": diagnostic.code.as_str(),
                "message": diagnostic.message.as_str(),
                "path": diagnostic.path.as_str(),
                "severity": severity,
            })
        })
        .collect::<Vec<_>>();
    let payload = json!({
        "hash": plan.graph_hash,
        "ok": plan.ok(),
        "diagnostics": diagnostics,
        "graph": plan.normalized,
    });

    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!("failed to serialize compiler result: {error}"))
    })
}

struct JsonNodeExecutor {
    outputs_by_node: BTreeMap<String, Value>,
}

impl NodeExecutor for JsonNodeExecutor {
    fn execute(&mut self, node: StartedNode) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        let Some(outputs) = self
            .outputs_by_node
            .get(&node.node_id)
            .and_then(Value::as_object)
        else {
            return Err(BlockError::new(
                format!("{}.missing_fixture", node.node_id),
                ErrorCategory::Configuration,
                "node output fixture must be an object",
                false,
            ));
        };

        Ok(outputs
            .iter()
            .map(|(port, value)| {
                (
                    PortRef::new(node.node_id.clone(), port.clone()),
                    Outcome::Value(value.clone()),
                )
            })
            .collect())
    }
}

#[pyfunction]
fn run_test_graph_json(
    graph_json: &str,
    inputs_json: &str,
    node_outputs_json: &str,
) -> PyResult<String> {
    let graph = serde_json::from_str::<Value>(graph_json)
        .map_err(|error| PyValueError::new_err(format!("invalid graph document JSON: {error}")))?;
    let inputs = serde_json::from_str::<Value>(inputs_json)
        .map_err(|error| PyValueError::new_err(format!("invalid runtime inputs JSON: {error}")))?;
    let node_outputs = serde_json::from_str::<Value>(node_outputs_json)
        .map_err(|error| PyValueError::new_err(format!("invalid node outputs JSON: {error}")))?;
    let Some(node_outputs) = node_outputs.as_object() else {
        return Err(PyValueError::new_err(
            "node outputs JSON must be an object keyed by node id",
        ));
    };
    let plan = compile_graph(&graph);
    if !plan.ok() {
        let error_codes = plan
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        return Err(PyValueError::new_err(format!(
            "graph did not compile: {error_codes}"
        )));
    }

    let spec = plan
        .normalized
        .get("spec")
        .and_then(Value::as_object)
        .ok_or_else(|| PyValueError::new_err("normalized graph spec must be an object"))?;
    let nodes = spec
        .get("nodes")
        .and_then(Value::as_object)
        .ok_or_else(|| PyValueError::new_err("normalized graph nodes must be an object"))?;
    let edges = spec
        .get("edges")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut dependencies_by_node = nodes
        .keys()
        .map(|node_id| (node_id.clone(), Vec::new()))
        .collect::<BTreeMap<_, _>>();

    for edge in &edges {
        let Some(edge) = edge.as_object() else {
            continue;
        };
        let (Some(source), Some(target)) = (
            edge.get("from").and_then(Value::as_str),
            edge.get("to").and_then(Value::as_str),
        ) else {
            continue;
        };
        let (source_owner, source_path) = source.split_once('.').unwrap_or((source, ""));
        let (target_owner, target_path) = target.split_once('.').unwrap_or((target, ""));
        if target_owner.starts_with('$') {
            continue;
        }
        if source_owner.starts_with('$') && source_owner != "$input" {
            continue;
        }
        let Some(source_port) = source_path
            .split('.')
            .next()
            .filter(|port| !port.is_empty())
        else {
            return Err(PyValueError::new_err(format!(
                "edge source {source:?} must include a port"
            )));
        };
        let Some(target_input) = target_path
            .split('.')
            .next()
            .filter(|port| !port.is_empty())
        else {
            return Err(PyValueError::new_err(format!(
                "edge target {target:?} must include an input"
            )));
        };
        let Some(dependencies) = dependencies_by_node.get_mut(target_owner) else {
            return Err(PyValueError::new_err(format!(
                "edge target references unknown node {target_owner:?}"
            )));
        };
        dependencies.push(InputDependency::value(
            target_input,
            PortRef::new(source_owner, source_port),
        ));
    }

    let scheduled_nodes = dependencies_by_node
        .into_iter()
        .map(|(node_id, dependencies)| ScheduledNode::new(node_id, dependencies))
        .collect::<Vec<_>>();
    let mut runtime =
        InProcessTestRuntime::new("run-000001", scheduled_nodes).map_err(|error| {
            PyValueError::new_err(format!("failed to create test runtime: {error:?}"))
        })?;
    if let Some(input_object) = inputs.as_object() {
        for (input_name, value) in input_object {
            runtime = runtime.with_initial_value(PortRef::new("$input", input_name), value.clone());
        }
    }
    let mut executor = JsonNodeExecutor {
        outputs_by_node: node_outputs
            .iter()
            .map(|(node_id, outputs)| (node_id.clone(), outputs.clone()))
            .collect(),
    };
    let result = runtime.run(&mut executor).map_err(|error| {
        PyRuntimeError::new_err(format!("test runtime execution failed: {error:?}"))
    })?;
    let status = match result.status {
        TestRunStatus::Succeeded => "succeeded",
        TestRunStatus::Failed => "failed",
        TestRunStatus::Cancelled => "cancelled",
    };
    let mut output_values = json!({});

    if result.status == TestRunStatus::Succeeded {
        for edge in &edges {
            let Some(edge) = edge.as_object() else {
                continue;
            };
            let (Some(source), Some(target)) = (
                edge.get("from").and_then(Value::as_str),
                edge.get("to").and_then(Value::as_str),
            ) else {
                continue;
            };
            let (source_owner, source_path) = source.split_once('.').unwrap_or((source, ""));
            let (target_owner, target_path) = target.split_once('.').unwrap_or((target, ""));
            if target_owner != "$output" {
                continue;
            }
            let mut value = if source_owner == "$input" {
                inputs.clone()
            } else {
                executor
                    .outputs_by_node
                    .get(source_owner)
                    .cloned()
                    .ok_or_else(|| {
                        PyRuntimeError::new_err(format!(
                            "output edge references missing node output {source_owner:?}"
                        ))
                    })?
            };
            if !source_path.is_empty() {
                for part in source_path.split('.') {
                    value = value.get(part).cloned().ok_or_else(|| {
                        PyRuntimeError::new_err(format!(
                            "output edge source {source:?} is missing path segment {part:?}"
                        ))
                    })?;
                }
            }
            let target_parts = target_path.split('.').collect::<Vec<_>>();
            if target_parts.is_empty() || target_parts.iter().any(|part| part.is_empty()) {
                return Err(PyValueError::new_err(format!(
                    "output edge target {target:?} must include an output path"
                )));
            }
            let mut current = &mut output_values;
            for part in &target_parts[..target_parts.len() - 1] {
                let Some(current_object) = current.as_object_mut() else {
                    return Err(PyRuntimeError::new_err(format!(
                        "output path conflict at {target:?}"
                    )));
                };
                current = current_object
                    .entry((*part).to_owned())
                    .or_insert_with(|| json!({}));
            }
            let Some(current_object) = current.as_object_mut() else {
                return Err(PyRuntimeError::new_err(format!(
                    "output path conflict at {target:?}"
                )));
            };
            current_object.insert(target_parts[target_parts.len() - 1].to_owned(), value);
        }
    }

    let journal = result
        .journal
        .records()
        .iter()
        .map(|record| {
            json!({
                "recordId": record.record_id.as_str(),
                "runId": record.run_id.as_str(),
                "runSequence": record.run_sequence,
                "kind": record.kind.as_str(),
                "causationId": record.causation_id.as_deref(),
                "nodeId": record.node_id.as_deref(),
                "attemptId": record.attempt_id.as_deref(),
                "leaseEpoch": record.lease_epoch,
                "payload": record.payload.as_ref(),
                "terminal": record.terminal,
            })
        })
        .collect::<Vec<_>>();
    let payload = json!({
        "runId": result.run_id,
        "graphHash": plan.graph_hash,
        "status": status,
        "outputs": output_values,
        "journal": journal,
    });

    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!("failed to serialize runtime result: {error}"))
    })
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add_function(wrap_pyfunction!(binding_version, module)?)?;
    module.add_function(wrap_pyfunction!(compile_graph_json, module)?)?;
    module.add_function(wrap_pyfunction!(run_test_graph_json, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::{compile_graph_json, run_test_graph_json};

    #[test]
    fn compile_graph_json_matches_shared_tck_cases() -> Result<(), String> {
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
                .ok_or_else(|| {
                    format!("compiler TCK case {name} is missing expected graph_hash")
                })?;
            let expected_error_codes = expected
                .get("error_codes")
                .and_then(Value::as_array)
                .ok_or_else(|| format!("compiler TCK case {name} is missing expected error_codes"))?
                .iter()
                .map(|code| {
                    code.as_str().ok_or_else(|| {
                        format!("compiler TCK case {name} has a non-string error code")
                    })
                })
                .collect::<Result<Vec<_>, _>>()?;

            let document_json =
                serde_json::to_string(document).map_err(|error| error.to_string())?;
            let compiled_json =
                compile_graph_json(&document_json).map_err(|error| error.to_string())?;
            let compiled =
                serde_json::from_str::<Value>(&compiled_json).map_err(|error| error.to_string())?;
            let diagnostics = compiled
                .get("diagnostics")
                .and_then(Value::as_array)
                .ok_or_else(|| {
                    format!("compiler bridge result for {name} is missing diagnostics")
                })?;
            let actual_error_codes = diagnostics
                .iter()
                .filter(|diagnostic| {
                    diagnostic.get("severity").and_then(Value::as_str) == Some("error")
                })
                .map(|diagnostic| {
                    diagnostic
                        .get("code")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            format!("compiler bridge result for {name} has an invalid code")
                        })
                })
                .collect::<Result<Vec<_>, _>>()?;

            assert_eq!(
                compiled.get("hash").and_then(Value::as_str),
                Some(expected_hash),
                "{name}"
            );
            assert_eq!(actual_error_codes, expected_error_codes, "{name}");
        }

        Ok(())
    }

    #[test]
    fn run_test_graph_json_executes_compiled_graph_with_fixture_outputs() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-runtime-bridge"},
            "spec": {
                "nodes": {
                    "model": {
                        "block": "model.generate@1",
                        "inputs": {"prompt": "render.prompt"},
                        "outputs": {"response": "$output.answer"}
                    },
                    "render": {
                        "block": "prompt.render@1",
                        "inputs": {"message": "$input.message"}
                    }
                }
            }
        });
        let node_outputs = json!({
            "render": {"prompt": "rendered"},
            "model": {"response": "generated"}
        });

        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let node_outputs_json =
            serde_json::to_string(&node_outputs).map_err(|error| error.to_string())?;
        let result_json =
            run_test_graph_json(&graph_json, r#"{"message":"hello"}"#, &node_outputs_json)
                .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let journal = result
            .get("journal")
            .and_then(Value::as_array)
            .ok_or_else(|| "runtime bridge result is missing journal".to_owned())?;
        let completed_nodes = journal
            .iter()
            .filter(|record| record.get("kind").and_then(Value::as_str) == Some("node_completed"))
            .map(|record| {
                record
                    .get("nodeId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "node_completed record is missing nodeId".to_owned())
            })
            .collect::<Result<Vec<_>, _>>()?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            result
                .get("outputs")
                .and_then(|outputs| outputs.get("answer"))
                .and_then(Value::as_str),
            Some("generated")
        );
        assert_eq!(completed_nodes, vec!["render", "model"]);

        Ok(())
    }

    #[test]
    fn run_test_graph_json_blocks_missing_external_inputs() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-runtime-missing-input"},
            "spec": {
                "nodes": {
                    "render": {
                        "block": "prompt.render@1",
                        "inputs": {"message": "$input.message"},
                        "outputs": {"prompt": "$output.prompt"}
                    }
                }
            }
        });
        let node_outputs = json!({"render": {"prompt": "rendered"}});
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let node_outputs_json =
            serde_json::to_string(&node_outputs).map_err(|error| error.to_string())?;

        let result_json = run_test_graph_json(&graph_json, "{}", &node_outputs_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("status").and_then(Value::as_str), Some("failed"));
        assert_eq!(
            result
                .get("journal")
                .and_then(Value::as_array)
                .and_then(|journal| journal.last())
                .and_then(|record| record.get("kind"))
                .and_then(Value::as_str),
            Some("run_failed")
        );

        Ok(())
    }
}

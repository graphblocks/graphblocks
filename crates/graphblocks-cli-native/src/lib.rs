use graphblocks_compiler::compiler::compile_graph_with_catalog;
use graphblocks_compiler::diagnostics::Diagnostic;
use graphblocks_runtime_core::stdlib_blocks::stdlib_block_catalog;
use graphblocks_runtime_core::stdlib_runtime::run_stdlib_graph_json;
use serde::Deserialize;
use serde_json::Value;
use std::error::Error;
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum NativeDocumentError {
    EmptyInput,
    ParseFailed { message: String },
    MultipleDocuments { count: usize },
    GraphNotFound { name: String },
    MultipleGraphsNamed { name: String, count: usize },
}

impl fmt::Display for NativeDocumentError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyInput => write!(formatter, "native graph input is empty"),
            Self::ParseFailed { message } => {
                write!(formatter, "failed to parse native graph input: {message}")
            }
            Self::MultipleDocuments { count } => write!(
                formatter,
                "native graph input contains {count} documents; explicit graph selection is required"
            ),
            Self::GraphNotFound { name } => {
                write!(
                    formatter,
                    "native graph input does not contain graph {name:?}"
                )
            }
            Self::MultipleGraphsNamed { name, count } => write!(
                formatter,
                "native graph input contains {count} graphs named {name:?}; graph selection is ambiguous"
            ),
        }
    }
}

impl Error for NativeDocumentError {}

pub fn load_single_graph_document(input: &str) -> Result<Value, NativeDocumentError> {
    load_graph_document(input, None)
}

pub fn load_graph_document(
    input: &str,
    graph_name: Option<&str>,
) -> Result<Value, NativeDocumentError> {
    if input.trim().is_empty() {
        return Err(NativeDocumentError::EmptyInput);
    }

    let mut documents = Vec::new();
    for document in serde_yaml::Deserializer::from_str(input) {
        let value =
            Value::deserialize(document).map_err(|error| NativeDocumentError::ParseFailed {
                message: error.to_string(),
            })?;
        if !value.is_null() {
            documents.push(value);
        }
    }

    if let Some(graph_name) = graph_name {
        let mut selected = documents
            .into_iter()
            .filter(|document| {
                document.get("kind").and_then(Value::as_str) == Some("Graph")
                    && document.pointer("/metadata/name").and_then(Value::as_str)
                        == Some(graph_name)
            })
            .collect::<Vec<_>>();
        return match selected.len() {
            0 => Err(NativeDocumentError::GraphNotFound {
                name: graph_name.to_owned(),
            }),
            1 => Ok(selected.remove(0)),
            count => Err(NativeDocumentError::MultipleGraphsNamed {
                name: graph_name.to_owned(),
                count,
            }),
        };
    }

    match documents.len() {
        0 => Err(NativeDocumentError::EmptyInput),
        1 => Ok(documents.remove(0)),
        count => Err(NativeDocumentError::MultipleDocuments { count }),
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeCliMode {
    Validate,
    Plan { expand: bool },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeCliReport {
    pub ok: bool,
    pub graph_hash: Option<String>,
    pub normalized: Option<Value>,
    pub diagnostics: Vec<Diagnostic>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeRuntimeReport {
    pub ok: bool,
    pub result: Option<Value>,
    pub error: Option<String>,
}

pub fn run_compiler_workflow(document: &Value, mode: NativeCliMode) -> NativeCliReport {
    let block_catalog = match stdlib_block_catalog() {
        Ok(block_catalog) => block_catalog,
        Err(error) => {
            return NativeCliReport {
                ok: false,
                graph_hash: None,
                normalized: None,
                diagnostics: vec![Diagnostic::error(
                    "GB9001",
                    format!("failed to construct the built-in block catalog: {error}"),
                    "$.spec.nodes",
                )],
            };
        }
    };
    let plan = compile_graph_with_catalog(document, &block_catalog);
    let ok = plan.ok();
    let include_normalized = matches!(mode, NativeCliMode::Plan { expand: true });

    NativeCliReport {
        ok,
        graph_hash: ok.then_some(plan.graph_hash),
        normalized: (ok && include_normalized).then_some(plan.normalized),
        diagnostics: plan.diagnostics,
    }
}

pub fn run_stdlib_workflow(document: &Value, inputs: &Value) -> NativeRuntimeReport {
    let graph_json = match serde_json::to_string(document) {
        Ok(graph_json) => graph_json,
        Err(error) => {
            return NativeRuntimeReport {
                ok: false,
                result: None,
                error: Some(format!("failed to serialize graph document: {error}")),
            };
        }
    };
    let inputs_json = match serde_json::to_string(inputs) {
        Ok(inputs_json) => inputs_json,
        Err(error) => {
            return NativeRuntimeReport {
                ok: false,
                result: None,
                error: Some(format!("failed to serialize graph inputs: {error}")),
            };
        }
    };
    let result_json = match run_stdlib_graph_json(&graph_json, &inputs_json) {
        Ok(result_json) => result_json,
        Err(error) => {
            return NativeRuntimeReport {
                ok: false,
                result: None,
                error: Some(error.to_string()),
            };
        }
    };
    let result = match serde_json::from_str::<Value>(&result_json) {
        Ok(result) => result,
        Err(error) => {
            return NativeRuntimeReport {
                ok: false,
                result: None,
                error: Some(format!("runtime returned invalid JSON: {error}")),
            };
        }
    };
    let ok = result.get("status").and_then(Value::as_str) == Some("succeeded");
    NativeRuntimeReport {
        ok,
        result: Some(result),
        error: None,
    }
}

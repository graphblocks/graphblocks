use graphblocks_compiler::compiler::compile_graph_with_catalog;
use graphblocks_compiler::diagnostics::Diagnostic;
use graphblocks_runtime_core::stdlib_blocks::stdlib_block_catalog;
use graphblocks_runtime_core::stdlib_runtime::run_stdlib_graph_with_options_json;
use graphblocks_schema::{parse_canonical_json, validate_canonical_json_depth};
use serde::Deserialize;
use serde::de::{self, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Number, Value};
use std::collections::BTreeSet;
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

struct StrictYamlJsonValue(Value);

impl<'de> Deserialize<'de> for StrictYamlJsonValue {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct StrictYamlJsonValueVisitor;

        impl<'de> Visitor<'de> for StrictYamlJsonValueVisitor {
            type Value = StrictYamlJsonValue;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a YAML value representable as canonical JSON")
            }

            fn visit_bool<E>(self, value: bool) -> Result<Self::Value, E> {
                Ok(StrictYamlJsonValue(Value::Bool(value)))
            }

            fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E> {
                Ok(StrictYamlJsonValue(Value::Number(value.into())))
            }

            fn visit_i128<E>(self, value: i128) -> Result<Self::Value, E>
            where
                E: de::Error,
            {
                Number::from_i128(value)
                    .map(Value::Number)
                    .map(StrictYamlJsonValue)
                    .ok_or_else(|| E::custom("YAML integer is not representable as JSON"))
            }

            fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E> {
                Ok(StrictYamlJsonValue(Value::Number(value.into())))
            }

            fn visit_u128<E>(self, value: u128) -> Result<Self::Value, E>
            where
                E: de::Error,
            {
                Number::from_u128(value)
                    .map(Value::Number)
                    .map(StrictYamlJsonValue)
                    .ok_or_else(|| E::custom("YAML integer is not representable as JSON"))
            }

            fn visit_f64<E>(self, value: f64) -> Result<Self::Value, E>
            where
                E: de::Error,
            {
                Number::from_f64(value)
                    .map(Value::Number)
                    .map(StrictYamlJsonValue)
                    .ok_or_else(|| E::custom("non-finite YAML number is not representable as JSON"))
            }

            fn visit_str<E>(self, value: &str) -> Result<Self::Value, E>
            where
                E: de::Error,
            {
                self.visit_string(value.to_owned())
            }

            fn visit_string<E>(self, value: String) -> Result<Self::Value, E> {
                Ok(StrictYamlJsonValue(Value::String(value)))
            }

            fn visit_none<E>(self) -> Result<Self::Value, E> {
                Ok(StrictYamlJsonValue(Value::Null))
            }

            fn visit_some<D>(self, deserializer: D) -> Result<Self::Value, D::Error>
            where
                D: serde::Deserializer<'de>,
            {
                StrictYamlJsonValue::deserialize(deserializer)
            }

            fn visit_unit<E>(self) -> Result<Self::Value, E> {
                Ok(StrictYamlJsonValue(Value::Null))
            }

            fn visit_seq<A>(self, mut sequence: A) -> Result<Self::Value, A::Error>
            where
                A: SeqAccess<'de>,
            {
                let mut values = Vec::with_capacity(sequence.size_hint().unwrap_or(0));
                while let Some(value) = sequence.next_element::<StrictYamlJsonValue>()? {
                    values.push(value.0);
                }
                Ok(StrictYamlJsonValue(Value::Array(values)))
            }

            fn visit_map<A>(self, mut mapping: A) -> Result<Self::Value, A::Error>
            where
                A: MapAccess<'de>,
            {
                let mut keys = BTreeSet::new();
                let mut values = Map::with_capacity(mapping.size_hint().unwrap_or(0));
                while let Some(key) = mapping.next_key::<String>()? {
                    if !keys.insert(key.clone()) {
                        return Err(de::Error::custom(format!(
                            "duplicate YAML mapping key {key:?}"
                        )));
                    }
                    let value = mapping.next_value::<StrictYamlJsonValue>()?;
                    values.insert(key, value.0);
                }
                Ok(StrictYamlJsonValue(Value::Object(values)))
            }
        }

        deserializer.deserialize_any(StrictYamlJsonValueVisitor)
    }
}

pub fn load_single_graph_document(input: &str) -> Result<Value, NativeDocumentError> {
    load_graph_document(input, None)
}

pub fn load_graph_document(
    input: &str,
    graph_name: Option<&str>,
) -> Result<Value, NativeDocumentError> {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return Err(NativeDocumentError::EmptyInput);
    }

    let mut documents = if matches!(trimmed.as_bytes().first(), Some(b'{' | b'[')) {
        vec![
            parse_canonical_json(trimmed).map_err(|error| NativeDocumentError::ParseFailed {
                message: format!("invalid JSON: {error}"),
            })?,
        ]
    } else {
        let mut documents = Vec::new();
        for document in serde_yaml::Deserializer::from_str(input) {
            let value = StrictYamlJsonValue::deserialize(document)
                .map(|value| value.0)
                .map_err(|error| NativeDocumentError::ParseFailed {
                    message: error.to_string(),
                })?;
            validate_canonical_json_depth(&value).map_err(|error| {
                NativeDocumentError::ParseFailed {
                    message: format!("invalid YAML value: {error}"),
                }
            })?;
            if !value.is_null() {
                documents.push(value);
            }
        }
        documents
    };

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
    run_stdlib_workflow_with_options(document, inputs, &Value::Object(serde_json::Map::new()))
}

pub fn run_stdlib_workflow_with_options(
    document: &Value,
    inputs: &Value,
    options: &Value,
) -> NativeRuntimeReport {
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
    let options_json = match serde_json::to_string(options) {
        Ok(options_json) => options_json,
        Err(error) => {
            return NativeRuntimeReport {
                ok: false,
                result: None,
                error: Some(format!("failed to serialize runtime options: {error}")),
            };
        }
    };
    let result_json =
        match run_stdlib_graph_with_options_json(&graph_json, &inputs_json, &options_json) {
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
    let ok = matches!(
        result.get("status").and_then(Value::as_str),
        Some("succeeded" | "waiting_callback")
    );
    NativeRuntimeReport {
        ok,
        result: Some(result),
        error: None,
    }
}

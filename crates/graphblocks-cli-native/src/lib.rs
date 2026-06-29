use graphblocks_compiler::compiler::compile_graph;
use graphblocks_compiler::diagnostics::Diagnostic;
use graphblocks_runtime_core::stdlib_runtime::run_stdlib_graph_json;
use serde_json::Value;

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
    let plan = compile_graph(document);
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

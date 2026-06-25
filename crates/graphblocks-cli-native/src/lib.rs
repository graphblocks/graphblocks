use graphblocks_compiler::compiler::compile_graph;
use graphblocks_compiler::diagnostics::Diagnostic;
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

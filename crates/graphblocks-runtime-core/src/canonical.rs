use serde_json::Value;

pub(crate) fn canonical_hash(value: &Value) -> String {
    graphblocks_compiler::canonical::canonical_hash(value)
        .expect("runtime contract values must satisfy canonical JSON depth limits")
}

pub(crate) fn canonical_json(value: &Value) -> String {
    graphblocks_compiler::canonical::canonical_json(value)
        .expect("runtime contract values must satisfy canonical JSON depth limits")
}

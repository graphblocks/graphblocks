pub(crate) use graphblocks_compiler::canonical::CanonicalJsonError;
use serde_json::Value;

pub(crate) fn canonical_hash(value: &Value) -> Result<String, CanonicalJsonError> {
    graphblocks_compiler::canonical::canonical_hash(value)
}

pub(crate) fn canonical_json(value: &Value) -> Result<String, CanonicalJsonError> {
    graphblocks_compiler::canonical::canonical_json(value)
}

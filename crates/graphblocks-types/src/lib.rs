use graphblocks_schema::{SchemaId, SchemaIdError};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TypedValue {
    schema: SchemaId,
    value: Value,
}

impl TypedValue {
    pub fn new(schema_id: impl AsRef<str>, value: Value) -> Result<Self, SchemaIdError> {
        Ok(Self {
            schema: SchemaId::parse(schema_id)?,
            value,
        })
    }

    pub fn from_schema(schema: SchemaId, value: Value) -> Self {
        Self { schema, value }
    }

    pub fn schema_id(&self) -> &SchemaId {
        &self.schema
    }

    pub fn value(&self) -> &Value {
        &self.value
    }

    pub fn canonical_value(&self) -> Value {
        json!({
            "schema": self.schema.as_str(),
            "value": self.value,
        })
    }

    pub fn to_canonical_json(&self) -> String {
        graphblocks_compiler::canonical::canonical_json(&self.canonical_value())
    }

    pub fn into_value(self) -> Value {
        self.value
    }
}

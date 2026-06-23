use std::collections::{BTreeMap, BTreeSet};

use graphblocks_schema::{SchemaId, SchemaIdError};
use serde_json::Value;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum JsonSchemaType {
    Null,
    Boolean,
    Integer,
    Number,
    String,
    Array,
    Object,
}

impl JsonSchemaType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Null => "null",
            Self::Boolean => "boolean",
            Self::Integer => "integer",
            Self::Number => "number",
            Self::String => "string",
            Self::Array => "array",
            Self::Object => "object",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct JsonSchemaNode {
    expected_type: Option<JsonSchemaType>,
    properties: BTreeMap<String, JsonSchemaNode>,
    required: BTreeSet<String>,
    items: Option<Box<JsonSchemaNode>>,
}

impl JsonSchemaNode {
    pub fn any() -> Self {
        Self {
            expected_type: None,
            properties: BTreeMap::new(),
            required: BTreeSet::new(),
            items: None,
        }
    }

    pub fn string() -> Self {
        Self::typed(JsonSchemaType::String)
    }

    pub fn integer() -> Self {
        Self::typed(JsonSchemaType::Integer)
    }

    pub fn number() -> Self {
        Self::typed(JsonSchemaType::Number)
    }

    pub fn boolean() -> Self {
        Self::typed(JsonSchemaType::Boolean)
    }

    pub fn object() -> Self {
        Self::typed(JsonSchemaType::Object)
    }

    pub fn array(items: JsonSchemaNode) -> Self {
        Self {
            expected_type: Some(JsonSchemaType::Array),
            properties: BTreeMap::new(),
            required: BTreeSet::new(),
            items: Some(Box::new(items)),
        }
    }

    pub fn property(mut self, name: impl Into<String>, schema: JsonSchemaNode) -> Self {
        self.properties.insert(name.into(), schema);
        self
    }

    pub fn required_property(mut self, name: impl Into<String>, schema: JsonSchemaNode) -> Self {
        let name = name.into();
        self.required.insert(name.clone());
        self.properties.insert(name, schema);
        self
    }

    fn typed(expected_type: JsonSchemaType) -> Self {
        Self {
            expected_type: Some(expected_type),
            properties: BTreeMap::new(),
            required: BTreeSet::new(),
            items: None,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct JsonSchema {
    pub schema_id: String,
    pub root: JsonSchemaNode,
}

impl JsonSchema {
    pub fn new(schema_id: impl Into<String>, root: JsonSchemaNode) -> Self {
        Self {
            schema_id: schema_id.into(),
            root,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolSchemaRegistryError {
    InvalidSchemaId {
        schema_id: String,
        error: SchemaIdError,
    },
    DuplicateSchema {
        schema_id: String,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolSchemaValidationError {
    SchemaMissing {
        schema_id: String,
    },
    TypeMismatch {
        schema_id: String,
        path: String,
        expected: String,
    },
    RequiredPropertyMissing {
        schema_id: String,
        path: String,
        property: String,
    },
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ToolSchemaRegistry {
    schemas: BTreeMap<String, JsonSchema>,
}

impl ToolSchemaRegistry {
    pub fn new<I>(schemas: I) -> Result<Self, ToolSchemaRegistryError>
    where
        I: IntoIterator<Item = JsonSchema>,
    {
        let mut indexed = BTreeMap::new();
        for schema in schemas {
            let schema_id = schema.schema_id.clone();
            if let Err(error) = SchemaId::parse(&schema_id) {
                return Err(ToolSchemaRegistryError::InvalidSchemaId { schema_id, error });
            }
            if indexed.contains_key(&schema_id) {
                return Err(ToolSchemaRegistryError::DuplicateSchema { schema_id });
            }
            indexed.insert(schema_id, schema);
        }
        Ok(Self { schemas: indexed })
    }

    pub fn validate(
        &self,
        schema_id: impl AsRef<str>,
        value: &Value,
    ) -> Result<(), ToolSchemaValidationError> {
        let schema_id = schema_id.as_ref();
        let schema = self.schemas.get(schema_id).ok_or_else(|| {
            ToolSchemaValidationError::SchemaMissing {
                schema_id: schema_id.to_owned(),
            }
        })?;
        self.validate_node(schema_id, &schema.root, value, "$")
    }

    fn validate_node(
        &self,
        schema_id: &str,
        schema: &JsonSchemaNode,
        value: &Value,
        path: &str,
    ) -> Result<(), ToolSchemaValidationError> {
        if let Some(expected_type) = schema.expected_type
            && !Self::value_matches_type(value, expected_type)
        {
            return Err(ToolSchemaValidationError::TypeMismatch {
                schema_id: schema_id.to_owned(),
                path: path.to_owned(),
                expected: expected_type.as_str().to_owned(),
            });
        }

        if schema.expected_type == Some(JsonSchemaType::Object) {
            if let Some(object) = value.as_object() {
                for required in &schema.required {
                    if !object.contains_key(required) {
                        return Err(ToolSchemaValidationError::RequiredPropertyMissing {
                            schema_id: schema_id.to_owned(),
                            path: path.to_owned(),
                            property: required.clone(),
                        });
                    }
                }
                for (property, property_schema) in &schema.properties {
                    if let Some(property_value) = object.get(property) {
                        let property_path = format!("{path}.{property}");
                        self.validate_node(
                            schema_id,
                            property_schema,
                            property_value,
                            &property_path,
                        )?;
                    }
                }
            }
        }

        if schema.expected_type == Some(JsonSchemaType::Array)
            && let Some(items) = &schema.items
            && let Some(array) = value.as_array()
        {
            for (index, item) in array.iter().enumerate() {
                let item_path = format!("{path}[{index}]");
                self.validate_node(schema_id, items, item, &item_path)?;
            }
        }

        Ok(())
    }

    fn value_matches_type(value: &Value, expected_type: JsonSchemaType) -> bool {
        match expected_type {
            JsonSchemaType::Null => value.is_null(),
            JsonSchemaType::Boolean => value.is_boolean(),
            JsonSchemaType::Integer => value.as_i64().is_some() || value.as_u64().is_some(),
            JsonSchemaType::Number => value.is_number(),
            JsonSchemaType::String => value.is_string(),
            JsonSchemaType::Array => value.is_array(),
            JsonSchemaType::Object => value.is_object(),
        }
    }
}

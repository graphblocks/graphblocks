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

    fn from_str(value: &str) -> Option<Self> {
        match value {
            "null" => Some(Self::Null),
            "boolean" => Some(Self::Boolean),
            "integer" => Some(Self::Integer),
            "number" => Some(Self::Number),
            "string" => Some(Self::String),
            "array" => Some(Self::Array),
            "object" => Some(Self::Object),
            _ => None,
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

    pub fn from_json_schema_value(value: &Value) -> Result<Self, JsonSchemaParseError> {
        if !value.is_object() {
            return Err(JsonSchemaParseError::InvalidNode {
                path: "$".to_owned(),
                reason: "schema node must be an object".to_owned(),
            });
        }

        let mut node = match value.get("type") {
            None => JsonSchemaNode::any(),
            Some(Value::String(schema_type)) => {
                match JsonSchemaType::from_str(schema_type.as_str()).ok_or_else(|| {
                    JsonSchemaParseError::InvalidNode {
                        path: "$.type".to_owned(),
                        reason: format!("unsupported schema type {schema_type:?}"),
                    }
                })? {
                    JsonSchemaType::Null => JsonSchemaNode::typed(JsonSchemaType::Null),
                    JsonSchemaType::Boolean => JsonSchemaNode::boolean(),
                    JsonSchemaType::Integer => JsonSchemaNode::integer(),
                    JsonSchemaType::Number => JsonSchemaNode::number(),
                    JsonSchemaType::String => JsonSchemaNode::string(),
                    JsonSchemaType::Array => {
                        let item_schema = if let Some(items) = value.get("items") {
                            JsonSchemaNode::from_json_schema_value(items)
                                .map_err(|error| error.with_prefix("$.items"))?
                        } else {
                            JsonSchemaNode::any()
                        };
                        JsonSchemaNode::array(item_schema)
                    }
                    JsonSchemaType::Object => JsonSchemaNode::object(),
                }
            }
            Some(_) => {
                return Err(JsonSchemaParseError::InvalidNode {
                    path: "$.type".to_owned(),
                    reason: "schema type must be a string".to_owned(),
                });
            }
        };

        if value.get("properties").is_some() || value.get("required").is_some() {
            if let Some(expected_type) = node.expected_type {
                if expected_type != JsonSchemaType::Object {
                    return Err(JsonSchemaParseError::InvalidNode {
                        path: "$".to_owned(),
                        reason: "properties and required are only supported for object schemas"
                            .to_owned(),
                    });
                }
            } else {
                node.expected_type = Some(JsonSchemaType::Object);
            }
        }

        if let Some(properties) = value.get("properties") {
            let properties =
                properties
                    .as_object()
                    .ok_or_else(|| JsonSchemaParseError::InvalidNode {
                        path: "$.properties".to_owned(),
                        reason: "properties must be an object".to_owned(),
                    })?;
            for (property, property_schema) in properties {
                let property_node = JsonSchemaNode::from_json_schema_value(property_schema)
                    .map_err(|error| error.with_prefix(format!("$.properties.{property}")))?;
                node.properties.insert(property.clone(), property_node);
            }
        }

        if let Some(required) = value.get("required") {
            let required =
                required
                    .as_array()
                    .ok_or_else(|| JsonSchemaParseError::InvalidNode {
                        path: "$.required".to_owned(),
                        reason: "required must be an array".to_owned(),
                    })?;
            for (index, property) in required.iter().enumerate() {
                let property =
                    property
                        .as_str()
                        .ok_or_else(|| JsonSchemaParseError::InvalidNode {
                            path: format!("$.required[{index}]"),
                            reason: "required entries must be strings".to_owned(),
                        })?;
                node.required.insert(property.to_owned());
                node.properties
                    .entry(property.to_owned())
                    .or_insert_with(JsonSchemaNode::any);
            }
        }

        Ok(node)
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

    pub fn from_json_schema_value(
        schema_id: impl Into<String>,
        value: &Value,
    ) -> Result<Self, JsonSchemaParseError> {
        Ok(Self::new(
            schema_id,
            JsonSchemaNode::from_json_schema_value(value)?,
        ))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum JsonSchemaParseError {
    InvalidNode { path: String, reason: String },
}

impl JsonSchemaParseError {
    fn with_prefix(self, prefix: impl Into<String>) -> Self {
        match self {
            Self::InvalidNode { path, reason } => {
                let prefix = prefix.into();
                let path = if path == "$" {
                    prefix
                } else if let Some(suffix) = path.strip_prefix('$') {
                    format!("{prefix}{suffix}")
                } else {
                    format!("{prefix}.{path}")
                };
                Self::InvalidNode { path, reason }
            }
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

        if schema.expected_type == Some(JsonSchemaType::Object)
            && let Some(object) = value.as_object()
        {
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
                    self.validate_node(schema_id, property_schema, property_value, &property_path)?;
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

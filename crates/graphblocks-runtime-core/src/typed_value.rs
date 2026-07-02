use std::error::Error;
use std::fmt;

use serde_json::Value;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ValueEncoding {
    Json,
    MessagePack,
    ArrowIpc,
    RawBytes,
    ArtifactRef,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TypedValueError {
    InvalidSchemaId,
    InvalidSchemaVersion,
    JsonEncoding,
    InvalidJson,
    UnexpectedEncoding {
        expected: ValueEncoding,
        actual: ValueEncoding,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RemoteBoundaryValuePolicyError {
    NonSerializableInlineValue {
        node_id: String,
        port: String,
        encoding: ValueEncoding,
    },
    InlineValueTooLarge {
        node_id: String,
        port: String,
        size_bytes: usize,
        max_inline_bytes: usize,
    },
}

impl fmt::Display for RemoteBoundaryValuePolicyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NonSerializableInlineValue {
                node_id,
                port,
                encoding,
            } => write!(
                formatter,
                "remote boundary value {node_id}.{port} uses non-serializable inline encoding {encoding:?}"
            ),
            Self::InlineValueTooLarge {
                node_id,
                port,
                size_bytes,
                max_inline_bytes,
            } => write!(
                formatter,
                "remote boundary value {node_id}.{port} is {size_bytes} bytes, exceeding inline limit {max_inline_bytes}"
            ),
        }
    }
}

impl Error for RemoteBoundaryValuePolicyError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RemoteBoundaryValueDiagnostic {
    pub code: &'static str,
    pub node_id: String,
    pub port: String,
    pub message: String,
}

impl RemoteBoundaryValueDiagnostic {
    pub fn for_value(
        node_id: impl AsRef<str>,
        port: impl AsRef<str>,
        value: &TypedValue,
        policy: &RemoteBoundaryValuePolicy,
    ) -> Vec<Self> {
        match policy.validate(node_id, port, value) {
            Ok(()) => Vec::new(),
            Err(RemoteBoundaryValuePolicyError::NonSerializableInlineValue {
                node_id,
                port,
                ..
            }) => vec![Self {
                code: "GB7001",
                node_id,
                port,
                message: "remote boundary value must use a serializable encoding or ArtifactRef"
                    .to_owned(),
            }],
            Err(RemoteBoundaryValuePolicyError::InlineValueTooLarge {
                node_id,
                port,
                size_bytes,
                max_inline_bytes,
            }) => vec![Self {
                code: "GB7002",
                node_id,
                port,
                message: format!(
                    "remote boundary inline value is {size_bytes} bytes, exceeding configured limit {max_inline_bytes}; use ArtifactRef"
                ),
            }],
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RemoteBoundaryValuePolicy {
    pub max_inline_bytes: usize,
}

impl RemoteBoundaryValuePolicy {
    pub fn new(max_inline_bytes: usize) -> Self {
        Self { max_inline_bytes }
    }

    pub fn validate(
        &self,
        node_id: impl AsRef<str>,
        port: impl AsRef<str>,
        value: &TypedValue,
    ) -> Result<(), RemoteBoundaryValuePolicyError> {
        let node_id = node_id.as_ref();
        let port = port.as_ref();
        if value.encoding == ValueEncoding::ArtifactRef {
            return Ok(());
        }
        if value.encoding == ValueEncoding::RawBytes {
            return Err(RemoteBoundaryValuePolicyError::NonSerializableInlineValue {
                node_id: node_id.to_owned(),
                port: port.to_owned(),
                encoding: value.encoding,
            });
        }
        let size_bytes = value.payload.len();
        if size_bytes > self.max_inline_bytes {
            return Err(RemoteBoundaryValuePolicyError::InlineValueTooLarge {
                node_id: node_id.to_owned(),
                port: port.to_owned(),
                size_bytes,
                max_inline_bytes: self.max_inline_bytes,
            });
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TypedValue {
    schema_id: String,
    schema_version: u32,
    encoding: ValueEncoding,
    payload: Vec<u8>,
}

impl TypedValue {
    pub fn new(
        schema_id: impl Into<String>,
        schema_version: u32,
        encoding: ValueEncoding,
        payload: Vec<u8>,
    ) -> Self {
        Self {
            schema_id: schema_id.into(),
            schema_version,
            encoding,
            payload,
        }
    }

    pub fn try_new(
        schema_id: impl Into<String>,
        schema_version: u32,
        encoding: ValueEncoding,
        payload: Vec<u8>,
    ) -> Result<Self, TypedValueError> {
        let schema_id = schema_id.into();
        if schema_id.is_empty() {
            return Err(TypedValueError::InvalidSchemaId);
        }
        if schema_version == 0 {
            return Err(TypedValueError::InvalidSchemaVersion);
        }
        Ok(Self {
            schema_id,
            schema_version,
            encoding,
            payload,
        })
    }

    pub fn json(
        schema_id: impl Into<String>,
        schema_version: u32,
        value: Value,
    ) -> Result<Self, TypedValueError> {
        let payload = serde_json::to_vec(&value).map_err(|_| TypedValueError::JsonEncoding)?;
        Self::try_new(schema_id, schema_version, ValueEncoding::Json, payload)
    }

    pub fn schema_id(&self) -> &str {
        &self.schema_id
    }

    pub fn schema_version(&self) -> u32 {
        self.schema_version
    }

    pub fn encoding(&self) -> ValueEncoding {
        self.encoding
    }

    pub fn payload(&self) -> &[u8] {
        &self.payload
    }

    pub fn into_payload(self) -> Vec<u8> {
        self.payload
    }

    pub fn decode_json(&self) -> Result<Value, TypedValueError> {
        if self.encoding != ValueEncoding::Json {
            return Err(TypedValueError::UnexpectedEncoding {
                expected: ValueEncoding::Json,
                actual: self.encoding,
            });
        }
        serde_json::from_slice(&self.payload).map_err(|_| TypedValueError::InvalidJson)
    }
}

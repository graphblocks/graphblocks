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

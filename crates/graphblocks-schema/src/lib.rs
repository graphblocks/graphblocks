use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Value, json};
use std::error::Error;
use std::fmt::{self, Display, Formatter};
use std::str::FromStr;

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct SchemaId {
    raw: String,
    version_separator: usize,
    major_version: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SchemaIdError {
    Empty,
    MissingVersion,
    EmptyName,
    InvalidName,
    InvalidMajorVersion,
    NonCanonicalVersion,
}

impl SchemaId {
    pub fn parse(input: impl AsRef<str>) -> Result<Self, SchemaIdError> {
        let raw = input.as_ref();
        if raw.is_empty() {
            return Err(SchemaIdError::Empty);
        }
        if raw.trim() != raw {
            return Err(SchemaIdError::InvalidName);
        }

        let Some((name, version)) = raw.rsplit_once('@') else {
            return Err(SchemaIdError::MissingVersion);
        };
        if name.is_empty() {
            return Err(SchemaIdError::EmptyName);
        }
        if version.is_empty() || !version.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(SchemaIdError::InvalidMajorVersion);
        }
        if version.len() > 1 && version.starts_with('0') {
            return Err(SchemaIdError::NonCanonicalVersion);
        }

        let major_version = version
            .parse::<u32>()
            .map_err(|_| SchemaIdError::InvalidMajorVersion)?;
        if major_version == 0 {
            return Err(SchemaIdError::InvalidMajorVersion);
        }

        Ok(Self {
            raw: raw.to_owned(),
            version_separator: name.len(),
            major_version,
        })
    }

    pub fn as_str(&self) -> &str {
        &self.raw
    }

    pub fn name(&self) -> &str {
        &self.raw[..self.version_separator]
    }

    pub fn major_version(&self) -> u32 {
        self.major_version
    }
}

impl Display for SchemaId {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for SchemaId {
    type Err = SchemaIdError;

    fn from_str(input: &str) -> Result<Self, Self::Err> {
        Self::parse(input)
    }
}

impl TryFrom<String> for SchemaId {
    type Error = SchemaIdError;

    fn try_from(input: String) -> Result<Self, Self::Error> {
        Self::parse(input)
    }
}

impl TryFrom<&str> for SchemaId {
    type Error = SchemaIdError;

    fn try_from(input: &str) -> Result<Self, Self::Error> {
        Self::parse(input)
    }
}

impl Serialize for SchemaId {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(self.as_str())
    }
}

impl<'de> Deserialize<'de> for SchemaId {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let raw = String::deserialize(deserializer)?;
        Self::parse(raw).map_err(serde::de::Error::custom)
    }
}

impl Display for SchemaIdError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty => formatter.write_str("schema id must not be empty"),
            Self::MissingVersion => {
                formatter.write_str("schema id must include a major version suffix")
            }
            Self::EmptyName => formatter.write_str("schema id name must not be empty"),
            Self::InvalidName => formatter.write_str("schema id name is not canonical"),
            Self::InvalidMajorVersion => {
                formatter.write_str("schema id major version must be a positive integer")
            }
            Self::NonCanonicalVersion => {
                formatter.write_str("schema id major version must not use leading zeroes")
            }
        }
    }
}

impl Error for SchemaIdError {}

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

    pub fn canonical_json(&self) -> String {
        canonical_json(&self.canonical_value())
    }

    pub fn to_canonical_json(&self) -> String {
        self.canonical_json()
    }

    pub fn into_value(self) -> Value {
        self.value
    }
}

/// Serializes a JSON value using the cross-language GraphBlocks identity format.
pub fn canonical_json(value: &Value) -> String {
    match value {
        Value::Null => "null".to_owned(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => {
            let original = value.to_string();
            if !original.contains(['.', 'e', 'E']) {
                original
            } else {
                let (negative, unsigned) = original
                    .strip_prefix('-')
                    .map_or((false, original.as_str()), |unsigned| (true, unsigned));
                let (mantissa, exponent_text) = unsigned
                    .split_once(['e', 'E'])
                    .map_or((unsigned, "0"), |(mantissa, exponent)| (mantissa, exponent));
                let fractional_digits = mantissa
                    .split_once('.')
                    .map_or(0, |(_, fractional)| fractional.len());
                let digits = mantissa.replace('.', "");
                let significant = digits.trim_start_matches('0');
                if significant.is_empty() {
                    if negative {
                        "-0.0".to_owned()
                    } else {
                        "0.0".to_owned()
                    }
                } else {
                    let exponent_negative = exponent_text.starts_with('-');
                    let exponent_magnitude = exponent_text
                        .trim_start_matches(['+', '-'])
                        .trim_start_matches('0');
                    let exponent_magnitude = if exponent_magnitude.is_empty() {
                        "0"
                    } else {
                        exponent_magnitude
                    };
                    let exponent_delta = i128::try_from(significant.len())
                        .expect("JSON number length must fit in i128")
                        - i128::try_from(fractional_digits)
                            .expect("JSON fraction length must fit in i128")
                        - 1;
                    let delta_negative = exponent_delta.is_negative();
                    let delta_magnitude = exponent_delta.unsigned_abs().to_string();

                    let (result_negative, mut result_magnitude) = if exponent_magnitude == "0" {
                        (delta_negative, delta_magnitude)
                    } else if delta_magnitude == "0" {
                        (exponent_negative, exponent_magnitude.to_owned())
                    } else if exponent_negative == delta_negative {
                        let left = exponent_magnitude.as_bytes();
                        let right = delta_magnitude.as_bytes();
                        let mut carry = 0_u8;
                        let mut reversed = Vec::with_capacity(left.len().max(right.len()) + 1);
                        for index in 0..left.len().max(right.len()) {
                            let left_digit = left
                                .len()
                                .checked_sub(index + 1)
                                .map_or(0, |position| left[position] - b'0');
                            let right_digit = right
                                .len()
                                .checked_sub(index + 1)
                                .map_or(0, |position| right[position] - b'0');
                            let sum = left_digit + right_digit + carry;
                            reversed.push((sum % 10) + b'0');
                            carry = sum / 10;
                        }
                        if carry > 0 {
                            reversed.push(carry + b'0');
                        }
                        reversed.reverse();
                        (
                            exponent_negative,
                            String::from_utf8(reversed)
                                .expect("canonical exponent digits must be UTF-8"),
                        )
                    } else {
                        let exponent_is_larger = exponent_magnitude.len() > delta_magnitude.len()
                            || (exponent_magnitude.len() == delta_magnitude.len()
                                && exponent_magnitude >= delta_magnitude.as_str());
                        let (larger, smaller, negative) = if exponent_is_larger {
                            (
                                exponent_magnitude,
                                delta_magnitude.as_str(),
                                exponent_negative,
                            )
                        } else {
                            (delta_magnitude.as_str(), exponent_magnitude, delta_negative)
                        };
                        let larger = larger.as_bytes();
                        let smaller = smaller.as_bytes();
                        let mut borrow = 0_i8;
                        let mut reversed = Vec::with_capacity(larger.len());
                        for index in 0..larger.len() {
                            let mut digit =
                                (larger[larger.len() - index - 1] - b'0') as i8 - borrow;
                            let smaller_digit = smaller
                                .len()
                                .checked_sub(index + 1)
                                .map_or(0, |position| (smaller[position] - b'0') as i8);
                            if digit < smaller_digit {
                                digit += 10;
                                borrow = 1;
                            } else {
                                borrow = 0;
                            }
                            reversed.push((digit - smaller_digit) as u8 + b'0');
                        }
                        while reversed.len() > 1 && reversed.last() == Some(&b'0') {
                            reversed.pop();
                        }
                        reversed.reverse();
                        (
                            negative,
                            String::from_utf8(reversed)
                                .expect("canonical exponent digits must be UTF-8"),
                        )
                    };
                    let coefficient = significant.trim_end_matches('0');
                    let adjusted_exponent = result_magnitude.parse::<i32>().ok().map(|value| {
                        if result_negative && value != 0 {
                            -value
                        } else {
                            value
                        }
                    });
                    let mut output = String::with_capacity(original.len() + 2);
                    if negative {
                        output.push('-');
                    }
                    if let Some(exponent) =
                        adjusted_exponent.filter(|exponent| (-4..16).contains(exponent))
                    {
                        let decimal_point = exponent + 1;
                        if decimal_point <= 0 {
                            output.push_str("0.");
                            for _ in 0..usize::try_from(-decimal_point)
                                .expect("fixed decimal prefix must fit in usize")
                            {
                                output.push('0');
                            }
                            output.push_str(coefficient);
                        } else {
                            let decimal_point = usize::try_from(decimal_point)
                                .expect("fixed decimal point must fit in usize");
                            if decimal_point >= coefficient.len() {
                                output.push_str(coefficient);
                                for _ in 0..decimal_point - coefficient.len() {
                                    output.push('0');
                                }
                                output.push_str(".0");
                            } else {
                                output.push_str(&coefficient[..decimal_point]);
                                output.push('.');
                                output.push_str(&coefficient[decimal_point..]);
                            }
                        }
                    } else {
                        while result_magnitude.len() < 2 {
                            result_magnitude.insert(0, '0');
                        }
                        output.push(coefficient.as_bytes()[0] as char);
                        if coefficient.len() > 1 {
                            output.push('.');
                            output.push_str(&coefficient[1..]);
                        }
                        output.push('e');
                        output.push(if result_negative && result_magnitude != "00" {
                            '-'
                        } else {
                            '+'
                        });
                        output.push_str(&result_magnitude);
                    }
                    output
                }
            }
        }
        Value::String(value) => Value::String(value.clone()).to_string(),
        Value::Array(values) => {
            let mut output = String::from("[");
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                output.push_str(&canonical_json(value));
            }
            output.push(']');
            output
        }
        Value::Object(values) => {
            let mut entries = values.iter().collect::<Vec<_>>();
            entries.sort_by(|(left, _), (right, _)| left.cmp(right));

            let mut output = String::from("{");
            for (index, (key, value)) in entries.into_iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                output.push_str(&Value::String(key.clone()).to_string());
                output.push(':');
                output.push_str(&canonical_json(value));
            }
            output.push('}');
            output
        }
    }
}

#[cfg(feature = "resource-validation")]
mod resource_validation {
    use super::*;
    use std::sync::OnceLock;

    /// One supported GraphBlocks resource wire schema.
    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    pub struct ResourceSchemaDescriptor {
        pub api_version: &'static str,
        pub kind: &'static str,
        pub path: &'static str,
    }

    /// Exact resource types supported by this crate, in stable selection order.
    pub const RESOURCE_SCHEMA_PATHS: [ResourceSchemaDescriptor; 7] = [
        ResourceSchemaDescriptor {
            api_version: "graphblocks.ai/v1",
            kind: "Graph",
            path: "graphblocks.ai/v1/graph.schema.json",
        },
        ResourceSchemaDescriptor {
            api_version: "graphblocks.ai/v1",
            kind: "PluginManifest",
            path: "graphblocks.ai/v1/plugin-manifest.schema.json",
        },
        ResourceSchemaDescriptor {
            api_version: "graphblocks.ai/v1alpha3",
            kind: "Graph",
            path: "graphblocks.ai/v1alpha3/graph.schema.json",
        },
        ResourceSchemaDescriptor {
            api_version: "graphblocks.ai/v1alpha1",
            kind: "Application",
            path: "graphblocks.ai/v1alpha1/application.schema.json",
        },
        ResourceSchemaDescriptor {
            api_version: "graphblocks.ai/v1alpha1",
            kind: "Binding",
            path: "graphblocks.ai/v1alpha1/binding.schema.json",
        },
        ResourceSchemaDescriptor {
            api_version: "graphblocks.ai/v1alpha1",
            kind: "PluginManifest",
            path: "graphblocks.ai/v1alpha1/plugin-manifest.schema.json",
        },
        ResourceSchemaDescriptor {
            api_version: "graphblocks.ai/composition/v1alpha1",
            kind: "GraphFragment",
            path: "graphblocks.ai/composition/v1alpha1/graph-fragment.schema.json",
        },
    ];

    /// Returns the authoritative schema path for an exact resource type.
    #[must_use]
    pub fn resource_schema_path(api_version: &str, kind: &str) -> Option<&'static str> {
        schema_definition(api_version, kind).map(|schema| schema.descriptor.path)
    }

    /// A deterministic resource-schema validation failure.
    #[derive(Clone, Debug, Eq, PartialEq, Serialize)]
    pub struct ResourceSchemaViolation {
        pub code: String,
        pub path: String,
        pub keyword: String,
        pub message: String,
        pub schema_path: String,
    }

    /// A checked-in resource schema could not be parsed or compiled.
    #[derive(Clone, Debug, Eq, PartialEq)]
    pub struct ResourceSchemaError {
        path: &'static str,
        message: String,
    }

    impl ResourceSchemaError {
        #[must_use]
        pub fn path(&self) -> &'static str {
            self.path
        }

        #[must_use]
        pub fn message(&self) -> &str {
            &self.message
        }
    }

    impl Display for ResourceSchemaError {
        fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
            write!(
                formatter,
                "resource schema {} is not valid Draft 2020-12: {}",
                self.path, self.message
            )
        }
    }

    impl Error for ResourceSchemaError {}

    /// A resource failed validation or its authoritative schema was unavailable.
    #[derive(Clone, Debug, Eq, PartialEq)]
    pub enum ResourceValidationError {
        Schema(ResourceSchemaError),
        Violations(Vec<ResourceSchemaViolation>),
    }

    impl ResourceValidationError {
        #[must_use]
        pub fn violations(&self) -> &[ResourceSchemaViolation] {
            match self {
                Self::Schema(_) => &[],
                Self::Violations(violations) => violations,
            }
        }
    }

    impl Display for ResourceValidationError {
        fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
            match self {
                Self::Schema(error) => Display::fmt(error, formatter),
                Self::Violations(violations) => {
                    for (index, violation) in violations.iter().enumerate() {
                        if index > 0 {
                            formatter.write_str("; ")?;
                        }
                        write!(
                            formatter,
                            "{} {}: {}",
                            violation.code, violation.path, violation.message
                        )?;
                    }
                    Ok(())
                }
            }
        }
    }

    impl Error for ResourceValidationError {}

    /// Returns ordered violations for a versioned GraphBlocks resource.
    ///
    /// Selection is an exact match on `apiVersion` and `kind`. Schemas are embedded
    /// in the crate so validation has no filesystem or network dependency.
    pub fn resource_schema_errors(
        document: &Value,
    ) -> Result<Vec<ResourceSchemaViolation>, ResourceSchemaError> {
        let Value::Object(object) = document else {
            return Ok(vec![resource_violation(
                "GB0012",
                "$",
                "type",
                "resource must be an object",
                "$",
            )]);
        };

        let api_version = object.get("apiVersion").and_then(Value::as_str);
        let kind = object.get("kind").and_then(Value::as_str);
        let mut envelope_errors = Vec::new();
        if api_version.is_none() {
            envelope_errors.push(resource_violation(
                "GB0012",
                "$.apiVersion",
                "type",
                "apiVersion must be a string",
                "$",
            ));
        }
        if kind.is_none() {
            envelope_errors.push(resource_violation(
                "GB0012",
                "$.kind",
                "type",
                "kind must be a string",
                "$",
            ));
        }
        if !envelope_errors.is_empty() {
            return Ok(envelope_errors);
        }

        let (Some(api_version), Some(kind)) = (api_version, kind) else {
            return Ok(envelope_errors);
        };
        let Some(schema) = schema_definition(api_version, kind) else {
            return Ok(vec![resource_violation(
                "GB0013",
                "$",
                "resourceType",
                &format!("unsupported resource type {api_version:?}/{kind:?}"),
                "$",
            )]);
        };

        let compiled = schema.compiled()?;
        let mut violations = compiled
            .validator
            .iter_errors(document)
            .map(|error| schema_violation(&error, document, &compiled.document))
            .collect::<Vec<_>>();
        violations.sort_by(|left, right| {
            (&left.path, &left.schema_path, &left.keyword, &left.message).cmp(&(
                &right.path,
                &right.schema_path,
                &right.keyword,
                &right.message,
            ))
        });
        Ok(violations)
    }

    /// Validates a resource against its exact `apiVersion` and `kind` schema.
    pub fn validate_resource(document: &Value) -> Result<(), ResourceValidationError> {
        let violations =
            resource_schema_errors(document).map_err(ResourceValidationError::Schema)?;
        if violations.is_empty() {
            Ok(())
        } else {
            Err(ResourceValidationError::Violations(violations))
        }
    }

    struct ResourceSchemaDefinition {
        descriptor: ResourceSchemaDescriptor,
        source: &'static str,
        compiled: OnceLock<Result<CompiledResourceSchema, ResourceSchemaError>>,
    }

    impl ResourceSchemaDefinition {
        const fn new(descriptor: ResourceSchemaDescriptor, source: &'static str) -> Self {
            Self {
                descriptor,
                source,
                compiled: OnceLock::new(),
            }
        }

        fn compiled(&self) -> Result<&CompiledResourceSchema, ResourceSchemaError> {
            let result = self.compiled.get_or_init(|| {
                let document = serde_json::from_str::<Value>(self.source).map_err(|error| {
                    ResourceSchemaError {
                        path: self.descriptor.path,
                        message: error.to_string(),
                    }
                })?;
                jsonschema::draft202012::meta::validate(&document).map_err(|error| {
                    ResourceSchemaError {
                        path: self.descriptor.path,
                        message: error.to_string(),
                    }
                })?;
                let validator = jsonschema::draft202012::new(&document).map_err(|error| {
                    ResourceSchemaError {
                        path: self.descriptor.path,
                        message: error.to_string(),
                    }
                })?;
                Ok(CompiledResourceSchema {
                    document,
                    validator,
                })
            });
            match result {
                Ok(compiled) => Ok(compiled),
                Err(error) => Err(error.clone()),
            }
        }
    }

    struct CompiledResourceSchema {
        document: Value,
        validator: jsonschema::Validator,
    }

    static STABLE_GRAPH_SCHEMA: ResourceSchemaDefinition = ResourceSchemaDefinition::new(
        RESOURCE_SCHEMA_PATHS[0],
        include_str!("../schemas/graphblocks.ai/v1/graph.schema.json"),
    );
    static STABLE_PLUGIN_MANIFEST_SCHEMA: ResourceSchemaDefinition = ResourceSchemaDefinition::new(
        RESOURCE_SCHEMA_PATHS[1],
        include_str!("../schemas/graphblocks.ai/v1/plugin-manifest.schema.json"),
    );
    static ALPHA3_GRAPH_SCHEMA: ResourceSchemaDefinition = ResourceSchemaDefinition::new(
        RESOURCE_SCHEMA_PATHS[2],
        include_str!("../schemas/graphblocks.ai/v1alpha3/graph.schema.json"),
    );
    static APPLICATION_SCHEMA: ResourceSchemaDefinition = ResourceSchemaDefinition::new(
        RESOURCE_SCHEMA_PATHS[3],
        include_str!("../schemas/graphblocks.ai/v1alpha1/application.schema.json"),
    );
    static BINDING_SCHEMA: ResourceSchemaDefinition = ResourceSchemaDefinition::new(
        RESOURCE_SCHEMA_PATHS[4],
        include_str!("../schemas/graphblocks.ai/v1alpha1/binding.schema.json"),
    );
    static ALPHA1_PLUGIN_MANIFEST_SCHEMA: ResourceSchemaDefinition = ResourceSchemaDefinition::new(
        RESOURCE_SCHEMA_PATHS[5],
        include_str!("../schemas/graphblocks.ai/v1alpha1/plugin-manifest.schema.json"),
    );
    static GRAPH_FRAGMENT_SCHEMA: ResourceSchemaDefinition = ResourceSchemaDefinition::new(
        RESOURCE_SCHEMA_PATHS[6],
        include_str!("../schemas/graphblocks.ai/composition/v1alpha1/graph-fragment.schema.json"),
    );

    fn schema_definition(
        api_version: &str,
        kind: &str,
    ) -> Option<&'static ResourceSchemaDefinition> {
        match (api_version, kind) {
            ("graphblocks.ai/v1", "Graph") => Some(&STABLE_GRAPH_SCHEMA),
            ("graphblocks.ai/v1", "PluginManifest") => Some(&STABLE_PLUGIN_MANIFEST_SCHEMA),
            ("graphblocks.ai/v1alpha3", "Graph") => Some(&ALPHA3_GRAPH_SCHEMA),
            ("graphblocks.ai/v1alpha1", "Application") => Some(&APPLICATION_SCHEMA),
            ("graphblocks.ai/v1alpha1", "Binding") => Some(&BINDING_SCHEMA),
            ("graphblocks.ai/v1alpha1", "PluginManifest") => Some(&ALPHA1_PLUGIN_MANIFEST_SCHEMA),
            ("graphblocks.ai/composition/v1alpha1", "GraphFragment") => {
                Some(&GRAPH_FRAGMENT_SCHEMA)
            }
            _ => None,
        }
    }

    fn schema_violation(
        error: &jsonschema::ValidationError<'_>,
        document: &Value,
        schema: &Value,
    ) -> ResourceSchemaViolation {
        let keyword = error.kind().keyword();
        let message = validation_message(error, schema);
        ResourceSchemaViolation {
            code: "GB0014".to_owned(),
            path: json_pointer_to_path(error.instance_path().as_str(), document),
            keyword: keyword.to_owned(),
            message,
            schema_path: json_pointer_to_path(error.schema_path().as_str(), schema),
        }
    }

    fn validation_message(error: &jsonschema::ValidationError<'_>, schema: &Value) -> String {
        use jsonschema::error::ValidationErrorKind;

        match error.kind() {
            ValidationErrorKind::AnyOf { .. } => {
                "value must match at least one allowed schema".into()
            }
            ValidationErrorKind::OneOfMultipleValid { .. }
            | ValidationErrorKind::OneOfNotValid { .. } => {
                "value must match exactly one allowed schema".into()
            }
            ValidationErrorKind::Not { .. } => "value matches a forbidden schema".into(),
            ValidationErrorKind::Constant { expected_value } => {
                format!("value must equal {}", canonical_json(expected_value))
            }
            ValidationErrorKind::Enum { options } => {
                format!("value must be one of {}", canonical_json(options))
            }
            ValidationErrorKind::Type { .. } => {
                let expected = schema
                    .pointer(error.schema_path().as_str())
                    .unwrap_or(&Value::Null);
                format!("value must have JSON type {}", canonical_json(expected))
            }
            ValidationErrorKind::UniqueItems => "array items must be unique".into(),
            ValidationErrorKind::AdditionalProperties { unexpected } => {
                let mut unexpected = unexpected.clone();
                unexpected.sort();
                format!(
                    "unexpected properties are not allowed: {}",
                    canonical_json(&Value::Array(
                        unexpected.into_iter().map(Value::String).collect()
                    ))
                )
            }
            _ => error.to_string(),
        }
    }

    fn resource_violation(
        code: &str,
        path: &str,
        keyword: &str,
        message: &str,
        schema_path: &str,
    ) -> ResourceSchemaViolation {
        ResourceSchemaViolation {
            code: code.to_owned(),
            path: path.to_owned(),
            keyword: keyword.to_owned(),
            message: message.to_owned(),
            schema_path: schema_path.to_owned(),
        }
    }

    fn json_pointer_to_path(pointer: &str, root: &Value) -> String {
        let mut output = "$".to_owned();
        let mut current = Some(root);
        for encoded in pointer.strip_prefix('/').unwrap_or(pointer).split('/') {
            if encoded.is_empty() {
                continue;
            }
            let segment = encoded.replace("~1", "/").replace("~0", "~");
            match current {
                Some(Value::Array(values)) => {
                    if let Ok(index) = segment.parse::<usize>() {
                        output.push('[');
                        output.push_str(&index.to_string());
                        output.push(']');
                        current = values.get(index);
                    } else {
                        append_property_path(&mut output, &segment);
                        current = None;
                    }
                }
                Some(Value::Object(values)) => {
                    append_property_path(&mut output, &segment);
                    current = values.get(&segment);
                }
                _ => {
                    append_property_path(&mut output, &segment);
                    current = None;
                }
            }
        }
        output
    }

    fn append_property_path(output: &mut String, property: &str) {
        let mut bytes = property.bytes();
        let identifier = bytes
            .next()
            .is_some_and(|byte| byte.is_ascii_alphabetic() || byte == b'_')
            && bytes.all(|byte| byte.is_ascii_alphanumeric() || byte == b'_');
        if identifier {
            output.push('.');
            output.push_str(property);
        } else {
            output.push('[');
            output.push_str(&canonical_json(&Value::String(property.to_owned())));
            output.push(']');
        }
    }
}

#[cfg(feature = "resource-validation")]
pub use resource_validation::{
    RESOURCE_SCHEMA_PATHS, ResourceSchemaDescriptor, ResourceSchemaError, ResourceSchemaViolation,
    ResourceValidationError, resource_schema_errors, resource_schema_path, validate_resource,
};

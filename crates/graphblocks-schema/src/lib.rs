use serde::{Deserialize, Deserializer, Serialize, Serializer};
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

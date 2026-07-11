use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SecretRef {
    pub uri: String,
    pub version: Option<String>,
}

impl SecretRef {
    pub fn new(uri: impl Into<String>) -> Self {
        Self {
            uri: uri.into(),
            version: None,
        }
    }

    pub fn with_version(mut self, version: impl Into<String>) -> Self {
        self.version = Some(version.into());
        self
    }

    fn safe_value(&self) -> Value {
        json!({
            "uri": self.uri,
            "version": self.version,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SecretAccessRecord {
    pub secret_uri: String,
    pub version: Option<String>,
    pub provider_kind: String,
    pub requester: String,
}

#[derive(Clone, Eq, PartialEq)]
pub struct ResolvedSecret {
    value: String,
    pub access: SecretAccessRecord,
}

impl ResolvedSecret {
    pub fn expose_secret(&self) -> &str {
        &self.value
    }
}

impl fmt::Debug for ResolvedSecret {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ResolvedSecret")
            .field("value", &"<redacted>")
            .field("access", &self.access)
            .finish()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SecretProviderError {
    UnsupportedUri {
        uri: String,
    },
    NotFound {
        uri: String,
        version: Option<String>,
    },
}

impl fmt::Display for SecretProviderError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnsupportedUri { uri } => {
                write!(formatter, "secret URI {uri:?} is not a secret:// reference")
            }
            Self::NotFound { uri, version } => {
                write!(
                    formatter,
                    "secret {uri:?} version {version:?} was not found"
                )
            }
        }
    }
}

impl Error for SecretProviderError {}

pub trait SecretResolver {
    fn resolve(
        &self,
        reference: &SecretRef,
        requester: &str,
    ) -> Result<ResolvedSecret, SecretProviderError>;
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct InMemorySecretProvider {
    provider_kind: String,
    values: BTreeMap<(String, Option<String>), String>,
}

impl InMemorySecretProvider {
    pub fn new(provider_kind: impl Into<String>) -> Self {
        Self {
            provider_kind: provider_kind.into(),
            values: BTreeMap::new(),
        }
    }

    pub fn insert(&mut self, reference: SecretRef, value: impl Into<String>) {
        self.values
            .insert((reference.uri, reference.version), value.into());
    }

    pub fn resolve(
        &self,
        reference: &SecretRef,
        requester: impl Into<String>,
    ) -> Result<ResolvedSecret, SecretProviderError> {
        if !reference.uri.starts_with("secret://") {
            return Err(SecretProviderError::UnsupportedUri {
                uri: reference.uri.clone(),
            });
        }
        let value = self
            .values
            .get(&(reference.uri.clone(), reference.version.clone()))
            .cloned()
            .ok_or_else(|| SecretProviderError::NotFound {
                uri: reference.uri.clone(),
                version: reference.version.clone(),
            })?;
        Ok(ResolvedSecret {
            value,
            access: SecretAccessRecord {
                secret_uri: reference.uri.clone(),
                version: reference.version.clone(),
                provider_kind: self.provider_kind.clone(),
                requester: requester.into(),
            },
        })
    }
}

impl SecretResolver for InMemorySecretProvider {
    fn resolve(
        &self,
        reference: &SecretRef,
        requester: &str,
    ) -> Result<ResolvedSecret, SecretProviderError> {
        InMemorySecretProvider::resolve(self, reference, requester)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ConnectionSpec {
    pub connection_id: String,
    pub kind: String,
    pub provider: String,
    pub config: BTreeMap<String, Value>,
    pub credentials: Option<SecretRef>,
}

impl ConnectionSpec {
    pub fn new(
        connection_id: impl Into<String>,
        kind: impl Into<String>,
        provider: impl Into<String>,
    ) -> Self {
        Self {
            connection_id: connection_id.into(),
            kind: kind.into(),
            provider: provider.into(),
            config: BTreeMap::new(),
            credentials: None,
        }
    }

    pub fn with_config(mut self, key: impl Into<String>, value: Value) -> Self {
        self.config.insert(key.into(), value);
        self
    }

    pub fn with_credentials(mut self, credentials: SecretRef) -> Self {
        self.credentials = Some(credentials);
        self
    }

    pub fn safe_config_value(&self) -> Value {
        json!({
            "connection_id": self.connection_id,
            "kind": self.kind,
            "provider": self.provider,
            "config": self.config,
            "credentials": self.credentials.as_ref().map(SecretRef::safe_value),
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CapabilityError {
    pub connection_id: String,
    pub missing: Vec<String>,
    pub supported: Vec<String>,
}

impl fmt::Display for CapabilityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "connection {:?} is missing capabilities {:?}; supported: {:?}",
            self.connection_id, self.missing, self.supported
        )
    }
}

impl Error for CapabilityError {}

pub fn ensure_capabilities<I, J, S, R>(
    connection_id: impl Into<String>,
    supported: I,
    required: J,
) -> Result<(), CapabilityError>
where
    I: IntoIterator<Item = S>,
    J: IntoIterator<Item = R>,
    S: Into<String>,
    R: Into<String>,
{
    let supported = supported
        .into_iter()
        .map(Into::into)
        .collect::<BTreeSet<_>>();
    let required = required
        .into_iter()
        .map(Into::into)
        .collect::<BTreeSet<_>>();
    let missing = required
        .difference(&supported)
        .cloned()
        .collect::<Vec<String>>();
    if missing.is_empty() {
        return Ok(());
    }
    Err(CapabilityError {
        connection_id: connection_id.into(),
        missing,
        supported: supported.into_iter().collect(),
    })
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PromptRef {
    pub name: String,
    pub version: Option<String>,
    pub label: Option<String>,
}

impl PromptRef {
    pub fn new(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            version: None,
            label: None,
        }
    }

    pub fn with_version(mut self, version: impl Into<String>) -> Self {
        self.version = Some(version.into());
        self
    }

    pub fn with_label(mut self, label: impl Into<String>) -> Self {
        self.label = Some(label.into());
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PromptTemplate {
    pub name: String,
    pub version: String,
    pub content: String,
    pub labels: BTreeSet<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl PromptTemplate {
    pub fn new(
        name: impl Into<String>,
        version: impl Into<String>,
        content: impl Into<String>,
    ) -> Self {
        Self {
            name: name.into(),
            version: version.into(),
            content: content.into(),
            labels: BTreeSet::new(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_label(mut self, label: impl Into<String>) -> Self {
        self.labels.insert(label.into());
        self
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: Value) -> Self {
        self.metadata.insert(key.into(), value);
        self
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "name": self.name,
            "version": self.version,
            "content": self.content,
            "metadata": self.metadata,
        }))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PromptRegistryError {
    PromptNotFound { name: String, selector: String },
    AmbiguousPromptRef { name: String },
}

impl fmt::Display for PromptRegistryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::PromptNotFound { name, selector } => {
                write!(
                    formatter,
                    "prompt {name:?} with selector {selector:?} was not found"
                )
            }
            Self::AmbiguousPromptRef { name } => {
                write!(
                    formatter,
                    "prompt {name:?} has multiple versions and requires a version or label"
                )
            }
        }
    }
}

impl Error for PromptRegistryError {}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct InMemoryPromptRegistry {
    templates: BTreeMap<(String, String), PromptTemplate>,
    labels: BTreeMap<(String, String), String>,
}

impl InMemoryPromptRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&mut self, template: PromptTemplate) {
        for label in &template.labels {
            self.labels.insert(
                (template.name.clone(), label.clone()),
                template.version.clone(),
            );
        }
        self.templates
            .insert((template.name.clone(), template.version.clone()), template);
    }

    pub fn resolve(&self, reference: PromptRef) -> Result<PromptTemplate, PromptRegistryError> {
        if let Some(version) = reference.version {
            return self
                .templates
                .get(&(reference.name.clone(), version.clone()))
                .cloned()
                .ok_or_else(|| PromptRegistryError::PromptNotFound {
                    name: reference.name,
                    selector: format!("version:{version}"),
                });
        }
        if let Some(label) = reference.label {
            let Some(version) = self
                .labels
                .get(&(reference.name.clone(), label.clone()))
                .cloned()
            else {
                return Err(PromptRegistryError::PromptNotFound {
                    name: reference.name,
                    selector: format!("label:{label}"),
                });
            };
            return self
                .templates
                .get(&(reference.name.clone(), version.clone()))
                .cloned()
                .ok_or_else(|| PromptRegistryError::PromptNotFound {
                    name: reference.name,
                    selector: format!("label:{label}"),
                });
        }
        let versions = self.list_versions(&reference.name);
        match versions.as_slice() {
            [] => Err(PromptRegistryError::PromptNotFound {
                name: reference.name,
                selector: "any".to_owned(),
            }),
            [version] => self
                .templates
                .get(&(reference.name.clone(), version.clone()))
                .cloned()
                .ok_or_else(|| PromptRegistryError::PromptNotFound {
                    name: reference.name,
                    selector: format!("version:{version}"),
                }),
            _ => Err(PromptRegistryError::AmbiguousPromptRef {
                name: reference.name,
            }),
        }
    }

    pub fn list_versions(&self, name: &str) -> Vec<String> {
        self.templates
            .keys()
            .filter(|(template_name, _)| template_name == name)
            .map(|(_, version)| version.clone())
            .collect()
    }
}

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use serde_json::Value;

use crate::documents::{
    ArtifactRef, AssetRevision, ParsedDocument, SourceAsset, parse_plain_text_document,
};

pub type ParserFn =
    fn(&SourceAsset, &AssetRevision, &[u8]) -> Result<ParsedDocument, DocumentParserError>;

#[derive(Clone, Debug)]
pub struct ParserDescriptor {
    pub processor_id: String,
    pub version: String,
    pub media_types: Vec<String>,
    pub extensions: Vec<String>,
    pub priority: i32,
    pub supports_ocr: bool,
    pub parser: Option<ParserFn>,
    pub metadata: BTreeMap<String, Value>,
}

impl ParserDescriptor {
    pub fn new(processor_id: impl Into<String>, version: impl Into<String>) -> Self {
        Self {
            processor_id: processor_id.into(),
            version: version.into(),
            media_types: Vec::new(),
            extensions: Vec::new(),
            priority: 0,
            supports_ocr: false,
            parser: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_media_types<I, S>(mut self, media_types: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.media_types = media_types.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_extensions<I, S>(mut self, extensions: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.extensions = extensions.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_priority(mut self, priority: i32) -> Self {
        self.priority = priority;
        self
    }

    pub fn with_parser(mut self, parser: ParserFn) -> Self {
        self.parser = Some(parser);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ParserSelectionLock {
    pub processor_id: String,
    pub processor_version: String,
    pub reason: String,
    pub media_type: Option<String>,
    pub filename: Option<String>,
    pub artifact_checksum: Option<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl ParserSelectionLock {
    pub fn new(
        processor_id: impl Into<String>,
        processor_version: impl Into<String>,
        reason: impl Into<String>,
    ) -> Self {
        Self {
            processor_id: processor_id.into(),
            processor_version: processor_version.into(),
            reason: reason.into(),
            media_type: None,
            filename: None,
            artifact_checksum: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_media_type(mut self, media_type: impl Into<String>) -> Self {
        self.media_type = Some(media_type.into());
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DocumentParserError {
    NotFound {
        message: String,
    },
    Utf8 {
        processor_id: String,
        message: String,
    },
}

impl fmt::Display for DocumentParserError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotFound { message } => write!(formatter, "{message}"),
            Self::Utf8 {
                processor_id,
                message,
            } => write!(
                formatter,
                "parser {processor_id:?} failed to decode UTF-8: {message}"
            ),
        }
    }
}

impl Error for DocumentParserError {}

#[derive(Clone, Debug, Default)]
pub struct DocumentParserRegistry {
    descriptors: BTreeMap<(String, String), ParserDescriptor>,
}

impl DocumentParserRegistry {
    pub fn new() -> Self {
        Self {
            descriptors: BTreeMap::new(),
        }
    }

    pub fn register(&mut self, descriptor: ParserDescriptor) {
        let media_types = descriptor
            .media_types
            .into_iter()
            .map(|media_type| media_type.to_ascii_lowercase())
            .collect();
        let extensions = descriptor
            .extensions
            .into_iter()
            .map(|extension| {
                let extension = extension.to_ascii_lowercase();
                if extension.starts_with('.') {
                    extension
                } else {
                    format!(".{extension}")
                }
            })
            .collect();
        let descriptor = ParserDescriptor {
            media_types,
            extensions,
            ..descriptor
        };
        self.descriptors.insert(
            (descriptor.processor_id.clone(), descriptor.version.clone()),
            descriptor,
        );
    }

    pub fn select(
        &self,
        artifact: &ArtifactRef,
    ) -> Result<ParserSelectionLock, DocumentParserError> {
        let media_type = artifact
            .media_type
            .as_ref()
            .map(|media_type| media_type.to_ascii_lowercase());
        let filename = artifact.filename.clone().or_else(|| {
            artifact
                .uri
                .rsplit('/')
                .next()
                .filter(|name| !name.is_empty())
                .map(str::to_owned)
        });
        let extension = filename.as_ref().and_then(|filename| {
            filename
                .rfind('.')
                .map(|index| filename[index..].to_ascii_lowercase())
        });
        let mut candidates = Vec::new();
        for descriptor in self.descriptors.values() {
            if media_type
                .as_ref()
                .is_some_and(|media_type| descriptor.media_types.contains(media_type))
            {
                candidates.push(("media_type", descriptor));
            } else if extension
                .as_ref()
                .is_some_and(|extension| descriptor.extensions.contains(extension))
            {
                candidates.push(("extension", descriptor));
            }
        }
        if candidates.is_empty() {
            return Err(DocumentParserError::NotFound {
                message: format!("no document parser for artifact {:?}", artifact.artifact_id),
            });
        }
        candidates.sort_by(|left, right| {
            right
                .1
                .priority
                .cmp(&left.1.priority)
                .then_with(|| left.1.processor_id.cmp(&right.1.processor_id))
                .then_with(|| left.1.version.cmp(&right.1.version))
        });
        let (reason, descriptor) = candidates[0];
        Ok(ParserSelectionLock {
            processor_id: descriptor.processor_id.clone(),
            processor_version: descriptor.version.clone(),
            reason: reason.to_owned(),
            media_type,
            filename,
            artifact_checksum: artifact.checksum.clone(),
            metadata: BTreeMap::new(),
        })
    }

    pub fn resolve_locked(
        &self,
        lock: &ParserSelectionLock,
    ) -> Result<&ParserDescriptor, DocumentParserError> {
        self.descriptors
            .get(&(lock.processor_id.clone(), lock.processor_version.clone()))
            .ok_or_else(|| DocumentParserError::NotFound {
                message: format!(
                    "locked parser {:?}@{:?} is not registered",
                    lock.processor_id, lock.processor_version
                ),
            })
    }

    pub fn parse_locked(
        &self,
        asset: &SourceAsset,
        revision: &AssetRevision,
        body: &[u8],
        lock: &ParserSelectionLock,
    ) -> Result<ParsedDocument, DocumentParserError> {
        let descriptor = self.resolve_locked(lock)?;
        let Some(parser) = descriptor.parser else {
            return Err(DocumentParserError::NotFound {
                message: format!(
                    "locked parser {:?}@{:?} has no local implementation",
                    lock.processor_id, lock.processor_version
                ),
            });
        };
        parser(asset, revision, body)
    }
}

pub fn plain_text_parser_descriptor() -> ParserDescriptor {
    ParserDescriptor::new("plain-text", "1")
        .with_media_types(["text/plain"])
        .with_extensions([".txt", ".text"])
        .with_parser(parse_plain_text_bytes)
}

fn parse_plain_text_bytes(
    asset: &SourceAsset,
    revision: &AssetRevision,
    body: &[u8],
) -> Result<ParsedDocument, DocumentParserError> {
    let text = std::str::from_utf8(body).map_err(|error| DocumentParserError::Utf8 {
        processor_id: "plain-text".to_owned(),
        message: error.to_string(),
    })?;
    Ok(parse_plain_text_document(asset, revision, text))
}

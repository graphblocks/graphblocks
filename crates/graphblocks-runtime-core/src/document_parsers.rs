use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use serde_json::{Map, Value, json};

use crate::documents::{
    ArtifactRef, AssetRevision, DocumentElement, ParsedDocument, SourceAsset, SourceLocation,
    parse_plain_text_document,
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

#[derive(Clone, Debug, PartialEq)]
pub struct OcrTextBlock {
    pub text: String,
    pub page: Option<u64>,
    pub region: Option<Value>,
    pub confidence: Option<f64>,
    pub rotation_degrees: Option<f64>,
    pub metadata: BTreeMap<String, Value>,
}

impl OcrTextBlock {
    pub fn new(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            page: None,
            region: None,
            confidence: None,
            rotation_degrees: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_page(mut self, page: u64) -> Self {
        self.page = Some(page);
        self
    }

    pub fn with_region(mut self, region: Value) -> Self {
        self.region = Some(region);
        self
    }

    pub fn with_confidence(mut self, confidence: f64) -> Self {
        self.confidence = Some(confidence);
        self
    }

    pub fn with_rotation_degrees(mut self, rotation_degrees: f64) -> Self {
        self.rotation_degrees = Some(rotation_degrees);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct OcrResult {
    pub processor_id: String,
    pub processor_version: String,
    pub source_variant: String,
    pub language_hints: Vec<String>,
    pub preprocessing_config_digest: Option<String>,
    pub source_artifact: Option<ArtifactRef>,
    pub blocks: Vec<OcrTextBlock>,
    pub metadata: BTreeMap<String, Value>,
}

impl OcrResult {
    pub fn new(
        processor_id: impl Into<String>,
        processor_version: impl Into<String>,
        source_variant: impl Into<String>,
    ) -> Self {
        Self {
            processor_id: processor_id.into(),
            processor_version: processor_version.into(),
            source_variant: source_variant.into(),
            language_hints: Vec::new(),
            preprocessing_config_digest: None,
            source_artifact: None,
            blocks: Vec::new(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_language_hints<I, S>(mut self, language_hints: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.language_hints = language_hints.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_preprocessing_config_digest(mut self, config_digest: impl Into<String>) -> Self {
        self.preprocessing_config_digest = Some(config_digest.into());
        self
    }

    pub fn with_source_artifact(mut self, source_artifact: ArtifactRef) -> Self {
        self.source_artifact = Some(source_artifact);
        self
    }

    pub fn with_text_block(mut self, block: OcrTextBlock) -> Self {
        self.blocks.push(block);
        self
    }

    fn plain_text(&self) -> String {
        self.blocks
            .iter()
            .map(|block| block.text.as_str())
            .collect::<Vec<_>>()
            .join("\n")
    }

    pub fn to_parsed_document(
        self,
        document_id: impl Into<String>,
        asset_id: impl Into<String>,
        revision_id: impl Into<String>,
    ) -> ParsedDocument {
        let plain_text = self.plain_text();
        let mut document = ParsedDocument::new(
            document_id,
            asset_id,
            revision_id,
            &self.processor_id,
            &self.processor_version,
        )
        .with_plain_text(plain_text);
        document.metadata.insert(
            "source_variant".to_owned(),
            json!(self.source_variant.clone()),
        );
        let mut ocr_metadata = Map::new();
        ocr_metadata.insert(
            "processor".to_owned(),
            json!({
                "processor_id": self.processor_id.clone(),
                "version": self.processor_version.clone(),
            }),
        );
        ocr_metadata.insert(
            "language_hints".to_owned(),
            json!(self.language_hints.clone()),
        );
        if let Some(config_digest) = self.preprocessing_config_digest {
            ocr_metadata.insert(
                "preprocessing_config_digest".to_owned(),
                json!(config_digest),
            );
        }
        if let Some(source_artifact) = &self.source_artifact {
            ocr_metadata.insert(
                "source_artifact_id".to_owned(),
                json!(source_artifact.artifact_id.clone()),
            );
            ocr_metadata.insert(
                "source_artifact_uri".to_owned(),
                json!(source_artifact.uri.clone()),
            );
        }
        document
            .metadata
            .insert("ocr".to_owned(), Value::Object(ocr_metadata));
        for (index, block) in self.blocks.into_iter().enumerate() {
            let mut location = SourceLocation::new();
            location.page = block.page;
            location.bbox = block.region;
            let mut element = DocumentElement::new(
                format!("ocr-{}", index + 1),
                "ocr_text",
                index,
                block.text,
                location,
            );
            element.metadata.insert(
                "source_variant".to_owned(),
                document.metadata["source_variant"].clone(),
            );
            if let Some(confidence) = block.confidence {
                element
                    .metadata
                    .insert("confidence".to_owned(), json!(confidence));
            }
            if let Some(rotation_degrees) = block.rotation_degrees {
                element
                    .metadata
                    .insert("rotation_degrees".to_owned(), json!(rotation_degrees));
            }
            if let Some(source_artifact) = &self.source_artifact {
                element.metadata.insert(
                    "source_artifact_id".to_owned(),
                    json!(source_artifact.artifact_id.clone()),
                );
            }
            for (key, value) in block.metadata {
                element.metadata.insert(key, value);
            }
            document.elements.push(element);
        }
        document
    }
}

pub fn apply_ocr_fallback(mut document: ParsedDocument, ocr_result: OcrResult) -> ParsedDocument {
    let has_primary_text = document
        .plain_text
        .as_ref()
        .is_some_and(|text| !text.trim().is_empty())
        || document
            .elements
            .iter()
            .any(|element| !element.content.trim().is_empty());
    if !has_primary_text {
        return ocr_result.to_parsed_document(
            document.document_id,
            document.asset_id,
            document.revision_id,
        );
    }

    let mut source_variants = match document.metadata.remove("source_variants") {
        Some(Value::Object(source_variants)) => source_variants,
        Some(other) => {
            let mut source_variants = Map::new();
            source_variants.insert("previous".to_owned(), other);
            source_variants
        }
        None => Map::new(),
    };
    let mut processor = Map::new();
    processor.insert(
        "processor_id".to_owned(),
        json!(ocr_result.processor_id.clone()),
    );
    processor.insert(
        "version".to_owned(),
        json!(ocr_result.processor_version.clone()),
    );
    let mut variant = Map::new();
    variant.insert("text".to_owned(), json!(ocr_result.plain_text()));
    variant.insert("processor".to_owned(), Value::Object(processor));
    variant.insert("block_count".to_owned(), json!(ocr_result.blocks.len()));
    variant.insert(
        "language_hints".to_owned(),
        json!(ocr_result.language_hints.clone()),
    );
    if let Some(config_digest) = ocr_result.preprocessing_config_digest {
        variant.insert(
            "preprocessing_config_digest".to_owned(),
            json!(config_digest),
        );
    }
    if let Some(source_artifact) = ocr_result.source_artifact {
        variant.insert(
            "source_artifact_id".to_owned(),
            json!(source_artifact.artifact_id.clone()),
        );
        variant.insert(
            "source_artifact_uri".to_owned(),
            json!(source_artifact.uri.clone()),
        );
    }
    source_variants.insert(ocr_result.source_variant, Value::Object(variant));
    document
        .metadata
        .insert("source_variants".to_owned(), Value::Object(source_variants));
    document
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

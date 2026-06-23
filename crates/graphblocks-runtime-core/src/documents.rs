use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ArtifactRef {
    pub artifact_id: String,
    pub uri: String,
    pub media_type: Option<String>,
    pub size_bytes: Option<usize>,
    pub checksum: Option<String>,
    pub etag: Option<String>,
    pub version: Option<String>,
    pub filename: Option<String>,
    pub metadata: BTreeMap<String, String>,
}

impl ArtifactRef {
    pub fn new(artifact_id: impl Into<String>, uri: impl Into<String>) -> Self {
        Self {
            artifact_id: artifact_id.into(),
            uri: uri.into(),
            media_type: None,
            size_bytes: None,
            checksum: None,
            etag: None,
            version: None,
            filename: None,
            metadata: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceAsset {
    pub asset_id: String,
    pub source_uri: String,
    pub source_kind: String,
    pub tenant_id: Option<String>,
    pub current_revision_id: Option<String>,
}

impl SourceAsset {
    pub fn new(
        asset_id: impl Into<String>,
        source_uri: impl Into<String>,
        source_kind: impl Into<String>,
    ) -> Self {
        Self {
            asset_id: asset_id.into(),
            source_uri: source_uri.into(),
            source_kind: source_kind.into(),
            tenant_id: None,
            current_revision_id: None,
        }
    }

    pub fn with_current_revision_id(mut self, revision_id: impl Into<String>) -> Self {
        self.current_revision_id = Some(revision_id.into());
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct AssetRevision {
    pub revision_id: String,
    pub asset_id: String,
    pub content_hash: String,
    pub observed_at: String,
    pub artifact: ArtifactRef,
    pub modified_at: Option<String>,
    pub source_metadata: BTreeMap<String, Value>,
    pub acl: Option<Value>,
}

impl AssetRevision {
    pub fn new(
        revision_id: impl Into<String>,
        asset_id: impl Into<String>,
        content_hash: impl Into<String>,
        observed_at: impl Into<String>,
        artifact: ArtifactRef,
    ) -> Self {
        Self {
            revision_id: revision_id.into(),
            asset_id: asset_id.into(),
            content_hash: content_hash.into(),
            observed_at: observed_at.into(),
            artifact,
            modified_at: None,
            source_metadata: BTreeMap::new(),
            acl: None,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SourceLocation {
    pub page: Option<u64>,
    pub bbox: Option<Value>,
    pub char_start: Option<usize>,
    pub char_end: Option<usize>,
    pub section_path: Vec<String>,
    pub sheet: Option<String>,
    pub cell_range: Option<String>,
    pub slide: Option<u64>,
}

impl SourceLocation {
    pub fn new() -> Self {
        Self {
            page: None,
            bbox: None,
            char_start: None,
            char_end: None,
            section_path: Vec::new(),
            sheet: None,
            cell_range: None,
            slide: None,
        }
    }

    pub fn with_char_span(mut self, char_start: usize, char_end: usize) -> Self {
        self.char_start = Some(char_start);
        self.char_end = Some(char_end);
        self
    }
}

impl Default for SourceLocation {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct DocumentElement {
    pub element_id: String,
    pub kind: String,
    pub order: usize,
    pub content: String,
    pub location: SourceLocation,
    pub parent_id: Option<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl DocumentElement {
    pub fn new(
        element_id: impl Into<String>,
        kind: impl Into<String>,
        order: usize,
        content: impl Into<String>,
        location: SourceLocation,
    ) -> Self {
        Self {
            element_id: element_id.into(),
            kind: kind.into(),
            order,
            content: content.into(),
            location,
            parent_id: None,
            metadata: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ParsedDocument {
    pub document_id: String,
    pub asset_id: String,
    pub revision_id: String,
    pub parser: BTreeMap<String, Value>,
    pub elements: Vec<DocumentElement>,
    pub plain_text: Option<String>,
    pub language: Option<String>,
    pub title: Option<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl ParsedDocument {
    pub fn new(
        document_id: impl Into<String>,
        asset_id: impl Into<String>,
        revision_id: impl Into<String>,
        processor_id: impl Into<String>,
        version: impl Into<String>,
    ) -> Self {
        let mut parser = BTreeMap::new();
        parser.insert("processor_id".to_owned(), json!(processor_id.into()));
        parser.insert("version".to_owned(), json!(version.into()));
        Self {
            document_id: document_id.into(),
            asset_id: asset_id.into(),
            revision_id: revision_id.into(),
            parser,
            elements: Vec::new(),
            plain_text: None,
            language: None,
            title: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_element(mut self, element: DocumentElement) -> Self {
        self.elements.push(element);
        self
    }

    pub fn with_plain_text(mut self, text: impl Into<String>) -> Self {
        self.plain_text = Some(text.into());
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct DocumentSpan {
    pub asset_id: String,
    pub revision_id: String,
    pub document_id: String,
    pub element_id: Option<String>,
    pub chunk_id: Option<String>,
    pub page: Option<u64>,
    pub bbox: Option<Value>,
    pub char_start: Option<usize>,
    pub char_end: Option<usize>,
    pub sheet: Option<String>,
    pub cell_range: Option<String>,
    pub slide: Option<u64>,
}

impl DocumentSpan {
    pub fn new(
        asset_id: impl Into<String>,
        revision_id: impl Into<String>,
        document_id: impl Into<String>,
    ) -> Self {
        Self {
            asset_id: asset_id.into(),
            revision_id: revision_id.into(),
            document_id: document_id.into(),
            element_id: None,
            chunk_id: None,
            page: None,
            bbox: None,
            char_start: None,
            char_end: None,
            sheet: None,
            cell_range: None,
            slide: None,
        }
    }

    pub fn with_element_id(mut self, element_id: impl Into<String>) -> Self {
        self.element_id = Some(element_id.into());
        self
    }

    pub fn with_chunk_id(mut self, chunk_id: impl Into<String>) -> Self {
        self.chunk_id = Some(chunk_id.into());
        self
    }

    pub fn with_char_span(mut self, char_start: usize, char_end: usize) -> Self {
        self.char_start = Some(char_start);
        self.char_end = Some(char_end);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SourceRef {
    pub source_id: String,
    pub source_kind: String,
    pub revision: Option<String>,
    pub digest: Option<String>,
    pub locator: Option<DocumentSpan>,
    pub observed_at: Option<String>,
    pub relevant_as_of: Option<String>,
    pub trust: String,
    pub access_policy: Option<Value>,
    pub metadata: BTreeMap<String, Value>,
}

impl SourceRef {
    pub fn document_chunk(
        source_id: impl Into<String>,
        revision: impl Into<String>,
        digest: impl Into<String>,
        locator: DocumentSpan,
    ) -> Self {
        Self {
            source_id: source_id.into(),
            source_kind: "document_chunk".to_owned(),
            revision: Some(revision.into()),
            digest: Some(digest.into()),
            locator: Some(locator),
            observed_at: None,
            relevant_as_of: None,
            trust: "unknown".to_owned(),
            access_policy: None,
            metadata: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct DocumentChunk {
    pub chunk_id: String,
    pub document_id: String,
    pub asset_id: String,
    pub revision_id: String,
    pub text: String,
    pub element_ids: Vec<String>,
    pub source_refs: Vec<SourceRef>,
    pub chunker: BTreeMap<String, Value>,
    pub token_count: Option<usize>,
    pub metadata: BTreeMap<String, Value>,
    pub acl: Option<Value>,
}

impl DocumentChunk {
    pub fn new(
        chunk_id: impl Into<String>,
        document_id: impl Into<String>,
        asset_id: impl Into<String>,
        revision_id: impl Into<String>,
        text: impl Into<String>,
    ) -> Self {
        Self {
            chunk_id: chunk_id.into(),
            document_id: document_id.into(),
            asset_id: asset_id.into(),
            revision_id: revision_id.into(),
            text: text.into(),
            element_ids: Vec::new(),
            source_refs: Vec::new(),
            chunker: BTreeMap::new(),
            token_count: None,
            metadata: BTreeMap::new(),
            acl: None,
        }
    }

    pub fn with_element_ids<I, S>(mut self, element_ids: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.element_ids = element_ids.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_source_ref(mut self, source_ref: SourceRef) -> Self {
        self.source_refs.push(source_ref);
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DocumentError {
    InvalidMaxElements,
}

impl fmt::Display for DocumentError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidMaxElements => write!(formatter, "max_elements must be at least 1"),
        }
    }
}

impl Error for DocumentError {}

pub fn create_local_text_revision(
    source_uri: impl AsRef<str>,
    text: impl AsRef<str>,
    observed_at: impl Into<String>,
    filename: Option<&str>,
) -> (SourceAsset, AssetRevision) {
    let source_uri = source_uri.as_ref();
    let text = text.as_ref();
    let mut content_hasher = Sha256::new();
    content_hasher.update(text.as_bytes());
    let content_hash = format!("sha256:{:x}", content_hasher.finalize());
    let mut asset_hasher = Sha256::new();
    asset_hasher.update(source_uri.as_bytes());
    let asset_id = format!("asset:sha256:{:x}", asset_hasher.finalize());
    let revision_id = format!("rev:{content_hash}");
    let artifact = ArtifactRef {
        artifact_id: format!("artifact:{content_hash}"),
        uri: source_uri.to_owned(),
        media_type: Some("text/plain".to_owned()),
        size_bytes: Some(text.len()),
        checksum: Some(content_hash.clone()),
        etag: None,
        version: None,
        filename: filename.map(str::to_owned),
        metadata: BTreeMap::new(),
    };
    let asset =
        SourceAsset::new(&asset_id, source_uri, "local").with_current_revision_id(&revision_id);
    let revision = AssetRevision::new(revision_id, asset_id, content_hash, observed_at, artifact);
    (asset, revision)
}

pub fn parse_plain_text_document(
    asset: &SourceAsset,
    revision: &AssetRevision,
    text: impl AsRef<str>,
) -> ParsedDocument {
    let text = text.as_ref();
    let mut document = ParsedDocument::new(
        format!("doc:{}", revision.revision_id),
        &asset.asset_id,
        &revision.revision_id,
        "plain-text",
        "1",
    )
    .with_plain_text(text);
    let mut offset = 0;
    let mut order = 0;
    for raw_line in text.split_inclusive('\n') {
        let line_without_newline = raw_line.trim_end_matches(['\r', '\n']);
        let line_start = offset;
        let line_end = line_start + line_without_newline.len();
        offset += raw_line.len();
        if line_without_newline.trim().is_empty() {
            continue;
        }
        let element = DocumentElement::new(
            format!("{}:element:{order:06}", revision.revision_id),
            "paragraph",
            order,
            line_without_newline,
            SourceLocation::new().with_char_span(line_start, line_end),
        );
        document.elements.push(element);
        order += 1;
    }
    document
}

pub fn chunk_document_by_lines(
    document: &ParsedDocument,
    revision: &AssetRevision,
    max_elements: usize,
) -> Result<Vec<DocumentChunk>, DocumentError> {
    if max_elements < 1 {
        return Err(DocumentError::InvalidMaxElements);
    }
    let mut chunks = Vec::new();
    for (chunk_index, grouped) in document.elements.chunks(max_elements).enumerate() {
        let text = grouped
            .iter()
            .map(|element| element.content.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        let char_start = grouped
            .iter()
            .filter_map(|element| element.location.char_start)
            .min();
        let char_end = grouped
            .iter()
            .filter_map(|element| element.location.char_end)
            .max();
        let chunk_id = format!("{}:chunk:{chunk_index:06}", document.document_id);
        let locator = DocumentSpan::new(
            &document.asset_id,
            &document.revision_id,
            &document.document_id,
        )
        .with_chunk_id(&chunk_id);
        let locator = match (char_start, char_end) {
            (Some(char_start), Some(char_end)) => locator.with_char_span(char_start, char_end),
            _ => locator,
        };
        let source_ref = SourceRef::document_chunk(
            &chunk_id,
            &document.revision_id,
            &revision.content_hash,
            locator,
        );
        let mut chunker = BTreeMap::new();
        chunker.insert("processor_id".to_owned(), json!("plain-text-lines"));
        chunker.insert("version".to_owned(), json!("1"));
        let mut chunk = DocumentChunk::new(
            &chunk_id,
            &document.document_id,
            &document.asset_id,
            &document.revision_id,
            &text,
        )
        .with_element_ids(grouped.iter().map(|element| element.element_id.clone()))
        .with_source_ref(source_ref);
        chunk.chunker = chunker;
        chunk.token_count = Some(text.split_whitespace().count());
        chunk.acl = revision.acl.clone();
        chunks.push(chunk);
    }
    Ok(chunks)
}

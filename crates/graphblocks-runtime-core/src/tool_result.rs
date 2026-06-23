use std::collections::BTreeMap;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

use crate::outcome::BlockError;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ContentPartKind {
    Text,
    Json,
    ArtifactRef,
}

impl ContentPartKind {
    fn as_str(self) -> &'static str {
        match self {
            Self::Text => "text",
            Self::Json => "json",
            Self::ArtifactRef => "artifact_ref",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ContentPart {
    pub kind: ContentPartKind,
    pub text: Option<String>,
    pub data: Option<Value>,
    pub metadata: BTreeMap<String, Value>,
}

impl ContentPart {
    pub fn text(text: impl Into<String>) -> Self {
        Self {
            kind: ContentPartKind::Text,
            text: Some(text.into()),
            data: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn json(data: Value) -> Self {
        Self {
            kind: ContentPartKind::Json,
            text: None,
            data: Some(data),
            metadata: BTreeMap::new(),
        }
    }

    pub fn artifact_ref(artifact: ArtifactRef) -> Self {
        Self {
            kind: ContentPartKind::ArtifactRef,
            text: None,
            data: Some(artifact.canonical_value()),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: Value) -> Self {
        self.metadata.insert(key.into(), value);
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ArtifactRef {
    pub artifact_id: String,
    pub uri: String,
    pub checksum: Option<String>,
    pub media_type: Option<String>,
}

impl ArtifactRef {
    pub fn new(artifact_id: impl Into<String>, uri: impl Into<String>) -> Self {
        Self {
            artifact_id: artifact_id.into(),
            uri: uri.into(),
            checksum: None,
            media_type: None,
        }
    }

    pub fn with_checksum(mut self, checksum: impl Into<String>) -> Self {
        self.checksum = Some(checksum.into());
        self
    }

    pub fn with_media_type(mut self, media_type: impl Into<String>) -> Self {
        self.media_type = Some(media_type.into());
        self
    }

    fn canonical_value(&self) -> Value {
        json!({
            "artifact_id": self.artifact_id,
            "uri": self.uri,
            "checksum": self.checksum,
            "media_type": self.media_type,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DiagnosticSeverity {
    Info,
    Warning,
    Error,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Diagnostic {
    pub code: String,
    pub message: String,
    pub severity: DiagnosticSeverity,
    pub path: Option<String>,
}

impl Diagnostic {
    pub fn warning(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            severity: DiagnosticSeverity::Warning,
            path: None,
        }
    }

    pub fn error(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            severity: DiagnosticSeverity::Error,
            path: None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolResultStatus {
    Completed,
    Failed,
    Denied,
    Cancelled,
    PolicyStopped,
    Incomplete,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolResult {
    pub tool_call_id: String,
    pub status: ToolResultStatus,
    pub output: Vec<ContentPart>,
    pub output_digest: Option<String>,
    pub artifacts: Vec<ArtifactRef>,
    pub diagnostics: Vec<Diagnostic>,
    pub error: Option<BlockError>,
    pub started_at_unix_ms: Option<u64>,
    pub completed_at_unix_ms: Option<u64>,
}

impl ToolResult {
    pub fn completed<I>(
        tool_call_id: impl Into<String>,
        output: I,
        started_at_unix_ms: u64,
        completed_at_unix_ms: u64,
    ) -> Self
    where
        I: IntoIterator<Item = ContentPart>,
    {
        let output = output.into_iter().collect::<Vec<_>>();
        let output_value = Value::Array(
            output
                .iter()
                .map(|part| {
                    json!({
                        "kind": part.kind.as_str(),
                        "text": part.text,
                        "data": part.data,
                        "metadata": part.metadata,
                    })
                })
                .collect(),
        );
        Self {
            tool_call_id: tool_call_id.into(),
            status: ToolResultStatus::Completed,
            output,
            output_digest: Some(canonical_hash(&output_value)),
            artifacts: Vec::new(),
            diagnostics: Vec::new(),
            error: None,
            started_at_unix_ms: Some(started_at_unix_ms),
            completed_at_unix_ms: Some(completed_at_unix_ms),
        }
    }

    pub fn failed(
        tool_call_id: impl Into<String>,
        error: BlockError,
        started_at_unix_ms: u64,
        completed_at_unix_ms: u64,
    ) -> Self {
        Self {
            tool_call_id: tool_call_id.into(),
            status: ToolResultStatus::Failed,
            output: Vec::new(),
            output_digest: None,
            artifacts: Vec::new(),
            diagnostics: Vec::new(),
            error: Some(error),
            started_at_unix_ms: Some(started_at_unix_ms),
            completed_at_unix_ms: Some(completed_at_unix_ms),
        }
    }

    pub fn policy_stopped(
        tool_call_id: impl Into<String>,
        error: BlockError,
        started_at_unix_ms: u64,
        completed_at_unix_ms: u64,
    ) -> Self {
        Self {
            tool_call_id: tool_call_id.into(),
            status: ToolResultStatus::PolicyStopped,
            output: Vec::new(),
            output_digest: None,
            artifacts: Vec::new(),
            diagnostics: Vec::new(),
            error: Some(error),
            started_at_unix_ms: Some(started_at_unix_ms),
            completed_at_unix_ms: Some(completed_at_unix_ms),
        }
    }

    pub fn with_artifacts<I>(mut self, artifacts: I) -> Self
    where
        I: IntoIterator<Item = ArtifactRef>,
    {
        self.artifacts = artifacts.into_iter().collect();
        self
    }

    pub fn with_diagnostics<I>(mut self, diagnostics: I) -> Self
    where
        I: IntoIterator<Item = Diagnostic>,
    {
        self.diagnostics = diagnostics.into_iter().collect();
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum ToolResultEvent {
    Started {
        tool_call_id: String,
        sequence: u64,
        started_at_unix_ms: u64,
    },
    Delta {
        tool_call_id: String,
        sequence: u64,
        output: Vec<ContentPart>,
    },
    ArtifactReady {
        tool_call_id: String,
        sequence: u64,
        artifact: ArtifactRef,
    },
    Completed {
        tool_call_id: String,
        sequence: u64,
        result: ToolResult,
    },
}

impl ToolResultEvent {
    pub fn started(
        tool_call_id: impl Into<String>,
        sequence: u64,
        started_at_unix_ms: u64,
    ) -> Self {
        Self::Started {
            tool_call_id: tool_call_id.into(),
            sequence,
            started_at_unix_ms,
        }
    }

    pub fn delta<I>(tool_call_id: impl Into<String>, sequence: u64, output: I) -> Self
    where
        I: IntoIterator<Item = ContentPart>,
    {
        Self::Delta {
            tool_call_id: tool_call_id.into(),
            sequence,
            output: output.into_iter().collect(),
        }
    }

    pub fn artifact_ready(
        tool_call_id: impl Into<String>,
        sequence: u64,
        artifact: ArtifactRef,
    ) -> Self {
        Self::ArtifactReady {
            tool_call_id: tool_call_id.into(),
            sequence,
            artifact,
        }
    }

    pub fn completed(tool_call_id: impl Into<String>, sequence: u64, result: ToolResult) -> Self {
        Self::Completed {
            tool_call_id: tool_call_id.into(),
            sequence,
            result,
        }
    }

    pub fn tool_call_id(&self) -> &str {
        match self {
            Self::Started { tool_call_id, .. }
            | Self::Delta { tool_call_id, .. }
            | Self::ArtifactReady { tool_call_id, .. }
            | Self::Completed { tool_call_id, .. } => tool_call_id,
        }
    }

    pub fn is_final_durable_result(&self) -> bool {
        matches!(self, Self::Completed { .. })
    }

    pub fn into_result(self) -> Option<ToolResult> {
        match self {
            Self::Completed { result, .. } => Some(result),
            Self::Started { .. } | Self::Delta { .. } | Self::ArtifactReady { .. } => None,
        }
    }
}

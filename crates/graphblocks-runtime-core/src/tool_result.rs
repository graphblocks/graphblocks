use std::collections::BTreeMap;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

use crate::observability::{CaptureDecision, CaptureMode, RedactionRule};
use crate::outcome::BlockError;
use crate::output_policy::RedactionInstruction;
use crate::tool::{ResolvedTool, ToolResultMode};
use crate::tool_call::ToolCall;
use crate::tool_schema::{ToolSchemaRegistry, ToolSchemaValidationError};

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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolEffectOutcome {
    NoExternalEffect,
    Committed,
    NotCommitted,
    Unknown,
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
    pub effect_outcome: ToolEffectOutcome,
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
            effect_outcome: ToolEffectOutcome::Unknown,
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
            effect_outcome: ToolEffectOutcome::Unknown,
        }
    }

    pub fn denied(
        tool_call_id: impl Into<String>,
        error: BlockError,
        completed_at_unix_ms: u64,
    ) -> Self {
        Self {
            tool_call_id: tool_call_id.into(),
            status: ToolResultStatus::Denied,
            output: Vec::new(),
            output_digest: None,
            artifacts: Vec::new(),
            diagnostics: Vec::new(),
            error: Some(error),
            started_at_unix_ms: None,
            completed_at_unix_ms: Some(completed_at_unix_ms),
            effect_outcome: ToolEffectOutcome::NotCommitted,
        }
    }

    pub fn cancelled(
        tool_call_id: impl Into<String>,
        started_at_unix_ms: u64,
        completed_at_unix_ms: u64,
    ) -> Self {
        Self {
            tool_call_id: tool_call_id.into(),
            status: ToolResultStatus::Cancelled,
            output: Vec::new(),
            output_digest: None,
            artifacts: Vec::new(),
            diagnostics: Vec::new(),
            error: None,
            started_at_unix_ms: Some(started_at_unix_ms),
            completed_at_unix_ms: Some(completed_at_unix_ms),
            effect_outcome: ToolEffectOutcome::Unknown,
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
            effect_outcome: ToolEffectOutcome::Unknown,
        }
    }

    pub fn incomplete(
        tool_call_id: impl Into<String>,
        started_at_unix_ms: u64,
        completed_at_unix_ms: u64,
    ) -> Self {
        Self {
            tool_call_id: tool_call_id.into(),
            status: ToolResultStatus::Incomplete,
            output: Vec::new(),
            output_digest: None,
            artifacts: Vec::new(),
            diagnostics: Vec::new(),
            error: None,
            started_at_unix_ms: Some(started_at_unix_ms),
            completed_at_unix_ms: Some(completed_at_unix_ms),
            effect_outcome: ToolEffectOutcome::Unknown,
        }
    }

    pub fn with_effect_outcome(mut self, effect_outcome: ToolEffectOutcome) -> Self {
        self.effect_outcome = effect_outcome;
        self
    }

    pub fn effect_was_committed(&self) -> bool {
        self.effect_outcome == ToolEffectOutcome::Committed
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

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ToolResultContentPolicy {
    pub max_output_bytes: Option<usize>,
    pub redactions: Vec<RedactionInstruction>,
    pub capture_decision: Option<CaptureDecision>,
}

impl ToolResultContentPolicy {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_max_output_bytes(mut self, max_output_bytes: usize) -> Self {
        self.max_output_bytes = Some(max_output_bytes);
        self
    }

    pub fn with_redactions<I>(mut self, redactions: I) -> Self
    where
        I: IntoIterator<Item = RedactionInstruction>,
    {
        self.redactions = redactions.into_iter().collect();
        self
    }

    pub fn with_capture_decision(mut self, capture_decision: CaptureDecision) -> Self {
        self.capture_decision = Some(capture_decision);
        self
    }
}

#[derive(Clone, Debug)]
pub struct ToolResultValidationRequest<'a> {
    pub call: &'a ToolCall,
    pub result: &'a ToolResult,
    pub resolved_tool: &'a ResolvedTool,
    pub schema_registry: &'a ToolSchemaRegistry,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolResultValidationError {
    ToolCallMismatch {
        expected: String,
        actual: String,
    },
    ResolvedToolMismatch {
        expected: String,
        actual: String,
    },
    OutputSchemaMissing {
        schema_id: String,
    },
    OutputContentMissing {
        tool_call_id: String,
    },
    OutputContentAmbiguous {
        tool_call_id: String,
        count: usize,
    },
    OutputSchemaInvalid {
        tool_call_id: String,
        schema_id: String,
        path: String,
        expected: String,
    },
    RequiredOutputMissing {
        tool_call_id: String,
        schema_id: String,
        path: String,
        property: String,
    },
    ModelOutputTooLarge {
        tool_call_id: String,
        max_bytes: usize,
        actual_bytes: usize,
    },
    ModelOutputRedactionInvalid {
        tool_call_id: String,
        path: String,
    },
    InlineOutputForbiddenForArtifactReference {
        tool_call_id: String,
    },
}

pub struct ToolResultValidation;

impl ToolResultValidation {
    pub fn validate_for_model(
        request: ToolResultValidationRequest<'_>,
    ) -> Result<(), ToolResultValidationError> {
        if request.result.tool_call_id != request.call.tool_call_id {
            return Err(ToolResultValidationError::ToolCallMismatch {
                expected: request.call.tool_call_id.clone(),
                actual: request.result.tool_call_id.clone(),
            });
        }
        if request.call.resolved_tool_id != request.resolved_tool.resolved_tool_id {
            return Err(ToolResultValidationError::ResolvedToolMismatch {
                expected: request.resolved_tool.resolved_tool_id.clone(),
                actual: request.call.resolved_tool_id.clone(),
            });
        }
        if request.result.status != ToolResultStatus::Completed {
            return Ok(());
        }

        if request.resolved_tool.binding.result_mode == ToolResultMode::ArtifactReference
            && request
                .result
                .output
                .iter()
                .any(|part| part.kind != ContentPartKind::ArtifactRef)
        {
            return Err(
                ToolResultValidationError::InlineOutputForbiddenForArtifactReference {
                    tool_call_id: request.result.tool_call_id.clone(),
                },
            );
        }

        let Some(output_schema) = request.resolved_tool.definition.output_schema.as_ref() else {
            return Ok(());
        };
        let json_outputs = request
            .result
            .output
            .iter()
            .filter(|part| part.kind == ContentPartKind::Json)
            .collect::<Vec<_>>();
        let [json_output] = json_outputs.as_slice() else {
            return if json_outputs.is_empty() {
                Err(ToolResultValidationError::OutputContentMissing {
                    tool_call_id: request.result.tool_call_id.clone(),
                })
            } else {
                Err(ToolResultValidationError::OutputContentAmbiguous {
                    tool_call_id: request.result.tool_call_id.clone(),
                    count: json_outputs.len(),
                })
            };
        };
        let Some(output_value) = json_output.data.as_ref() else {
            return Err(ToolResultValidationError::OutputContentMissing {
                tool_call_id: request.result.tool_call_id.clone(),
            });
        };

        request
            .schema_registry
            .validate(output_schema, output_value)
            .map_err(|error| match error {
                ToolSchemaValidationError::SchemaMissing { schema_id } => {
                    ToolResultValidationError::OutputSchemaMissing { schema_id }
                }
                ToolSchemaValidationError::TypeMismatch {
                    schema_id,
                    path,
                    expected,
                } => ToolResultValidationError::OutputSchemaInvalid {
                    tool_call_id: request.result.tool_call_id.clone(),
                    schema_id,
                    path,
                    expected,
                },
                ToolSchemaValidationError::RequiredPropertyMissing {
                    schema_id,
                    path,
                    property,
                } => ToolResultValidationError::RequiredOutputMissing {
                    tool_call_id: request.result.tool_call_id.clone(),
                    schema_id,
                    path,
                    property,
                },
            })
    }

    pub fn prepare_for_model(
        request: ToolResultValidationRequest<'_>,
    ) -> Result<Vec<ContentPart>, ToolResultValidationError> {
        Self::prepare_for_model_with_content_policy(request, &ToolResultContentPolicy::new())
    }

    pub fn prepare_for_model_with_limits(
        request: ToolResultValidationRequest<'_>,
        max_output_bytes: Option<usize>,
    ) -> Result<Vec<ContentPart>, ToolResultValidationError> {
        let mut policy = ToolResultContentPolicy::new();
        if let Some(max_output_bytes) = max_output_bytes {
            policy = policy.with_max_output_bytes(max_output_bytes);
        }
        Self::prepare_for_model_with_content_policy(request, &policy)
    }

    pub fn prepare_for_model_with_content_policy(
        request: ToolResultValidationRequest<'_>,
        content_policy: &ToolResultContentPolicy,
    ) -> Result<Vec<ContentPart>, ToolResultValidationError> {
        Self::validate_for_model(ToolResultValidationRequest {
            call: request.call,
            result: request.result,
            resolved_tool: request.resolved_tool,
            schema_registry: request.schema_registry,
        })?;
        if request.result.status != ToolResultStatus::Completed {
            return Ok(Vec::new());
        }

        let mut model_output = request
            .result
            .output
            .iter()
            .cloned()
            .map(|mut part| {
                part.metadata
                    .entry("trust_designation".to_string())
                    .or_insert_with(|| json!("untrusted_external"));
                part.metadata
                    .entry("prompt_injection_label".to_string())
                    .or_insert_with(|| json!("untrusted_tool_output"));
                part.metadata
                    .entry("content_classification".to_string())
                    .or_insert_with(|| json!("external_tool_output"));
                part
            })
            .collect::<Vec<_>>();

        if !content_policy.redactions.is_empty() {
            let mut redactions_by_part: BTreeMap<usize, Vec<&RedactionInstruction>> =
                BTreeMap::new();
            for redaction in &content_policy.redactions {
                let Some(part_index_text) = redaction
                    .path
                    .strip_prefix("/parts/")
                    .and_then(|suffix| suffix.strip_suffix("/text"))
                else {
                    return Err(ToolResultValidationError::ModelOutputRedactionInvalid {
                        tool_call_id: request.result.tool_call_id.clone(),
                        path: redaction.path.clone(),
                    });
                };
                let Ok(part_index) = part_index_text.parse::<usize>() else {
                    return Err(ToolResultValidationError::ModelOutputRedactionInvalid {
                        tool_call_id: request.result.tool_call_id.clone(),
                        path: redaction.path.clone(),
                    });
                };
                redactions_by_part
                    .entry(part_index)
                    .or_default()
                    .push(redaction);
            }

            for (part_index, mut redactions) in redactions_by_part {
                let Some(part) = model_output.get_mut(part_index) else {
                    return Err(ToolResultValidationError::ModelOutputRedactionInvalid {
                        tool_call_id: request.result.tool_call_id.clone(),
                        path: format!("/parts/{part_index}/text"),
                    });
                };
                let Some(text) = part.text.as_mut() else {
                    return Err(ToolResultValidationError::ModelOutputRedactionInvalid {
                        tool_call_id: request.result.tool_call_id.clone(),
                        path: format!("/parts/{part_index}/text"),
                    });
                };
                redactions.sort_by(|left, right| right.start.cmp(&left.start));
                for redaction in redactions {
                    let Ok(start) = usize::try_from(redaction.start) else {
                        return Err(ToolResultValidationError::ModelOutputRedactionInvalid {
                            tool_call_id: request.result.tool_call_id.clone(),
                            path: redaction.path.clone(),
                        });
                    };
                    let Ok(end) = usize::try_from(redaction.end) else {
                        return Err(ToolResultValidationError::ModelOutputRedactionInvalid {
                            tool_call_id: request.result.tool_call_id.clone(),
                            path: redaction.path.clone(),
                        });
                    };
                    if start > end
                        || end > text.len()
                        || !text.is_char_boundary(start)
                        || !text.is_char_boundary(end)
                    {
                        return Err(ToolResultValidationError::ModelOutputRedactionInvalid {
                            tool_call_id: request.result.tool_call_id.clone(),
                            path: redaction.path.clone(),
                        });
                    }
                    text.replace_range(start..end, &redaction.replacement);
                }
            }
        }

        if let Some(capture_decision) = content_policy.capture_decision.as_ref() {
            for part in &mut model_output {
                let capture_input = match part.kind {
                    ContentPartKind::Text => part
                        .text
                        .as_ref()
                        .map(|text| ("tool_result_text", text.clone(), None)),
                    ContentPartKind::Json => part.data.as_ref().map(|data| {
                        (
                            "tool_result_json",
                            serde_json::to_string(data).unwrap_or_default(),
                            None,
                        )
                    }),
                    ContentPartKind::ArtifactRef => part.data.as_ref().map(|data| {
                        (
                            "tool_result_artifact_ref",
                            serde_json::to_string(data).unwrap_or_default(),
                            data.get("uri").and_then(Value::as_str).map(str::to_owned),
                        )
                    }),
                };
                let Some((content_kind, capture_text, content_ref)) = capture_input else {
                    continue;
                };
                let captured = capture_decision.capture_text(
                    content_kind,
                    &capture_text,
                    content_ref.as_deref(),
                    Vec::<RedactionRule>::new(),
                );
                let mode = match captured.mode {
                    CaptureMode::None => "none",
                    CaptureMode::HashOnly => "hash_only",
                    CaptureMode::ReferenceOnly => "reference_only",
                    CaptureMode::RedactedPreview => "redacted_preview",
                    CaptureMode::Full => "full",
                };
                part.metadata.insert(
                    "capture".to_string(),
                    json!({
                        "mode": mode,
                        "content_kind": captured.content_kind,
                        "content_digest": captured.content_digest,
                        "preview": captured.preview,
                        "content_ref": captured.content_ref,
                        "retention_policy": captured.retention_policy,
                        "consent_ref": captured.consent_ref,
                        "redaction_count": captured.redaction_count,
                        "original_bytes": captured.original_bytes,
                    }),
                );
            }
        }

        if let Some(max_bytes) = content_policy.max_output_bytes {
            let mut actual_bytes = 0usize;
            for part in &model_output {
                if let Some(text) = part.text.as_ref() {
                    actual_bytes = actual_bytes.saturating_add(text.as_bytes().len());
                }
                if let Some(data) = part.data.as_ref() {
                    actual_bytes = actual_bytes
                        .saturating_add(serde_json::to_vec(data).unwrap_or_default().len());
                }
            }
            if actual_bytes > max_bytes {
                return Err(ToolResultValidationError::ModelOutputTooLarge {
                    tool_call_id: request.result.tool_call_id.clone(),
                    max_bytes,
                    actual_bytes,
                });
            }
        }

        Ok(model_output)
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
    Failed {
        tool_call_id: String,
        sequence: u64,
        result: ToolResult,
    },
    Denied {
        tool_call_id: String,
        sequence: u64,
        result: ToolResult,
    },
    Cancelled {
        tool_call_id: String,
        sequence: u64,
        result: ToolResult,
    },
    PolicyStopped {
        tool_call_id: String,
        sequence: u64,
        result: ToolResult,
    },
    Incomplete {
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

    pub fn failed(tool_call_id: impl Into<String>, sequence: u64, result: ToolResult) -> Self {
        Self::Failed {
            tool_call_id: tool_call_id.into(),
            sequence,
            result,
        }
    }

    pub fn denied(tool_call_id: impl Into<String>, sequence: u64, result: ToolResult) -> Self {
        Self::Denied {
            tool_call_id: tool_call_id.into(),
            sequence,
            result,
        }
    }

    pub fn cancelled(tool_call_id: impl Into<String>, sequence: u64, result: ToolResult) -> Self {
        Self::Cancelled {
            tool_call_id: tool_call_id.into(),
            sequence,
            result,
        }
    }

    pub fn policy_stopped(
        tool_call_id: impl Into<String>,
        sequence: u64,
        result: ToolResult,
    ) -> Self {
        Self::PolicyStopped {
            tool_call_id: tool_call_id.into(),
            sequence,
            result,
        }
    }

    pub fn incomplete(tool_call_id: impl Into<String>, sequence: u64, result: ToolResult) -> Self {
        Self::Incomplete {
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
            | Self::Completed { tool_call_id, .. }
            | Self::Failed { tool_call_id, .. }
            | Self::Denied { tool_call_id, .. }
            | Self::Cancelled { tool_call_id, .. }
            | Self::PolicyStopped { tool_call_id, .. }
            | Self::Incomplete { tool_call_id, .. } => tool_call_id,
        }
    }

    pub fn is_final_durable_result(&self) -> bool {
        matches!(
            self,
            Self::Completed { .. }
                | Self::Failed { .. }
                | Self::Denied { .. }
                | Self::Cancelled { .. }
                | Self::PolicyStopped { .. }
                | Self::Incomplete { .. }
        )
    }

    pub fn into_result(self) -> Option<ToolResult> {
        match self {
            Self::Completed { result, .. }
            | Self::Failed { result, .. }
            | Self::Denied { result, .. }
            | Self::Cancelled { result, .. }
            | Self::PolicyStopped { result, .. }
            | Self::Incomplete { result, .. } => Some(result),
            Self::Started { .. } | Self::Delta { .. } | Self::ArtifactReady { .. } => None,
        }
    }
}

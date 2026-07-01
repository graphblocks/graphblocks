use std::collections::{BTreeMap, BTreeSet};

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

    pub fn validate(&self) -> Result<(), ContentPartError> {
        match self.kind {
            ContentPartKind::Text => {
                if self.text.is_none() {
                    return Err(ContentPartError::MissingTextPayload);
                }
                if self.data.is_some() {
                    return Err(ContentPartError::UnexpectedDataPayload {
                        kind: ContentPartKind::Text,
                    });
                }
            }
            ContentPartKind::Json => {
                if self.data.is_none() {
                    return Err(ContentPartError::MissingJsonPayload);
                }
                if self.text.is_some() {
                    return Err(ContentPartError::UnexpectedTextPayload {
                        kind: ContentPartKind::Json,
                    });
                }
            }
            ContentPartKind::ArtifactRef => {
                if self.data.is_none() {
                    return Err(ContentPartError::MissingArtifactRefPayload);
                }
                if self.text.is_some() {
                    return Err(ContentPartError::UnexpectedTextPayload {
                        kind: ContentPartKind::ArtifactRef,
                    });
                }
            }
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ContentPartError {
    MissingTextPayload,
    MissingJsonPayload,
    MissingArtifactRefPayload,
    UnexpectedTextPayload { kind: ContentPartKind },
    UnexpectedDataPayload { kind: ContentPartKind },
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

    pub fn validate(&self) -> Result<(), ToolResultError> {
        for (field, value) in [
            ("artifact_id", self.artifact_id.as_str()),
            ("uri", self.uri.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(ToolResultError::EmptyArtifactField { field });
            }
        }
        for (field, value) in [
            ("checksum", self.checksum.as_deref()),
            ("media_type", self.media_type.as_deref()),
        ] {
            if value.is_some_and(|value| value.trim().is_empty()) {
                return Err(ToolResultError::EmptyArtifactField { field });
            }
        }
        Ok(())
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

impl ToolResultStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Completed => "completed",
            Self::Failed => "failed",
            Self::Denied => "denied",
            Self::Cancelled => "cancelled",
            Self::PolicyStopped => "policy_stopped",
            Self::Incomplete => "incomplete",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolEffectOutcome {
    NoExternalEffect,
    Committed,
    NotCommitted,
    Unknown,
}

impl ToolEffectOutcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::NoExternalEffect => "no_external_effect",
            Self::Committed => "committed",
            Self::NotCommitted => "not_committed",
            Self::Unknown => "unknown",
        }
    }
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolResultError {
    EmptyToolCallId,
    InvalidContentPart {
        source: ContentPartError,
    },
    EmptyArtifactField {
        field: &'static str,
    },
    EmptyDiagnosticField {
        field: &'static str,
    },
    CompletedBeforeStarted {
        started_at_unix_ms: u64,
        completed_at_unix_ms: u64,
    },
    InvalidEffectOutcome {
        status: ToolResultStatus,
        effect_outcome: ToolEffectOutcome,
    },
    OutputDigestMismatch {
        tool_call_id: String,
    },
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
        Self {
            tool_call_id: tool_call_id.into(),
            status: ToolResultStatus::Completed,
            output_digest: Some(tool_result_output_digest(&output)),
            output,
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

    pub fn validate(&self) -> Result<(), ToolResultError> {
        if self.tool_call_id.trim().is_empty() {
            return Err(ToolResultError::EmptyToolCallId);
        }
        if let (Some(started_at_unix_ms), Some(completed_at_unix_ms)) =
            (self.started_at_unix_ms, self.completed_at_unix_ms)
            && completed_at_unix_ms < started_at_unix_ms
        {
            return Err(ToolResultError::CompletedBeforeStarted {
                started_at_unix_ms,
                completed_at_unix_ms,
            });
        }
        if self.status == ToolResultStatus::Denied
            && matches!(
                self.effect_outcome,
                ToolEffectOutcome::Committed | ToolEffectOutcome::Unknown
            )
        {
            return Err(ToolResultError::InvalidEffectOutcome {
                status: self.status,
                effect_outcome: self.effect_outcome,
            });
        }
        for artifact in &self.artifacts {
            artifact.validate()?;
        }
        for diagnostic in &self.diagnostics {
            for (field, value) in [
                ("code", diagnostic.code.as_str()),
                ("message", diagnostic.message.as_str()),
            ] {
                if value.trim().is_empty() {
                    return Err(ToolResultError::EmptyDiagnosticField { field });
                }
            }
            if diagnostic
                .path
                .as_ref()
                .is_some_and(|path| path.trim().is_empty())
            {
                return Err(ToolResultError::EmptyDiagnosticField { field: "path" });
            }
        }
        for part in &self.output {
            part.validate()
                .map_err(|source| ToolResultError::InvalidContentPart { source })?;
        }
        if let Some(output_digest) = self.output_digest.as_ref()
            && output_digest != &tool_result_output_digest(&self.output)
        {
            return Err(ToolResultError::OutputDigestMismatch {
                tool_call_id: self.tool_call_id.clone(),
            });
        }
        Ok(())
    }
}

fn tool_result_output_digest(output: &[ContentPart]) -> String {
    canonical_hash(&Value::Array(
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
    ))
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ToolResultContentPolicy {
    pub max_output_bytes: Option<usize>,
    pub redactions: Vec<RedactionInstruction>,
    pub capture_decision: Option<CaptureDecision>,
    pub trust_designation: String,
    pub prompt_injection_label: String,
    pub content_classification: String,
}

impl Default for ToolResultContentPolicy {
    fn default() -> Self {
        Self {
            max_output_bytes: None,
            redactions: Vec::new(),
            capture_decision: None,
            trust_designation: "untrusted_external".to_string(),
            prompt_injection_label: "untrusted_tool_output".to_string(),
            content_classification: "external_tool_output".to_string(),
        }
    }
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

    pub fn with_model_output_labels(
        mut self,
        trust_designation: impl Into<String>,
        prompt_injection_label: impl Into<String>,
        content_classification: impl Into<String>,
    ) -> Self {
        self.trust_designation = trust_designation.into();
        self.prompt_injection_label = prompt_injection_label.into();
        self.content_classification = content_classification.into();
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
    InvalidToolResult {
        source: ToolResultError,
    },
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
    OutputDigestMissing {
        tool_call_id: String,
    },
    OutputDigestMismatch {
        tool_call_id: String,
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
    ModelOutputLabelInvalid {
        field: String,
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
        if let Err(source) = request.result.validate() {
            return match source {
                ToolResultError::OutputDigestMismatch { tool_call_id } => {
                    Err(ToolResultValidationError::OutputDigestMismatch { tool_call_id })
                }
                source => Err(ToolResultValidationError::InvalidToolResult { source }),
            };
        }
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
        let Some(output_digest) = request.result.output_digest.as_ref() else {
            return Err(ToolResultValidationError::OutputDigestMissing {
                tool_call_id: request.result.tool_call_id.clone(),
            });
        };
        if output_digest != &tool_result_output_digest(&request.result.output) {
            return Err(ToolResultValidationError::OutputDigestMismatch {
                tool_call_id: request.result.tool_call_id.clone(),
            });
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
        for (field, value) in [
            ("trust_designation", &content_policy.trust_designation),
            (
                "prompt_injection_label",
                &content_policy.prompt_injection_label,
            ),
            (
                "content_classification",
                &content_policy.content_classification,
            ),
        ] {
            if value.trim().is_empty() {
                return Err(ToolResultValidationError::ModelOutputLabelInvalid {
                    field: field.to_string(),
                });
            }
        }

        let mut model_output = request
            .result
            .output
            .iter()
            .cloned()
            .map(|mut part| {
                part.metadata.insert(
                    "trust_designation".to_string(),
                    Value::String(content_policy.trust_designation.clone()),
                );
                part.metadata.insert(
                    "prompt_injection_label".to_string(),
                    Value::String(content_policy.prompt_injection_label.clone()),
                );
                part.metadata.insert(
                    "content_classification".to_string(),
                    Value::String(content_policy.content_classification.clone()),
                );
                part
            })
            .collect::<Vec<_>>();

        let mut redaction_counts_by_part: BTreeMap<usize, u64> = BTreeMap::new();
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
                if part_index_text.is_empty()
                    || !part_index_text.bytes().all(|byte| byte.is_ascii_digit())
                    || (part_index_text != "0" && part_index_text.starts_with('0'))
                {
                    return Err(ToolResultValidationError::ModelOutputRedactionInvalid {
                        tool_call_id: request.result.tool_call_id.clone(),
                        path: redaction.path.clone(),
                    });
                }
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
                    let char_count = text.chars().count();
                    if start > end || end > char_count {
                        return Err(ToolResultValidationError::ModelOutputRedactionInvalid {
                            tool_call_id: request.result.tool_call_id.clone(),
                            path: redaction.path.clone(),
                        });
                    }
                    let start_byte = if start == char_count {
                        text.len()
                    } else {
                        text.char_indices()
                            .nth(start)
                            .map(|(index, _)| index)
                            .unwrap_or(text.len())
                    };
                    let end_byte = if end == char_count {
                        text.len()
                    } else {
                        text.char_indices()
                            .nth(end)
                            .map(|(index, _)| index)
                            .unwrap_or(text.len())
                    };
                    text.replace_range(start_byte..end_byte, &redaction.replacement);
                    let redaction_count = redaction_counts_by_part.entry(part_index).or_default();
                    *redaction_count = redaction_count.saturating_add(1);
                }
            }
        }

        if let Some(capture_decision) = content_policy.capture_decision.as_ref() {
            for (part_index, part) in model_output.iter_mut().enumerate() {
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
                        "redaction_count": captured.redaction_count.saturating_add(
                            redaction_counts_by_part.get(&part_index).copied().unwrap_or(0),
                        ),
                        "original_bytes": captured.original_bytes,
                    }),
                );
            }
        }

        if let Some(max_bytes) = content_policy.max_output_bytes {
            let mut actual_bytes = 0usize;
            for part in &model_output {
                if let Some(text) = part.text.as_ref() {
                    actual_bytes = actual_bytes.saturating_add(text.len());
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolResultEventError {
    EmptyToolCallId,
    InvalidSequence {
        sequence: u64,
    },
    InvalidOutput {
        source: ContentPartError,
    },
    InvalidArtifact {
        source: ToolResultError,
    },
    InvalidResult {
        source: ToolResultError,
    },
    ResultToolCallMismatch {
        event_tool_call_id: String,
        result_tool_call_id: String,
    },
    ResultStatusMismatch {
        kind: String,
        expected: ToolResultStatus,
        actual: ToolResultStatus,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolResultStreamError {
    InvalidEvent {
        source: ToolResultEventError,
    },
    NonMonotonicSequence {
        tool_call_id: String,
        last_sequence: u64,
        sequence: u64,
    },
    EventAfterFinalResult {
        tool_call_id: String,
        final_status: ToolResultStatus,
    },
    DuplicateStarted {
        tool_call_id: String,
        last_sequence: u64,
        sequence: u64,
    },
    EventBeforeStarted {
        tool_call_id: String,
        kind: String,
        sequence: u64,
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

    pub fn sequence(&self) -> u64 {
        match self {
            Self::Started { sequence, .. }
            | Self::Delta { sequence, .. }
            | Self::ArtifactReady { sequence, .. }
            | Self::Completed { sequence, .. }
            | Self::Failed { sequence, .. }
            | Self::Denied { sequence, .. }
            | Self::Cancelled { sequence, .. }
            | Self::PolicyStopped { sequence, .. }
            | Self::Incomplete { sequence, .. } => *sequence,
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

    pub fn validate(&self) -> Result<(), ToolResultEventError> {
        if self.tool_call_id().trim().is_empty() {
            return Err(ToolResultEventError::EmptyToolCallId);
        }
        if self.sequence() == 0 {
            return Err(ToolResultEventError::InvalidSequence { sequence: 0 });
        }
        if let Self::Delta { output, .. } = self {
            for part in output {
                part.validate()
                    .map_err(|source| ToolResultEventError::InvalidOutput { source })?;
            }
        }
        if let Self::ArtifactReady { artifact, .. } = self {
            artifact
                .validate()
                .map_err(|source| ToolResultEventError::InvalidArtifact { source })?;
        }

        let Some((kind, expected, event_tool_call_id, result)) = (match self {
            Self::Completed {
                tool_call_id,
                result,
                ..
            } => Some((
                "completed",
                ToolResultStatus::Completed,
                tool_call_id,
                result,
            )),
            Self::Failed {
                tool_call_id,
                result,
                ..
            } => Some(("failed", ToolResultStatus::Failed, tool_call_id, result)),
            Self::Denied {
                tool_call_id,
                result,
                ..
            } => Some(("denied", ToolResultStatus::Denied, tool_call_id, result)),
            Self::Cancelled {
                tool_call_id,
                result,
                ..
            } => Some((
                "cancelled",
                ToolResultStatus::Cancelled,
                tool_call_id,
                result,
            )),
            Self::PolicyStopped {
                tool_call_id,
                result,
                ..
            } => Some((
                "policy_stopped",
                ToolResultStatus::PolicyStopped,
                tool_call_id,
                result,
            )),
            Self::Incomplete {
                tool_call_id,
                result,
                ..
            } => Some((
                "incomplete",
                ToolResultStatus::Incomplete,
                tool_call_id,
                result,
            )),
            Self::Started { .. } | Self::Delta { .. } | Self::ArtifactReady { .. } => None,
        }) else {
            return Ok(());
        };
        result
            .validate()
            .map_err(|source| ToolResultEventError::InvalidResult { source })?;
        if result.tool_call_id != *event_tool_call_id {
            return Err(ToolResultEventError::ResultToolCallMismatch {
                event_tool_call_id: event_tool_call_id.clone(),
                result_tool_call_id: result.tool_call_id.clone(),
            });
        }
        if result.status != expected {
            return Err(ToolResultEventError::ResultStatusMismatch {
                kind: kind.to_owned(),
                expected,
                actual: result.status,
            });
        }
        Ok(())
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

#[derive(Clone, Debug, Default)]
pub struct ToolResultStreamState {
    last_sequences: BTreeMap<String, u64>,
    started_tool_calls: BTreeSet<String>,
    final_results: BTreeMap<String, ToolResult>,
    accepted_events: Vec<ToolResultEvent>,
}

impl ToolResultStreamState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn accept(
        &mut self,
        event: ToolResultEvent,
    ) -> Result<ToolResultEvent, ToolResultStreamError> {
        event
            .validate()
            .map_err(|source| ToolResultStreamError::InvalidEvent { source })?;
        let tool_call_id = event.tool_call_id().to_owned();
        if let Some(final_result) = self.final_results.get(&tool_call_id) {
            return Err(ToolResultStreamError::EventAfterFinalResult {
                tool_call_id,
                final_status: final_result.status,
            });
        }

        let sequence = event.sequence();
        if let Some(last_sequence) = self.last_sequences.get(&tool_call_id)
            && sequence <= *last_sequence
        {
            return Err(ToolResultStreamError::NonMonotonicSequence {
                tool_call_id,
                last_sequence: *last_sequence,
                sequence,
            });
        }

        match &event {
            ToolResultEvent::Started { .. } => {
                if self.started_tool_calls.contains(&tool_call_id) {
                    return Err(ToolResultStreamError::DuplicateStarted {
                        tool_call_id,
                        last_sequence: *self.last_sequences.get(event.tool_call_id()).unwrap_or(&0),
                        sequence,
                    });
                }
            }
            ToolResultEvent::Delta { .. } | ToolResultEvent::ArtifactReady { .. } => {
                if !self.started_tool_calls.contains(&tool_call_id) {
                    return Err(ToolResultStreamError::EventBeforeStarted {
                        tool_call_id,
                        kind: match &event {
                            ToolResultEvent::Delta { .. } => "delta",
                            ToolResultEvent::ArtifactReady { .. } => "artifact_ready",
                            _ => unreachable!(),
                        }
                        .to_owned(),
                        sequence,
                    });
                }
            }
            ToolResultEvent::Completed { result, .. }
            | ToolResultEvent::Failed { result, .. }
            | ToolResultEvent::Denied { result, .. }
            | ToolResultEvent::Cancelled { result, .. }
            | ToolResultEvent::PolicyStopped { result, .. }
            | ToolResultEvent::Incomplete { result, .. } => {
                if result.started_at_unix_ms.is_some()
                    && !self.started_tool_calls.contains(&tool_call_id)
                {
                    return Err(ToolResultStreamError::EventBeforeStarted {
                        tool_call_id,
                        kind: match &event {
                            ToolResultEvent::Completed { .. } => "completed",
                            ToolResultEvent::Failed { .. } => "failed",
                            ToolResultEvent::Denied { .. } => "denied",
                            ToolResultEvent::Cancelled { .. } => "cancelled",
                            ToolResultEvent::PolicyStopped { .. } => "policy_stopped",
                            ToolResultEvent::Incomplete { .. } => "incomplete",
                            _ => unreachable!(),
                        }
                        .to_owned(),
                        sequence,
                    });
                }
            }
        }

        if let Some(result) = event.clone().into_result() {
            self.final_results.insert(tool_call_id.clone(), result);
        }
        if matches!(event, ToolResultEvent::Started { .. }) {
            self.started_tool_calls.insert(tool_call_id.clone());
        }
        self.last_sequences.insert(tool_call_id, sequence);
        self.accepted_events.push(event.clone());
        Ok(event)
    }

    pub fn accepted_events(&self) -> &[ToolResultEvent] {
        &self.accepted_events
    }

    pub fn final_result_for(&self, tool_call_id: &str) -> Option<&ToolResult> {
        self.final_results.get(tool_call_id)
    }

    pub fn last_sequence_for(&self, tool_call_id: &str) -> Option<u64> {
        self.last_sequences.get(tool_call_id).copied()
    }
}

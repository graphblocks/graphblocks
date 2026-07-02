use std::collections::{BTreeMap, VecDeque};
use std::error::Error;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct SpanTiming {
    pub scheduled_at: Option<u64>,
    pub admitted_at: Option<u64>,
    pub started_at: Option<u64>,
    pub first_output_at: Option<u64>,
    pub completed_at: Option<u64>,
}

impl SpanTiming {
    pub fn new(scheduled_at: u64) -> Self {
        Self {
            scheduled_at: Some(scheduled_at),
            admitted_at: None,
            started_at: None,
            first_output_at: None,
            completed_at: None,
        }
    }

    pub fn with_admitted_at(mut self, admitted_at: u64) -> Self {
        self.admitted_at = Some(admitted_at);
        self
    }

    pub fn with_started_at(mut self, started_at: u64) -> Self {
        self.started_at = Some(started_at);
        self
    }

    pub fn with_first_output_at(mut self, first_output_at: u64) -> Self {
        self.first_output_at = Some(first_output_at);
        self
    }

    pub fn with_completed_at(mut self, completed_at: u64) -> Self {
        self.completed_at = Some(completed_at);
        self
    }

    pub fn queue_wait_ms(&self) -> Option<u64> {
        checked_duration(self.scheduled_at, self.admitted_at)
    }

    pub fn flow_wait_ms(&self) -> Option<u64> {
        checked_duration(self.admitted_at, self.started_at)
    }

    pub fn time_to_first_output_ms(&self) -> Option<u64> {
        checked_duration(self.started_at, self.first_output_at)
    }

    pub fn execution_ms(&self) -> Option<u64> {
        checked_duration(self.started_at, self.completed_at)
    }

    pub fn streaming_ms(&self) -> Option<u64> {
        checked_duration(self.first_output_at, self.completed_at)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GenerationObservation {
    pub span_id: String,
    pub node_id: String,
    pub provider: String,
    pub model: String,
    pub timing: SpanTiming,
    pub chunk_count: u64,
    pub first_chunk_sequence: Option<u64>,
    pub last_chunk_sequence: Option<u64>,
    pub output_bytes: u64,
    pub usage: BTreeMap<String, u64>,
    pub finish_reason: Option<String>,
}

impl GenerationObservation {
    pub fn new(
        span_id: impl Into<String>,
        node_id: impl Into<String>,
        provider: impl Into<String>,
        model: impl Into<String>,
    ) -> Self {
        Self {
            span_id: span_id.into(),
            node_id: node_id.into(),
            provider: provider.into(),
            model: model.into(),
            timing: SpanTiming::default(),
            chunk_count: 0,
            first_chunk_sequence: None,
            last_chunk_sequence: None,
            output_bytes: 0,
            usage: BTreeMap::new(),
            finish_reason: None,
        }
    }

    pub fn with_timing(mut self, timing: SpanTiming) -> Self {
        self.timing = timing;
        self
    }

    pub fn record_chunk(mut self, sequence: u64, byte_count: u64, observed_at: u64) -> Self {
        self.chunk_count += 1;
        self.output_bytes += byte_count;
        self.first_chunk_sequence = Some(
            self.first_chunk_sequence
                .map_or(sequence, |current| current.min(sequence)),
        );
        self.last_chunk_sequence = Some(
            self.last_chunk_sequence
                .map_or(sequence, |current| current.max(sequence)),
        );
        if self.timing.first_output_at.is_none() {
            self.timing.first_output_at = Some(observed_at);
        }
        self
    }

    pub fn record_usage(mut self, unit: impl Into<String>, amount: u64) -> Self {
        *self.usage.entry(unit.into()).or_insert(0) += amount;
        self
    }

    pub fn finish(mut self, finish_reason: impl Into<String>, completed_at: u64) -> Self {
        self.finish_reason = Some(finish_reason.into());
        self.timing.completed_at = Some(completed_at);
        self
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct MetricLabelSet {
    pub labels: BTreeMap<String, String>,
}

impl MetricLabelSet {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_label(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.labels.insert(key.into(), value.into());
        self
    }

    pub fn validate_cardinality_budget(&self) -> Result<(), MetricLabelError> {
        let labels = self
            .labels
            .keys()
            .filter(|label| is_forbidden_metric_label(label))
            .cloned()
            .collect::<Vec<_>>();
        if labels.is_empty() {
            Ok(())
        } else {
            Err(MetricLabelError::ForbiddenLabels { labels })
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum MetricLabelError {
    ForbiddenLabels { labels: Vec<String> },
}

impl fmt::Display for MetricLabelError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ForbiddenLabels { labels } => {
                write!(
                    formatter,
                    "forbidden high-cardinality metric labels: {labels:?}"
                )
            }
        }
    }
}

impl Error for MetricLabelError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ObservabilityEventName {
    AsyncOperationStart,
    AsyncOperationWait,
    AsyncOperationCallbackReceived,
    AsyncOperationResume,
    CallbackDeliverySchedule,
    CallbackDeliveryAttempt,
    CallbackDeliverySuccess,
    CallbackDeliveryFailure,
    CallbackDeliveryDeadLetter,
    RunAttach,
    RunDetach,
    RunReplay,
}

impl ObservabilityEventName {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::AsyncOperationStart => "async.operation.start",
            Self::AsyncOperationWait => "async.operation.wait",
            Self::AsyncOperationCallbackReceived => "async.operation.callback_received",
            Self::AsyncOperationResume => "async.operation.resume",
            Self::CallbackDeliverySchedule => "callback.delivery.schedule",
            Self::CallbackDeliveryAttempt => "callback.delivery.attempt",
            Self::CallbackDeliverySuccess => "callback.delivery.success",
            Self::CallbackDeliveryFailure => "callback.delivery.failure",
            Self::CallbackDeliveryDeadLetter => "callback.delivery.dead_letter",
            Self::RunAttach => "run.attach",
            Self::RunDetach => "run.detach",
            Self::RunReplay => "run.replay",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ObservabilityObservation {
    pub name: ObservabilityEventName,
    pub labels: MetricLabelSet,
    pub attributes: BTreeMap<String, Value>,
}

impl ObservabilityObservation {
    pub fn new(name: ObservabilityEventName) -> Self {
        Self {
            name,
            labels: MetricLabelSet::new(),
            attributes: BTreeMap::new(),
        }
    }

    pub fn with_label(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.labels = self.labels.with_label(key, value);
        self
    }

    pub fn with_attribute(mut self, key: impl Into<String>, value: Value) -> Self {
        self.attributes.insert(key.into(), value);
        self
    }

    pub fn validate_metric_labels(&self) -> Result<(), MetricLabelError> {
        self.labels.validate_cardinality_budget()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CaptureMode {
    None,
    HashOnly,
    ReferenceOnly,
    RedactedPreview,
    Full,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CaptureDecision {
    pub mode: CaptureMode,
    pub retention_policy: String,
    pub consent_ref: Option<String>,
}

impl CaptureDecision {
    pub fn none(retention_policy: impl Into<String>) -> Self {
        Self {
            mode: CaptureMode::None,
            retention_policy: retention_policy.into(),
            consent_ref: None,
        }
    }

    pub fn hash_only(retention_policy: impl Into<String>) -> Self {
        Self {
            mode: CaptureMode::HashOnly,
            retention_policy: retention_policy.into(),
            consent_ref: None,
        }
    }

    pub fn reference_only(retention_policy: impl Into<String>) -> Self {
        Self {
            mode: CaptureMode::ReferenceOnly,
            retention_policy: retention_policy.into(),
            consent_ref: None,
        }
    }

    pub fn redacted_preview(retention_policy: impl Into<String>) -> Self {
        Self {
            mode: CaptureMode::RedactedPreview,
            retention_policy: retention_policy.into(),
            consent_ref: None,
        }
    }

    pub fn full(retention_policy: impl Into<String>) -> Self {
        Self {
            mode: CaptureMode::Full,
            retention_policy: retention_policy.into(),
            consent_ref: None,
        }
    }

    pub fn with_consent_ref(mut self, consent_ref: impl Into<String>) -> Self {
        self.consent_ref = Some(consent_ref.into());
        self
    }

    pub fn capture_text(
        &self,
        content_kind: impl Into<String>,
        text: impl AsRef<str>,
        content_ref: Option<&str>,
        redactions: impl IntoIterator<Item = RedactionRule>,
    ) -> CapturedContent {
        let text = text.as_ref();
        let mut preview = text.to_owned();
        let mut redaction_count = 0;
        for redaction in redactions {
            if redaction.pattern.is_empty() {
                continue;
            }
            let count = preview.matches(&redaction.pattern).count();
            if count > 0 {
                preview = preview.replace(&redaction.pattern, &redaction.replacement);
                redaction_count += count as u64;
            }
        }

        CapturedContent {
            mode: self.mode,
            content_kind: content_kind.into(),
            content_digest: canonical_hash(&json!(text)),
            preview: match self.mode {
                CaptureMode::RedactedPreview | CaptureMode::Full => Some(preview),
                CaptureMode::None | CaptureMode::HashOnly | CaptureMode::ReferenceOnly => None,
            },
            content_ref: match self.mode {
                CaptureMode::ReferenceOnly => content_ref.map(str::to_owned),
                CaptureMode::None
                | CaptureMode::HashOnly
                | CaptureMode::RedactedPreview
                | CaptureMode::Full => None,
            },
            retention_policy: self.retention_policy.clone(),
            consent_ref: self.consent_ref.clone(),
            redaction_count: match self.mode {
                CaptureMode::RedactedPreview | CaptureMode::Full => redaction_count,
                CaptureMode::None | CaptureMode::HashOnly | CaptureMode::ReferenceOnly => 0,
            },
            original_bytes: text.len() as u64,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RedactionRule {
    pub pattern: String,
    pub replacement: String,
}

impl RedactionRule {
    pub fn literal(pattern: impl Into<String>, replacement: impl Into<String>) -> Self {
        Self {
            pattern: pattern.into(),
            replacement: replacement.into(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CapturedContent {
    pub mode: CaptureMode,
    pub content_kind: String,
    pub content_digest: String,
    pub preview: Option<String>,
    pub content_ref: Option<String>,
    pub retention_policy: String,
    pub consent_ref: Option<String>,
    pub redaction_count: u64,
    pub original_bytes: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TelemetryOnFull {
    DropLowPriority,
    DropNewest,
    Reject,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum TelemetryPriority {
    Low,
    Normal,
    High,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TelemetryRecordKind {
    DebugSpan,
    Progress,
    TokenDebug,
    Span,
    Metric,
    ExporterHealth,
    RequiredAudit,
    UsageLedger,
    EffectTerminal,
    RunTerminal,
    RequiredEvaluationResult,
}

impl TelemetryRecordKind {
    pub fn requires_durable_path(self) -> bool {
        matches!(
            self,
            Self::RequiredAudit
                | Self::UsageLedger
                | Self::EffectTerminal
                | Self::RunTerminal
                | Self::RequiredEvaluationResult
        )
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TelemetryExporterKind {
    Otlp,
    Langfuse,
    Prometheus,
    AuditLog,
    UsageLedger,
    DiagnosticBundle,
    Custom,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TelemetryExporterReliability {
    Durable,
    Lossless,
    Lossy,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryExporterRoute {
    pub exporter_id: String,
    pub kind: TelemetryExporterKind,
    pub reliability: TelemetryExporterReliability,
}

impl TelemetryExporterRoute {
    pub fn new(
        exporter_id: impl Into<String>,
        kind: TelemetryExporterKind,
        reliability: TelemetryExporterReliability,
    ) -> Self {
        Self {
            exporter_id: exporter_id.into(),
            kind,
            reliability,
        }
    }

    pub fn validate_record_kind(
        &self,
        record_kind: TelemetryRecordKind,
    ) -> Result<(), TelemetryExporterRouteError> {
        if self.exporter_id.trim().is_empty() {
            return Err(TelemetryExporterRouteError::EmptyExporterId);
        }
        if self.reliability == TelemetryExporterReliability::Lossy
            && record_kind.requires_durable_path()
        {
            return Err(TelemetryExporterRouteError::LossyDurableRecord {
                exporter_id: self.exporter_id.clone(),
                kind: record_kind,
            });
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TelemetryExporterRouteError {
    EmptyExporterId,
    LossyDurableRecord {
        exporter_id: String,
        kind: TelemetryRecordKind,
    },
}

impl fmt::Display for TelemetryExporterRouteError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyExporterId => write!(formatter, "telemetry exporter id must not be empty"),
            Self::LossyDurableRecord { exporter_id, kind } => write!(
                formatter,
                "telemetry exporter {exporter_id:?} is lossy and cannot export durable record kind {kind:?}"
            ),
        }
    }
}

impl Error for TelemetryExporterRouteError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryQueuePolicy {
    pub max_items: usize,
    pub on_full: TelemetryOnFull,
}

impl TelemetryQueuePolicy {
    pub fn new(max_items: usize, on_full: TelemetryOnFull) -> Self {
        Self { max_items, on_full }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryRecord {
    pub record_id: String,
    pub kind: TelemetryRecordKind,
    pub priority: TelemetryPriority,
}

impl TelemetryRecord {
    pub fn new(
        record_id: impl Into<String>,
        kind: TelemetryRecordKind,
        priority: TelemetryPriority,
    ) -> Self {
        Self {
            record_id: record_id.into(),
            kind,
            priority,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryEnqueueOutcome {
    pub accepted: bool,
    pub dropped_record_ids: Vec<String>,
}

impl TelemetryEnqueueOutcome {
    pub fn accepted() -> Self {
        Self {
            accepted: true,
            dropped_record_ids: Vec::new(),
        }
    }

    pub fn accepted_with_drop(records: impl IntoIterator<Item = impl Into<String>>) -> Self {
        Self {
            accepted: true,
            dropped_record_ids: records.into_iter().map(Into::into).collect(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TelemetryBufferError {
    QueueFull { record_id: String },
    RequiredDurablePath { kind: TelemetryRecordKind },
}

impl fmt::Display for TelemetryBufferError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::QueueFull { record_id } => {
                write!(
                    formatter,
                    "telemetry queue is full for record {record_id:?}"
                )
            }
            Self::RequiredDurablePath { kind } => {
                write!(
                    formatter,
                    "telemetry record kind {kind:?} requires a durable record path"
                )
            }
        }
    }
}

impl Error for TelemetryBufferError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DiagnosticBundleRedaction {
    ContentFree,
    Redacted,
    Full,
}

impl DiagnosticBundleRedaction {
    fn as_str(self) -> &'static str {
        match self {
            Self::ContentFree => "content_free",
            Self::Redacted => "redacted",
            Self::Full => "full",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DiagnosticExcerptKind {
    Trace,
    Log,
    Metric,
    WorkerStatus,
    RunSummary,
}

impl DiagnosticExcerptKind {
    fn as_str(self) -> &'static str {
        match self {
            Self::Trace => "trace",
            Self::Log => "log",
            Self::Metric => "metric",
            Self::WorkerStatus => "worker_status",
            Self::RunSummary => "run_summary",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct DiagnosticExcerpt {
    pub excerpt_id: String,
    pub kind: DiagnosticExcerptKind,
    pub content_mode: CaptureMode,
    pub payload: Value,
}

impl DiagnosticExcerpt {
    pub fn new(excerpt_id: impl Into<String>, kind: DiagnosticExcerptKind) -> Self {
        Self {
            excerpt_id: excerpt_id.into(),
            kind,
            content_mode: CaptureMode::HashOnly,
            payload: json!({}),
        }
    }

    pub fn with_content_mode(mut self, content_mode: CaptureMode) -> Self {
        self.content_mode = content_mode;
        self
    }

    pub fn with_payload(mut self, payload: Value) -> Self {
        self.payload = payload;
        self
    }

    fn canonical_value(&self) -> Value {
        json!({
            "excerpt_id": self.excerpt_id,
            "kind": self.kind.as_str(),
            "content_mode": capture_mode_str(self.content_mode),
            "payload": self.payload,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct DiagnosticBundle {
    pub bundle_id: String,
    pub run_id: String,
    pub redaction: DiagnosticBundleRedaction,
    pub release_id: Option<String>,
    pub deployment_revision_id: Option<String>,
    pub normalized_graph_hash: Option<String>,
    pub physical_plan_hash: Option<String>,
    pub package_inventory: BTreeMap<String, String>,
    pub configuration_hashes: BTreeMap<String, String>,
    pub run_terminal_summary: Option<Value>,
    pub node_terminal_summaries: BTreeMap<String, Value>,
    pub excerpts: Vec<DiagnosticExcerpt>,
    pub redaction_report: Option<String>,
}

impl DiagnosticBundle {
    pub fn content_free(bundle_id: impl Into<String>, run_id: impl Into<String>) -> Self {
        Self::new(bundle_id, run_id, DiagnosticBundleRedaction::ContentFree)
    }

    pub fn redacted(bundle_id: impl Into<String>, run_id: impl Into<String>) -> Self {
        Self::new(bundle_id, run_id, DiagnosticBundleRedaction::Redacted)
    }

    pub fn full(bundle_id: impl Into<String>, run_id: impl Into<String>) -> Self {
        Self::new(bundle_id, run_id, DiagnosticBundleRedaction::Full)
    }

    fn new(
        bundle_id: impl Into<String>,
        run_id: impl Into<String>,
        redaction: DiagnosticBundleRedaction,
    ) -> Self {
        Self {
            bundle_id: bundle_id.into(),
            run_id: run_id.into(),
            redaction,
            release_id: None,
            deployment_revision_id: None,
            normalized_graph_hash: None,
            physical_plan_hash: None,
            package_inventory: BTreeMap::new(),
            configuration_hashes: BTreeMap::new(),
            run_terminal_summary: None,
            node_terminal_summaries: BTreeMap::new(),
            excerpts: Vec::new(),
            redaction_report: None,
        }
    }

    pub fn with_release(
        mut self,
        release_id: impl Into<String>,
        deployment_revision_id: impl Into<String>,
    ) -> Self {
        self.release_id = Some(release_id.into());
        self.deployment_revision_id = Some(deployment_revision_id.into());
        self
    }

    pub fn with_plan_hashes(
        mut self,
        normalized_graph_hash: impl Into<String>,
        physical_plan_hash: impl Into<String>,
    ) -> Self {
        self.normalized_graph_hash = Some(normalized_graph_hash.into());
        self.physical_plan_hash = Some(physical_plan_hash.into());
        self
    }

    pub fn with_package(mut self, package: impl Into<String>, version: impl Into<String>) -> Self {
        self.package_inventory
            .insert(package.into(), version.into());
        self
    }

    pub fn with_configuration_hash(
        mut self,
        name: impl Into<String>,
        digest: impl Into<String>,
    ) -> Self {
        self.configuration_hashes.insert(name.into(), digest.into());
        self
    }

    pub fn with_run_terminal_summary(mut self, summary: Value) -> Self {
        self.run_terminal_summary = Some(summary);
        self
    }

    pub fn with_node_terminal_summary(
        mut self,
        node_id: impl Into<String>,
        summary: Value,
    ) -> Self {
        self.node_terminal_summaries.insert(node_id.into(), summary);
        self
    }

    pub fn with_excerpt(mut self, excerpt: DiagnosticExcerpt) -> Self {
        self.excerpts.push(excerpt);
        self
    }

    pub fn with_redaction_report(mut self, redaction_report: impl Into<String>) -> Self {
        self.redaction_report = Some(redaction_report.into());
        self
    }

    pub fn validate_redaction(&self) -> Result<(), DiagnosticBundleError> {
        for excerpt in &self.excerpts {
            let unredacted = match self.redaction {
                DiagnosticBundleRedaction::ContentFree => matches!(
                    excerpt.content_mode,
                    CaptureMode::RedactedPreview | CaptureMode::Full
                ),
                DiagnosticBundleRedaction::Redacted => excerpt.content_mode == CaptureMode::Full,
                DiagnosticBundleRedaction::Full => false,
            };
            if unredacted {
                return Err(DiagnosticBundleError::UnredactedContent {
                    excerpt_id: excerpt.excerpt_id.clone(),
                });
            }
        }
        Ok(())
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "run_id": self.run_id,
            "redaction": self.redaction.as_str(),
            "release_id": self.release_id,
            "deployment_revision_id": self.deployment_revision_id,
            "normalized_graph_hash": self.normalized_graph_hash,
            "physical_plan_hash": self.physical_plan_hash,
            "package_inventory": self.package_inventory,
            "configuration_hashes": self.configuration_hashes,
            "run_terminal_summary": self.run_terminal_summary,
            "node_terminal_summaries": self.node_terminal_summaries,
            "excerpts": self.excerpts.iter().map(DiagnosticExcerpt::canonical_value).collect::<Vec<_>>(),
            "redaction_report": self.redaction_report,
        }))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DiagnosticBundleError {
    UnredactedContent { excerpt_id: String },
}

impl fmt::Display for DiagnosticBundleError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnredactedContent { excerpt_id } => {
                write!(
                    formatter,
                    "diagnostic excerpt {excerpt_id:?} contains content disallowed by bundle redaction"
                )
            }
        }
    }
}

impl Error for DiagnosticBundleError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryBuffer {
    policy: TelemetryQueuePolicy,
    records: VecDeque<TelemetryRecord>,
    dropped_count: u64,
}

impl TelemetryBuffer {
    pub fn new(policy: TelemetryQueuePolicy) -> Self {
        Self {
            policy,
            records: VecDeque::new(),
            dropped_count: 0,
        }
    }

    pub fn enqueue(
        &mut self,
        record: TelemetryRecord,
    ) -> Result<TelemetryEnqueueOutcome, TelemetryBufferError> {
        if record.kind.requires_durable_path() {
            return Err(TelemetryBufferError::RequiredDurablePath { kind: record.kind });
        }

        if self.records.len() < self.policy.max_items {
            self.records.push_back(record);
            return Ok(TelemetryEnqueueOutcome::accepted());
        }

        match self.policy.on_full {
            TelemetryOnFull::DropLowPriority => {
                let mut lowest_position = None;
                let mut lowest_priority = record.priority;
                for (position, queued) in self.records.iter().enumerate() {
                    if queued.priority < lowest_priority {
                        lowest_position = Some(position);
                        lowest_priority = queued.priority;
                    }
                }

                if let Some(position) = lowest_position {
                    let dropped = self
                        .records
                        .remove(position)
                        .expect("selected telemetry record position must exist");
                    let dropped_record_id = dropped.record_id;
                    self.dropped_count += 1;
                    self.records.push_back(record);
                    Ok(TelemetryEnqueueOutcome::accepted_with_drop([
                        dropped_record_id,
                    ]))
                } else {
                    Err(TelemetryBufferError::QueueFull {
                        record_id: record.record_id,
                    })
                }
            }
            TelemetryOnFull::DropNewest => {
                self.dropped_count += 1;
                Ok(TelemetryEnqueueOutcome {
                    accepted: false,
                    dropped_record_ids: vec![record.record_id],
                })
            }
            TelemetryOnFull::Reject => Err(TelemetryBufferError::QueueFull {
                record_id: record.record_id,
            }),
        }
    }

    pub fn records(&self) -> &VecDeque<TelemetryRecord> {
        &self.records
    }

    pub fn dropped_count(&self) -> u64 {
        self.dropped_count
    }
}

fn capture_mode_str(mode: CaptureMode) -> &'static str {
    match mode {
        CaptureMode::None => "none",
        CaptureMode::HashOnly => "hash_only",
        CaptureMode::ReferenceOnly => "reference_only",
        CaptureMode::RedactedPreview => "redacted_preview",
        CaptureMode::Full => "full",
    }
}

fn checked_duration(start: Option<u64>, end: Option<u64>) -> Option<u64> {
    end?.checked_sub(start?)
}

fn is_forbidden_metric_label(label: &str) -> bool {
    matches!(
        label,
        "run_id"
            | "trace_id"
            | "conversation_id"
            | "turn_id"
            | "operation_id"
            | "event_id"
            | "delivery_id"
            | "user_id"
            | "document_id"
            | "chunk_id"
            | "provider_response_id"
    )
}

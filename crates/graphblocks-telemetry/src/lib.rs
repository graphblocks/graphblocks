use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

pub use graphblocks_runtime_core::observability::*;
use serde_json::{Value, json};

pub const DEFAULT_BLOCKED_METRIC_LABELS: &[&str] = &[
    "attempt_id",
    "conversation_id",
    "record_id",
    "run_id",
    "span_id",
    "trace_id",
    "turn_id",
    "user_id",
];

pub const DEFAULT_SENSITIVE_TELEMETRY_ATTRIBUTE_KEYS: &[&str] = &[
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
];

pub const DEFAULT_CONTENT_TELEMETRY_ATTRIBUTE_KEYS: &[&str] = &[
    "completion",
    "input",
    "messages",
    "output",
    "prompt",
    "tool_result",
];

#[derive(Clone, Debug, PartialEq)]
pub struct GenerationTelemetryRecord {
    pub record_id: String,
    pub run_id: String,
    pub span_id: String,
    pub node_id: String,
    pub provider: String,
    pub model: String,
    pub release_id: Option<String>,
    pub input_digest: Option<String>,
    pub output_digest: Option<String>,
    pub usage: BTreeMap<String, u64>,
    pub timing_ms: BTreeMap<String, u64>,
    pub attributes: BTreeMap<String, Value>,
}

impl GenerationTelemetryRecord {
    pub fn new(
        record_id: impl Into<String>,
        run_id: impl Into<String>,
        span_id: impl Into<String>,
        node_id: impl Into<String>,
        provider: impl Into<String>,
        model: impl Into<String>,
    ) -> Self {
        Self {
            record_id: record_id.into(),
            run_id: run_id.into(),
            span_id: span_id.into(),
            node_id: node_id.into(),
            provider: provider.into(),
            model: model.into(),
            release_id: None,
            input_digest: None,
            output_digest: None,
            usage: BTreeMap::new(),
            timing_ms: BTreeMap::new(),
            attributes: BTreeMap::new(),
        }
    }

    pub fn from_generation_observation(
        record_id: impl Into<String>,
        run_id: impl Into<String>,
        observation: &GenerationObservation,
    ) -> Self {
        let mut timing_ms = BTreeMap::new();
        if let Some(duration) = observation.timing.queue_wait_ms() {
            timing_ms.insert("queue_wait".to_owned(), duration);
        }
        if let Some(duration) = observation.timing.flow_wait_ms() {
            timing_ms.insert("flow_wait".to_owned(), duration);
        }
        if let Some(duration) = observation.timing.time_to_first_output_ms() {
            timing_ms.insert("time_to_first_output".to_owned(), duration);
        }
        if let Some(duration) = observation.timing.execution_ms() {
            timing_ms.insert("execution".to_owned(), duration);
        }
        if let Some(duration) = observation.timing.streaming_ms() {
            timing_ms.insert("streaming".to_owned(), duration);
        }

        Self {
            record_id: record_id.into(),
            run_id: run_id.into(),
            span_id: observation.span_id.clone(),
            node_id: observation.node_id.clone(),
            provider: observation.provider.clone(),
            model: observation.model.clone(),
            release_id: None,
            input_digest: None,
            output_digest: None,
            usage: observation.usage.clone(),
            timing_ms,
            attributes: BTreeMap::new(),
        }
    }

    pub fn with_release_id(mut self, release_id: impl Into<String>) -> Self {
        self.release_id = Some(release_id.into());
        self
    }

    pub fn with_input_digest(mut self, input_digest: impl Into<String>) -> Self {
        self.input_digest = Some(input_digest.into());
        self
    }

    pub fn with_output_digest(mut self, output_digest: impl Into<String>) -> Self {
        self.output_digest = Some(output_digest.into());
        self
    }

    pub fn with_usage(mut self, unit: impl Into<String>, amount: u64) -> Self {
        self.usage.insert(unit.into(), amount);
        self
    }

    pub fn with_timing_ms(mut self, name: impl Into<String>, duration_ms: u64) -> Self {
        self.timing_ms.insert(name.into(), duration_ms);
        self
    }

    pub fn with_attribute(mut self, key: impl Into<String>, value: impl Into<Value>) -> Self {
        self.attributes.insert(key.into(), value.into());
        self
    }

    pub fn observation_contract(&self) -> Value {
        json!({
            "record_id": self.record_id,
            "run_id": self.run_id,
            "span_id": self.span_id,
            "node_id": self.node_id,
            "provider": self.provider,
            "model": self.model,
            "release_id": self.release_id,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "usage": self.usage,
            "timing_ms": self.timing_ms,
            "attributes": self.attributes,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryCapturePolicy {
    pub redacted_attribute_keys: BTreeSet<String>,
    pub dropped_attribute_keys: BTreeSet<String>,
    pub replacement: String,
    pub capture_input_digest: bool,
    pub capture_output_digest: bool,
}

impl Default for TelemetryCapturePolicy {
    fn default() -> Self {
        Self::new()
    }
}

impl TelemetryCapturePolicy {
    pub fn new() -> Self {
        Self {
            redacted_attribute_keys: BTreeSet::new(),
            dropped_attribute_keys: BTreeSet::new(),
            replacement: "[redacted]".to_owned(),
            capture_input_digest: true,
            capture_output_digest: true,
        }
    }

    pub fn with_redacted_attribute_key(mut self, key: impl Into<String>) -> Self {
        self.redacted_attribute_keys.insert(key.into());
        self
    }

    pub fn with_dropped_attribute_key(mut self, key: impl Into<String>) -> Self {
        self.dropped_attribute_keys.insert(key.into());
        self
    }

    pub fn with_replacement(mut self, replacement: impl Into<String>) -> Self {
        self.replacement = replacement.into();
        self
    }

    pub fn without_input_digest(mut self) -> Self {
        self.capture_input_digest = false;
        self
    }

    pub fn without_output_digest(mut self) -> Self {
        self.capture_output_digest = false;
        self
    }

    pub fn apply_generation(
        &self,
        record: &GenerationTelemetryRecord,
    ) -> GenerationTelemetryRecord {
        let attributes = record
            .attributes
            .iter()
            .filter_map(|(key, value)| {
                if self.dropped_attribute_keys.contains(key) {
                    None
                } else if self.redacted_attribute_keys.contains(key) {
                    Some((key.clone(), Value::String(self.replacement.clone())))
                } else {
                    Some((key.clone(), value.clone()))
                }
            })
            .collect::<BTreeMap<_, _>>();

        GenerationTelemetryRecord {
            input_digest: if self.capture_input_digest {
                record.input_digest.clone()
            } else {
                None
            },
            output_digest: if self.capture_output_digest {
                record.output_digest.clone()
            } else {
                None
            },
            attributes,
            ..record.clone()
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryCapturePolicyIssue {
    pub attribute_key: String,
    pub reason: String,
    pub required_action: String,
}

impl TelemetryCapturePolicyIssue {
    pub fn new(
        attribute_key: impl Into<String>,
        reason: impl Into<String>,
        required_action: impl Into<String>,
    ) -> Self {
        Self {
            attribute_key: attribute_key.into(),
            reason: reason.into(),
            required_action: required_action.into(),
        }
    }

    pub fn issue_contract(&self) -> Value {
        json!({
            "attribute_key": self.attribute_key,
            "reason": self.reason,
            "required_action": self.required_action,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryCapturePolicyLintResult {
    pub issues: Vec<TelemetryCapturePolicyIssue>,
}

impl TelemetryCapturePolicyLintResult {
    pub fn new(mut issues: Vec<TelemetryCapturePolicyIssue>) -> Self {
        issues.sort_by(|left, right| {
            (left.attribute_key.as_str(), left.reason.as_str())
                .cmp(&(right.attribute_key.as_str(), right.reason.as_str()))
        });
        Self { issues }
    }

    pub fn passed(&self) -> bool {
        self.issues.is_empty()
    }

    pub fn issue_contracts(&self) -> Vec<Value> {
        self.issues
            .iter()
            .map(TelemetryCapturePolicyIssue::issue_contract)
            .collect()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryCapturePolicyLinter {
    pub sensitive_attribute_keys: BTreeSet<String>,
    pub content_attribute_keys: BTreeSet<String>,
}

impl Default for TelemetryCapturePolicyLinter {
    fn default() -> Self {
        Self::new()
    }
}

impl TelemetryCapturePolicyLinter {
    pub fn new() -> Self {
        Self::from_keys(
            DEFAULT_SENSITIVE_TELEMETRY_ATTRIBUTE_KEYS.iter().copied(),
            DEFAULT_CONTENT_TELEMETRY_ATTRIBUTE_KEYS.iter().copied(),
        )
    }

    pub fn from_keys(
        sensitive_attribute_keys: impl IntoIterator<Item = impl Into<String>>,
        content_attribute_keys: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        Self {
            sensitive_attribute_keys: sensitive_attribute_keys
                .into_iter()
                .map(Into::into)
                .collect(),
            content_attribute_keys: content_attribute_keys.into_iter().map(Into::into).collect(),
        }
    }

    pub fn lint_policy(&self, policy: &TelemetryCapturePolicy) -> TelemetryCapturePolicyLintResult {
        let protected = policy
            .redacted_attribute_keys
            .union(&policy.dropped_attribute_keys)
            .cloned()
            .collect::<BTreeSet<_>>();
        let mut issues = Vec::new();

        for attribute_key in &self.sensitive_attribute_keys {
            if !protected.contains(attribute_key) {
                issues.push(TelemetryCapturePolicyIssue::new(
                    attribute_key,
                    "sensitive_attribute_not_protected",
                    "redact_or_drop",
                ));
            }
        }
        for attribute_key in &self.content_attribute_keys {
            if !protected.contains(attribute_key) {
                issues.push(TelemetryCapturePolicyIssue::new(
                    attribute_key,
                    "content_attribute_not_protected",
                    "redact_or_drop",
                ));
            }
        }
        if !policy.redacted_attribute_keys.is_empty() && policy.replacement.trim().is_empty() {
            for attribute_key in &policy.redacted_attribute_keys {
                issues.push(TelemetryCapturePolicyIssue::new(
                    attribute_key,
                    "redaction_replacement_empty",
                    "set_non_empty_replacement",
                ));
            }
        }

        TelemetryCapturePolicyLintResult::new(issues)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryExportResult {
    pub exporter: String,
    pub status: String,
    pub record_ids: Vec<String>,
    pub error_type: Option<String>,
    pub retryable: bool,
    pub run_impact: String,
}

impl TelemetryExportResult {
    pub fn new(
        exporter: impl Into<String>,
        status: impl Into<String>,
        record_ids: impl IntoIterator<Item = impl Into<String>>,
        error_type: Option<impl Into<String>>,
        retryable: bool,
        run_impact: impl Into<String>,
    ) -> Result<Self, TelemetryProjectionError> {
        let run_impact = run_impact.into();
        if run_impact != "none" {
            return Err(TelemetryProjectionError::ExportAffectsRunCorrectness);
        }
        Ok(Self {
            exporter: exporter.into(),
            status: status.into(),
            record_ids: record_ids.into_iter().map(Into::into).collect(),
            error_type: error_type.map(Into::into),
            retryable,
            run_impact,
        })
    }

    pub fn completed(
        exporter: impl Into<String>,
        record_ids: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        Self {
            exporter: exporter.into(),
            status: "completed".to_owned(),
            record_ids: record_ids.into_iter().map(Into::into).collect(),
            error_type: None,
            retryable: false,
            run_impact: "none".to_owned(),
        }
    }

    pub fn failed(
        exporter: impl Into<String>,
        record_ids: impl IntoIterator<Item = impl Into<String>>,
        error_type: impl Into<String>,
        retryable: bool,
    ) -> Self {
        Self {
            exporter: exporter.into(),
            status: "failed".to_owned(),
            record_ids: record_ids.into_iter().map(Into::into).collect(),
            error_type: Some(error_type.into()),
            retryable,
            run_impact: "none".to_owned(),
        }
    }

    pub fn result_contract(&self) -> Value {
        json!({
            "exporter": self.exporter,
            "status": self.status,
            "record_ids": self.record_ids,
            "error_type": self.error_type,
            "retryable": self.retryable,
            "run_impact": self.run_impact,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MetricSample {
    pub name: String,
    pub labels: BTreeMap<String, String>,
}

impl MetricSample {
    pub fn new(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            labels: BTreeMap::new(),
        }
    }

    pub fn with_label(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.labels.insert(key.into(), value.into());
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MetricCardinalityIssue {
    pub metric_name: String,
    pub label: String,
    pub distinct_values: usize,
    pub limit: usize,
    pub reason: String,
}

impl MetricCardinalityIssue {
    pub fn new(
        metric_name: impl Into<String>,
        label: impl Into<String>,
        distinct_values: usize,
        limit: usize,
        reason: impl Into<String>,
    ) -> Self {
        Self {
            metric_name: metric_name.into(),
            label: label.into(),
            distinct_values,
            limit,
            reason: reason.into(),
        }
    }

    pub fn issue_contract(&self) -> Value {
        json!({
            "metric_name": self.metric_name,
            "label": self.label,
            "distinct_values": self.distinct_values,
            "limit": self.limit,
            "reason": self.reason,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MetricCardinalityLintResult {
    pub issues: Vec<MetricCardinalityIssue>,
}

impl MetricCardinalityLintResult {
    pub fn new(mut issues: Vec<MetricCardinalityIssue>) -> Self {
        issues.sort_by(|left, right| {
            (
                left.metric_name.as_str(),
                left.label.as_str(),
                left.reason.as_str(),
            )
                .cmp(&(
                    right.metric_name.as_str(),
                    right.label.as_str(),
                    right.reason.as_str(),
                ))
        });
        Self { issues }
    }

    pub fn passed(&self) -> bool {
        self.issues.is_empty()
    }

    pub fn issue_contracts(&self) -> Vec<Value> {
        self.issues
            .iter()
            .map(MetricCardinalityIssue::issue_contract)
            .collect()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MetricCardinalityLinter {
    pub max_distinct_values_per_label: usize,
    pub blocked_labels: BTreeSet<String>,
}

impl Default for MetricCardinalityLinter {
    fn default() -> Self {
        Self::new()
    }
}

impl MetricCardinalityLinter {
    pub fn new() -> Self {
        Self {
            max_distinct_values_per_label: 32,
            blocked_labels: DEFAULT_BLOCKED_METRIC_LABELS
                .iter()
                .map(|label| (*label).to_owned())
                .collect(),
        }
    }

    pub fn with_max_distinct_values_per_label(mut self, limit: usize) -> Self {
        self.max_distinct_values_per_label = limit;
        self
    }

    pub fn with_blocked_label(mut self, label: impl Into<String>) -> Self {
        self.blocked_labels.insert(label.into());
        self
    }

    pub fn lint_samples<'a>(
        &self,
        samples: impl IntoIterator<Item = &'a MetricSample>,
    ) -> Result<MetricCardinalityLintResult, TelemetryProjectionError> {
        let mut label_values: BTreeMap<(String, String), BTreeSet<String>> = BTreeMap::new();
        let mut blocked_label_values: BTreeMap<(String, String), BTreeSet<String>> =
            BTreeMap::new();

        for sample in samples {
            if sample.name.trim().is_empty() {
                return Err(TelemetryProjectionError::InvalidMetricSampleName);
            }
            for (label, value) in &sample.labels {
                let key = (sample.name.clone(), label.clone());
                if self.blocked_labels.contains(label) {
                    blocked_label_values
                        .entry(key)
                        .or_default()
                        .insert(value.clone());
                } else {
                    label_values.entry(key).or_default().insert(value.clone());
                }
            }
        }

        let mut issues = Vec::new();
        for ((metric_name, label), values) in label_values {
            if values.len() > self.max_distinct_values_per_label {
                issues.push(MetricCardinalityIssue::new(
                    metric_name,
                    label,
                    values.len(),
                    self.max_distinct_values_per_label,
                    "too_many_values",
                ));
            }
        }
        for ((metric_name, label), values) in blocked_label_values {
            issues.push(MetricCardinalityIssue::new(
                metric_name,
                label,
                values.len(),
                0,
                "blocked_label",
            ));
        }

        Ok(MetricCardinalityLintResult::new(issues))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TelemetryProjectionError {
    ExportAffectsRunCorrectness,
    InvalidMetricSampleName,
}

impl fmt::Display for TelemetryProjectionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ExportAffectsRunCorrectness => {
                write!(
                    formatter,
                    "telemetry export result must not affect run correctness"
                )
            }
            Self::InvalidMetricSampleName => {
                write!(formatter, "metric sample name must be a non-empty string")
            }
        }
    }
}

impl Error for TelemetryProjectionError {}

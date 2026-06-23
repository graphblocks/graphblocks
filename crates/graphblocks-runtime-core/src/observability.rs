use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

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
            | "user_id"
            | "document_id"
            | "chunk_id"
            | "provider_response_id"
    )
}

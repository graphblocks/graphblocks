use crate::outcome::{BlockError, CancelCode, CancelReason, ErrorCategory};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TimeoutError {
    InvalidDuration,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TimeoutPolicy {
    duration_ms: u64,
}

impl TimeoutPolicy {
    pub fn new(duration_ms: u64) -> Result<Self, TimeoutError> {
        if duration_ms == 0 {
            return Err(TimeoutError::InvalidDuration);
        }
        Ok(Self { duration_ms })
    }

    pub fn duration_ms(self) -> u64 {
        self.duration_ms
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Deadline {
    node_id: String,
    started_at_ms: u64,
    deadline_ms: u64,
}

impl Deadline {
    pub fn new(
        node_id: impl Into<String>,
        started_at_ms: u64,
        policy: TimeoutPolicy,
    ) -> Result<Self, TimeoutError> {
        Ok(Self {
            node_id: node_id.into(),
            started_at_ms,
            deadline_ms: started_at_ms.saturating_add(policy.duration_ms()),
        })
    }

    pub fn node_id(&self) -> &str {
        &self.node_id
    }

    pub fn started_at_ms(&self) -> u64 {
        self.started_at_ms
    }

    pub fn deadline_ms(&self) -> u64 {
        self.deadline_ms
    }

    pub fn remaining_ms(&self, now_ms: u64) -> u64 {
        self.deadline_ms.saturating_sub(now_ms)
    }

    pub fn check(&self, now_ms: u64) -> TimeoutDecision {
        if now_ms >= self.deadline_ms {
            return TimeoutDecision::Expired {
                node_id: self.node_id.clone(),
                deadline_ms: self.deadline_ms,
                now_ms,
            };
        }
        TimeoutDecision::Pending {
            remaining_ms: self.remaining_ms(now_ms),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TimeoutDecision {
    Pending {
        remaining_ms: u64,
    },
    Expired {
        node_id: String,
        deadline_ms: u64,
        now_ms: u64,
    },
}

impl TimeoutDecision {
    pub fn cancel_reason(&self) -> CancelReason {
        match self {
            Self::Pending { .. } => CancelReason::new(CancelCode::Timeout),
            Self::Expired { node_id, .. } => {
                let mut reason = CancelReason::new(CancelCode::Timeout);
                reason.message = Some(format!("node {node_id} exceeded timeout deadline"));
                reason
            }
        }
    }

    pub fn block_error(&self) -> BlockError {
        match self {
            Self::Pending { .. } => BlockError::new(
                "runtime.timeout.pending",
                ErrorCategory::Timeout,
                "timeout has not expired",
                false,
            ),
            Self::Expired {
                node_id,
                deadline_ms,
                now_ms,
            } => {
                let mut error = BlockError::new(
                    "runtime.timeout",
                    ErrorCategory::Timeout,
                    format!("node {node_id} exceeded timeout deadline"),
                    false,
                );
                error.details.insert(
                    "deadline_ms".to_owned(),
                    serde_json::Value::from(*deadline_ms),
                );
                error
                    .details
                    .insert("now_ms".to_owned(), serde_json::Value::from(*now_ms));
                error
            }
        }
    }
}

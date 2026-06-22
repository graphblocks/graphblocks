use std::collections::BTreeMap;

use serde_json::Value;

#[derive(Clone, Debug, PartialEq)]
pub enum Outcome<T> {
    Value(T),
    Absent,
    Skipped(SkipReason),
    Denied(PolicyDecisionRef),
    BudgetExhausted(BudgetExhaustion),
    Paused(PauseReason),
    Failed(BlockError),
    Cancelled(CancelReason),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SkipReason {
    pub code: String,
    pub message: Option<String>,
}

impl SkipReason {
    pub fn new(code: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: None,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PolicyDecisionRef {
    pub decision_id: String,
}

impl PolicyDecisionRef {
    pub fn new(decision_id: impl Into<String>) -> Self {
        Self {
            decision_id: decision_id.into(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BudgetExhaustion {
    pub code: String,
    pub message: Option<String>,
}

impl BudgetExhaustion {
    pub fn new(code: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: None,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PauseReason {
    pub code: String,
    pub message: Option<String>,
}

impl PauseReason {
    pub fn new(code: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CancelCode {
    ClientDisconnect,
    UserCancel,
    Timeout,
    Superseded,
    PolicyDenied,
    BudgetExhausted,
    ProviderQuotaExhausted,
    DependencyFailed,
    Shutdown,
    BargeIn,
    RolloutDrain,
    LeaseLost,
    EntitlementRevoked,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CancelReason {
    pub code: CancelCode,
    pub message: Option<String>,
    pub requested_by: Option<String>,
    pub policy_decision_ref: Option<String>,
}

impl CancelReason {
    pub fn new(code: CancelCode) -> Self {
        Self {
            code,
            message: None,
            requested_by: None,
            policy_decision_ref: None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum ErrorCategory {
    Validation,
    Configuration,
    Authentication,
    Authorization,
    NotFound,
    RateLimit,
    Quota,
    Budget,
    Capacity,
    Timeout,
    Transient,
    Permanent,
    Provider,
    Policy,
    Cancelled,
    Conflict,
    Internal,
}

#[derive(Clone, Debug, PartialEq)]
pub struct BlockError {
    pub code: String,
    pub category: ErrorCategory,
    pub message: String,
    pub retryable: bool,
    pub details: BTreeMap<String, Value>,
    pub cause_chain: Vec<String>,
}

impl BlockError {
    pub fn new(
        code: impl Into<String>,
        category: ErrorCategory,
        message: impl Into<String>,
        retryable: bool,
    ) -> Self {
        Self {
            code: code.into(),
            category,
            message: message.into(),
            retryable,
            details: BTreeMap::new(),
            cause_chain: Vec::new(),
        }
    }
}

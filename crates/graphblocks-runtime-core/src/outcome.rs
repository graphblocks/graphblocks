use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OutcomeTextErrorKind {
    Empty,
    SurroundingWhitespace,
    ControlCharacter,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OutcomeTextRole {
    Identifier,
    HumanText,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OutcomeValidationError {
    pub field: String,
    pub kind: OutcomeTextErrorKind,
    pub role: OutcomeTextRole,
}

impl OutcomeValidationError {
    fn text(field: impl Into<String>, kind: OutcomeTextErrorKind, role: OutcomeTextRole) -> Self {
        Self {
            field: field.into(),
            kind,
            role,
        }
    }
}

impl fmt::Display for OutcomeValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let reason = match self.kind {
            OutcomeTextErrorKind::Empty => "must not be empty",
            OutcomeTextErrorKind::SurroundingWhitespace => {
                "must not contain surrounding whitespace"
            }
            OutcomeTextErrorKind::ControlCharacter => "must not contain control characters",
        };
        write!(formatter, "{} {reason}", self.field)
    }
}

impl Error for OutcomeValidationError {}

impl<T> Outcome<T> {
    pub fn validate(&self) -> Result<(), OutcomeValidationError> {
        match self {
            Self::Value(_) | Self::Absent => Ok(()),
            Self::Skipped(reason) => reason.validate(),
            Self::Denied(decision) => decision.validate(),
            Self::BudgetExhausted(exhaustion) => exhaustion.validate(),
            Self::Paused(reason) => reason.validate(),
            Self::Failed(error) => error.validate(),
            Self::Cancelled(reason) => reason.validate(),
        }
    }
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

    pub fn validate(&self) -> Result<(), OutcomeValidationError> {
        validate_identifier("skip reason code", &self.code)?;
        validate_optional_human_text("skip reason message", self.message.as_deref())
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

    pub fn validate(&self) -> Result<(), OutcomeValidationError> {
        validate_identifier("policy decision id", &self.decision_id)
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

    pub fn validate(&self) -> Result<(), OutcomeValidationError> {
        validate_identifier("budget exhaustion code", &self.code)?;
        validate_optional_human_text("budget exhaustion message", self.message.as_deref())
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

    pub fn validate(&self) -> Result<(), OutcomeValidationError> {
        validate_identifier("pause reason code", &self.code)?;
        validate_optional_human_text("pause reason message", self.message.as_deref())
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

    pub fn validate(&self) -> Result<(), OutcomeValidationError> {
        validate_optional_human_text("cancellation message", self.message.as_deref())?;
        validate_optional_identifier("cancellation requested by", self.requested_by.as_deref())?;
        validate_optional_identifier(
            "cancellation policy decision ref",
            self.policy_decision_ref.as_deref(),
        )
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

    pub fn validate(&self) -> Result<(), OutcomeValidationError> {
        validate_identifier("block error code", &self.code)?;
        validate_human_text("block error message", &self.message)?;
        for (index, cause) in self.cause_chain.iter().enumerate() {
            validate_human_text(format!("block error cause chain[{index}]"), cause)?;
        }
        Ok(())
    }
}

fn validate_identifier(
    field: impl Into<String>,
    value: &str,
) -> Result<(), OutcomeValidationError> {
    let field = field.into();
    if value.trim().is_empty() {
        return Err(OutcomeValidationError::text(
            field,
            OutcomeTextErrorKind::Empty,
            OutcomeTextRole::Identifier,
        ));
    }
    if value != value.trim() {
        return Err(OutcomeValidationError::text(
            field,
            OutcomeTextErrorKind::SurroundingWhitespace,
            OutcomeTextRole::Identifier,
        ));
    }
    if value
        .chars()
        .any(|character| character <= '\u{001f}' || character == '\u{007f}')
    {
        return Err(OutcomeValidationError::text(
            field,
            OutcomeTextErrorKind::ControlCharacter,
            OutcomeTextRole::Identifier,
        ));
    }
    Ok(())
}

fn validate_optional_identifier(
    field: impl Into<String>,
    value: Option<&str>,
) -> Result<(), OutcomeValidationError> {
    let Some(value) = value else {
        return Ok(());
    };
    validate_identifier(field, value)
}

fn validate_human_text(
    field: impl Into<String>,
    value: &str,
) -> Result<(), OutcomeValidationError> {
    let field = field.into();
    if value.trim().is_empty() {
        return Err(OutcomeValidationError::text(
            field,
            OutcomeTextErrorKind::Empty,
            OutcomeTextRole::HumanText,
        ));
    }
    if value
        .chars()
        .any(|character| character <= '\u{001f}' || character == '\u{007f}')
    {
        return Err(OutcomeValidationError::text(
            field,
            OutcomeTextErrorKind::ControlCharacter,
            OutcomeTextRole::HumanText,
        ));
    }
    Ok(())
}

fn validate_optional_human_text(
    field: impl Into<String>,
    value: Option<&str>,
) -> Result<(), OutcomeValidationError> {
    let Some(value) = value else {
        return Ok(());
    };
    validate_human_text(field, value)
}

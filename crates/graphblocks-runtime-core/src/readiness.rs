use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::error::Error;
use std::fmt;

use serde_json::Value;

use crate::outcome::{Outcome, OutcomeValidationError};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ReadinessValidationError {
    InvalidText {
        field: String,
        reason: &'static str,
    },
    InvalidOutcome {
        port: PortRef,
        error: OutcomeValidationError,
    },
    DuplicateSignal {
        port: PortRef,
    },
    DuplicateDependencyInput {
        input: String,
    },
}

impl fmt::Display for ReadinessValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidText { field, reason } => write!(formatter, "{field} {reason}"),
            Self::InvalidOutcome { port, error } => write!(
                formatter,
                "readiness signal {}.{} has invalid outcome: {error}",
                port.node, port.port
            ),
            Self::DuplicateSignal { port } => write!(
                formatter,
                "readiness signal {}.{} is duplicated",
                port.node, port.port
            ),
            Self::DuplicateDependencyInput { input } => {
                write!(
                    formatter,
                    "readiness dependency input {input:?} is duplicated"
                )
            }
        }
    }
}

impl Error for ReadinessValidationError {}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct PortRef {
    pub node: String,
    pub port: String,
}

impl PortRef {
    pub fn new(node: impl Into<String>, port: impl Into<String>) -> Self {
        Self {
            node: node.into(),
            port: port.into(),
        }
    }

    pub fn try_new(
        node: impl Into<String>,
        port: impl Into<String>,
    ) -> Result<Self, ReadinessValidationError> {
        let port_ref = Self::new(node, port);
        port_ref.validate()?;
        Ok(port_ref)
    }

    pub fn validate(&self) -> Result<(), ReadinessValidationError> {
        validate_readiness_text("port ref node", &self.node)?;
        validate_readiness_text("port ref port", &self.port)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InputMode {
    Value,
    Outcome,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct InputDependency {
    pub input: String,
    pub source: PortRef,
    pub source_path: Vec<String>,
    pub mode: InputMode,
}

impl InputDependency {
    pub fn value(input: impl Into<String>, source: PortRef) -> Self {
        Self {
            input: input.into(),
            source,
            source_path: Vec::new(),
            mode: InputMode::Value,
        }
    }

    pub fn try_value(
        input: impl Into<String>,
        source: PortRef,
    ) -> Result<Self, ReadinessValidationError> {
        let dependency = Self::value(input, source);
        dependency.validate()?;
        Ok(dependency)
    }

    pub fn outcome(input: impl Into<String>, source: PortRef) -> Self {
        Self {
            input: input.into(),
            source,
            source_path: Vec::new(),
            mode: InputMode::Outcome,
        }
    }

    pub fn try_outcome(
        input: impl Into<String>,
        source: PortRef,
    ) -> Result<Self, ReadinessValidationError> {
        let dependency = Self::outcome(input, source);
        dependency.validate()?;
        Ok(dependency)
    }

    pub fn with_source_path<I, S>(mut self, path: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.source_path = path.into_iter().map(Into::into).collect();
        self
    }

    pub fn try_with_source_path<I, S>(mut self, path: I) -> Result<Self, ReadinessValidationError>
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.source_path = path.into_iter().map(Into::into).collect();
        self.validate()?;
        Ok(self)
    }

    pub fn validate(&self) -> Result<(), ReadinessValidationError> {
        validate_readiness_text("input dependency input", &self.input)?;
        self.source.validate()?;
        for (index, segment) in self.source_path.iter().enumerate() {
            validate_readiness_text(format!("input dependency source path[{index}]"), segment)?;
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum ResolvedInput {
    Value(Value),
    Outcome(Outcome<Value>),
}

#[derive(Clone, Debug, PartialEq)]
pub enum Readiness {
    Ready(BTreeMap<String, ResolvedInput>),
    Waiting {
        missing: Vec<PortRef>,
    },
    Blocked {
        input: String,
        source: PortRef,
        outcome: Outcome<Value>,
    },
}

#[derive(Clone, Debug, Default)]
pub struct ReadinessTracker {
    signals: HashMap<PortRef, Outcome<Value>>,
}

impl ReadinessTracker {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn publish(&mut self, port: PortRef, outcome: Outcome<Value>) -> Option<Outcome<Value>> {
        self.signals.insert(port, outcome)
    }

    pub fn try_publish(
        &mut self,
        port: PortRef,
        outcome: Outcome<Value>,
    ) -> Result<(), ReadinessValidationError> {
        port.validate()?;
        outcome
            .validate()
            .map_err(|error| ReadinessValidationError::InvalidOutcome {
                port: port.clone(),
                error,
            })?;
        if self.signals.contains_key(&port) {
            return Err(ReadinessValidationError::DuplicateSignal { port });
        }
        self.signals.insert(port, outcome);
        Ok(())
    }

    pub fn signal(&self, port: &PortRef) -> Option<&Outcome<Value>> {
        self.signals.get(port)
    }

    pub fn readiness(&self, dependencies: impl IntoIterator<Item = InputDependency>) -> Readiness {
        let dependencies = dependencies.into_iter().collect::<Vec<_>>();
        self.readiness_validated(&dependencies)
    }

    pub fn try_readiness(
        &self,
        dependencies: impl IntoIterator<Item = InputDependency>,
    ) -> Result<Readiness, ReadinessValidationError> {
        let dependencies = dependencies.into_iter().collect::<Vec<_>>();
        let mut inputs = BTreeSet::new();
        for dependency in &dependencies {
            dependency.validate()?;
            if !inputs.insert(dependency.input.clone()) {
                return Err(ReadinessValidationError::DuplicateDependencyInput {
                    input: dependency.input.clone(),
                });
            }
        }
        Ok(self.readiness_validated(&dependencies))
    }

    fn readiness_validated(&self, dependencies: &[InputDependency]) -> Readiness {
        let mut missing = Vec::new();
        let mut resolved = BTreeMap::new();

        for dependency in dependencies {
            let Some(outcome) = self.signals.get(&dependency.source) else {
                missing.push(dependency.source.clone());
                continue;
            };

            match (dependency.mode, outcome) {
                (mode, Outcome::Value(value)) => {
                    let mut resolved_value = value;
                    for segment in &dependency.source_path {
                        let nested = match resolved_value {
                            Value::Object(object) => object.get(segment),
                            Value::Array(array)
                                if !segment.is_empty()
                                    && segment.bytes().all(|byte| byte.is_ascii_digit())
                                    && (segment.len() == 1 || !segment.starts_with('0')) =>
                            {
                                segment
                                    .parse::<usize>()
                                    .ok()
                                    .and_then(|index| array.get(index))
                            }
                            _ => None,
                        };
                        let Some(nested) = nested else {
                            return Readiness::Blocked {
                                input: dependency.input.clone(),
                                source: dependency.source.clone(),
                                outcome: Outcome::Failed(crate::outcome::BlockError::new(
                                    "runtime.missing_source_path",
                                    crate::outcome::ErrorCategory::Configuration,
                                    format!(
                                        "source {}.{} is missing nested path {}",
                                        dependency.source.node,
                                        dependency.source.port,
                                        dependency.source_path.join(".")
                                    ),
                                    false,
                                )),
                            };
                        };
                        resolved_value = nested;
                    }
                    let resolved_value = resolved_value.clone();
                    resolved.insert(
                        dependency.input.clone(),
                        match mode {
                            InputMode::Value => ResolvedInput::Value(resolved_value),
                            InputMode::Outcome => {
                                ResolvedInput::Outcome(Outcome::Value(resolved_value))
                            }
                        },
                    );
                }
                (InputMode::Value, outcome) => {
                    return Readiness::Blocked {
                        input: dependency.input.clone(),
                        source: dependency.source.clone(),
                        outcome: outcome.clone(),
                    };
                }
                (InputMode::Outcome, outcome) => {
                    resolved.insert(
                        dependency.input.clone(),
                        ResolvedInput::Outcome(outcome.clone()),
                    );
                }
            }
        }

        if missing.is_empty() {
            Readiness::Ready(resolved)
        } else {
            Readiness::Waiting { missing }
        }
    }
}

fn validate_readiness_text(
    field: impl Into<String>,
    value: &str,
) -> Result<(), ReadinessValidationError> {
    let field = field.into();
    if value.trim().is_empty() {
        return Err(ReadinessValidationError::InvalidText {
            field,
            reason: "must not be empty",
        });
    }
    if value != value.trim() {
        return Err(ReadinessValidationError::InvalidText {
            field,
            reason: "must not contain surrounding whitespace",
        });
    }
    if value
        .chars()
        .any(|character| character <= '\u{001f}' || character == '\u{007f}')
    {
        return Err(ReadinessValidationError::InvalidText {
            field,
            reason: "must not contain control characters",
        });
    }
    Ok(())
}

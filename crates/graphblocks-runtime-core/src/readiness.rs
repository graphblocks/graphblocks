use std::collections::{BTreeMap, HashMap};

use serde_json::Value;

use crate::outcome::Outcome;

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

    pub fn outcome(input: impl Into<String>, source: PortRef) -> Self {
        Self {
            input: input.into(),
            source,
            source_path: Vec::new(),
            mode: InputMode::Outcome,
        }
    }

    pub fn with_source_path<I, S>(mut self, path: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.source_path = path.into_iter().map(Into::into).collect();
        self
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

    pub fn signal(&self, port: &PortRef) -> Option<&Outcome<Value>> {
        self.signals.get(port)
    }

    pub fn readiness(&self, dependencies: impl IntoIterator<Item = InputDependency>) -> Readiness {
        let dependencies = dependencies.into_iter().collect::<Vec<_>>();
        let mut missing = Vec::new();
        let mut resolved = BTreeMap::new();

        for dependency in &dependencies {
            let Some(outcome) = self.signals.get(&dependency.source) else {
                missing.push(dependency.source.clone());
                continue;
            };

            match (dependency.mode, outcome) {
                (InputMode::Value, Outcome::Value(value)) => {
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
                    resolved.insert(
                        dependency.input.clone(),
                        ResolvedInput::Value(resolved_value.clone()),
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

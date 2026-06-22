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
    pub mode: InputMode,
}

impl InputDependency {
    pub fn value(input: impl Into<String>, source: PortRef) -> Self {
        Self {
            input: input.into(),
            source,
            mode: InputMode::Value,
        }
    }

    pub fn outcome(input: impl Into<String>, source: PortRef) -> Self {
        Self {
            input: input.into(),
            source,
            mode: InputMode::Outcome,
        }
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
                    resolved.insert(
                        dependency.input.clone(),
                        ResolvedInput::Value(value.clone()),
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

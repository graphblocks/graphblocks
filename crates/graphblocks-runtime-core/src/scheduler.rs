use std::collections::BTreeMap;

use serde_json::Value;

use crate::outcome::{CancelReason, Outcome};
use crate::readiness::{InputDependency, PortRef, Readiness, ReadinessTracker, ResolvedInput};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NodeExecutionState {
    Pending,
    Ready,
    Running,
    Completed,
    Blocked,
    Cancelled,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScheduledNode {
    pub node_id: String,
    pub dependencies: Vec<InputDependency>,
    pub condition: Option<ScheduledCondition>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScheduledCondition {
    pub input: String,
    pub source: PortRef,
    pub path: Vec<String>,
}

impl ScheduledCondition {
    pub fn new<I, S>(input: impl Into<String>, source: PortRef, path: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self {
            input: input.into(),
            source,
            path: path.into_iter().map(Into::into).collect(),
        }
    }
}

impl ScheduledNode {
    pub fn new<I>(node_id: impl Into<String>, dependencies: I) -> Self
    where
        I: IntoIterator<Item = InputDependency>,
    {
        Self {
            node_id: node_id.into(),
            dependencies: dependencies.into_iter().collect(),
            condition: None,
        }
    }

    pub fn with_condition(mut self, condition: ScheduledCondition) -> Self {
        self.condition = Some(condition);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct StartedNode {
    pub node_id: String,
    pub inputs: BTreeMap<String, ResolvedInput>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SchedulerError {
    DuplicateNode {
        node_id: String,
    },
    UnknownNode {
        node_id: String,
    },
    RunNotAdmitted,
    NodeNotReady {
        node_id: String,
        state: NodeExecutionState,
    },
    NodeNotRunning {
        node_id: String,
        state: NodeExecutionState,
    },
    OutputOwnerMismatch {
        node_id: String,
        output_node_id: String,
    },
}

#[derive(Clone, Debug)]
pub struct LocalScheduler {
    admitted: bool,
    nodes: BTreeMap<String, ScheduledNode>,
    states: BTreeMap<String, NodeExecutionState>,
    ready_inputs: BTreeMap<String, BTreeMap<String, ResolvedInput>>,
    readiness: ReadinessTracker,
}

impl LocalScheduler {
    pub fn new<I>(nodes: I) -> Result<Self, SchedulerError>
    where
        I: IntoIterator<Item = ScheduledNode>,
    {
        let mut scheduled_nodes = BTreeMap::new();
        let mut states = BTreeMap::new();
        for node in nodes {
            if scheduled_nodes
                .insert(node.node_id.clone(), node.clone())
                .is_some()
            {
                return Err(SchedulerError::DuplicateNode {
                    node_id: node.node_id,
                });
            }
            states.insert(node.node_id, NodeExecutionState::Pending);
        }

        Ok(Self {
            admitted: false,
            nodes: scheduled_nodes,
            states,
            ready_inputs: BTreeMap::new(),
            readiness: ReadinessTracker::new(),
        })
    }

    pub fn admit_run(&mut self) -> Result<Vec<String>, SchedulerError> {
        self.admitted = true;
        Ok(self.evaluate_readiness())
    }

    pub fn ready_nodes(&self) -> Vec<String> {
        self.states
            .iter()
            .filter_map(|(node_id, state)| {
                if *state == NodeExecutionState::Ready {
                    Some(node_id.clone())
                } else {
                    None
                }
            })
            .collect()
    }

    pub fn publish_signal(&mut self, port: PortRef, outcome: Outcome<Value>) -> Vec<String> {
        self.readiness.publish(port, outcome);
        if self.admitted {
            self.evaluate_readiness()
        } else {
            Vec::new()
        }
    }

    pub fn node_state(&self, node_id: impl AsRef<str>) -> Option<NodeExecutionState> {
        self.states.get(node_id.as_ref()).copied()
    }

    pub fn node_states(&self) -> Vec<(String, NodeExecutionState)> {
        self.states
            .iter()
            .map(|(node_id, state)| (node_id.clone(), *state))
            .collect()
    }

    pub fn start_node(&mut self, node_id: impl AsRef<str>) -> Result<StartedNode, SchedulerError> {
        let node_id = node_id.as_ref();
        let Some(state) = self.states.get(node_id).copied() else {
            return Err(SchedulerError::UnknownNode {
                node_id: node_id.to_owned(),
            });
        };
        if !self.admitted {
            return Err(SchedulerError::RunNotAdmitted);
        }
        if state != NodeExecutionState::Ready {
            return Err(SchedulerError::NodeNotReady {
                node_id: node_id.to_owned(),
                state,
            });
        }
        self.states
            .insert(node_id.to_owned(), NodeExecutionState::Running);
        Ok(StartedNode {
            node_id: node_id.to_owned(),
            inputs: self.ready_inputs.remove(node_id).unwrap_or_default(),
        })
    }

    pub fn complete_node<I>(
        &mut self,
        node_id: impl AsRef<str>,
        outputs: I,
    ) -> Result<Vec<String>, SchedulerError>
    where
        I: IntoIterator<Item = (PortRef, Outcome<Value>)>,
    {
        let node_id = node_id.as_ref();
        let Some(state) = self.states.get(node_id).copied() else {
            return Err(SchedulerError::UnknownNode {
                node_id: node_id.to_owned(),
            });
        };
        if state != NodeExecutionState::Running {
            return Err(SchedulerError::NodeNotRunning {
                node_id: node_id.to_owned(),
                state,
            });
        }

        let outputs = outputs.into_iter().collect::<Vec<_>>();
        if let Some((port, _)) = outputs.iter().find(|(port, _)| port.node != node_id) {
            return Err(SchedulerError::OutputOwnerMismatch {
                node_id: node_id.to_owned(),
                output_node_id: port.node.clone(),
            });
        }
        for (port, outcome) in outputs {
            self.readiness.publish(port, outcome);
        }
        self.states
            .insert(node_id.to_owned(), NodeExecutionState::Completed);
        Ok(self.evaluate_readiness())
    }

    pub fn cancel_node<I>(
        &mut self,
        node_id: impl AsRef<str>,
        output_ports: I,
        reason: CancelReason,
    ) -> Result<Vec<String>, SchedulerError>
    where
        I: IntoIterator<Item = PortRef>,
    {
        let node_id = node_id.as_ref();
        let Some(state) = self.states.get(node_id).copied() else {
            return Err(SchedulerError::UnknownNode {
                node_id: node_id.to_owned(),
            });
        };
        if matches!(
            state,
            NodeExecutionState::Completed
                | NodeExecutionState::Blocked
                | NodeExecutionState::Cancelled
        ) {
            return Ok(Vec::new());
        }
        if !self.admitted {
            return Err(SchedulerError::RunNotAdmitted);
        }

        let output_ports = output_ports.into_iter().collect::<Vec<_>>();
        if let Some(port) = output_ports.iter().find(|port| port.node != node_id) {
            return Err(SchedulerError::OutputOwnerMismatch {
                node_id: node_id.to_owned(),
                output_node_id: port.node.clone(),
            });
        }
        for port in output_ports {
            self.readiness
                .publish(port, Outcome::Cancelled(reason.clone()));
        }
        self.ready_inputs.remove(node_id);
        self.states
            .insert(node_id.to_owned(), NodeExecutionState::Cancelled);
        Ok(self.evaluate_readiness())
    }

    fn evaluate_readiness(&mut self) -> Vec<String> {
        let mut newly_ready = Vec::new();
        for (node_id, node) in &self.nodes {
            if self.states.get(node_id) != Some(&NodeExecutionState::Pending) {
                continue;
            }

            let readiness = if let Some(condition) = &node.condition {
                match self.readiness.signal(&condition.source) {
                    None => Readiness::Waiting {
                        missing: vec![condition.source.clone()],
                    },
                    Some(Outcome::Value(condition_value)) => {
                        let mut resolved_condition = Some(condition_value);
                        for part in &condition.path {
                            resolved_condition =
                                resolved_condition.and_then(|value| value.get(part));
                        }
                        let condition_input = ResolvedInput::Value(condition_value.clone());
                        if resolved_condition.and_then(Value::as_bool) == Some(true) {
                            match self.readiness.readiness(node.dependencies.clone()) {
                                Readiness::Ready(mut resolved) => {
                                    resolved.insert(condition.input.clone(), condition_input);
                                    Readiness::Ready(resolved)
                                }
                                other => other,
                            }
                        } else {
                            Readiness::Ready(BTreeMap::from([(
                                condition.input.clone(),
                                condition_input,
                            )]))
                        }
                    }
                    Some(Outcome::Skipped(reason)) => Readiness::Ready(BTreeMap::from([(
                        condition.input.clone(),
                        ResolvedInput::Outcome(Outcome::Skipped(reason.clone())),
                    )])),
                    Some(outcome) => Readiness::Blocked {
                        input: condition.input.clone(),
                        source: condition.source.clone(),
                        outcome: outcome.clone(),
                    },
                }
            } else {
                self.readiness.readiness(node.dependencies.clone())
            };

            match readiness {
                Readiness::Ready(resolved) => {
                    self.ready_inputs.insert(node_id.clone(), resolved);
                    self.states
                        .insert(node_id.clone(), NodeExecutionState::Ready);
                    newly_ready.push(node_id.clone());
                }
                Readiness::Waiting { .. } => {}
                Readiness::Blocked { .. } => {
                    self.states
                        .insert(node_id.clone(), NodeExecutionState::Blocked);
                }
            }
        }
        newly_ready
    }
}

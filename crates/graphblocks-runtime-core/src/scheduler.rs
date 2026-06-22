use std::collections::BTreeMap;

use serde_json::Value;

use crate::outcome::Outcome;
use crate::readiness::{InputDependency, PortRef, Readiness, ReadinessTracker};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NodeExecutionState {
    Pending,
    Ready,
    Running,
    Completed,
    Blocked,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScheduledNode {
    pub node_id: String,
    pub dependencies: Vec<InputDependency>,
}

impl ScheduledNode {
    pub fn new<I>(node_id: impl Into<String>, dependencies: I) -> Self
    where
        I: IntoIterator<Item = InputDependency>,
    {
        Self {
            node_id: node_id.into(),
            dependencies: dependencies.into_iter().collect(),
        }
    }
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
}

#[derive(Clone, Debug)]
pub struct LocalScheduler {
    admitted: bool,
    nodes: BTreeMap<String, ScheduledNode>,
    states: BTreeMap<String, NodeExecutionState>,
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

    pub fn node_state(&self, node_id: impl AsRef<str>) -> Option<NodeExecutionState> {
        self.states.get(node_id.as_ref()).copied()
    }

    pub fn start_node(&mut self, node_id: impl AsRef<str>) -> Result<(), SchedulerError> {
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
        Ok(())
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

        for (port, outcome) in outputs {
            self.readiness.publish(port, outcome);
        }
        self.states
            .insert(node_id.to_owned(), NodeExecutionState::Completed);
        Ok(self.evaluate_readiness())
    }

    fn evaluate_readiness(&mut self) -> Vec<String> {
        let mut newly_ready = Vec::new();
        for (node_id, node) in &self.nodes {
            if self.states.get(node_id) != Some(&NodeExecutionState::Pending) {
                continue;
            }

            match self.readiness.readiness(node.dependencies.clone()) {
                Readiness::Ready(_) => {
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

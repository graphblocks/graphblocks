use std::collections::BTreeMap;

use serde_json::{Map, Number, Value};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RunStatus {
    Created,
    Validating,
    AdmissionPending,
    Queued,
    Running,
    Paused,
    Interrupted,
    Completed,
    Failed,
    Cancelled,
    PolicyStopped,
}

impl RunStatus {
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Completed | Self::Failed | Self::Cancelled | Self::PolicyStopped
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RunRecord {
    pub run_id: String,
    pub sequence: u64,
    pub graph_hash: String,
    pub inputs: Value,
    pub status: RunStatus,
    pub state: Value,
    pub state_revision: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub enum RunStoreError {
    NotFound {
        run_id: String,
    },
    StateConflict {
        run_id: String,
        expected_revision: u64,
        current_revision: u64,
    },
    StatePatchAfterTerminal {
        run_id: String,
        status: RunStatus,
    },
    StatusAfterTerminal {
        run_id: String,
        status: RunStatus,
    },
    InvalidStatePath {
        path: Vec<String>,
    },
    StatePathConflict {
        path: Vec<String>,
        expected: &'static str,
    },
    NumericOverflow {
        path: Vec<String>,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub struct StatePatch {
    pub expected_revision: Option<u64>,
    pub operations: Vec<PatchOperation>,
}

impl StatePatch {
    pub fn new(expected_revision: Option<u64>) -> Self {
        Self {
            expected_revision,
            operations: Vec::new(),
        }
    }

    pub fn with(mut self, operation: PatchOperation) -> Self {
        self.operations.push(operation);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum PatchOperation {
    Set { path: Vec<String>, value: Value },
    Merge { path: Vec<String>, value: Value },
    Remove { path: Vec<String> },
    Increment { path: Vec<String>, amount: i64 },
    Append { path: Vec<String>, value: Value },
}

impl PatchOperation {
    pub fn set<I, S>(path: I, value: Value) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Set {
            path: path.into_iter().map(Into::into).collect(),
            value,
        }
    }

    pub fn merge<I, S>(path: I, value: Value) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Merge {
            path: path.into_iter().map(Into::into).collect(),
            value,
        }
    }

    pub fn remove<I, S>(path: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Remove {
            path: path.into_iter().map(Into::into).collect(),
        }
    }

    pub fn increment<I, S>(path: I, amount: i64) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Increment {
            path: path.into_iter().map(Into::into).collect(),
            amount,
        }
    }

    pub fn append<I, S>(path: I, value: Value) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Append {
            path: path.into_iter().map(Into::into).collect(),
            value,
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct InMemoryRunStore {
    runs: BTreeMap<String, RunRecord>,
    next_sequence: u64,
}

impl InMemoryRunStore {
    pub fn new() -> Self {
        Self {
            runs: BTreeMap::new(),
            next_sequence: 1,
        }
    }

    pub fn create_run(&mut self, graph_hash: impl Into<String>, inputs: Value) -> RunRecord {
        let sequence = self.next_sequence;
        self.next_sequence += 1;
        let run_id = format!("run-{sequence:06}");
        let record = RunRecord {
            run_id: run_id.clone(),
            sequence,
            graph_hash: graph_hash.into(),
            inputs,
            status: RunStatus::Created,
            state: Value::Object(Map::new()),
            state_revision: 0,
        };
        self.runs.insert(run_id, record.clone());
        record
    }

    pub fn get_run(&self, run_id: impl AsRef<str>) -> Result<RunRecord, RunStoreError> {
        let run_id = run_id.as_ref();
        let Some(record) = self.runs.get(run_id) else {
            return Err(RunStoreError::NotFound {
                run_id: run_id.to_owned(),
            });
        };
        Ok(record.clone())
    }

    pub fn set_status(
        &mut self,
        run_id: impl AsRef<str>,
        status: RunStatus,
    ) -> Result<RunRecord, RunStoreError> {
        let run_id = run_id.as_ref();
        let Some(current) = self.runs.get(run_id) else {
            return Err(RunStoreError::NotFound {
                run_id: run_id.to_owned(),
            });
        };
        if current.status.is_terminal() {
            return Err(RunStoreError::StatusAfterTerminal {
                run_id: run_id.to_owned(),
                status: current.status,
            });
        }

        let mut updated = current.clone();
        updated.status = status;
        self.runs.insert(run_id.to_owned(), updated.clone());
        Ok(updated)
    }

    pub fn patch_state(
        &mut self,
        run_id: impl AsRef<str>,
        patch: StatePatch,
    ) -> Result<RunRecord, RunStoreError> {
        let run_id = run_id.as_ref();
        let Some(current) = self.runs.get(run_id) else {
            return Err(RunStoreError::NotFound {
                run_id: run_id.to_owned(),
            });
        };
        if current.status.is_terminal() {
            return Err(RunStoreError::StatePatchAfterTerminal {
                run_id: run_id.to_owned(),
                status: current.status,
            });
        }
        if let Some(expected_revision) = patch.expected_revision
            && current.state_revision != expected_revision
        {
            return Err(RunStoreError::StateConflict {
                run_id: run_id.to_owned(),
                expected_revision,
                current_revision: current.state_revision,
            });
        }

        let mut next_state = current.state.clone();
        'operations: for operation in &patch.operations {
            match operation {
                PatchOperation::Set { path, value } => {
                    if path.is_empty() {
                        return Err(RunStoreError::InvalidStatePath { path: path.clone() });
                    }
                    let mut cursor = &mut next_state;
                    for segment in &path[..path.len() - 1] {
                        let Value::Object(object) = cursor else {
                            return Err(RunStoreError::StatePathConflict {
                                path: path.clone(),
                                expected: "object parent",
                            });
                        };
                        cursor = object
                            .entry(segment.clone())
                            .or_insert_with(|| Value::Object(Map::new()));
                    }
                    let Value::Object(object) = cursor else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "object parent",
                        });
                    };
                    object.insert(path[path.len() - 1].clone(), value.clone());
                }
                PatchOperation::Merge { path, value } => {
                    if path.is_empty() {
                        return Err(RunStoreError::InvalidStatePath { path: path.clone() });
                    }
                    let Value::Object(source) = value else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "object value",
                        });
                    };
                    let mut cursor = &mut next_state;
                    for segment in &path[..path.len() - 1] {
                        let Value::Object(object) = cursor else {
                            return Err(RunStoreError::StatePathConflict {
                                path: path.clone(),
                                expected: "object parent",
                            });
                        };
                        cursor = object
                            .entry(segment.clone())
                            .or_insert_with(|| Value::Object(Map::new()));
                    }
                    let Value::Object(object) = cursor else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "object parent",
                        });
                    };
                    let target = object
                        .entry(path[path.len() - 1].clone())
                        .or_insert_with(|| Value::Object(Map::new()));
                    let Value::Object(target_object) = target else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "object target",
                        });
                    };
                    for (key, next_value) in source {
                        target_object.insert(key.clone(), next_value.clone());
                    }
                }
                PatchOperation::Remove { path } => {
                    if path.is_empty() {
                        return Err(RunStoreError::InvalidStatePath { path: path.clone() });
                    }
                    let mut cursor = &mut next_state;
                    for segment in &path[..path.len() - 1] {
                        let Value::Object(object) = cursor else {
                            return Err(RunStoreError::StatePathConflict {
                                path: path.clone(),
                                expected: "object parent",
                            });
                        };
                        let Some(next_cursor) = object.get_mut(segment) else {
                            continue 'operations;
                        };
                        cursor = next_cursor;
                    }
                    let Value::Object(object) = cursor else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "object parent",
                        });
                    };
                    object.remove(&path[path.len() - 1]);
                }
                PatchOperation::Increment { path, amount } => {
                    if path.is_empty() {
                        return Err(RunStoreError::InvalidStatePath { path: path.clone() });
                    }
                    let mut cursor = &mut next_state;
                    for segment in &path[..path.len() - 1] {
                        let Value::Object(object) = cursor else {
                            return Err(RunStoreError::StatePathConflict {
                                path: path.clone(),
                                expected: "object parent",
                            });
                        };
                        cursor = object
                            .entry(segment.clone())
                            .or_insert_with(|| Value::Object(Map::new()));
                    }
                    let Value::Object(object) = cursor else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "object parent",
                        });
                    };
                    let target = object
                        .entry(path[path.len() - 1].clone())
                        .or_insert_with(|| Value::Number(Number::from(0)));
                    let Some(current_number) = target.as_i64() else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "integer target",
                        });
                    };
                    let Some(next_number) = current_number.checked_add(*amount) else {
                        return Err(RunStoreError::NumericOverflow { path: path.clone() });
                    };
                    *target = Value::Number(Number::from(next_number));
                }
                PatchOperation::Append { path, value } => {
                    if path.is_empty() {
                        return Err(RunStoreError::InvalidStatePath { path: path.clone() });
                    }
                    let mut cursor = &mut next_state;
                    for segment in &path[..path.len() - 1] {
                        let Value::Object(object) = cursor else {
                            return Err(RunStoreError::StatePathConflict {
                                path: path.clone(),
                                expected: "object parent",
                            });
                        };
                        cursor = object
                            .entry(segment.clone())
                            .or_insert_with(|| Value::Object(Map::new()));
                    }
                    let Value::Object(object) = cursor else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "object parent",
                        });
                    };
                    let target = object
                        .entry(path[path.len() - 1].clone())
                        .or_insert_with(|| Value::Array(Vec::new()));
                    let Value::Array(target_array) = target else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "array target",
                        });
                    };
                    target_array.push(value.clone());
                }
            }
        }

        let mut updated = current.clone();
        updated.state = next_state;
        updated.state_revision += 1;
        self.runs.insert(run_id.to_owned(), updated.clone());
        Ok(updated)
    }
}

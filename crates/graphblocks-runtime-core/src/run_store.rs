use std::{collections::BTreeMap, path::Path};

use rusqlite::{Connection, OptionalExtension, params};
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

    fn as_str(self) -> &'static str {
        match self {
            Self::Created => "created",
            Self::Validating => "validating",
            Self::AdmissionPending => "admission_pending",
            Self::Queued => "queued",
            Self::Running => "running",
            Self::Paused => "paused",
            Self::Interrupted => "interrupted",
            Self::Completed => "completed",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::PolicyStopped => "policy_stopped",
        }
    }

    fn from_str(status: &str) -> Option<Self> {
        match status {
            "created" => Some(Self::Created),
            "validating" => Some(Self::Validating),
            "admission_pending" => Some(Self::AdmissionPending),
            "queued" => Some(Self::Queued),
            "running" => Some(Self::Running),
            "paused" => Some(Self::Paused),
            "interrupted" => Some(Self::Interrupted),
            "completed" => Some(Self::Completed),
            "failed" => Some(Self::Failed),
            "cancelled" => Some(Self::Cancelled),
            "policy_stopped" => Some(Self::PolicyStopped),
            _ => None,
        }
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
    Storage {
        message: String,
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

fn record_with_status(current: &RunRecord, status: RunStatus) -> Result<RunRecord, RunStoreError> {
    if current.status.is_terminal() {
        return Err(RunStoreError::StatusAfterTerminal {
            run_id: current.run_id.clone(),
            status: current.status,
        });
    }

    let mut updated = current.clone();
    updated.status = status;
    Ok(updated)
}

fn record_with_state_patch(
    current: &RunRecord,
    patch: &StatePatch,
) -> Result<RunRecord, RunStoreError> {
    if current.status.is_terminal() {
        return Err(RunStoreError::StatePatchAfterTerminal {
            run_id: current.run_id.clone(),
            status: current.status,
        });
    }
    if let Some(expected_revision) = patch.expected_revision
        && current.state_revision != expected_revision
    {
        return Err(RunStoreError::StateConflict {
            run_id: current.run_id.clone(),
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
    Ok(updated)
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

        let updated = record_with_status(current, status)?;
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

        let updated = record_with_state_patch(current, &patch)?;
        self.runs.insert(run_id.to_owned(), updated.clone());
        Ok(updated)
    }
}

pub struct SqliteRunStore {
    connection: Connection,
}

impl SqliteRunStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, RunStoreError> {
        let connection = Connection::open(path).map_err(storage_error)?;
        let store = Self { connection };
        store.initialize()?;
        Ok(store)
    }

    pub fn open_in_memory() -> Result<Self, RunStoreError> {
        let connection = Connection::open_in_memory().map_err(storage_error)?;
        let store = Self { connection };
        store.initialize()?;
        Ok(store)
    }

    fn initialize(&self) -> Result<(), RunStoreError> {
        self.connection
            .execute_batch(
                "
                CREATE TABLE IF NOT EXISTS runs (
                    sequence INTEGER PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE,
                    graph_hash TEXT NOT NULL,
                    inputs_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    state_revision INTEGER NOT NULL
                );
                ",
            )
            .map_err(storage_error)?;
        Ok(())
    }

    pub fn create_run(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
    ) -> Result<RunRecord, RunStoreError> {
        let transaction = self.connection.transaction().map_err(storage_error)?;
        let next_sequence = transaction
            .query_row(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM runs",
                [],
                |row| row.get::<_, i64>(0),
            )
            .map_err(storage_error)?;
        let sequence = sqlite_i64_to_u64(next_sequence, "run sequence")?;
        let run_id = format!("run-{sequence:06}");
        let record = RunRecord {
            run_id,
            sequence,
            graph_hash: graph_hash.into(),
            inputs,
            status: RunStatus::Created,
            state: Value::Object(Map::new()),
            state_revision: 0,
        };
        transaction
            .execute(
                "
                INSERT INTO runs (
                    sequence,
                    run_id,
                    graph_hash,
                    inputs_json,
                    status,
                    state_json,
                    state_revision
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ",
                params![
                    sqlite_u64_to_i64(record.sequence, "run sequence")?,
                    &record.run_id,
                    &record.graph_hash,
                    storage_json(&record.inputs)?,
                    record.status.as_str(),
                    storage_json(&record.state)?,
                    sqlite_u64_to_i64(record.state_revision, "state revision")?,
                ],
            )
            .map_err(storage_error)?;
        transaction.commit().map_err(storage_error)?;
        Ok(record)
    }

    pub fn get_run(&self, run_id: impl AsRef<str>) -> Result<RunRecord, RunStoreError> {
        let run_id = run_id.as_ref();
        let row = self
            .connection
            .query_row(
                "
                SELECT sequence, run_id, graph_hash, inputs_json, status, state_json, state_revision
                FROM runs
                WHERE run_id = ?
                ",
                params![run_id],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, String>(5)?,
                        row.get::<_, i64>(6)?,
                    ))
                },
            )
            .optional()
            .map_err(storage_error)?;
        let Some((sequence, run_id, graph_hash, inputs, status, state, state_revision)) = row
        else {
            return Err(RunStoreError::NotFound {
                run_id: run_id.to_owned(),
            });
        };
        record_from_storage(
            sequence,
            run_id,
            graph_hash,
            inputs,
            status,
            state,
            state_revision,
        )
    }

    pub fn set_status(
        &mut self,
        run_id: impl AsRef<str>,
        status: RunStatus,
    ) -> Result<RunRecord, RunStoreError> {
        let current = self.get_run(run_id.as_ref())?;
        let updated = record_with_status(&current, status)?;
        self.connection
            .execute(
                "UPDATE runs SET status = ? WHERE run_id = ?",
                params![updated.status.as_str(), &updated.run_id],
            )
            .map_err(storage_error)?;
        Ok(updated)
    }

    pub fn patch_state(
        &mut self,
        run_id: impl AsRef<str>,
        patch: StatePatch,
    ) -> Result<RunRecord, RunStoreError> {
        let current = self.get_run(run_id.as_ref())?;
        let updated = record_with_state_patch(&current, &patch)?;
        self.connection
            .execute(
                "
                UPDATE runs
                SET state_json = ?, state_revision = ?
                WHERE run_id = ?
                ",
                params![
                    storage_json(&updated.state)?,
                    sqlite_u64_to_i64(updated.state_revision, "state revision")?,
                    &updated.run_id,
                ],
            )
            .map_err(storage_error)?;
        Ok(updated)
    }
}

fn record_from_storage(
    sequence: i64,
    run_id: String,
    graph_hash: String,
    inputs: String,
    status: String,
    state: String,
    state_revision: i64,
) -> Result<RunRecord, RunStoreError> {
    let status = RunStatus::from_str(&status).ok_or_else(|| RunStoreError::Storage {
        message: format!("unknown stored run status {status:?}"),
    })?;
    Ok(RunRecord {
        run_id,
        sequence: sqlite_i64_to_u64(sequence, "run sequence")?,
        graph_hash,
        inputs: parse_storage_json(&inputs)?,
        status,
        state: parse_storage_json(&state)?,
        state_revision: sqlite_i64_to_u64(state_revision, "state revision")?,
    })
}

fn storage_json(value: &Value) -> Result<String, RunStoreError> {
    serde_json::to_string(value).map_err(storage_error)
}

fn parse_storage_json(text: &str) -> Result<Value, RunStoreError> {
    serde_json::from_str(text).map_err(storage_error)
}

fn sqlite_u64_to_i64(value: u64, label: &'static str) -> Result<i64, RunStoreError> {
    i64::try_from(value).map_err(|_| RunStoreError::Storage {
        message: format!("{label} exceeds SQLite integer range"),
    })
}

fn sqlite_i64_to_u64(value: i64, label: &'static str) -> Result<u64, RunStoreError> {
    u64::try_from(value).map_err(|_| RunStoreError::Storage {
        message: format!("{label} must be non-negative"),
    })
}

fn storage_error(error: impl std::fmt::Display) -> RunStoreError {
    RunStoreError::Storage {
        message: error.to_string(),
    }
}

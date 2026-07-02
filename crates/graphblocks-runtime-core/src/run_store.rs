use std::{collections::BTreeMap, path::Path};

use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Map, Number, Value, json};

use crate::evaluation::ModelVisibleToolRef;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RunStatus {
    Created,
    Validating,
    AdmissionPending,
    Admitted,
    Queued,
    Running,
    WaitingInput,
    WaitingApproval,
    WaitingReview,
    WaitingCallback,
    Paused,
    PausedBudget,
    PausedPolicy,
    PausedOperator,
    Interrupted,
    Resuming,
    Completed,
    Failed,
    Cancelled,
    Expired,
    PolicyStopped,
}

impl RunStatus {
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Completed | Self::Failed | Self::Cancelled | Self::Expired | Self::PolicyStopped
        )
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Created => "created",
            Self::Validating => "validating",
            Self::AdmissionPending => "admission_pending",
            Self::Admitted => "admitted",
            Self::Queued => "queued",
            Self::Running => "running",
            Self::WaitingInput => "waiting_input",
            Self::WaitingApproval => "waiting_approval",
            Self::WaitingReview => "waiting_review",
            Self::WaitingCallback => "waiting_callback",
            Self::Paused => "paused",
            Self::PausedBudget => "paused_budget",
            Self::PausedPolicy => "paused_policy",
            Self::PausedOperator => "paused_operator",
            Self::Interrupted => "interrupted",
            Self::Resuming => "resuming",
            Self::Completed => "completed",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::Expired => "expired",
            Self::PolicyStopped => "policy_stopped",
        }
    }

    fn from_str(status: &str) -> Option<Self> {
        match status {
            "created" => Some(Self::Created),
            "validating" => Some(Self::Validating),
            "admission_pending" => Some(Self::AdmissionPending),
            "admitted" => Some(Self::Admitted),
            "queued" => Some(Self::Queued),
            "running" => Some(Self::Running),
            "waiting_input" => Some(Self::WaitingInput),
            "waiting_approval" => Some(Self::WaitingApproval),
            "waiting_review" => Some(Self::WaitingReview),
            "waiting_callback" => Some(Self::WaitingCallback),
            "paused" => Some(Self::Paused),
            "paused_budget" => Some(Self::PausedBudget),
            "paused_policy" => Some(Self::PausedPolicy),
            "paused_operator" => Some(Self::PausedOperator),
            "interrupted" => Some(Self::Interrupted),
            "resuming" => Some(Self::Resuming),
            "completed" => Some(Self::Completed),
            "failed" => Some(Self::Failed),
            "cancelled" => Some(Self::Cancelled),
            "expired" => Some(Self::Expired),
            "policy_stopped" => Some(Self::PolicyStopped),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RunDeploymentProvenance {
    pub release_digest: Option<String>,
    pub deployment_revision_id: Option<String>,
    pub physical_plan_hash: Option<String>,
    pub release_signature_digest: Option<String>,
}

impl RunDeploymentProvenance {
    pub fn new() -> Self {
        Self {
            release_digest: None,
            deployment_revision_id: None,
            physical_plan_hash: None,
            release_signature_digest: None,
        }
    }

    pub fn with_release_digest(mut self, release_digest: impl Into<String>) -> Self {
        self.release_digest = Some(release_digest.into());
        self
    }

    pub fn with_deployment_revision_id(
        mut self,
        deployment_revision_id: impl Into<String>,
    ) -> Self {
        self.deployment_revision_id = Some(deployment_revision_id.into());
        self
    }

    pub fn with_physical_plan_hash(mut self, physical_plan_hash: impl Into<String>) -> Self {
        self.physical_plan_hash = Some(physical_plan_hash.into());
        self
    }

    pub fn with_release_signature_digest(
        mut self,
        release_signature_digest: impl Into<String>,
    ) -> Self {
        self.release_signature_digest = Some(release_signature_digest.into());
        self
    }

    pub fn canonical_value(&self) -> Value {
        json!({
            "release_digest": self.release_digest,
            "deployment_revision_id": self.deployment_revision_id,
            "physical_plan_hash": self.physical_plan_hash,
            "release_signature_digest": self.release_signature_digest,
        })
    }
}

impl Default for RunDeploymentProvenance {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RunRecord {
    pub run_id: String,
    pub sequence: u64,
    pub graph_hash: String,
    pub inputs: Value,
    pub deployment_provenance: RunDeploymentProvenance,
    pub model_visible_tools: Vec<ModelVisibleToolRef>,
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
    InvocationProvenanceAfterTerminal {
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

fn record_with_model_visible_tools(
    current: &RunRecord,
    model_visible_tools: Vec<ModelVisibleToolRef>,
) -> Result<RunRecord, RunStoreError> {
    if current.status.is_terminal() {
        return Err(RunStoreError::InvocationProvenanceAfterTerminal {
            run_id: current.run_id.clone(),
            status: current.status,
        });
    }

    let mut updated = current.clone();
    updated.model_visible_tools = sorted_model_visible_tools(model_visible_tools);
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
        self.create_run_with_provenance(graph_hash, inputs, RunDeploymentProvenance::new())
    }

    pub fn create_run_with_provenance(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
        deployment_provenance: RunDeploymentProvenance,
    ) -> RunRecord {
        self.create_run_with_invocation_provenance(
            graph_hash,
            inputs,
            deployment_provenance,
            Vec::new(),
        )
    }

    pub fn create_run_with_invocation_provenance(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
        deployment_provenance: RunDeploymentProvenance,
        model_visible_tools: Vec<ModelVisibleToolRef>,
    ) -> RunRecord {
        let sequence = self.next_sequence;
        self.next_sequence += 1;
        let run_id = format!("run-{sequence:06}");
        let record = RunRecord {
            run_id: run_id.clone(),
            sequence,
            graph_hash: graph_hash.into(),
            inputs,
            deployment_provenance,
            model_visible_tools: sorted_model_visible_tools(model_visible_tools),
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

    pub fn record_model_visible_tools(
        &mut self,
        run_id: impl AsRef<str>,
        model_visible_tools: Vec<ModelVisibleToolRef>,
    ) -> Result<RunRecord, RunStoreError> {
        let run_id = run_id.as_ref();
        let Some(current) = self.runs.get(run_id) else {
            return Err(RunStoreError::NotFound {
                run_id: run_id.to_owned(),
            });
        };

        let updated = record_with_model_visible_tools(current, model_visible_tools)?;
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
                    deployment_provenance_json TEXT NOT NULL,
                    model_visible_tools_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    state_revision INTEGER NOT NULL
                );
                ",
            )
            .map_err(storage_error)?;
        let columns = self
            .connection
            .prepare("PRAGMA table_info(runs)")
            .map_err(storage_error)?
            .query_map([], |row| row.get::<_, String>(1))
            .map_err(storage_error)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(storage_error)?;
        if !columns
            .iter()
            .any(|name| name == "deployment_provenance_json")
        {
            self.connection
                .execute(
                    "ALTER TABLE runs ADD COLUMN deployment_provenance_json TEXT",
                    [],
                )
                .map_err(storage_error)?;
            self.connection
                .execute(
                    "UPDATE runs SET deployment_provenance_json = ? WHERE deployment_provenance_json IS NULL",
                    params![storage_json(&RunDeploymentProvenance::new().canonical_value())?],
                )
                .map_err(storage_error)?;
        }
        if !columns
            .iter()
            .any(|name| name == "model_visible_tools_json")
        {
            self.connection
                .execute(
                    "ALTER TABLE runs ADD COLUMN model_visible_tools_json TEXT",
                    [],
                )
                .map_err(storage_error)?;
            self.connection
                .execute(
                    "UPDATE runs SET model_visible_tools_json = ? WHERE model_visible_tools_json IS NULL",
                    params![storage_json(&Value::Array(Vec::new()))?],
                )
                .map_err(storage_error)?;
        }
        Ok(())
    }

    pub fn create_run(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
    ) -> Result<RunRecord, RunStoreError> {
        self.create_run_with_provenance(graph_hash, inputs, RunDeploymentProvenance::new())
    }

    pub fn create_run_with_provenance(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
        deployment_provenance: RunDeploymentProvenance,
    ) -> Result<RunRecord, RunStoreError> {
        self.create_run_with_invocation_provenance(
            graph_hash,
            inputs,
            deployment_provenance,
            Vec::new(),
        )
    }

    pub fn create_run_with_invocation_provenance(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
        deployment_provenance: RunDeploymentProvenance,
        model_visible_tools: Vec<ModelVisibleToolRef>,
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
            deployment_provenance,
            model_visible_tools: sorted_model_visible_tools(model_visible_tools),
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
                    deployment_provenance_json,
                    model_visible_tools_json,
                    status,
                    state_json,
                    state_revision
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
                params![
                    sqlite_u64_to_i64(record.sequence, "run sequence")?,
                    &record.run_id,
                    &record.graph_hash,
                    storage_json(&record.inputs)?,
                    storage_json(&record.deployment_provenance.canonical_value())?,
                    storage_json(&model_visible_tools_value(&record.model_visible_tools))?,
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
                SELECT
                    sequence,
                    run_id,
                    graph_hash,
                    inputs_json,
                    deployment_provenance_json,
                    model_visible_tools_json,
                    status,
                    state_json,
                    state_revision
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
                        row.get::<_, String>(6)?,
                        row.get::<_, String>(7)?,
                        row.get::<_, i64>(8)?,
                    ))
                },
            )
            .optional()
            .map_err(storage_error)?;
        let Some((
            sequence,
            run_id,
            graph_hash,
            inputs,
            deployment_provenance,
            model_visible_tools,
            status,
            state,
            state_revision,
        )) = row
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
            deployment_provenance,
            model_visible_tools,
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

    pub fn record_model_visible_tools(
        &mut self,
        run_id: impl AsRef<str>,
        model_visible_tools: Vec<ModelVisibleToolRef>,
    ) -> Result<RunRecord, RunStoreError> {
        let current = self.get_run(run_id.as_ref())?;
        let updated = record_with_model_visible_tools(&current, model_visible_tools)?;
        self.connection
            .execute(
                "
                UPDATE runs
                SET model_visible_tools_json = ?
                WHERE run_id = ?
                ",
                params![
                    storage_json(&model_visible_tools_value(&updated.model_visible_tools))?,
                    &updated.run_id,
                ],
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

#[allow(clippy::too_many_arguments)]
fn record_from_storage(
    sequence: i64,
    run_id: String,
    graph_hash: String,
    inputs: String,
    deployment_provenance: String,
    model_visible_tools: String,
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
        deployment_provenance: deployment_provenance_from_storage(&deployment_provenance)?,
        model_visible_tools: model_visible_tools_from_storage(&model_visible_tools)?,
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

fn deployment_provenance_from_storage(
    text: &str,
) -> Result<RunDeploymentProvenance, RunStoreError> {
    let value = parse_storage_json(text)?;
    let Some(object) = value.as_object() else {
        return Ok(RunDeploymentProvenance::new());
    };
    Ok(RunDeploymentProvenance {
        release_digest: optional_string(object, "release_digest"),
        deployment_revision_id: optional_string(object, "deployment_revision_id"),
        physical_plan_hash: optional_string(object, "physical_plan_hash"),
        release_signature_digest: optional_string(object, "release_signature_digest"),
    })
}

fn model_visible_tools_value(tools: &[ModelVisibleToolRef]) -> Value {
    Value::Array(
        tools
            .iter()
            .map(|tool| {
                json!({
                    "tool_name": tool.tool_name,
                    "resolved_tool_id": tool.resolved_tool_id,
                    "definition_digest": tool.definition_digest,
                    "binding_digest": tool.binding_digest,
                    "effective_policy_snapshot_id": tool.effective_policy_snapshot_id,
                    "allowed_for_principal": tool.allowed_for_principal,
                    "valid_until_unix_ms": tool.valid_until_unix_ms,
                })
            })
            .collect(),
    )
}

fn model_visible_tools_from_storage(text: &str) -> Result<Vec<ModelVisibleToolRef>, RunStoreError> {
    let value = parse_storage_json(text)?;
    let Some(array) = value.as_array() else {
        return Ok(Vec::new());
    };
    let mut tools = Vec::with_capacity(array.len());
    for item in array {
        let Some(object) = item.as_object() else {
            return Err(RunStoreError::Storage {
                message: "stored model-visible tool provenance must be an array of objects"
                    .to_owned(),
            });
        };
        let valid_until_unix_ms = match object.get("valid_until_unix_ms") {
            Some(Value::Null) | None => None,
            Some(value) => value.as_u64(),
        };
        tools.push(ModelVisibleToolRef {
            tool_name: required_storage_string(object, "tool_name")?,
            resolved_tool_id: required_storage_string(object, "resolved_tool_id")?,
            definition_digest: required_storage_string(object, "definition_digest")?,
            binding_digest: required_storage_string(object, "binding_digest")?,
            effective_policy_snapshot_id: required_storage_string(
                object,
                "effective_policy_snapshot_id",
            )?,
            allowed_for_principal: object
                .get("allowed_for_principal")
                .and_then(Value::as_bool)
                .ok_or_else(|| RunStoreError::Storage {
                    message:
                        "stored model-visible tool provenance is missing allowed_for_principal"
                            .to_owned(),
                })?,
            valid_until_unix_ms,
        });
    }
    Ok(sorted_model_visible_tools(tools))
}

fn sorted_model_visible_tools(mut tools: Vec<ModelVisibleToolRef>) -> Vec<ModelVisibleToolRef> {
    tools.sort();
    tools
}

fn optional_string(object: &Map<String, Value>, key: &str) -> Option<String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
}

fn required_storage_string(
    object: &Map<String, Value>,
    key: &'static str,
) -> Result<String, RunStoreError> {
    object
        .get(key)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .ok_or_else(|| RunStoreError::Storage {
            message: format!("stored model-visible tool provenance is missing {key}"),
        })
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

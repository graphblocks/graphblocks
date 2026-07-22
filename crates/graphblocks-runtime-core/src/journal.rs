use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::Value;

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct JournalMetadata {
    pub causation_id: Option<String>,
    pub node_id: Option<String>,
    pub attempt_id: Option<String>,
    pub lease_epoch: Option<u64>,
}

impl JournalMetadata {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_causation_id(mut self, causation_id: impl Into<String>) -> Self {
        self.causation_id = Some(causation_id.into());
        self
    }

    pub fn with_node_id(mut self, node_id: impl Into<String>) -> Self {
        self.node_id = Some(node_id.into());
        self
    }

    pub fn with_attempt_id(mut self, attempt_id: impl Into<String>) -> Self {
        self.attempt_id = Some(attempt_id.into());
        self
    }

    pub fn with_lease_epoch(mut self, lease_epoch: u64) -> Self {
        self.lease_epoch = Some(lease_epoch);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct JournalRecord {
    pub record_id: String,
    pub run_id: String,
    pub run_sequence: u64,
    pub kind: String,
    pub causation_id: Option<String>,
    pub node_id: Option<String>,
    pub attempt_id: Option<String>,
    pub lease_epoch: Option<u64>,
    pub payload: Option<Value>,
    pub terminal: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum JournalError {
    AppendAfterTerminal { terminal_kind: String },
    TerminalAlreadyRecorded { terminal_kind: String },
    Storage { message: String },
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ExecutionJournal {
    run_id: String,
    records: Vec<JournalRecord>,
    terminal_kind: Option<String>,
}

impl ExecutionJournal {
    pub fn new(run_id: impl Into<String>) -> Self {
        Self {
            run_id: run_id.into(),
            records: Vec::new(),
            terminal_kind: None,
        }
    }

    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    pub fn records(&self) -> &[JournalRecord] {
        &self.records
    }

    pub fn terminal_kind(&self) -> Option<&str> {
        self.terminal_kind.as_deref()
    }

    pub fn append(
        &mut self,
        kind: impl Into<String>,
        payload: Value,
    ) -> Result<JournalRecord, JournalError> {
        self.append_with_metadata(kind, JournalMetadata::new(), Some(payload))
    }

    pub fn append_with_metadata(
        &mut self,
        kind: impl Into<String>,
        metadata: JournalMetadata,
        payload: Option<Value>,
    ) -> Result<JournalRecord, JournalError> {
        if let Some(terminal_kind) = &self.terminal_kind {
            return Err(JournalError::AppendAfterTerminal {
                terminal_kind: terminal_kind.clone(),
            });
        }

        let run_sequence = self.records.len() as u64 + 1;
        let record = JournalRecord {
            record_id: format!("{}:{run_sequence}", self.run_id),
            run_id: self.run_id.clone(),
            run_sequence,
            kind: kind.into(),
            causation_id: metadata.causation_id,
            node_id: metadata.node_id,
            attempt_id: metadata.attempt_id,
            lease_epoch: metadata.lease_epoch,
            payload,
            terminal: false,
        };
        self.records.push(record.clone());
        Ok(record)
    }

    pub fn append_terminal(
        &mut self,
        kind: impl Into<String>,
        payload: Value,
    ) -> Result<JournalRecord, JournalError> {
        self.append_terminal_with_metadata(kind, JournalMetadata::new(), Some(payload))
    }

    pub fn append_terminal_with_metadata(
        &mut self,
        kind: impl Into<String>,
        metadata: JournalMetadata,
        payload: Option<Value>,
    ) -> Result<JournalRecord, JournalError> {
        if let Some(terminal_kind) = &self.terminal_kind {
            return Err(JournalError::TerminalAlreadyRecorded {
                terminal_kind: terminal_kind.clone(),
            });
        }

        let mut record = self.append_with_metadata(kind, metadata, payload)?;
        record.terminal = true;
        self.terminal_kind = Some(record.kind.clone());
        if let Some(stored) = self.records.last_mut() {
            stored.terminal = true;
        }
        Ok(record)
    }
}

pub struct SqliteExecutionJournal {
    connection: Connection,
    run_id: String,
}

impl SqliteExecutionJournal {
    pub fn open(path: impl AsRef<Path>, run_id: impl Into<String>) -> Result<Self, JournalError> {
        let connection = Connection::open(path).map_err(journal_storage_error)?;
        let journal = Self {
            connection,
            run_id: run_id.into(),
        };
        journal.initialize()?;
        Ok(journal)
    }

    pub fn open_in_memory(run_id: impl Into<String>) -> Result<Self, JournalError> {
        let connection = Connection::open_in_memory().map_err(journal_storage_error)?;
        let journal = Self {
            connection,
            run_id: run_id.into(),
        };
        journal.initialize()?;
        Ok(journal)
    }

    fn initialize(&self) -> Result<(), JournalError> {
        self.connection
            .busy_timeout(Duration::from_secs(5))
            .map_err(journal_storage_error)?;
        self.connection
            .execute_batch(
                "
                CREATE TABLE IF NOT EXISTS journal_records (
                    run_id TEXT NOT NULL,
                    run_sequence INTEGER NOT NULL,
                    record_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    causation_id TEXT,
                    node_id TEXT,
                    attempt_id TEXT,
                    lease_epoch INTEGER,
                    payload_json TEXT,
                    terminal INTEGER NOT NULL,
                    PRIMARY KEY (run_id, run_sequence)
                );
                ",
            )
            .map_err(journal_storage_error)?;
        Ok(())
    }

    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    pub fn records(&self) -> Result<Vec<JournalRecord>, JournalError> {
        let mut statement = self
            .connection
            .prepare(
                "
                SELECT
                    record_id,
                    run_id,
                    run_sequence,
                    kind,
                    causation_id,
                    node_id,
                    attempt_id,
                    lease_epoch,
                    payload_json,
                    terminal
                FROM journal_records
                WHERE run_id = ?
                ORDER BY run_sequence
                ",
            )
            .map_err(journal_storage_error)?;
        let rows = statement
            .query_map(params![&self.run_id], |row| {
                Ok(StoredJournalRecord {
                    record_id: row.get::<_, String>(0)?,
                    run_id: row.get::<_, String>(1)?,
                    run_sequence: row.get::<_, i64>(2)?,
                    kind: row.get::<_, String>(3)?,
                    causation_id: row.get::<_, Option<String>>(4)?,
                    node_id: row.get::<_, Option<String>>(5)?,
                    attempt_id: row.get::<_, Option<String>>(6)?,
                    lease_epoch: row.get::<_, Option<i64>>(7)?,
                    payload: row.get::<_, Option<String>>(8)?,
                    terminal: row.get::<_, bool>(9)?,
                })
            })
            .map_err(journal_storage_error)?;
        let mut records = Vec::new();
        for row in rows {
            records.push(record_from_storage(row.map_err(journal_storage_error)?)?);
        }
        Ok(records)
    }

    pub fn terminal_kind(&self) -> Result<Option<String>, JournalError> {
        self.connection
            .query_row(
                "
                SELECT kind
                FROM journal_records
                WHERE run_id = ? AND terminal = 1
                ORDER BY run_sequence DESC
                LIMIT 1
                ",
                params![&self.run_id],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(journal_storage_error)
    }

    pub fn append(
        &mut self,
        kind: impl Into<String>,
        payload: Value,
    ) -> Result<JournalRecord, JournalError> {
        self.append_with_metadata(kind, JournalMetadata::new(), Some(payload))
    }

    pub fn append_with_metadata(
        &mut self,
        kind: impl Into<String>,
        metadata: JournalMetadata,
        payload: Option<Value>,
    ) -> Result<JournalRecord, JournalError> {
        self.insert_record(kind.into(), metadata, payload, false)
    }

    pub fn append_terminal(
        &mut self,
        kind: impl Into<String>,
        payload: Value,
    ) -> Result<JournalRecord, JournalError> {
        self.append_terminal_with_metadata(kind, JournalMetadata::new(), Some(payload))
    }

    pub fn append_terminal_with_metadata(
        &mut self,
        kind: impl Into<String>,
        metadata: JournalMetadata,
        payload: Option<Value>,
    ) -> Result<JournalRecord, JournalError> {
        self.insert_record(kind.into(), metadata, payload, true)
    }

    fn insert_record(
        &mut self,
        kind: String,
        metadata: JournalMetadata,
        payload: Option<Value>,
        terminal: bool,
    ) -> Result<JournalRecord, JournalError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(journal_storage_error)?;
        let terminal_kind = transaction
            .query_row(
                "
                SELECT kind
                FROM journal_records
                WHERE run_id = ? AND terminal = 1
                ORDER BY run_sequence DESC
                LIMIT 1
                ",
                params![&self.run_id],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(journal_storage_error)?;
        if let Some(terminal_kind) = terminal_kind {
            return Err(if terminal {
                JournalError::TerminalAlreadyRecorded { terminal_kind }
            } else {
                JournalError::AppendAfterTerminal { terminal_kind }
            });
        }
        let next_sequence = transaction
            .query_row(
                "
                SELECT COALESCE(MAX(run_sequence), 0) + 1
                FROM journal_records
                WHERE run_id = ?
                ",
                params![&self.run_id],
                |row| row.get::<_, i64>(0),
            )
            .map_err(journal_storage_error)?;
        let run_sequence = journal_i64_to_u64(next_sequence, "journal sequence")?;
        let record = JournalRecord {
            record_id: format!("{}:{run_sequence}", self.run_id),
            run_id: self.run_id.clone(),
            run_sequence,
            kind,
            causation_id: metadata.causation_id,
            node_id: metadata.node_id,
            attempt_id: metadata.attempt_id,
            lease_epoch: metadata.lease_epoch,
            payload,
            terminal,
        };
        transaction
            .execute(
                "
                INSERT INTO journal_records (
                    run_id,
                    run_sequence,
                    record_id,
                    kind,
                    causation_id,
                    node_id,
                    attempt_id,
                    lease_epoch,
                    payload_json,
                    terminal
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
                params![
                    &record.run_id,
                    journal_u64_to_i64(record.run_sequence, "journal sequence")?,
                    &record.record_id,
                    &record.kind,
                    &record.causation_id,
                    &record.node_id,
                    &record.attempt_id,
                    optional_journal_u64_to_i64(record.lease_epoch, "lease epoch")?,
                    optional_journal_json(&record.payload)?,
                    record.terminal,
                ],
            )
            .map_err(journal_storage_error)?;
        transaction.commit().map_err(journal_storage_error)?;
        Ok(record)
    }
}

struct StoredJournalRecord {
    record_id: String,
    run_id: String,
    run_sequence: i64,
    kind: String,
    causation_id: Option<String>,
    node_id: Option<String>,
    attempt_id: Option<String>,
    lease_epoch: Option<i64>,
    payload: Option<String>,
    terminal: bool,
}

fn record_from_storage(stored: StoredJournalRecord) -> Result<JournalRecord, JournalError> {
    for (field, value) in [
        ("record_id", stored.record_id.as_str()),
        ("run_id", stored.run_id.as_str()),
        ("kind", stored.kind.as_str()),
    ] {
        if value.trim().is_empty() {
            return Err(JournalError::Storage {
                message: format!("stored journal record {field} must not be empty"),
            });
        }
    }
    for (field, value) in [
        ("causation_id", stored.causation_id.as_deref()),
        ("node_id", stored.node_id.as_deref()),
        ("attempt_id", stored.attempt_id.as_deref()),
    ] {
        if value.is_some_and(|value| value.trim().is_empty()) {
            return Err(JournalError::Storage {
                message: format!("stored journal record {field} must not be empty"),
            });
        }
    }
    Ok(JournalRecord {
        record_id: stored.record_id,
        run_id: stored.run_id,
        run_sequence: journal_i64_to_u64(stored.run_sequence, "journal sequence")?,
        kind: stored.kind,
        causation_id: stored.causation_id,
        node_id: stored.node_id,
        attempt_id: stored.attempt_id,
        lease_epoch: optional_journal_i64_to_u64(stored.lease_epoch, "lease epoch")?,
        payload: optional_parse_journal_json(stored.payload)?,
        terminal: stored.terminal,
    })
}

fn optional_journal_json(value: &Option<Value>) -> Result<Option<String>, JournalError> {
    value
        .as_ref()
        .map(serde_json::to_string)
        .transpose()
        .map_err(journal_storage_error)
}

fn optional_parse_journal_json(value: Option<String>) -> Result<Option<Value>, JournalError> {
    value
        .as_deref()
        .map(serde_json::from_str)
        .transpose()
        .map_err(journal_storage_error)
}

fn journal_u64_to_i64(value: u64, label: &'static str) -> Result<i64, JournalError> {
    i64::try_from(value).map_err(|_| JournalError::Storage {
        message: format!("{label} exceeds SQLite integer range"),
    })
}

fn journal_i64_to_u64(value: i64, label: &'static str) -> Result<u64, JournalError> {
    u64::try_from(value).map_err(|_| JournalError::Storage {
        message: format!("{label} must be non-negative"),
    })
}

fn optional_journal_u64_to_i64(
    value: Option<u64>,
    label: &'static str,
) -> Result<Option<i64>, JournalError> {
    value
        .map(|value| journal_u64_to_i64(value, label))
        .transpose()
}

fn optional_journal_i64_to_u64(
    value: Option<i64>,
    label: &'static str,
) -> Result<Option<u64>, JournalError> {
    value
        .map(|value| journal_i64_to_u64(value, label))
        .transpose()
}

fn journal_storage_error(error: impl std::fmt::Display) -> JournalError {
    JournalError::Storage {
        message: error.to_string(),
    }
}

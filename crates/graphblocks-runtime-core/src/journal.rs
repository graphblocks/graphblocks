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

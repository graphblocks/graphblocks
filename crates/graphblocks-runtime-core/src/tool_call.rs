use graphblocks_compiler::canonical::canonical_hash;
use serde_json::Value;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolCallDraftStatus {
    Proposed,
    ArgumentsStreaming,
    ArgumentsComplete,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolCallStatus {
    Validated,
    PolicyPending,
    ApprovalPending,
    Admitted,
    Running,
    Completed,
    Failed,
    Denied,
    Cancelled,
    PolicyStopped,
    Expired,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolCallError {
    DraftAlreadyComplete,
    ArgumentsNotComplete {
        status: ToolCallDraftStatus,
    },
    InvalidArgumentsJson,
    CannotReviseArguments {
        status: ToolCallStatus,
    },
    EmptyField {
        field: &'static str,
    },
    InvalidRevision {
        revision: u32,
    },
    AdmittedBeforeCreated {
        created_at_unix_ms: u64,
        admitted_at_unix_ms: u64,
    },
    CompletedBeforeAdmitted {
        admitted_at_unix_ms: u64,
        completed_at_unix_ms: u64,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ToolCallDraft {
    pub response_id: String,
    pub tool_call_id: String,
    pub tool_name: String,
    pub argument_fragments: Vec<String>,
    pub sequence: u64,
    pub status: ToolCallDraftStatus,
}

impl ToolCallDraft {
    pub fn proposed(
        response_id: impl Into<String>,
        tool_call_id: impl Into<String>,
        tool_name: impl Into<String>,
    ) -> Self {
        Self {
            response_id: response_id.into(),
            tool_call_id: tool_call_id.into(),
            tool_name: tool_name.into(),
            argument_fragments: Vec::new(),
            sequence: 0,
            status: ToolCallDraftStatus::Proposed,
        }
    }

    pub fn validate(&self) -> Result<(), ToolCallError> {
        for (field, value) in [
            ("response_id", self.response_id.as_str()),
            ("tool_call_id", self.tool_call_id.as_str()),
            ("tool_name", self.tool_name.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(ToolCallError::EmptyField { field });
            }
        }
        Ok(())
    }

    pub fn append_argument_fragment(
        &mut self,
        fragment: impl Into<String>,
    ) -> Result<(), ToolCallError> {
        if self.status == ToolCallDraftStatus::ArgumentsComplete {
            return Err(ToolCallError::DraftAlreadyComplete);
        }
        self.validate()?;
        self.argument_fragments.push(fragment.into());
        self.sequence += 1;
        self.status = ToolCallDraftStatus::ArgumentsStreaming;
        Ok(())
    }

    pub fn complete_arguments(&mut self) -> Result<(), ToolCallError> {
        if self.status == ToolCallDraftStatus::ArgumentsComplete {
            return Err(ToolCallError::DraftAlreadyComplete);
        }
        self.validate()?;
        self.status = ToolCallDraftStatus::ArgumentsComplete;
        Ok(())
    }

    pub fn into_completed_tool_call(
        mut self,
        resolved_tool_id: impl Into<String>,
        created_at_unix_ms: u64,
    ) -> Result<ToolCall, ToolCallError> {
        self.complete_arguments()?;
        self.into_tool_call(resolved_tool_id, created_at_unix_ms)
    }

    pub fn into_tool_call(
        self,
        resolved_tool_id: impl Into<String>,
        created_at_unix_ms: u64,
    ) -> Result<ToolCall, ToolCallError> {
        if self.status != ToolCallDraftStatus::ArgumentsComplete {
            return Err(ToolCallError::ArgumentsNotComplete {
                status: self.status,
            });
        }
        self.validate()?;

        let mut assembled = String::new();
        for fragment in self.argument_fragments {
            assembled.push_str(&fragment);
        }
        let arguments = serde_json::from_str::<Value>(&assembled)
            .map_err(|_| ToolCallError::InvalidArgumentsJson)?;
        let arguments_digest = canonical_hash(&arguments);

        let call = ToolCall {
            tool_call_id: self.tool_call_id,
            response_id: self.response_id,
            resolved_tool_id: resolved_tool_id.into(),
            name: self.tool_name,
            arguments,
            arguments_digest,
            revision: 1,
            status: ToolCallStatus::Validated,
            depends_on: Vec::new(),
            created_at_unix_ms,
            admitted_at_unix_ms: None,
            completed_at_unix_ms: None,
        };
        call.validate()?;
        Ok(call)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolCall {
    pub tool_call_id: String,
    pub response_id: String,
    pub resolved_tool_id: String,
    pub name: String,
    pub arguments: Value,
    pub arguments_digest: String,
    pub revision: u32,
    pub status: ToolCallStatus,
    pub depends_on: Vec<String>,
    pub created_at_unix_ms: u64,
    pub admitted_at_unix_ms: Option<u64>,
    pub completed_at_unix_ms: Option<u64>,
}

impl ToolCall {
    pub fn validate(&self) -> Result<(), ToolCallError> {
        for (field, value) in [
            ("tool_call_id", self.tool_call_id.as_str()),
            ("response_id", self.response_id.as_str()),
            ("resolved_tool_id", self.resolved_tool_id.as_str()),
            ("name", self.name.as_str()),
            ("arguments_digest", self.arguments_digest.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(ToolCallError::EmptyField { field });
            }
        }
        if self.revision == 0 {
            return Err(ToolCallError::InvalidRevision {
                revision: self.revision,
            });
        }
        if let Some(admitted_at_unix_ms) = self.admitted_at_unix_ms
            && admitted_at_unix_ms < self.created_at_unix_ms
        {
            return Err(ToolCallError::AdmittedBeforeCreated {
                created_at_unix_ms: self.created_at_unix_ms,
                admitted_at_unix_ms,
            });
        }
        if let (Some(admitted_at_unix_ms), Some(completed_at_unix_ms)) =
            (self.admitted_at_unix_ms, self.completed_at_unix_ms)
            && completed_at_unix_ms < admitted_at_unix_ms
        {
            return Err(ToolCallError::CompletedBeforeAdmitted {
                admitted_at_unix_ms,
                completed_at_unix_ms,
            });
        }
        if self
            .depends_on
            .iter()
            .any(|dependency| dependency.trim().is_empty())
        {
            return Err(ToolCallError::EmptyField {
                field: "depends_on",
            });
        }
        Ok(())
    }

    pub fn revise_arguments(&self, arguments: Value) -> Result<Self, ToolCallError> {
        self.validate()?;
        if self.status != ToolCallStatus::Validated {
            return Err(ToolCallError::CannotReviseArguments {
                status: self.status,
            });
        }

        let mut revised = self.clone();
        revised.arguments_digest = canonical_hash(&arguments);
        revised.arguments = arguments;
        revised.revision += 1;
        revised.status = ToolCallStatus::Validated;
        revised.admitted_at_unix_ms = None;
        revised.completed_at_unix_ms = None;
        revised.validate()?;
        Ok(revised)
    }
}

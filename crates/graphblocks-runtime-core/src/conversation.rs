use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use serde_json::Value;
use serde_json::json;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MessageRole {
    System,
    Developer,
    User,
    Assistant,
    Tool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MessageStatus {
    Draft,
    Committed,
    Superseded,
    Retracted,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ContentPartKind {
    Text,
    Json,
    ArtifactRef,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ContentPart {
    pub kind: ContentPartKind,
    pub text: Option<String>,
    pub data: Option<Value>,
    pub metadata: BTreeMap<String, Value>,
}

impl ContentPart {
    pub fn text(text: impl Into<String>) -> Self {
        Self {
            kind: ContentPartKind::Text,
            text: Some(text.into()),
            data: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn json(data: Value) -> Self {
        Self {
            kind: ContentPartKind::Json,
            text: None,
            data: Some(data),
            metadata: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Message {
    pub message_id: String,
    pub role: MessageRole,
    pub parts: Vec<ContentPart>,
    pub parent_message_id: Option<String>,
    pub revision: u64,
    pub status: MessageStatus,
    pub created_at: Option<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl Message {
    pub fn new(message_id: impl Into<String>, role: MessageRole) -> Self {
        Self {
            message_id: message_id.into(),
            role,
            parts: Vec::new(),
            parent_message_id: None,
            revision: 0,
            status: MessageStatus::Committed,
            created_at: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_part(mut self, part: ContentPart) -> Self {
        self.parts.push(part);
        self
    }

    fn with_status(mut self, status: MessageStatus) -> Self {
        self.status = status;
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Conversation {
    pub conversation_id: String,
    pub messages: Vec<Message>,
    pub revision: u64,
    pub archived: bool,
    pub branch_of: Option<String>,
    pub branched_from_message_id: Option<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl Conversation {
    pub fn new(conversation_id: impl Into<String>) -> Self {
        Self {
            conversation_id: conversation_id.into(),
            messages: Vec::new(),
            revision: 0,
            archived: false,
            branch_of: None,
            branched_from_message_id: None,
            metadata: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ConversationSnapshot {
    pub conversation: Conversation,
    pub revision: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BranchRequest {
    pub conversation_id: String,
    pub from_message_id: String,
    pub new_conversation_id: Option<String>,
    pub include_attachments: bool,
    pub include_memory: bool,
}

impl BranchRequest {
    pub fn new(conversation_id: impl Into<String>, from_message_id: impl Into<String>) -> Self {
        Self {
            conversation_id: conversation_id.into(),
            from_message_id: from_message_id.into(),
            new_conversation_id: None,
            include_attachments: true,
            include_memory: false,
        }
    }

    pub fn with_new_conversation_id(mut self, conversation_id: impl Into<String>) -> Self {
        self.new_conversation_id = Some(conversation_id.into());
        self
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeletePolicy {
    Tombstone,
    Hard,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TurnStatus {
    Created,
    ContextBuilding,
    ModelRunning,
    ToolWaiting,
    ApprovalWaiting,
    Finalizing,
    Completed,
    Failed,
    Cancelled,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Turn {
    pub turn_id: String,
    pub conversation_id: String,
    pub base_revision: u64,
    pub status: TurnStatus,
    pub messages: Vec<Message>,
    pub committed_revision: Option<u64>,
    pub committed_message_ids: Vec<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl Turn {
    fn new(
        turn_id: impl Into<String>,
        conversation_id: impl Into<String>,
        base_revision: u64,
    ) -> Self {
        Self {
            turn_id: turn_id.into(),
            conversation_id: conversation_id.into(),
            base_revision,
            status: TurnStatus::Created,
            messages: Vec::new(),
            committed_revision: None,
            committed_message_ids: Vec::new(),
            metadata: BTreeMap::new(),
        }
    }

    fn is_terminal(&self) -> bool {
        matches!(
            self.status,
            TurnStatus::Completed | TurnStatus::Failed | TurnStatus::Cancelled
        )
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ConversationError {
    AlreadyExists {
        conversation_id: String,
    },
    NotFound {
        conversation_id: String,
    },
    Archived {
        conversation_id: String,
    },
    RevisionConflict {
        conversation_id: String,
        expected: u64,
        actual: u64,
    },
}

impl fmt::Display for ConversationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::AlreadyExists { conversation_id } => {
                write!(formatter, "conversation {conversation_id:?} already exists")
            }
            Self::NotFound { conversation_id } => {
                write!(formatter, "conversation {conversation_id:?} does not exist")
            }
            Self::Archived { conversation_id } => {
                write!(formatter, "conversation {conversation_id:?} is archived")
            }
            Self::RevisionConflict {
                conversation_id,
                expected,
                actual,
            } => write!(
                formatter,
                "conversation {conversation_id:?} is at revision {actual}, not {expected}"
            ),
        }
    }
}

impl Error for ConversationError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum MessageError {
    Conversation(ConversationError),
    NotFound { message_id: String },
}

impl fmt::Display for MessageError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Conversation(error) => error.fmt(formatter),
            Self::NotFound { message_id } => {
                write!(formatter, "message {message_id:?} does not exist")
            }
        }
    }
}

impl Error for MessageError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Conversation(error) => Some(error),
            Self::NotFound { .. } => None,
        }
    }
}

impl From<ConversationError> for MessageError {
    fn from(error: ConversationError) -> Self {
        Self::Conversation(error)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TurnError {
    AlreadyExists { turn_id: String },
    NotFound { turn_id: String },
    Terminal { turn_id: String, status: TurnStatus },
    Conversation(ConversationError),
}

impl fmt::Display for TurnError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::AlreadyExists { turn_id } => {
                write!(formatter, "turn {turn_id:?} already exists")
            }
            Self::NotFound { turn_id } => write!(formatter, "turn {turn_id:?} does not exist"),
            Self::Terminal { turn_id, status } => {
                write!(
                    formatter,
                    "turn {turn_id:?} is already terminal as {status:?}"
                )
            }
            Self::Conversation(error) => error.fmt(formatter),
        }
    }
}

impl Error for TurnError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Conversation(error) => Some(error),
            Self::AlreadyExists { .. } | Self::NotFound { .. } | Self::Terminal { .. } => None,
        }
    }
}

impl From<ConversationError> for TurnError {
    fn from(error: ConversationError) -> Self {
        Self::Conversation(error)
    }
}

#[derive(Clone, Debug, Default)]
pub struct InMemoryConversationStore {
    conversations: BTreeMap<String, Conversation>,
    turns: BTreeMap<String, Turn>,
}

impl InMemoryConversationStore {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn create(&mut self, conversation: Conversation) -> Result<(), ConversationError> {
        let conversation_id = conversation.conversation_id.clone();
        if self.conversations.contains_key(&conversation_id) {
            return Err(ConversationError::AlreadyExists { conversation_id });
        }
        self.conversations.insert(conversation_id, conversation);
        Ok(())
    }

    pub fn get(
        &self,
        conversation_id: impl AsRef<str>,
    ) -> Result<ConversationSnapshot, ConversationError> {
        let conversation_id = conversation_id.as_ref();
        let conversation =
            self.conversations
                .get(conversation_id)
                .ok_or_else(|| ConversationError::NotFound {
                    conversation_id: conversation_id.to_owned(),
                })?;
        Ok(ConversationSnapshot {
            conversation: conversation.clone(),
            revision: conversation.revision,
        })
    }

    pub fn append_messages<I>(
        &mut self,
        conversation_id: impl AsRef<str>,
        expected_revision: u64,
        messages: I,
    ) -> Result<u64, ConversationError>
    where
        I: IntoIterator<Item = Message>,
    {
        let conversation_id = conversation_id.as_ref();
        let conversation = self.conversations.get_mut(conversation_id).ok_or_else(|| {
            ConversationError::NotFound {
                conversation_id: conversation_id.to_owned(),
            }
        })?;
        if conversation.archived {
            return Err(ConversationError::Archived {
                conversation_id: conversation_id.to_owned(),
            });
        }
        if conversation.revision != expected_revision {
            return Err(ConversationError::RevisionConflict {
                conversation_id: conversation_id.to_owned(),
                expected: expected_revision,
                actual: conversation.revision,
            });
        }

        conversation.messages.extend(messages);
        conversation.revision += 1;
        Ok(conversation.revision)
    }

    pub fn begin_turn(
        &mut self,
        conversation_id: impl AsRef<str>,
        expected_revision: u64,
        turn_id: impl Into<String>,
    ) -> Result<Turn, TurnError> {
        let conversation_id = conversation_id.as_ref();
        let turn_id = turn_id.into();
        if self.turns.contains_key(&turn_id) {
            return Err(TurnError::AlreadyExists { turn_id });
        }
        let conversation =
            self.conversations
                .get(conversation_id)
                .ok_or_else(|| ConversationError::NotFound {
                    conversation_id: conversation_id.to_owned(),
                })?;
        if conversation.archived {
            return Err(ConversationError::Archived {
                conversation_id: conversation_id.to_owned(),
            }
            .into());
        }
        if conversation.revision != expected_revision {
            return Err(ConversationError::RevisionConflict {
                conversation_id: conversation_id.to_owned(),
                expected: expected_revision,
                actual: conversation.revision,
            }
            .into());
        }

        let turn = Turn::new(
            turn_id.clone(),
            conversation_id.to_owned(),
            expected_revision,
        );
        self.turns.insert(turn_id, turn.clone());
        Ok(turn)
    }

    pub fn get_turn(&self, turn_id: impl AsRef<str>) -> Result<Turn, TurnError> {
        let turn_id = turn_id.as_ref();
        self.turns
            .get(turn_id)
            .cloned()
            .ok_or_else(|| TurnError::NotFound {
                turn_id: turn_id.to_owned(),
            })
    }

    pub fn append_turn_message(
        &mut self,
        turn_id: impl AsRef<str>,
        message: Message,
    ) -> Result<Turn, TurnError> {
        let turn_id = turn_id.as_ref();
        let turn = self
            .turns
            .get_mut(turn_id)
            .ok_or_else(|| TurnError::NotFound {
                turn_id: turn_id.to_owned(),
            })?;
        if turn.is_terminal() {
            return Err(TurnError::Terminal {
                turn_id: turn_id.to_owned(),
                status: turn.status,
            });
        }

        turn.messages
            .push(message.with_status(MessageStatus::Draft));
        if turn.status == TurnStatus::Created {
            turn.status = TurnStatus::ModelRunning;
        }
        Ok(turn.clone())
    }

    pub fn commit_turn(&mut self, turn_id: impl AsRef<str>) -> Result<Turn, TurnError> {
        let turn_id = turn_id.as_ref();
        let turn = self.get_turn(turn_id)?;
        if turn.is_terminal() {
            return Err(TurnError::Terminal {
                turn_id: turn_id.to_owned(),
                status: turn.status,
            });
        }

        let committed_messages = turn
            .messages
            .iter()
            .cloned()
            .map(|message| message.with_status(MessageStatus::Committed))
            .collect::<Vec<_>>();

        let new_revision = match self.append_messages(
            &turn.conversation_id,
            turn.base_revision,
            committed_messages.clone(),
        ) {
            Ok(new_revision) => new_revision,
            Err(error) => {
                if let Some(stored_turn) = self.turns.get_mut(turn_id) {
                    stored_turn.status = TurnStatus::Failed;
                }
                return Err(error.into());
            }
        };

        let mut completed = turn;
        completed.status = TurnStatus::Completed;
        completed.messages = committed_messages;
        completed.committed_revision = Some(new_revision);
        completed.committed_message_ids = completed
            .messages
            .iter()
            .map(|message| message.message_id.clone())
            .collect();
        self.turns.insert(turn_id.to_owned(), completed.clone());
        Ok(completed)
    }

    pub fn abort_turn(&mut self, turn_id: impl AsRef<str>) -> Result<Turn, TurnError> {
        let turn_id = turn_id.as_ref();
        let turn = self
            .turns
            .get_mut(turn_id)
            .ok_or_else(|| TurnError::NotFound {
                turn_id: turn_id.to_owned(),
            })?;
        if turn.is_terminal() {
            return Err(TurnError::Terminal {
                turn_id: turn_id.to_owned(),
                status: turn.status,
            });
        }

        turn.status = TurnStatus::Cancelled;
        turn.messages = turn
            .messages
            .iter()
            .cloned()
            .map(|message| message.with_status(MessageStatus::Retracted))
            .collect();
        turn.committed_revision = None;
        turn.committed_message_ids.clear();
        Ok(turn.clone())
    }

    pub fn branch(&mut self, request: BranchRequest) -> Result<Conversation, MessageError> {
        let conversation = self
            .conversations
            .get(&request.conversation_id)
            .ok_or_else(|| ConversationError::NotFound {
                conversation_id: request.conversation_id.clone(),
            })?;
        let source_index = conversation
            .messages
            .iter()
            .position(|message| message.message_id == request.from_message_id)
            .ok_or_else(|| MessageError::NotFound {
                message_id: request.from_message_id.clone(),
            })?;
        let branch_id = request.new_conversation_id.clone().unwrap_or_else(|| {
            format!(
                "{}:branch:{}",
                request.conversation_id, request.from_message_id
            )
        });
        if self.conversations.contains_key(&branch_id) {
            return Err(ConversationError::AlreadyExists {
                conversation_id: branch_id,
            }
            .into());
        }

        let mut metadata = BTreeMap::new();
        metadata.insert("source_revision".to_owned(), json!(conversation.revision));
        metadata.insert(
            "include_attachments".to_owned(),
            json!(request.include_attachments),
        );
        metadata.insert("include_memory".to_owned(), json!(request.include_memory));
        let branch = Conversation {
            conversation_id: branch_id.clone(),
            messages: conversation.messages[..=source_index].to_vec(),
            revision: 0,
            archived: false,
            branch_of: Some(conversation.conversation_id.clone()),
            branched_from_message_id: Some(request.from_message_id),
            metadata,
        };
        self.conversations.insert(branch_id, branch.clone());
        Ok(branch)
    }

    pub fn archive(&mut self, conversation_id: impl AsRef<str>) -> Result<u64, ConversationError> {
        let conversation_id = conversation_id.as_ref();
        let conversation = self.conversations.get_mut(conversation_id).ok_or_else(|| {
            ConversationError::NotFound {
                conversation_id: conversation_id.to_owned(),
            }
        })?;
        conversation.archived = true;
        conversation.revision += 1;
        Ok(conversation.revision)
    }

    pub fn delete(
        &mut self,
        conversation_id: impl AsRef<str>,
        policy: DeletePolicy,
    ) -> Result<Option<u64>, ConversationError> {
        let conversation_id = conversation_id.as_ref();
        match policy {
            DeletePolicy::Hard => {
                if self.conversations.remove(conversation_id).is_none() {
                    return Err(ConversationError::NotFound {
                        conversation_id: conversation_id.to_owned(),
                    });
                }
                Ok(None)
            }
            DeletePolicy::Tombstone => {
                let conversation =
                    self.conversations.get_mut(conversation_id).ok_or_else(|| {
                        ConversationError::NotFound {
                            conversation_id: conversation_id.to_owned(),
                        }
                    })?;
                conversation.messages.clear();
                conversation.archived = true;
                conversation.revision += 1;
                conversation
                    .metadata
                    .insert("deleted".to_owned(), json!(true));
                Ok(Some(conversation.revision))
            }
        }
    }
}

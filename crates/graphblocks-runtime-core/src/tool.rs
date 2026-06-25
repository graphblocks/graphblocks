use std::collections::{BTreeMap, BTreeSet};

use graphblocks_compiler::canonical::canonical_hash;
use graphblocks_schema::{SchemaId, SchemaIdError};
use serde_json::{Value, json};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ToolDefinition {
    pub name: String,
    pub description: String,
    pub input_schema: String,
    pub output_schema: Option<String>,
    pub tags: BTreeSet<String>,
    pub version: Option<String>,
}

impl ToolDefinition {
    pub fn new(
        name: impl Into<String>,
        description: impl Into<String>,
        input_schema: impl Into<String>,
    ) -> Self {
        Self {
            name: name.into(),
            description: description.into(),
            input_schema: input_schema.into(),
            output_schema: None,
            tags: BTreeSet::new(),
            version: None,
        }
    }

    pub fn with_output_schema(mut self, output_schema: impl Into<String>) -> Self {
        self.output_schema = Some(output_schema.into());
        self
    }

    pub fn with_tags<I, S>(mut self, tags: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.tags = tags.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_version(mut self, version: impl Into<String>) -> Self {
        self.version = Some(version.into());
        self
    }

    pub fn validate(&self) -> Result<(), ToolResolutionError> {
        for (field, value) in [
            ("name", self.name.as_str()),
            ("description", self.description.as_str()),
            ("input_schema", self.input_schema.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(ToolResolutionError::EmptyToolDefinitionField { field });
            }
        }
        Ok(())
    }

    pub fn digest(&self) -> String {
        canonical_hash(&json!({
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "tags": self.tags.iter().collect::<Vec<_>>(),
            "version": self.version,
        }))
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum ToolEffect {
    None,
    ExternalRead,
    ExternalWrite,
    FilesystemRead,
    FilesystemWrite,
    Process,
    Network,
    Destructive,
}

impl ToolEffect {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::ExternalRead => "external_read",
            Self::ExternalWrite => "external_write",
            Self::FilesystemRead => "filesystem_read",
            Self::FilesystemWrite => "filesystem_write",
            Self::Process => "process",
            Self::Network => "network",
            Self::Destructive => "destructive",
        }
    }
}

pub(crate) fn canonical_effect_names(effects: &BTreeSet<ToolEffect>) -> Vec<&'static str> {
    let mut names = effects
        .iter()
        .map(|effect| effect.as_str())
        .collect::<Vec<_>>();
    names.sort_unstable();
    names
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolApproval {
    Never,
    Policy,
    Always,
}

impl ToolApproval {
    fn as_str(self) -> &'static str {
        match self {
            Self::Never => "never",
            Self::Policy => "policy",
            Self::Always => "always",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolIdempotency {
    NotApplicable,
    Optional,
    Required,
}

impl ToolIdempotency {
    fn as_str(self) -> &'static str {
        match self {
            Self::NotApplicable => "not_applicable",
            Self::Optional => "optional",
            Self::Required => "required",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolCancellation {
    Unsupported,
    Cooperative,
    ForceTerminable,
}

impl ToolCancellation {
    fn as_str(self) -> &'static str {
        match self {
            Self::Unsupported => "unsupported",
            Self::Cooperative => "cooperative",
            Self::ForceTerminable => "force_terminable",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolResultMode {
    Value,
    Incremental,
    BoundedSequence,
    ArtifactReference,
}

impl ToolResultMode {
    fn as_str(self) -> &'static str {
        match self {
            Self::Value => "value",
            Self::Incremental => "incremental",
            Self::BoundedSequence => "bounded_sequence",
            Self::ArtifactReference => "artifact_reference",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BlockToolImplementation {
    pub block: String,
    pub input_mapping: BTreeMap<String, String>,
    pub output_mapping: BTreeMap<String, String>,
}

impl BlockToolImplementation {
    pub fn new(block: impl Into<String>) -> Self {
        Self {
            block: block.into(),
            input_mapping: BTreeMap::new(),
            output_mapping: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphToolImplementation {
    pub graph: String,
    pub input_mapping: BTreeMap<String, String>,
    pub output_mapping: BTreeMap<String, String>,
}

impl GraphToolImplementation {
    pub fn new(graph: impl Into<String>) -> Self {
        Self {
            graph: graph.into(),
            input_mapping: BTreeMap::new(),
            output_mapping: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RemoteToolImplementation {
    pub connection: String,
    pub operation: String,
}

impl RemoteToolImplementation {
    pub fn new(connection: impl Into<String>, operation: impl Into<String>) -> Self {
        Self {
            connection: connection.into(),
            operation: operation.into(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct McpToolImplementation {
    pub server: String,
    pub remote_name: String,
}

impl McpToolImplementation {
    pub fn new(server: impl Into<String>, remote_name: impl Into<String>) -> Self {
        Self {
            server: server.into(),
            remote_name: remote_name.into(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OpenApiToolImplementation {
    pub connection: String,
    pub operation_id: String,
}

impl OpenApiToolImplementation {
    pub fn new(connection: impl Into<String>, operation_id: impl Into<String>) -> Self {
        Self {
            connection: connection.into(),
            operation_id: operation_id.into(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolImplementation {
    Block(BlockToolImplementation),
    Graph(GraphToolImplementation),
    Remote(RemoteToolImplementation),
    Mcp(McpToolImplementation),
    OpenApi(OpenApiToolImplementation),
}

impl ToolImplementation {
    fn canonical_value(&self) -> Value {
        match self {
            Self::Block(implementation) => json!({
                "kind": "block",
                "block": implementation.block,
                "input_mapping": implementation.input_mapping,
                "output_mapping": implementation.output_mapping,
            }),
            Self::Graph(implementation) => json!({
                "kind": "graph",
                "graph": implementation.graph,
                "input_mapping": implementation.input_mapping,
                "output_mapping": implementation.output_mapping,
            }),
            Self::Remote(implementation) => json!({
                "kind": "remote",
                "connection": implementation.connection,
                "operation": implementation.operation,
            }),
            Self::Mcp(implementation) => json!({
                "kind": "mcp",
                "server": implementation.server,
                "remote_name": implementation.remote_name,
            }),
            Self::OpenApi(implementation) => json!({
                "kind": "openapi",
                "connection": implementation.connection,
                "operation_id": implementation.operation_id,
            }),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ToolBinding {
    pub binding_id: String,
    pub tool_name: String,
    pub implementation: ToolImplementation,
    pub effects: BTreeSet<ToolEffect>,
    pub approval: ToolApproval,
    pub idempotency: ToolIdempotency,
    pub cancellation: ToolCancellation,
    pub result_mode: ToolResultMode,
    pub timeout_ms: Option<u64>,
    pub retry_policy_ref: Option<String>,
    pub policy_profile_ref: Option<String>,
    pub execution_class: Option<String>,
}

impl ToolBinding {
    pub fn new(
        binding_id: impl Into<String>,
        tool_name: impl Into<String>,
        implementation: ToolImplementation,
    ) -> Self {
        Self {
            binding_id: binding_id.into(),
            tool_name: tool_name.into(),
            implementation,
            effects: BTreeSet::new(),
            approval: ToolApproval::Policy,
            idempotency: ToolIdempotency::Optional,
            cancellation: ToolCancellation::Cooperative,
            result_mode: ToolResultMode::Value,
            timeout_ms: None,
            retry_policy_ref: None,
            policy_profile_ref: None,
            execution_class: None,
        }
    }

    pub fn with_effects<I>(mut self, effects: I) -> Self
    where
        I: IntoIterator<Item = ToolEffect>,
    {
        self.effects = effects.into_iter().collect();
        self
    }

    pub fn with_approval(mut self, approval: ToolApproval) -> Self {
        self.approval = approval;
        self
    }

    pub fn with_idempotency(mut self, idempotency: ToolIdempotency) -> Self {
        self.idempotency = idempotency;
        self
    }

    pub fn with_cancellation(mut self, cancellation: ToolCancellation) -> Self {
        self.cancellation = cancellation;
        self
    }

    pub fn with_result_mode(mut self, result_mode: ToolResultMode) -> Self {
        self.result_mode = result_mode;
        self
    }

    pub fn with_timeout_ms(mut self, timeout_ms: u64) -> Self {
        self.timeout_ms = Some(timeout_ms);
        self
    }

    pub fn validate(&self) -> Result<(), ToolResolutionError> {
        for (field, value) in [
            ("binding_id", self.binding_id.as_str()),
            ("tool_name", self.tool_name.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(ToolResolutionError::EmptyToolBindingField { field });
            }
        }
        Ok(())
    }

    pub fn digest(&self) -> String {
        canonical_hash(&json!({
            "binding_id": self.binding_id,
            "tool_name": self.tool_name,
            "implementation": self.implementation.canonical_value(),
            "effects": canonical_effect_names(&self.effects),
            "approval": self.approval.as_str(),
            "idempotency": self.idempotency.as_str(),
            "cancellation": self.cancellation.as_str(),
            "result_mode": self.result_mode.as_str(),
            "timeout_ms": self.timeout_ms,
            "retry_policy_ref": self.retry_policy_ref,
            "policy_profile_ref": self.policy_profile_ref,
            "execution_class": self.execution_class,
        }))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ResolvedTool {
    pub resolved_tool_id: String,
    pub definition: ToolDefinition,
    pub binding: ToolBinding,
    pub definition_digest: String,
    pub binding_digest: String,
    pub effective_policy_snapshot_id: String,
    pub allowed_for_principal: bool,
    pub valid_until_unix_ms: Option<u64>,
}

impl ResolvedTool {
    pub fn from_definition_and_binding(
        resolved_tool_id: impl Into<String>,
        definition: ToolDefinition,
        binding: ToolBinding,
        effective_policy_snapshot_id: impl Into<String>,
        allowed_for_principal: bool,
        valid_until_unix_ms: Option<u64>,
    ) -> Result<Self, ToolResolutionError> {
        if definition.name != binding.tool_name {
            return Err(ToolResolutionError::BindingToolNameMismatch {
                binding_id: binding.binding_id,
                definition_name: definition.name,
                binding_tool_name: binding.tool_name,
            });
        }
        let definition_digest = definition.digest();
        let binding_digest = binding.digest();
        Ok(Self {
            resolved_tool_id: resolved_tool_id.into(),
            definition,
            binding,
            definition_digest,
            binding_digest,
            effective_policy_snapshot_id: effective_policy_snapshot_id.into(),
            allowed_for_principal,
            valid_until_unix_ms,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolResolutionError {
    EmptyToolDefinitionField {
        field: &'static str,
    },
    EmptyToolBindingField {
        field: &'static str,
    },
    DuplicateToolDefinition {
        tool_name: String,
    },
    DuplicateToolBinding {
        binding_id: String,
    },
    MultipleBindingsForTool {
        tool_name: String,
    },
    BindingWithoutDefinition {
        binding_id: String,
        tool_name: String,
    },
    BindingToolNameMismatch {
        binding_id: String,
        definition_name: String,
        binding_tool_name: String,
    },
    InvalidToolSchemaId {
        tool_name: String,
        schema_id: String,
        error: SchemaIdError,
    },
    ToolBindingMissing {
        tool_name: String,
    },
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ToolResolutionScope {
    application_tools: Option<BTreeSet<String>>,
    graph_tools: Option<BTreeSet<String>>,
    principal_tools: Option<BTreeSet<String>>,
    tenant_policy_tools: Option<BTreeSet<String>>,
    conversation_policy_tools: Option<BTreeSet<String>>,
    data_classification_tools: Option<BTreeSet<String>>,
    deployment_tools: Option<BTreeSet<String>>,
    budget_tools: Option<BTreeSet<String>>,
}

impl ToolResolutionScope {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_application_tools<I, S>(mut self, tools: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.application_tools = Some(tools.into_iter().map(Into::into).collect());
        self
    }

    pub fn with_graph_tools<I, S>(mut self, tools: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.graph_tools = Some(tools.into_iter().map(Into::into).collect());
        self
    }

    pub fn with_principal_tools<I, S>(mut self, tools: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.principal_tools = Some(tools.into_iter().map(Into::into).collect());
        self
    }

    pub fn with_tenant_policy_tools<I, S>(mut self, tools: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.tenant_policy_tools = Some(tools.into_iter().map(Into::into).collect());
        self
    }

    pub fn with_conversation_policy_tools<I, S>(mut self, tools: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.conversation_policy_tools = Some(tools.into_iter().map(Into::into).collect());
        self
    }

    pub fn with_data_classification_tools<I, S>(mut self, tools: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.data_classification_tools = Some(tools.into_iter().map(Into::into).collect());
        self
    }

    pub fn with_deployment_tools<I, S>(mut self, tools: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.deployment_tools = Some(tools.into_iter().map(Into::into).collect());
        self
    }

    pub fn with_budget_tools<I, S>(mut self, tools: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.budget_tools = Some(tools.into_iter().map(Into::into).collect());
        self
    }

    fn allows(&self, tool_name: &str) -> bool {
        let dimensions = [
            &self.application_tools,
            &self.graph_tools,
            &self.principal_tools,
            &self.tenant_policy_tools,
            &self.conversation_policy_tools,
            &self.data_classification_tools,
            &self.deployment_tools,
            &self.budget_tools,
        ];
        dimensions
            .iter()
            .all(|tools| tools.as_ref().is_none_or(|tools| tools.contains(tool_name)))
    }

    fn contains_in_principal_scope(&self, tool_name: &str) -> bool {
        self.principal_tools
            .as_ref()
            .is_none_or(|tools| tools.contains(tool_name))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ToolCatalog {
    definitions: BTreeMap<String, ToolDefinition>,
    bindings_by_tool: BTreeMap<String, ToolBinding>,
}

impl ToolCatalog {
    pub fn new<D, B>(definitions: D, bindings: B) -> Result<Self, ToolResolutionError>
    where
        D: IntoIterator<Item = ToolDefinition>,
        B: IntoIterator<Item = ToolBinding>,
    {
        let mut indexed_definitions = BTreeMap::new();
        for definition in definitions {
            definition.validate()?;
            let tool_name = definition.name.clone();
            if let Err(error) = SchemaId::parse(&definition.input_schema) {
                return Err(ToolResolutionError::InvalidToolSchemaId {
                    tool_name,
                    schema_id: definition.input_schema,
                    error,
                });
            }
            if let Some(output_schema) = &definition.output_schema
                && let Err(error) = SchemaId::parse(output_schema)
            {
                return Err(ToolResolutionError::InvalidToolSchemaId {
                    tool_name,
                    schema_id: output_schema.clone(),
                    error,
                });
            }
            if indexed_definitions.contains_key(&tool_name) {
                return Err(ToolResolutionError::DuplicateToolDefinition { tool_name });
            }
            indexed_definitions.insert(tool_name, definition);
        }

        let mut binding_ids = BTreeSet::new();
        let mut bindings_by_tool = BTreeMap::new();
        for binding in bindings {
            binding.validate()?;
            let binding_id = binding.binding_id.clone();
            let tool_name = binding.tool_name.clone();
            if !binding_ids.insert(binding_id.clone()) {
                return Err(ToolResolutionError::DuplicateToolBinding { binding_id });
            }
            if !indexed_definitions.contains_key(&tool_name) {
                return Err(ToolResolutionError::BindingWithoutDefinition {
                    binding_id,
                    tool_name,
                });
            }
            if bindings_by_tool.contains_key(&tool_name) {
                return Err(ToolResolutionError::MultipleBindingsForTool { tool_name });
            }
            bindings_by_tool.insert(tool_name, binding);
        }

        Ok(Self {
            definitions: indexed_definitions,
            bindings_by_tool,
        })
    }

    pub fn resolve(
        &self,
        scope: ToolResolutionScope,
        effective_policy_snapshot_id: impl Into<String>,
    ) -> Result<Vec<ResolvedTool>, ToolResolutionError> {
        let policy_snapshot = effective_policy_snapshot_id.into();
        let mut resolved = Vec::new();
        for (tool_name, definition) in &self.definitions {
            if !scope.allows(tool_name) {
                continue;
            }
            let binding = self.bindings_by_tool.get(tool_name).ok_or_else(|| {
                ToolResolutionError::ToolBindingMissing {
                    tool_name: tool_name.clone(),
                }
            })?;
            let definition_digest = definition.digest();
            let binding_digest = binding.digest();
            let resolved_tool_id = canonical_hash(&json!({
                "tool_name": tool_name,
                "definition_digest": definition_digest,
                "binding_digest": binding_digest,
                "policy_snapshot": policy_snapshot,
            }));
            resolved.push(ResolvedTool {
                resolved_tool_id,
                definition: definition.clone(),
                binding: binding.clone(),
                definition_digest,
                binding_digest,
                effective_policy_snapshot_id: policy_snapshot.clone(),
                allowed_for_principal: scope.contains_in_principal_scope(tool_name),
                valid_until_unix_ms: None,
            });
        }
        Ok(resolved)
    }
}

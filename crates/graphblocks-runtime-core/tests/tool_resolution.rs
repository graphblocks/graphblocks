use graphblocks_compiler::canonical::canonical_hash;
use graphblocks_runtime_core::tool::{
    BlockToolImplementation, GraphToolImplementation, McpToolImplementation,
    OpenApiToolImplementation, RemoteToolImplementation, ResolvedTool, ToolBinding, ToolCatalog,
    ToolDefinition, ToolEffect, ToolImplementation, ToolResolutionError, ToolResolutionScope,
};
use graphblocks_schema::SchemaIdError;
use serde_json::json;

fn search_definition() -> ToolDefinition {
    ToolDefinition::new(
        "knowledge.search",
        "Search internal documentation.",
        "schemas/KnowledgeSearchRequest@1",
    )
    .with_output_schema("schemas/KnowledgeSearchResult@1")
    .with_tags(["knowledge", "read"])
    .with_version("1.0.0")
}

fn ticket_definition() -> ToolDefinition {
    ToolDefinition::new(
        "ticket.create",
        "Create a support ticket.",
        "schemas/TicketCreateRequest@1",
    )
    .with_output_schema("schemas/Ticket@1")
    .with_tags(["ticket", "write"])
    .with_version("1.0.0")
}

#[test]
fn tool_resolution_intersects_scoped_capabilities() -> Result<(), ToolResolutionError> {
    let catalog = ToolCatalog::new(
        [search_definition(), ticket_definition()],
        [
            ToolBinding::new(
                "binding-search",
                "knowledge.search",
                ToolImplementation::Block(BlockToolImplementation::new("blocks.search")),
            )
            .with_effects([ToolEffect::ExternalRead]),
            ToolBinding::new(
                "binding-ticket",
                "ticket.create",
                ToolImplementation::OpenApi(OpenApiToolImplementation::new(
                    "ticket-system",
                    "createTicket",
                )),
            )
            .with_effects([ToolEffect::ExternalWrite, ToolEffect::Network]),
        ],
    )?;
    let scope = ToolResolutionScope::new()
        .with_application_tools(["knowledge.search", "ticket.create"])
        .with_graph_tools(["knowledge.search", "ticket.create"])
        .with_principal_tools(["knowledge.search"])
        .with_tenant_policy_tools(["knowledge.search", "ticket.create"])
        .with_deployment_tools(["knowledge.search", "ticket.create"]);

    let resolved = catalog.resolve(scope, "policy-snapshot-1")?;

    assert_eq!(resolved.len(), 1);
    assert_eq!(resolved[0].definition.name, "knowledge.search");
    assert_eq!(resolved[0].binding.binding_id, "binding-search");
    assert!(resolved[0].definition_digest.starts_with("sha256:"));
    assert!(resolved[0].binding_digest.starts_with("sha256:"));
    assert_eq!(
        resolved[0].effective_policy_snapshot_id,
        "policy-snapshot-1"
    );
    assert!(resolved[0].allowed_for_principal);
    Ok(())
}

#[test]
fn model_visible_tool_without_binding_is_reported() {
    let catalog = ToolCatalog::new([search_definition()], []).expect("catalog is valid");
    let scope = ToolResolutionScope::new().with_application_tools(["knowledge.search"]);

    assert_eq!(
        catalog.resolve(scope, "policy-snapshot-1"),
        Err(ToolResolutionError::ToolBindingMissing {
            tool_name: "knowledge.search".to_owned()
        }),
    );
}

#[test]
fn catalog_rejects_invalid_tool_definition_schema_ids() {
    assert_eq!(
        ToolCatalog::new(
            [ToolDefinition::new(
                "knowledge.search",
                "Search internal documentation.",
                "schemas/KnowledgeSearchRequest",
            )],
            [],
        ),
        Err(ToolResolutionError::InvalidToolSchemaId {
            tool_name: "knowledge.search".to_owned(),
            schema_id: "schemas/KnowledgeSearchRequest".to_owned(),
            error: SchemaIdError::MissingVersion,
        }),
    );
}

#[test]
fn tool_definition_validates_identity_fields() {
    assert_eq!(
        ToolDefinition::new(
            " ",
            "Search internal documentation.",
            "schemas/KnowledgeSearchRequest@1",
        )
        .validate(),
        Err(ToolResolutionError::EmptyToolDefinitionField { field: "name" })
    );
    assert_eq!(
        ToolDefinition::new("knowledge.search", "", "schemas/KnowledgeSearchRequest@1").validate(),
        Err(ToolResolutionError::EmptyToolDefinitionField {
            field: "description",
        })
    );
    assert_eq!(
        ToolDefinition::new("knowledge.search", "Search internal documentation.", " ").validate(),
        Err(ToolResolutionError::EmptyToolDefinitionField {
            field: "input_schema",
        })
    );
    assert_eq!(
        ToolCatalog::new(
            [ToolDefinition::new(
                " ",
                "Search internal documentation.",
                "schemas/KnowledgeSearchRequest@1",
            )],
            [],
        ),
        Err(ToolResolutionError::EmptyToolDefinitionField { field: "name" })
    );
}

#[test]
fn tool_binding_validates_identity_fields() {
    let valid_implementation =
        ToolImplementation::Block(BlockToolImplementation::new("blocks.search"));

    assert_eq!(
        ToolBinding::new(" ", "knowledge.search", valid_implementation.clone()).validate(),
        Err(ToolResolutionError::EmptyToolBindingField {
            field: "binding_id",
        })
    );
    assert_eq!(
        ToolBinding::new("binding-search", "", valid_implementation.clone()).validate(),
        Err(ToolResolutionError::EmptyToolBindingField { field: "tool_name" })
    );
    let mut empty_retry_policy_ref = ToolBinding::new(
        "binding-search",
        "knowledge.search",
        valid_implementation.clone(),
    );
    empty_retry_policy_ref.retry_policy_ref = Some(" ".to_owned());
    assert_eq!(
        empty_retry_policy_ref.validate(),
        Err(ToolResolutionError::EmptyToolBindingField {
            field: "retry_policy_ref",
        })
    );
    let mut empty_policy_profile_ref = ToolBinding::new(
        "binding-search",
        "knowledge.search",
        valid_implementation.clone(),
    );
    empty_policy_profile_ref.policy_profile_ref = Some("".to_owned());
    assert_eq!(
        empty_policy_profile_ref.validate(),
        Err(ToolResolutionError::EmptyToolBindingField {
            field: "policy_profile_ref",
        })
    );
    let mut empty_execution_class = ToolBinding::new(
        "binding-search",
        "knowledge.search",
        valid_implementation.clone(),
    );
    empty_execution_class.execution_class = Some(" ".to_owned());
    assert_eq!(
        empty_execution_class.validate(),
        Err(ToolResolutionError::EmptyToolBindingField {
            field: "execution_class",
        })
    );
    assert_eq!(
        ToolCatalog::new(
            [search_definition()],
            [ToolBinding::new(
                " ",
                "knowledge.search",
                valid_implementation
            )],
        ),
        Err(ToolResolutionError::EmptyToolBindingField {
            field: "binding_id",
        })
    );
}

#[test]
fn tool_implementations_validate_execution_targets() {
    assert_eq!(
        ToolImplementation::Block(BlockToolImplementation::new(" ")).validate(),
        Err(ToolResolutionError::EmptyToolImplementationField {
            kind: "block",
            field: "block",
        })
    );
    assert_eq!(
        ToolImplementation::Graph(GraphToolImplementation::new("")).validate(),
        Err(ToolResolutionError::EmptyToolImplementationField {
            kind: "graph",
            field: "graph",
        })
    );
    assert_eq!(
        ToolImplementation::Remote(RemoteToolImplementation::new(" ", "search")).validate(),
        Err(ToolResolutionError::EmptyToolImplementationField {
            kind: "remote",
            field: "connection",
        })
    );
    assert_eq!(
        ToolImplementation::Remote(RemoteToolImplementation::new("support-api", "")).validate(),
        Err(ToolResolutionError::EmptyToolImplementationField {
            kind: "remote",
            field: "operation",
        })
    );
    assert_eq!(
        ToolImplementation::Mcp(McpToolImplementation::new("", "tool.search")).validate(),
        Err(ToolResolutionError::EmptyToolImplementationField {
            kind: "mcp",
            field: "server",
        })
    );
    assert_eq!(
        ToolImplementation::Mcp(McpToolImplementation::new("support-mcp", " ")).validate(),
        Err(ToolResolutionError::EmptyToolImplementationField {
            kind: "mcp",
            field: "remote_name",
        })
    );
    assert_eq!(
        ToolImplementation::OpenApi(OpenApiToolImplementation::new(" ", "createTicket")).validate(),
        Err(ToolResolutionError::EmptyToolImplementationField {
            kind: "openapi",
            field: "connection",
        })
    );
    assert_eq!(
        ToolImplementation::OpenApi(OpenApiToolImplementation::new("ticket-system", "")).validate(),
        Err(ToolResolutionError::EmptyToolImplementationField {
            kind: "openapi",
            field: "operation_id",
        })
    );
    assert_eq!(
        ToolBinding::new(
            "binding-search",
            "knowledge.search",
            ToolImplementation::Block(BlockToolImplementation::new(" ")),
        )
        .validate(),
        Err(ToolResolutionError::EmptyToolImplementationField {
            kind: "block",
            field: "block",
        })
    );
}

#[test]
fn resolved_tool_validates_identity_fields() {
    let binding = ToolBinding::new(
        "binding-search",
        "knowledge.search",
        ToolImplementation::Block(BlockToolImplementation::new("blocks.search")),
    );

    assert_eq!(
        ResolvedTool::from_definition_and_binding(
            " ",
            search_definition(),
            binding.clone(),
            "policy-snapshot-1",
            true,
            None,
        ),
        Err(ToolResolutionError::EmptyResolvedToolField {
            field: "resolved_tool_id",
        })
    );
    assert_eq!(
        ResolvedTool::from_definition_and_binding(
            "resolved-1",
            search_definition(),
            binding.clone(),
            " ",
            true,
            None,
        ),
        Err(ToolResolutionError::EmptyResolvedToolField {
            field: "effective_policy_snapshot_id",
        })
    );

    let mut resolved = ResolvedTool::from_definition_and_binding(
        "resolved-1",
        search_definition(),
        binding,
        "policy-snapshot-1",
        true,
        None,
    )
    .expect("resolved tool is valid");
    resolved.definition_digest.clear();
    assert_eq!(
        resolved.validate(),
        Err(ToolResolutionError::EmptyResolvedToolField {
            field: "definition_digest",
        })
    );

    let catalog = ToolCatalog::new(
        [search_definition()],
        [ToolBinding::new(
            "binding-search",
            "knowledge.search",
            ToolImplementation::Block(BlockToolImplementation::new("blocks.search")),
        )],
    )
    .expect("catalog is valid");
    assert_eq!(
        catalog.resolve(ToolResolutionScope::new(), " "),
        Err(ToolResolutionError::EmptyResolvedToolField {
            field: "effective_policy_snapshot_id",
        })
    );
}

#[test]
fn resolved_tool_rejects_definition_binding_name_mismatch() {
    assert_eq!(
        graphblocks_runtime_core::tool::ResolvedTool::from_definition_and_binding(
            "resolved-1",
            search_definition(),
            ToolBinding::new(
                "binding-ticket",
                "ticket.create",
                ToolImplementation::OpenApi(OpenApiToolImplementation::new(
                    "ticket-system",
                    "createTicket",
                )),
            ),
            "policy-snapshot-1",
            true,
            None,
        ),
        Err(ToolResolutionError::BindingToolNameMismatch {
            binding_id: "binding-ticket".to_owned(),
            definition_name: "knowledge.search".to_owned(),
            binding_tool_name: "ticket.create".to_owned(),
        }),
    );
}

#[test]
fn tool_digests_are_stable_for_set_insertion_order() -> Result<(), ToolResolutionError> {
    let left_definition = ToolDefinition::new("knowledge.search", "Search.", "schemas/Search@1")
        .with_tags(["b", "a"]);
    let right_definition = ToolDefinition::new("knowledge.search", "Search.", "schemas/Search@1")
        .with_tags(["a", "b"]);
    let left_binding = ToolBinding::new(
        "binding-search",
        "knowledge.search",
        ToolImplementation::Block(BlockToolImplementation::new("blocks.search")),
    )
    .with_effects([ToolEffect::Network, ToolEffect::ExternalRead]);
    let right_binding = ToolBinding::new(
        "binding-search",
        "knowledge.search",
        ToolImplementation::Block(BlockToolImplementation::new("blocks.search")),
    )
    .with_effects([ToolEffect::ExternalRead, ToolEffect::Network]);

    let left = ToolCatalog::new([left_definition], [left_binding])?
        .resolve(ToolResolutionScope::new(), "policy-snapshot-1")?;
    let right = ToolCatalog::new([right_definition], [right_binding])?
        .resolve(ToolResolutionScope::new(), "policy-snapshot-1")?;

    assert_eq!(left[0].definition_digest, right[0].definition_digest);
    assert_eq!(left[0].binding_digest, right[0].binding_digest);
    assert_eq!(left[0].resolved_tool_id, right[0].resolved_tool_id);
    Ok(())
}

#[test]
fn tool_binding_digest_serializes_effects_by_canonical_string_order() {
    let binding = ToolBinding::new(
        "binding-ticket",
        "ticket.create",
        ToolImplementation::Block(BlockToolImplementation::new("blocks.ticket")),
    )
    .with_effects([
        ToolEffect::ExternalWrite,
        ToolEffect::Network,
        ToolEffect::Destructive,
    ]);

    let expected = canonical_hash(&json!({
        "binding_id": "binding-ticket",
        "tool_name": "ticket.create",
        "implementation": {
            "kind": "block",
            "block": "blocks.ticket",
            "input_mapping": {},
            "output_mapping": {},
        },
        "effects": ["destructive", "external_write", "network"],
        "approval": "policy",
        "idempotency": "optional",
        "cancellation": "cooperative",
        "result_mode": "value",
        "timeout_ms": null,
        "retry_policy_ref": null,
        "policy_profile_ref": null,
        "execution_class": null,
    }));

    assert_eq!(binding.digest(), expected);
}

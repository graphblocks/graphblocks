use graphblocks_runtime_core::tool::{
    BlockToolImplementation, OpenApiToolImplementation, ToolBinding, ToolCatalog, ToolDefinition,
    ToolEffect, ToolImplementation, ToolResolutionError, ToolResolutionScope,
};
use graphblocks_schema::SchemaIdError;

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

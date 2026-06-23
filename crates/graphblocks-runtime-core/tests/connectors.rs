use graphblocks_runtime_core::connectors::{
    CapabilityError, ConnectionSpec, InMemoryPromptRegistry, PromptRef, PromptRegistryError,
    PromptTemplate, SecretRef, ensure_capabilities,
};
use serde_json::json;

#[test]
fn connection_spec_preserves_secret_reference_without_resolved_secret() {
    let connection = ConnectionSpec::new("answer-model", "model", "openai")
        .with_config("model", json!("gpt-test"))
        .with_config("baseUrl", json!("https://api.example.invalid/v1"))
        .with_credentials(SecretRef::new("secret://env/OPENAI_API_KEY").with_version("2026-06"));

    let value = connection.safe_config_value();

    assert_eq!(value["connection_id"], "answer-model");
    assert_eq!(value["kind"], "model");
    assert_eq!(value["provider"], "openai");
    assert_eq!(value["credentials"]["uri"], "secret://env/OPENAI_API_KEY");
    assert_eq!(value["credentials"]["version"], "2026-06");
    assert!(value["credentials"].get("value").is_none());
}

#[test]
fn capability_negotiation_reports_missing_requirement_before_execution() {
    let error = ensure_capabilities(
        "company-knowledge",
        ["generation_namespace", "non_atomic_publish"],
        ["atomic_alias_swap"],
    )
    .expect_err("missing connection capability should fail bind-time negotiation");

    assert_eq!(
        error,
        CapabilityError {
            connection_id: "company-knowledge".to_owned(),
            missing: vec!["atomic_alias_swap".to_owned()],
            supported: vec![
                "generation_namespace".to_owned(),
                "non_atomic_publish".to_owned()
            ],
        }
    );
}

#[test]
fn in_memory_prompt_registry_resolves_labels_to_immutable_template_digest() {
    let mut registry = InMemoryPromptRegistry::new();
    registry.insert(
        PromptTemplate::new("support.reply", "2026-06-23", "Answer with citations.")
            .with_label("production")
            .with_metadata("owner", json!("support")),
    );
    registry.insert(PromptTemplate::new(
        "support.reply",
        "2026-06-24",
        "Answer with citations and escalation hints.",
    ));

    let resolved = registry
        .resolve(PromptRef::new("support.reply").with_label("production"))
        .expect("label should resolve to a pinned template");
    let by_version = registry
        .resolve(PromptRef::new("support.reply").with_version("2026-06-23"))
        .expect("version should resolve to the same template");

    assert_eq!(resolved.version, "2026-06-23");
    assert_eq!(resolved.content_digest(), by_version.content_digest());
    assert_eq!(
        registry.list_versions("support.reply"),
        vec!["2026-06-23".to_owned(), "2026-06-24".to_owned()]
    );
}

#[test]
fn in_memory_prompt_registry_rejects_ambiguous_or_unknown_references() {
    let mut registry = InMemoryPromptRegistry::new();
    registry.insert(PromptTemplate::new("support.reply", "v1", "first"));
    registry.insert(PromptTemplate::new("support.reply", "v2", "second"));

    assert_eq!(
        registry.resolve(PromptRef::new("support.reply")),
        Err(PromptRegistryError::AmbiguousPromptRef {
            name: "support.reply".to_owned()
        })
    );
    assert_eq!(
        registry.resolve(PromptRef::new("support.reply").with_label("missing")),
        Err(PromptRegistryError::PromptNotFound {
            name: "support.reply".to_owned(),
            selector: "label:missing".to_owned()
        })
    );
}

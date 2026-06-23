use graphblocks_runtime_core::connectors::{
    CapabilityError, ConnectionSpec, InMemoryPromptRegistry, InMemorySecretProvider, PromptRef,
    PromptRegistryError, PromptTemplate, SecretProviderError, SecretRef, ensure_capabilities,
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

#[test]
fn in_memory_secret_provider_resolves_value_with_redacted_debug_and_access_metadata() {
    let mut provider = InMemorySecretProvider::new("env");
    provider.insert(
        SecretRef::new("secret://env/OPENAI_API_KEY").with_version("2026-06"),
        "sk-test-123",
    );

    let resolved = provider
        .resolve(
            &SecretRef::new("secret://env/OPENAI_API_KEY").with_version("2026-06"),
            "runtime-admission",
        )
        .expect("secret should resolve");

    assert_eq!(resolved.expose_secret(), "sk-test-123");
    assert_eq!(resolved.access.provider_kind, "env");
    assert_eq!(resolved.access.secret_uri, "secret://env/OPENAI_API_KEY");
    assert_eq!(resolved.access.version.as_deref(), Some("2026-06"));
    assert_eq!(resolved.access.requester, "runtime-admission");
    assert!(format!("{resolved:?}").contains("<redacted>"));
    assert!(!format!("{resolved:?}").contains("sk-test-123"));
}

#[test]
fn in_memory_secret_provider_rejects_missing_and_unsupported_secret_refs() {
    let provider = InMemorySecretProvider::new("env");

    assert_eq!(
        provider.resolve(&SecretRef::new("env:OPENAI_API_KEY"), "runtime-admission"),
        Err(SecretProviderError::UnsupportedUri {
            uri: "env:OPENAI_API_KEY".to_owned(),
        })
    );
    assert_eq!(
        provider.resolve(
            &SecretRef::new("secret://env/OPENAI_API_KEY"),
            "runtime-admission"
        ),
        Err(SecretProviderError::NotFound {
            uri: "secret://env/OPENAI_API_KEY".to_owned(),
            version: None,
        })
    );
}

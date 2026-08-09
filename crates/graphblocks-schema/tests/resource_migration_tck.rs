use graphblocks_schema::{ResourceMigrationFailure, migrate_resource, resource_schema_errors};
use serde_json::{Value, json};
use std::error::Error;

const MIGRATION_CASES: &str = include_str!("fixtures/migration.json");

#[test]
fn rust_resource_migration_matches_shared_cases() -> Result<(), Box<dyn Error>> {
    let cases = serde_json::from_str::<Value>(MIGRATION_CASES)?;
    let cases = cases
        .as_array()
        .ok_or("migration TCK root must be an array")?;

    for case in cases {
        let name = case
            .get("name")
            .and_then(Value::as_str)
            .ok_or("migration TCK case is missing string field name")?;
        let document = case
            .get("document")
            .ok_or_else(|| format!("{name} is missing document"))?;
        let expected = case
            .get("expected")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("{name} is missing expected object"))?;
        let source = document.clone();

        if let Some(expected_error) = expected.get("error") {
            let expected_error = expected_error
                .as_object()
                .ok_or_else(|| format!("{name} expected error must be an object"))?;
            let error = migrate_resource(document)
                .expect_err("an expected migration failure must not succeed");
            let ResourceMigrationFailure::Migration(error) = error else {
                return Err(format!("{name} failed because an embedded schema was invalid").into());
            };
            let expected_code = expected_error
                .get("code")
                .and_then(Value::as_str)
                .ok_or_else(|| format!("{name} expected error is missing code"))?;
            let expected_path = expected_error
                .get("path")
                .and_then(Value::as_str)
                .ok_or_else(|| format!("{name} expected error is missing path"))?;
            assert_eq!(error.code, expected_code, "{name}");
            assert_eq!(error.path, expected_path, "{name}");
        } else {
            let expected_document = expected
                .get("document")
                .ok_or_else(|| format!("{name} is missing expected document"))?;
            let migrated = migrate_resource(document)?;
            assert_eq!(&migrated, expected_document, "{name}");
            assert_eq!(migrate_resource(document)?, migrated, "{name}");
            assert!(resource_schema_errors(&migrated)?.is_empty(), "{name}");
        }
        assert_eq!(document, &source, "{name} mutated its source");
    }
    Ok(())
}

#[test]
fn plugin_migration_completes_new_stable_block_fields() -> Result<(), Box<dyn Error>> {
    let document = json!({
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "PluginManifest",
        "metadata": {"name": "example.plugin"},
        "spec": {
            "pluginId": "example.plugin",
            "version": "1.0.0",
            "blocks": [{"typeId": "example.block@1"}]
        }
    });

    let migrated = migrate_resource(&document)?;

    assert_eq!(migrated["spec"]["blocks"][0]["capabilities"], json!([]));
    assert_eq!(
        migrated["spec"]["blocks"][0]["configSchema"],
        json!({"type": "object"})
    );
    assert!(resource_schema_errors(&migrated)?.is_empty());
    assert_eq!(document["apiVersion"], "graphblocks.ai/v1alpha1");
    Ok(())
}

#[test]
fn known_resources_cannot_bypass_stable_target_validation() -> Result<(), Box<dyn Error>> {
    for (document, expected_code) in [
        (
            json!({
                "apiVersion": "graphblocks.ai/v1",
                "kind": "Graph",
                "metadata": {"name": "invalid"},
                "spec": {"nodes": {}, "previewOnly": true}
            }),
            "GB0002",
        ),
        (
            json!({
                "apiVersion": "graphblocks.ai/v1",
                "kind": "PluginManifest",
                "metadata": {"name": "invalid"},
                "spec": {
                    "pluginId": "invalid",
                    "version": "1.0.0",
                    "blocks": [{"typeId": "example.block@1"}]
                }
            }),
            "GB2018",
        ),
        (
            json!({
                "apiVersion": "graphblocks.ai/v1alpha3",
                "kind": "Graph",
                "metadata": {"name": "invalid-alpha"},
                "spec": {"nodes": {}, "state": {"previewOnly": true}}
            }),
            "GB0002",
        ),
        (
            json!({
                "apiVersion": "graphblocks.ai/v1alpha1",
                "kind": "PluginManifest",
                "metadata": {"name": "invalid-alpha"},
                "spec": {
                    "pluginId": "invalid-alpha",
                    "version": "1.0.0",
                    "blocks": [],
                    "previewOnly": true
                }
            }),
            "GB2018",
        ),
    ] {
        let error = migrate_resource(&document)
            .expect_err("an invalid stable resource must fail migration");
        let migration_error = error
            .migration_error()
            .expect("checked-in schemas must be available");
        assert_eq!(migration_error.code, expected_code);
        assert_ne!(migration_error.path, "$");
        let serialized = serde_json::to_value(migration_error)?;
        let mut serialized_fields = serialized
            .as_object()
            .ok_or("serialized migration error must be an object")?
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>();
        serialized_fields.sort_unstable();
        assert_eq!(serialized_fields, ["code", "message", "path"]);
    }
    Ok(())
}

#[test]
fn unrelated_resource_kinds_are_identity_copies() -> Result<(), Box<dyn Error>> {
    let document = json!({
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "application"}
    });

    let migrated = migrate_resource(&document)?;

    assert_eq!(migrated, document);
    Ok(())
}

#[test]
fn migration_rejects_excessive_depth_before_cloning() {
    let mut document = json!({"leaf": true});
    for _ in 0..65 {
        document = json!({"nested": document});
    }

    let error = migrate_resource(&document)
        .expect_err("an excessively deep resource must fail before migration");
    let migration_error = error
        .migration_error()
        .expect("depth rejection must be a structured migration error");

    assert_eq!(migration_error.code, "GB0014");
    assert_eq!(migration_error.path.matches(".nested").count(), 65);
}

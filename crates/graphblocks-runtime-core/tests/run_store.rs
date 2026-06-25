use graphblocks_runtime_core::run_store::{
    InMemoryRunStore, PatchOperation, RunDeploymentProvenance, RunStatus, RunStoreError,
    SqliteRunStore, StatePatch,
};
use serde_json::json;

#[test]
fn run_store_allocates_monotonic_run_snapshots() -> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();

    let first = store.create_run("sha256:one", json!({"message": "hello"}));
    let second = store.create_run("sha256:two", json!({}));

    assert_eq!(first.run_id, "run-000001");
    assert_eq!(first.sequence, 1);
    assert_eq!(first.graph_hash, "sha256:one");
    assert_eq!(first.inputs, json!({"message": "hello"}));
    assert_eq!(first.status, RunStatus::Created);
    assert_eq!(first.state, json!({}));
    assert_eq!(first.state_revision, 0);
    assert_eq!(second.run_id, "run-000002");
    assert_eq!(second.sequence, 2);
    assert_eq!(store.get_run("run-000001")?, first);
    Ok(())
}

#[test]
fn run_store_records_deployment_provenance_and_preserves_it_across_mutations()
-> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();
    let provenance = RunDeploymentProvenance::new()
        .with_release_digest("sha256:release")
        .with_deployment_revision_id("rev-1")
        .with_physical_plan_hash("sha256:physical")
        .with_release_signature_digest("sha256:signature");

    let record = store.create_run_with_provenance("sha256:test", json!({}), provenance.clone());
    let patched = store.patch_state(
        &record.run_id,
        StatePatch::new(Some(0)).with(PatchOperation::set(["step"], json!(1))),
    )?;
    let running = store.set_status(&record.run_id, RunStatus::Running)?;

    assert_eq!(
        record.deployment_provenance.canonical_value(),
        json!({
            "release_digest": "sha256:release",
            "deployment_revision_id": "rev-1",
            "physical_plan_hash": "sha256:physical",
            "release_signature_digest": "sha256:signature",
        })
    );
    assert_eq!(patched.deployment_provenance, provenance);
    assert_eq!(running.deployment_provenance, provenance);
    Ok(())
}

#[test]
fn run_store_applies_state_patch_operations_with_revision_cas() -> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();
    let record = store.create_run("sha256:test", json!({}));

    let updated = store.patch_state(
        &record.run_id,
        StatePatch::new(Some(0))
            .with(PatchOperation::set(["conversation", "turns"], json!(1)))
            .with(PatchOperation::merge(
                ["conversation"],
                json!({"topic": "support"}),
            ))
            .with(PatchOperation::append(["messages"], json!("hello")))
            .with(PatchOperation::increment(["usage", "tokens"], 7))
            .with(PatchOperation::remove(["conversation", "topic"])),
    )?;

    assert_eq!(updated.state_revision, 1);
    assert_eq!(
        updated.state,
        json!({
            "conversation": {"turns": 1},
            "messages": ["hello"],
            "usage": {"tokens": 7}
        }),
    );
    Ok(())
}

#[test]
fn run_store_rejects_stale_state_revision_and_keeps_state() -> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();
    let record = store.create_run("sha256:test", json!({}));

    store.patch_state(
        &record.run_id,
        StatePatch::new(Some(0)).with(PatchOperation::set(["count"], json!(1))),
    )?;

    assert_eq!(
        store.patch_state(
            &record.run_id,
            StatePatch::new(Some(0)).with(PatchOperation::set(["count"], json!(2))),
        ),
        Err(RunStoreError::StateConflict {
            run_id: record.run_id.clone(),
            expected_revision: 0,
            current_revision: 1,
        }),
    );
    assert_eq!(store.get_run(&record.run_id)?.state, json!({"count": 1}));
    Ok(())
}

#[test]
fn run_store_rejects_state_patch_and_status_after_terminal() -> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();
    let record = store.create_run("sha256:test", json!({}));

    store.set_status(&record.run_id, RunStatus::Running)?;
    let terminal = store.set_status(&record.run_id, RunStatus::Completed)?;
    assert_eq!(terminal.status, RunStatus::Completed);

    assert_eq!(
        store.patch_state(
            &record.run_id,
            StatePatch::new(Some(0)).with(PatchOperation::set(["late"], json!(true))),
        ),
        Err(RunStoreError::StatePatchAfterTerminal {
            run_id: record.run_id.clone(),
            status: RunStatus::Completed,
        }),
    );
    assert_eq!(
        store.set_status(&record.run_id, RunStatus::Failed),
        Err(RunStoreError::StatusAfterTerminal {
            run_id: record.run_id,
            status: RunStatus::Completed,
        }),
    );
    Ok(())
}

#[test]
fn sqlite_run_store_persists_runs_across_reopen() -> Result<(), String> {
    let mut path = std::env::temp_dir();
    path.push(format!(
        "graphblocks-sqlite-run-store-{}-persist.sqlite3",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);

    {
        let mut store = SqliteRunStore::open(&path).map_err(|error| format!("{error:?}"))?;
        let provenance = RunDeploymentProvenance::new()
            .with_release_digest("sha256:release")
            .with_deployment_revision_id("rev-1")
            .with_physical_plan_hash("sha256:physical")
            .with_release_signature_digest("sha256:signature");
        let first = store
            .create_run_with_provenance("sha256:one", json!({"message": "hello"}), provenance)
            .map_err(|error| format!("{error:?}"))?;
        let second = store
            .create_run("sha256:two", json!({}))
            .map_err(|error| format!("{error:?}"))?;
        store
            .set_status(&first.run_id, RunStatus::Running)
            .map_err(|error| format!("{error:?}"))?;

        assert_eq!(first.run_id, "run-000001");
        assert_eq!(second.run_id, "run-000002");
    }

    let store = SqliteRunStore::open(&path).map_err(|error| format!("{error:?}"))?;
    let first = store
        .get_run("run-000001")
        .map_err(|error| format!("{error:?}"))?;
    assert_eq!(first.sequence, 1);
    assert_eq!(first.graph_hash, "sha256:one");
    assert_eq!(first.inputs, json!({"message": "hello"}));
    assert_eq!(first.status, RunStatus::Running);
    assert_eq!(first.state, json!({}));
    assert_eq!(first.state_revision, 0);
    assert_eq!(
        first.deployment_provenance.canonical_value(),
        json!({
            "release_digest": "sha256:release",
            "deployment_revision_id": "rev-1",
            "physical_plan_hash": "sha256:physical",
            "release_signature_digest": "sha256:signature",
        })
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn sqlite_run_store_applies_state_patch_with_revision_cas() -> Result<(), String> {
    let mut store = SqliteRunStore::open_in_memory().map_err(|error| format!("{error:?}"))?;
    let record = store
        .create_run("sha256:test", json!({}))
        .map_err(|error| format!("{error:?}"))?;

    let updated = store
        .patch_state(
            &record.run_id,
            StatePatch::new(Some(0))
                .with(PatchOperation::set(["conversation", "turns"], json!(1)))
                .with(PatchOperation::append(["messages"], json!("hello"))),
        )
        .map_err(|error| format!("{error:?}"))?;

    assert_eq!(updated.state_revision, 1);
    assert_eq!(
        updated.state,
        json!({
            "conversation": {"turns": 1},
            "messages": ["hello"],
        }),
    );
    assert_eq!(
        store.patch_state(
            &record.run_id,
            StatePatch::new(Some(0)).with(PatchOperation::set(["late"], json!(true))),
        ),
        Err(RunStoreError::StateConflict {
            run_id: record.run_id,
            expected_revision: 0,
            current_revision: 1,
        }),
    );
    Ok(())
}

#[test]
fn sqlite_run_store_rejects_writes_after_terminal() -> Result<(), String> {
    let mut store = SqliteRunStore::open_in_memory().map_err(|error| format!("{error:?}"))?;
    let record = store
        .create_run("sha256:test", json!({}))
        .map_err(|error| format!("{error:?}"))?;

    store
        .set_status(&record.run_id, RunStatus::Running)
        .map_err(|error| format!("{error:?}"))?;
    store
        .set_status(&record.run_id, RunStatus::Completed)
        .map_err(|error| format!("{error:?}"))?;

    assert_eq!(
        store.patch_state(
            &record.run_id,
            StatePatch::new(Some(0)).with(PatchOperation::set(["late"], json!(true))),
        ),
        Err(RunStoreError::StatePatchAfterTerminal {
            run_id: record.run_id.clone(),
            status: RunStatus::Completed,
        }),
    );
    assert_eq!(
        store.set_status(&record.run_id, RunStatus::Failed),
        Err(RunStoreError::StatusAfterTerminal {
            run_id: record.run_id,
            status: RunStatus::Completed,
        }),
    );
    Ok(())
}

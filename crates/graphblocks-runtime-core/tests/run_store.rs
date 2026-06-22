use graphblocks_runtime_core::run_store::{
    InMemoryRunStore, PatchOperation, RunStatus, RunStoreError, StatePatch,
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

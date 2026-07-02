use graphblocks_runtime_core::{
    evaluation::ModelVisibleToolRef,
    run_store::{
        InMemoryRunStore, PatchOperation, RunDeploymentProvenance, RunInvocationMode,
        RunInvocationResponse, RunStatus, RunStoreError, SqliteRunStore, StatePatch,
    },
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
    assert_eq!(first.invocation_mode, RunInvocationMode::Sync);
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
fn run_store_records_invocation_mode_and_builds_accepted_handle() -> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();

    let accepted = store.create_run_with_invocation_mode(
        "sha256:accepted",
        json!({"task": "code"}),
        RunInvocationMode::Accepted,
    );
    let background = store.create_run_with_invocation_mode(
        "sha256:background",
        json!({"task": "ingest"}),
        RunInvocationMode::Background,
    );
    let handle = RunInvocationResponse::from_accepted_run(&accepted, "/v1", "evt_000000")
        .expect("accepted run response is valid");

    assert_eq!(accepted.invocation_mode, RunInvocationMode::Accepted);
    assert_eq!(background.invocation_mode, RunInvocationMode::Background);
    assert_eq!(handle.run_id, accepted.run_id);
    assert_eq!(handle.status, "accepted");
    assert_eq!(handle.mode, RunInvocationMode::Accepted);
    assert_eq!(handle.event_stream, "/v1/runs/run-000001/events");
    assert_eq!(handle.websocket, "/v1/runs/run-000001/ws");
    assert_eq!(handle.cancel, "/v1/runs/run-000001/cancel");
    assert_eq!(handle.initial_cursor, "evt_000000");
    Ok(())
}

#[test]
fn run_invocation_response_rejects_sync_or_empty_cursor() {
    let mut store = InMemoryRunStore::new();
    let sync = store.create_run("sha256:sync", json!({}));
    let accepted = store.create_run_with_invocation_mode(
        "sha256:accepted",
        json!({}),
        RunInvocationMode::Accepted,
    );

    assert_eq!(
        RunInvocationResponse::from_accepted_run(&sync, "/v1", "evt_000000"),
        Err(RunStoreError::InvalidInvocationMode {
            run_id: sync.run_id,
            invocation_mode: RunInvocationMode::Sync,
        })
    );
    assert_eq!(
        RunInvocationResponse::from_accepted_run(&accepted, "/v1", " "),
        Err(RunStoreError::EmptyField {
            field: "initial_cursor",
        })
    );
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
fn run_store_records_model_visible_tools_and_preserves_them_across_mutations()
-> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();
    let ticket_tool = model_visible_tool("ticket.create", "resolved-ticket", false);
    let search_tool = model_visible_tool("knowledge.search", "resolved-search", true);

    let record = store.create_run_with_invocation_provenance(
        "sha256:test",
        json!({}),
        RunDeploymentProvenance::new(),
        vec![ticket_tool.clone(), search_tool.clone()],
    );
    let patched = store.patch_state(
        &record.run_id,
        StatePatch::new(Some(0)).with(PatchOperation::set(["step"], json!(1))),
    )?;
    let running = store.set_status(&record.run_id, RunStatus::Running)?;

    assert_eq!(record.model_visible_tools, vec![search_tool, ticket_tool]);
    assert_eq!(patched.model_visible_tools, record.model_visible_tools);
    assert_eq!(running.model_visible_tools, record.model_visible_tools);
    Ok(())
}

#[test]
fn run_store_records_model_visible_tools_after_run_creation() -> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();
    let record = store.create_run("sha256:test", json!({}));
    let ticket_tool = model_visible_tool("ticket.create", "resolved-ticket", false);
    let search_tool = model_visible_tool("knowledge.search", "resolved-search", true);

    let updated = store.record_model_visible_tools(
        &record.run_id,
        vec![ticket_tool.clone(), search_tool.clone()],
    )?;

    assert_eq!(updated.model_visible_tools, vec![search_tool, ticket_tool]);
    assert_eq!(updated.state_revision, 0);
    assert_eq!(store.get_run(&record.run_id)?, updated);
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
            run_id: record.run_id.clone(),
            status: RunStatus::Completed,
        }),
    );
    assert_eq!(
        store.record_model_visible_tools(
            &record.run_id,
            vec![model_visible_tool(
                "knowledge.search",
                "resolved-search",
                true
            )],
        ),
        Err(RunStoreError::InvocationProvenanceAfterTerminal {
            run_id: record.run_id,
            status: RunStatus::Completed,
        }),
    );
    Ok(())
}

#[test]
fn run_store_rejects_model_visible_tools_after_terminal() -> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();
    let record = store.create_run("sha256:test", json!({}));

    store.set_status(&record.run_id, RunStatus::Completed)?;

    assert_eq!(
        store.record_model_visible_tools(
            &record.run_id,
            vec![model_visible_tool(
                "knowledge.search",
                "resolved-search",
                true
            )],
        ),
        Err(RunStoreError::InvocationProvenanceAfterTerminal {
            run_id: record.run_id,
            status: RunStatus::Completed,
        }),
    );
    Ok(())
}

#[test]
fn run_store_supports_durable_async_lifecycle_statuses() -> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();
    let record = store.create_run("sha256:test", json!({}));

    for status in [
        RunStatus::Admitted,
        RunStatus::Running,
        RunStatus::WaitingInput,
        RunStatus::WaitingApproval,
        RunStatus::WaitingReview,
        RunStatus::WaitingCallback,
        RunStatus::PausedBudget,
        RunStatus::PausedPolicy,
        RunStatus::PausedOperator,
        RunStatus::Resuming,
    ] {
        assert_eq!(store.set_status(&record.run_id, status)?.status, status);
    }

    let expired = store.set_status(&record.run_id, RunStatus::Expired)?;
    assert_eq!(expired.status, RunStatus::Expired);
    assert!(expired.status.is_terminal());
    assert_eq!(
        store.set_status(&record.run_id, RunStatus::Running),
        Err(RunStoreError::StatusAfterTerminal {
            run_id: record.run_id,
            status: RunStatus::Expired,
        }),
    );
    Ok(())
}

#[test]
fn sqlite_run_store_persists_durable_async_lifecycle_statuses() -> Result<(), String> {
    let mut store = SqliteRunStore::open_in_memory().map_err(|error| format!("{error:?}"))?;
    let waiting = store
        .create_run("sha256:waiting", json!({}))
        .map_err(|error| format!("{error:?}"))?;
    let paused = store
        .create_run("sha256:paused", json!({}))
        .map_err(|error| format!("{error:?}"))?;
    let expired = store
        .create_run("sha256:expired", json!({}))
        .map_err(|error| format!("{error:?}"))?;

    store
        .set_status(&waiting.run_id, RunStatus::WaitingCallback)
        .map_err(|error| format!("{error:?}"))?;
    store
        .set_status(&paused.run_id, RunStatus::PausedBudget)
        .map_err(|error| format!("{error:?}"))?;
    store
        .set_status(&expired.run_id, RunStatus::Expired)
        .map_err(|error| format!("{error:?}"))?;

    assert_eq!(
        store
            .get_run(&waiting.run_id)
            .map_err(|error| format!("{error:?}"))?
            .status,
        RunStatus::WaitingCallback
    );
    assert_eq!(
        store
            .get_run(&paused.run_id)
            .map_err(|error| format!("{error:?}"))?
            .status,
        RunStatus::PausedBudget
    );
    assert_eq!(
        store
            .get_run(&expired.run_id)
            .map_err(|error| format!("{error:?}"))?
            .status,
        RunStatus::Expired
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
            .create_run_with_invocation_provenance(
                "sha256:one",
                json!({"message": "hello"}),
                provenance,
                vec![
                    model_visible_tool("ticket.create", "resolved-ticket", false),
                    model_visible_tool("knowledge.search", "resolved-search", true),
                ],
            )
            .map_err(|error| format!("{error:?}"))?;
        let second = store
            .create_run_with_invocation_mode("sha256:two", json!({}), RunInvocationMode::Background)
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
    assert_eq!(first.invocation_mode, RunInvocationMode::Sync);
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
    assert_eq!(
        first.model_visible_tools,
        vec![
            model_visible_tool("knowledge.search", "resolved-search", true),
            model_visible_tool("ticket.create", "resolved-ticket", false),
        ]
    );
    let second = store
        .get_run("run-000002")
        .map_err(|error| format!("{error:?}"))?;
    assert_eq!(second.invocation_mode, RunInvocationMode::Background);

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn sqlite_run_store_records_model_visible_tools_after_run_creation() -> Result<(), String> {
    let mut store = SqliteRunStore::open_in_memory().map_err(|error| format!("{error:?}"))?;
    let record = store
        .create_run("sha256:test", json!({}))
        .map_err(|error| format!("{error:?}"))?;
    let ticket_tool = model_visible_tool("ticket.create", "resolved-ticket", false);
    let search_tool = model_visible_tool("knowledge.search", "resolved-search", true);

    let updated = store
        .record_model_visible_tools(
            &record.run_id,
            vec![ticket_tool.clone(), search_tool.clone()],
        )
        .map_err(|error| format!("{error:?}"))?;

    assert_eq!(updated.model_visible_tools, vec![search_tool, ticket_tool]);
    assert_eq!(updated.state_revision, 0);
    assert_eq!(
        store
            .get_run(&record.run_id)
            .map_err(|error| format!("{error:?}"))?,
        updated
    );
    Ok(())
}

fn model_visible_tool(
    tool_name: impl Into<String>,
    resolved_tool_id: impl Into<String>,
    allowed_for_principal: bool,
) -> ModelVisibleToolRef {
    ModelVisibleToolRef {
        tool_name: tool_name.into(),
        resolved_tool_id: resolved_tool_id.into(),
        definition_digest: "sha256:definition".to_owned(),
        binding_digest: "sha256:binding".to_owned(),
        effective_policy_snapshot_id: "policy-snapshot-1".to_owned(),
        allowed_for_principal,
        valid_until_unix_ms: Some(1_783_036_800_000),
    }
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

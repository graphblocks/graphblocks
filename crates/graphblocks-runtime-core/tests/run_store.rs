use graphblocks_runtime_core::{
    evaluation::ModelVisibleToolRef,
    run_store::{
        InMemoryRunStore, PatchOperation, ProductionRunProvenanceDiagnostic,
        RunDeploymentProvenance, RunInvocationMode, RunInvocationResponse,
        RunInvocationRouteConfig, RunInvocationRouteDiagnostic, RunLifetime, RunOwnershipLease,
        RunStatus, RunStatusSnapshot, RunStoreError, RunWaitReason, SqliteRunStore, StatePatch,
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
fn run_status_snapshot_reports_waiting_callback_and_active_operations() -> Result<(), RunStoreError>
{
    let mut store = InMemoryRunStore::new();
    let record = store.create_run_with_provenance(
        "sha256:graph",
        json!({"task": "ci"}),
        RunDeploymentProvenance::new().with_release_digest("release-2026-07-02"),
    );
    let waiting = store.set_status(&record.run_id, RunStatus::WaitingCallback)?;

    let snapshot = RunStatusSnapshot::from_run(
        &waiting,
        "evt_000042",
        1_000,
        1_500,
        None,
        vec![RunWaitReason::callback("op-ci-1", Some("waitCI"))?],
        vec!["op-ci-1".to_owned()],
    )?;

    assert_eq!(snapshot.run_id, waiting.run_id);
    assert_eq!(snapshot.state, RunStatus::WaitingCallback);
    assert_eq!(snapshot.release_id, "release-2026-07-02");
    assert_eq!(snapshot.last_cursor, "evt_000042");
    assert_eq!(snapshot.started_at_unix_ms, 1_000);
    assert_eq!(snapshot.updated_at_unix_ms, 1_500);
    assert_eq!(snapshot.completed_at_unix_ms, None);
    assert_eq!(snapshot.waiting_on.len(), 1);
    assert_eq!(
        snapshot.waiting_on[0].operation_id.as_deref(),
        Some("op-ci-1")
    );
    assert_eq!(snapshot.waiting_on[0].node_id.as_deref(), Some("waitCI"));
    assert_eq!(snapshot.active_operations, vec!["op-ci-1"]);
    Ok(())
}

#[test]
fn waiting_callback_snapshot_requires_callback_wait_metadata() -> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();
    let record = store.create_run("sha256:graph", json!({"task": "ci"}));
    let waiting = store.set_status(&record.run_id, RunStatus::WaitingCallback)?;

    assert_eq!(
        RunStatusSnapshot::from_run(
            &waiting,
            "evt_000042",
            1_000,
            1_500,
            None,
            vec![],
            vec!["op-ci-1".to_owned()],
        ),
        Err(RunStoreError::InvalidRunStatusSnapshot {
            run_id: waiting.run_id.clone(),
            reason: "waiting_callback requires callback wait reason",
        })
    );
    assert_eq!(
        RunStatusSnapshot::from_run(
            &waiting,
            "evt_000043",
            1_000,
            1_600,
            None,
            vec![RunWaitReason::callback("op-ci-1", Some("waitCI"))?],
            vec![],
        ),
        Err(RunStoreError::InvalidRunStatusSnapshot {
            run_id: waiting.run_id,
            reason: "waiting_callback operation must be active",
        })
    );
    Ok(())
}

#[test]
fn run_status_snapshot_rejects_duplicate_active_operations() -> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();
    let record = store.create_run("sha256:graph", json!({"task": "ci"}));
    let waiting = store.set_status(&record.run_id, RunStatus::WaitingCallback)?;

    assert_eq!(
        RunStatusSnapshot::from_run(
            &waiting,
            "evt_000044",
            1_000,
            1_600,
            None,
            vec![RunWaitReason::callback("op-ci-1", Some("waitCI"))?],
            vec!["op-ci-1".to_owned(), "op-ci-1".to_owned()],
        ),
        Err(RunStoreError::InvalidRunStatusSnapshot {
            run_id: waiting.run_id,
            reason: "active operations must not contain duplicates",
        })
    );
    Ok(())
}

#[test]
fn run_status_snapshot_rejects_wait_reasons_for_active_states() -> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();
    let record = store.create_run("sha256:graph", json!({"task": "ci"}));
    let running = store.set_status(&record.run_id, RunStatus::Running)?;

    assert_eq!(
        RunStatusSnapshot::from_run(
            &running,
            "evt_000045",
            1_000,
            1_600,
            None,
            vec![RunWaitReason::callback("op-ci-1", Some("waitCI"))?],
            vec!["op-ci-1".to_owned()],
        ),
        Err(RunStoreError::InvalidRunStatusSnapshot {
            run_id: running.run_id,
            reason: "active run cannot expose wait reasons",
        })
    );
    Ok(())
}

#[test]
fn run_status_snapshot_requires_matching_wait_reason_for_paused_or_waiting_states()
    -> Result<(), RunStoreError>
{
    let mut store = InMemoryRunStore::new();
    let approval_record = store.create_run("sha256:approval", json!({}));
    let waiting_approval =
        store.set_status(&approval_record.run_id, RunStatus::WaitingApproval)?;

    assert_eq!(
        RunStatusSnapshot::from_run(
            &waiting_approval,
            "evt_000050",
            1_000,
            1_500,
            None,
            vec![],
            vec![],
        ),
        Err(RunStoreError::InvalidRunStatusSnapshot {
            run_id: waiting_approval.run_id,
            reason: "waiting_approval requires approval wait reason",
        })
    );

    let budget_record = store.create_run("sha256:budget", json!({}));
    let paused_budget = store.set_status(&budget_record.run_id, RunStatus::PausedBudget)?;
    let snapshot = RunStatusSnapshot::from_run(
        &paused_budget,
        "evt_000060",
        2_000,
        2_500,
        None,
        vec![RunWaitReason::budget("quota_exhausted")?],
        vec![],
    )?;

    assert_eq!(snapshot.state, RunStatus::PausedBudget);
    assert_eq!(snapshot.waiting_on.len(), 1);
    assert_eq!(snapshot.waiting_on[0].message.as_deref(), Some("quota_exhausted"));

    let callback_delivery_record = store.create_run("sha256:callback-delivery", json!({}));
    let paused_callback_delivery =
        store.set_status(&callback_delivery_record.run_id, RunStatus::PausedCallbackDelivery)?;
    assert_eq!(
        RunStatusSnapshot::from_run(
            &paused_callback_delivery,
            "evt_000070",
            3_000,
            3_500,
            None,
            vec![],
            vec![],
        ),
        Err(RunStoreError::InvalidRunStatusSnapshot {
            run_id: paused_callback_delivery.run_id.clone(),
            reason: "paused_callback_delivery requires callback delivery wait reason",
        })
    );
    let snapshot = RunStatusSnapshot::from_run(
        &paused_callback_delivery,
        "evt_000071",
        3_000,
        3_500,
        None,
        vec![RunWaitReason::callback_delivery("del_001")?],
        vec![],
    )?;
    assert_eq!(snapshot.state, RunStatus::PausedCallbackDelivery);
    assert_eq!(
        snapshot.waiting_on[0].message.as_deref(),
        Some("del_001")
    );
    Ok(())
}

#[test]
fn run_status_snapshot_projects_protocol_json() -> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();
    let record = store.create_run_with_provenance(
        "sha256:graph",
        json!({}),
        RunDeploymentProvenance::new().with_release_digest("release-2026-07-03"),
    );
    let waiting = store.set_status(&record.run_id, RunStatus::WaitingCallback)?;
    let snapshot = RunStatusSnapshot::from_run(
        &waiting,
        "evt_000070",
        1_000,
        1_500,
        None,
        vec![RunWaitReason::callback("op-ci-1", Some("waitCI"))?],
        vec!["op-ci-1".to_owned()],
    )?;

    assert_eq!(
        snapshot.protocol_value(),
        json!({
            "runId": "run-000001",
            "state": "waiting_callback",
            "releaseId": "release-2026-07-03",
            "lastCursor": "evt_000070",
            "startedAtUnixMs": 1_000,
            "updatedAtUnixMs": 1_500,
            "completedAtUnixMs": null,
            "waitingOn": [
                {
                    "kind": "callback",
                    "operationId": "op-ci-1",
                    "nodeId": "waitCI"
                }
            ],
            "activeOperations": ["op-ci-1"]
        })
    );
    Ok(())
}

#[test]
fn run_status_snapshot_validates_terminal_completion_and_nonterminal_completion() {
    let mut store = InMemoryRunStore::new();
    let record = store.create_run("sha256:graph", json!({}));
    let running = store
        .set_status(&record.run_id, RunStatus::Running)
        .expect("run can start");
    assert_eq!(
        RunStatusSnapshot::from_run(&running, "evt_1", 1_000, 1_200, Some(1_300), vec![], vec![]),
        Err(RunStoreError::InvalidRunStatusSnapshot {
            run_id: running.run_id.clone(),
            reason: "nonterminal run cannot have completed_at",
        })
    );

    let completed = store
        .set_status(&running.run_id, RunStatus::Completed)
        .expect("run can complete");
    assert_eq!(
        RunStatusSnapshot::from_run(&completed, "evt_2", 1_000, 1_400, None, vec![], vec![]),
        Err(RunStoreError::InvalidRunStatusSnapshot {
            run_id: completed.run_id,
            reason: "terminal run requires completed_at",
        })
    );
}

#[test]
fn run_status_snapshot_rejects_zero_status_timestamps() {
    let mut store = InMemoryRunStore::new();
    let record = store.create_run("sha256:graph", json!({}));
    let running = store
        .set_status(&record.run_id, RunStatus::Running)
        .expect("run can start");

    assert_eq!(
        RunStatusSnapshot::from_run(&running, "evt_1", 0, 1_200, None, vec![], vec![]),
        Err(RunStoreError::InvalidRunStatusSnapshot {
            run_id: running.run_id.clone(),
            reason: "started_at must be positive",
        })
    );
    assert_eq!(
        RunStatusSnapshot::from_run(&running, "evt_2", 1_000, 0, None, vec![], vec![]),
        Err(RunStoreError::InvalidRunStatusSnapshot {
            run_id: running.run_id,
            reason: "updated_at must be positive",
        })
    );
}

#[test]
fn terminal_run_status_snapshot_rejects_wait_reasons_and_active_operations()
-> Result<(), RunStoreError> {
    let mut store = InMemoryRunStore::new();
    let record = store.create_run("sha256:graph", json!({}));
    let cancelled = store.set_status(&record.run_id, RunStatus::Cancelled)?;

    assert_eq!(
        RunStatusSnapshot::from_run(
            &cancelled,
            "evt_3",
            1_000,
            1_400,
            Some(1_500),
            vec![RunWaitReason::callback("op-ci-1", Some("waitCI"))?],
            vec!["op-ci-1".to_owned()],
        ),
        Err(RunStoreError::InvalidRunStatusSnapshot {
            run_id: cancelled.run_id,
            reason: "terminal run cannot expose wait reasons or active operations",
        })
    );
    Ok(())
}

#[test]
fn run_store_ownership_lease_fences_stale_coordinator_after_failover() -> Result<(), RunStoreError>
{
    let mut store = InMemoryRunStore::new();
    let record = store.create_run_with_invocation_mode(
        "sha256:graph",
        json!({}),
        RunInvocationMode::Background,
    );

    let first = store.acquire_ownership_lease(&record.run_id, "coordinator-a", 1_000, 1_500)?;
    assert_eq!(first.fencing_epoch, 1);
    assert_eq!(
        store.acquire_ownership_lease(&record.run_id, "coordinator-b", 1_100, 1_600),
        Err(RunStoreError::RunOwnershipLeaseActive {
            run_id: record.run_id.clone(),
            owner: "coordinator-a".to_owned(),
            expires_at_unix_ms: 1_500,
        })
    );

    let second = store.acquire_ownership_lease(&record.run_id, "coordinator-b", 1_501, 2_000)?;
    assert_eq!(second.fencing_epoch, 2);
    assert_eq!(
        store.validate_ownership_lease(&record.run_id, &first.lease_id, first.fencing_epoch, 1_600),
        Err(RunStoreError::RunOwnershipLeaseMismatch {
            run_id: record.run_id.clone(),
            expected_lease_id: second.lease_id.clone(),
            actual_lease_id: first.lease_id,
            expected_fencing_epoch: second.fencing_epoch,
            actual_fencing_epoch: first.fencing_epoch,
        })
    );
    store.validate_ownership_lease(
        &record.run_id,
        &second.lease_id,
        second.fencing_epoch,
        1_700,
    )?;
    Ok(())
}

#[test]
fn sqlite_run_store_persists_ownership_lease_across_reopen_and_allows_failover()
-> Result<(), String> {
    let mut path = std::env::temp_dir();
    path.push(format!(
        "graphblocks-sqlite-run-lease-{}-persist.sqlite3",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);

    {
        let mut store = SqliteRunStore::open(&path).map_err(|error| format!("{error:?}"))?;
        let record = store
            .create_run_with_invocation_mode(
                "sha256:graph",
                json!({}),
                RunInvocationMode::Background,
            )
            .map_err(|error| format!("{error:?}"))?;
        let lease = store
            .acquire_ownership_lease(&record.run_id, "coordinator-a", 1_000, 1_500)
            .map_err(|error| format!("{error:?}"))?;
        assert_eq!(
            lease,
            RunOwnershipLease {
                run_id: record.run_id,
                lease_id: "run-000001:1".to_owned(),
                owner: "coordinator-a".to_owned(),
                fencing_epoch: 1,
                acquired_at_unix_ms: 1_000,
                expires_at_unix_ms: 1_500,
            }
        );
    }

    let mut store = SqliteRunStore::open(&path).map_err(|error| format!("{error:?}"))?;
    assert_eq!(
        store
            .acquire_ownership_lease("run-000001", "coordinator-b", 1_100, 1_600)
            .map_err(|error| format!("{error:?}")),
        Err(
            "RunOwnershipLeaseActive { run_id: \"run-000001\", owner: \"coordinator-a\", expires_at_unix_ms: 1500 }"
                .to_owned()
        )
    );
    let failover = store
        .acquire_ownership_lease("run-000001", "coordinator-b", 1_501, 2_000)
        .map_err(|error| format!("{error:?}"))?;

    assert_eq!(failover.lease_id, "run-000001:2");
    assert_eq!(failover.fencing_epoch, 2);
    assert_eq!(failover.owner, "coordinator-b");

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn run_store_fenced_mutations_reject_stale_coordinator_after_failover() -> Result<(), RunStoreError>
{
    let mut store = InMemoryRunStore::new();
    let record = store.create_run_with_invocation_mode(
        "sha256:graph",
        json!({}),
        RunInvocationMode::Background,
    );
    let first = store.acquire_ownership_lease(&record.run_id, "coordinator-a", 1_000, 1_500)?;
    let second = store.acquire_ownership_lease(&record.run_id, "coordinator-b", 1_501, 2_000)?;

    assert_eq!(
        store.patch_state_with_ownership_lease(
            &record.run_id,
            StatePatch::new(Some(0)).with(PatchOperation::set(["stale"], json!(true))),
            &first.lease_id,
            first.fencing_epoch,
            1_600,
        ),
        Err(RunStoreError::RunOwnershipLeaseMismatch {
            run_id: record.run_id.clone(),
            expected_lease_id: second.lease_id.clone(),
            actual_lease_id: first.lease_id.clone(),
            expected_fencing_epoch: second.fencing_epoch,
            actual_fencing_epoch: first.fencing_epoch,
        })
    );
    assert_eq!(
        store.set_status_with_ownership_lease(
            &record.run_id,
            RunStatus::Running,
            &first.lease_id,
            first.fencing_epoch,
            1_600,
        ),
        Err(RunStoreError::RunOwnershipLeaseMismatch {
            run_id: record.run_id.clone(),
            expected_lease_id: second.lease_id.clone(),
            actual_lease_id: first.lease_id,
            expected_fencing_epoch: second.fencing_epoch,
            actual_fencing_epoch: first.fencing_epoch,
        })
    );

    let patched = store.patch_state_with_ownership_lease(
        &record.run_id,
        StatePatch::new(Some(0)).with(PatchOperation::set(["owner"], json!("coordinator-b"))),
        &second.lease_id,
        second.fencing_epoch,
        1_700,
    )?;
    let running = store.set_status_with_ownership_lease(
        &record.run_id,
        RunStatus::Running,
        &second.lease_id,
        second.fencing_epoch,
        1_700,
    )?;

    assert_eq!(patched.state["owner"], json!("coordinator-b"));
    assert_eq!(running.status, RunStatus::Running);
    assert_eq!(store.get_run(&record.run_id)?.state.get("stale"), None);
    Ok(())
}

#[test]
fn sqlite_run_store_fenced_mutations_reject_stale_coordinator_after_reopen() -> Result<(), String> {
    let mut path = std::env::temp_dir();
    path.push(format!(
        "graphblocks-sqlite-run-fenced-mutation-{}.sqlite3",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);

    let (run_id, first, second) = {
        let mut store = SqliteRunStore::open(&path).map_err(|error| format!("{error:?}"))?;
        let record = store
            .create_run_with_invocation_mode(
                "sha256:graph",
                json!({}),
                RunInvocationMode::Background,
            )
            .map_err(|error| format!("{error:?}"))?;
        let first = store
            .acquire_ownership_lease(&record.run_id, "coordinator-a", 1_000, 1_500)
            .map_err(|error| format!("{error:?}"))?;
        let second = store
            .acquire_ownership_lease(&record.run_id, "coordinator-b", 1_501, 2_000)
            .map_err(|error| format!("{error:?}"))?;
        (record.run_id, first, second)
    };

    let mut store = SqliteRunStore::open(&path).map_err(|error| format!("{error:?}"))?;
    assert_eq!(
        store
            .patch_state_with_ownership_lease(
                &run_id,
                StatePatch::new(Some(0)).with(PatchOperation::set(["stale"], json!(true))),
                &first.lease_id,
                first.fencing_epoch,
                1_600,
            )
            .map_err(|error| format!("{error:?}")),
        Err(format!(
            "RunOwnershipLeaseMismatch {{ run_id: \"{run_id}\", expected_lease_id: \"{}\", actual_lease_id: \"{}\", expected_fencing_epoch: {}, actual_fencing_epoch: {} }}",
            second.lease_id, first.lease_id, second.fencing_epoch, first.fencing_epoch
        ))
    );
    let patched = store
        .patch_state_with_ownership_lease(
            &run_id,
            StatePatch::new(Some(0)).with(PatchOperation::set(["owner"], json!("coordinator-b"))),
            &second.lease_id,
            second.fencing_epoch,
            1_700,
        )
        .map_err(|error| format!("{error:?}"))?;
    let running = store
        .set_status_with_ownership_lease(
            &run_id,
            RunStatus::Running,
            &second.lease_id,
            second.fencing_epoch,
            1_700,
        )
        .map_err(|error| format!("{error:?}"))?;

    assert_eq!(patched.state["owner"], json!("coordinator-b"));
    assert_eq!(running.status, RunStatus::Running);
    assert_eq!(
        store
            .get_run(&run_id)
            .map_err(|error| format!("{error:?}"))?
            .state
            .get("stale"),
        None
    );

    let _ = std::fs::remove_file(&path);
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
fn run_invocation_diagnostics_report_durable_run_without_replay() {
    let accepted =
        RunInvocationRouteConfig::new("create-coding-task", RunInvocationMode::Accepted, false)
            .expect("route config is valid");
    let background =
        RunInvocationRouteConfig::new("start-ingestion", RunInvocationMode::Background, false)
            .expect("route config is valid");

    let accepted_diagnostics = RunInvocationRouteDiagnostic::for_route(&accepted);
    let background_diagnostics = RunInvocationRouteDiagnostic::for_route(&background);

    assert_eq!(accepted_diagnostics.len(), 1);
    assert_eq!(accepted_diagnostics[0].code, "GB6005");
    assert_eq!(accepted_diagnostics[0].field, "cursor_replay");
    assert!(
        accepted_diagnostics[0]
            .message
            .contains("create-coding-task")
    );
    assert_eq!(background_diagnostics.len(), 1);
    assert_eq!(background_diagnostics[0].code, "GB6005");
}

#[test]
fn run_invocation_diagnostics_allow_sync_without_replay() {
    let config = RunInvocationRouteConfig::new("sync-chat", RunInvocationMode::Sync, false)
        .expect("route config is valid");

    assert_eq!(RunInvocationRouteDiagnostic::for_route(&config), Vec::new());
}

#[test]
fn run_invocation_diagnostics_report_client_bound_durable_run() {
    let config =
        RunInvocationRouteConfig::new("coding-task-ws", RunInvocationMode::Background, true)
            .expect("route config is valid")
            .with_lifetime(RunLifetime::ClientConnection);

    let diagnostics = RunInvocationRouteDiagnostic::for_route(&config);

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6009");
    assert_eq!(diagnostics[0].field, "lifetime");
    assert!(diagnostics[0].message.contains("coding-task-ws"));
}

#[test]
fn run_invocation_diagnostics_allow_sync_client_connection_lifetime() {
    let config = RunInvocationRouteConfig::new("sync-chat", RunInvocationMode::Sync, false)
        .expect("route config is valid")
        .with_lifetime(RunLifetime::ClientConnection);

    assert_eq!(RunInvocationRouteDiagnostic::for_route(&config), Vec::new());
}

#[test]
fn run_invocation_diagnostics_report_retention_shorter_than_replay_guarantee() {
    let config =
        RunInvocationRouteConfig::new("coding-task-events", RunInvocationMode::Background, true)
            .expect("route config is valid")
            .with_event_retention_ms(3_600_000)
            .with_replay_guarantee_ms(86_400_000);

    let diagnostics = RunInvocationRouteDiagnostic::for_route(&config);

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6013");
    assert_eq!(diagnostics[0].field, "event_retention_ms");
    assert!(diagnostics[0].message.contains("coding-task-events"));
}

#[test]
fn run_invocation_diagnostics_allow_retention_covering_replay_guarantee() {
    let config =
        RunInvocationRouteConfig::new("coding-task-events", RunInvocationMode::Background, true)
            .expect("route config is valid")
            .with_event_retention_ms(86_400_000)
            .with_replay_guarantee_ms(3_600_000);

    assert_eq!(RunInvocationRouteDiagnostic::for_route(&config), Vec::new());
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
fn production_run_provenance_diagnostics_report_missing_required_fields() {
    let diagnostics = ProductionRunProvenanceDiagnostic::for_provenance(
        &RunDeploymentProvenance::new().with_deployment_revision_id("rev-1"),
    );

    assert_eq!(
        diagnostics,
        vec![
            ProductionRunProvenanceDiagnostic {
                code: "GB7101",
                field: "release_digest",
                message: "production runs must record signed release digest".to_owned(),
            },
            ProductionRunProvenanceDiagnostic {
                code: "GB7102",
                field: "physical_plan_hash",
                message: "production runs must record physical execution plan hash".to_owned(),
            },
            ProductionRunProvenanceDiagnostic {
                code: "GB7103",
                field: "release_signature_digest",
                message: "production runs must record release signature digest".to_owned(),
            },
        ]
    );
}

#[test]
fn production_run_provenance_diagnostics_accept_complete_provenance() {
    let provenance = RunDeploymentProvenance::new()
        .with_release_digest("sha256:release")
        .with_deployment_revision_id("rev-1")
        .with_physical_plan_hash("sha256:physical")
        .with_release_signature_digest("sha256:signature");

    assert_eq!(
        ProductionRunProvenanceDiagnostic::for_provenance(&provenance),
        Vec::new()
    );
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
        RunStatus::PausedCallbackDelivery,
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
    let paused_callback_delivery = store
        .create_run("sha256:paused-callback-delivery", json!({}))
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
        .set_status(
            &paused_callback_delivery.run_id,
            RunStatus::PausedCallbackDelivery,
        )
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
            .get_run(&paused_callback_delivery.run_id)
            .map_err(|error| format!("{error:?}"))?
            .status,
        RunStatus::PausedCallbackDelivery
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

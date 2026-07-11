use graphblocks_runtime_core::application_event::{
    ApplicationProtocolEvent, ApplicationProtocolEventKind, ApplicationProtocolEventMetadata,
    AttachToRunReplay,
};
use graphblocks_runtime_core::run_store::{RunStatus, RunStatusSnapshot};
use graphblocks_runtime_core::tui::{TuiAttachState, TuiRowSeverity, TuiRunView};
use serde_json::json;

fn status_snapshot() -> RunStatusSnapshot {
    RunStatusSnapshot {
        run_id: "run-coding-1".to_owned(),
        state: RunStatus::Running,
        release_id: "release-2026-07-02".to_owned(),
        last_cursor: "evt-000".to_owned(),
        last_sequence: 0,
        started_at_unix_ms: 1_800_000,
        updated_at_unix_ms: 1_800_100,
        completed_at_unix_ms: None,
        waiting_on: Vec::new(),
        active_operations: vec!["op-ci-1".to_owned()],
    }
}

fn protocol_event(
    event_id: &str,
    sequence: u64,
    cursor: &str,
    kind: ApplicationProtocolEventKind,
    payload: serde_json::Value,
) -> ApplicationProtocolEvent {
    ApplicationProtocolEvent {
        kind,
        metadata: ApplicationProtocolEventMetadata {
            event_id: event_id.to_owned(),
            protocol_version: "graphblocks.app.v1".to_owned(),
            run_id: "run-coding-1".to_owned(),
            release_id: "release-1".to_owned(),
            turn_id: None,
            operation_id: None,
            sequence,
            cursor: Some(cursor.to_owned()),
            occurred_at_unix_ms: 1_800_000 + sequence,
        },
        payload,
    }
}

fn protocol_event_for_run(
    run_id: &str,
    event_id: &str,
    sequence: u64,
    cursor: &str,
    kind: ApplicationProtocolEventKind,
    payload: serde_json::Value,
) -> ApplicationProtocolEvent {
    let mut event = protocol_event(event_id, sequence, cursor, kind, payload);
    event.metadata.run_id = run_id.to_owned();
    event
}

#[test]
fn tui_view_deduplicates_replay_and_live_events() {
    let mut view = TuiRunView::from_status(status_snapshot());
    let started = protocol_event(
        "evt-001",
        1,
        "cursor-001",
        ApplicationProtocolEventKind::RunStarted,
        json!({"summary": "coding task accepted"}),
    );
    let progress = protocol_event(
        "evt-002",
        2,
        "cursor-002",
        ApplicationProtocolEventKind::JobProgress,
        json!({"message": "running tests"}),
    );
    let failed = protocol_event(
        "evt-003",
        3,
        "cursor-003",
        ApplicationProtocolEventKind::RunFailed,
        json!({"reason": "unit test failed"}),
    );

    assert_eq!(
        view.apply_attach_replay(AttachToRunReplay::Attached {
            replayed_events: vec![started.clone(), progress.clone()],
            live_cursor: Some("cursor-002".to_owned()),
        }),
        TuiAttachState::Attached
    );
    assert!(!view.apply_event(started));
    assert!(view.apply_event(failed));

    assert_eq!(view.rows().len(), 3);
    assert_eq!(view.last_cursor(), Some("cursor-003"));
    assert_eq!(view.last_sequence(), Some(3));
    assert_eq!(view.rows()[1].summary, "running tests");
    assert_eq!(view.rows()[2].severity, TuiRowSeverity::Error);
    assert_eq!(
        view.content_digest(),
        "sha256:f1c917f9c7ac90bb972b5fa6d9586125a521c212b4b33175c5ca2036b87b734e"
    );
}

#[test]
fn tui_view_records_cursor_expiry_without_losing_status_context() {
    let mut view = TuiRunView::from_status(status_snapshot());
    let recovery_status = RunStatusSnapshot {
        last_cursor: "cursor-030".to_owned(),
        last_sequence: 30,
        updated_at_unix_ms: 1_800_300,
        active_operations: vec!["op-ci-2".to_owned()],
        ..status_snapshot()
    };

    assert_eq!(
        view.apply_attach_replay(AttachToRunReplay::CursorExpired {
            requested_cursor: "cursor-old".to_owned(),
            earliest_available_cursor: Some("cursor-010".to_owned()),
            last_cursor: Some("cursor-030".to_owned()),
            last_sequence: Some(30),
            run_status: Some(Box::new(recovery_status)),
        }),
        TuiAttachState::CursorExpired
    );

    assert_eq!(view.run_id(), "run-coding-1");
    assert_eq!(view.last_cursor(), Some("cursor-030"));
    assert_eq!(view.last_sequence(), Some(30));
    assert_eq!(view.active_operations(), ["op-ci-2"]);
    assert_eq!(
        view.cursor_expired()
            .expect("cursor-expired replay must populate projection")
            .requested_cursor,
        "cursor-old"
    );
    assert_eq!(view.rows().len(), 0);
}

#[test]
fn tui_view_ignores_other_run_events_without_poisoning_deduplication() {
    let mut view = TuiRunView::from_status(status_snapshot());
    let wrong_run_event = protocol_event_for_run(
        "run-other",
        "evt-shared",
        1,
        "cursor-other",
        ApplicationProtocolEventKind::JobProgress,
        json!({"message": "wrong run"}),
    );
    let correct_run_event = protocol_event(
        "evt-shared",
        2,
        "cursor-002",
        ApplicationProtocolEventKind::JobProgress,
        json!({"message": "right run"}),
    );

    assert!(!view.apply_event(wrong_run_event));
    assert!(view.apply_event(correct_run_event));

    assert_eq!(view.rows().len(), 1);
    assert_eq!(view.rows()[0].summary, "right run");
    assert_eq!(view.last_cursor(), Some("cursor-002"));
}

#[test]
fn tui_view_projects_policy_stopped_as_terminal_error_state() {
    let mut view = TuiRunView::from_status(status_snapshot());
    let policy_stopped = protocol_event(
        "evt-policy-stopped",
        4,
        "cursor-004",
        ApplicationProtocolEventKind::RunPolicyStopped,
        json!({"reason": "output policy denied"}),
    );

    assert!(view.apply_event(policy_stopped));

    assert_eq!(view.state(), RunStatus::PolicyStopped);
    assert_eq!(view.rows().len(), 1);
    assert_eq!(view.rows()[0].summary, "output policy denied");
    assert_eq!(view.rows()[0].severity, TuiRowSeverity::Error);
}

#[test]
fn tui_view_projects_expired_as_terminal_error_state() {
    let mut view = TuiRunView::from_status(status_snapshot());
    let expired = protocol_event(
        "evt-expired",
        5,
        "cursor-005",
        ApplicationProtocolEventKind::RunExpired,
        json!({"reason": "run deadline exceeded"}),
    );

    assert!(view.apply_event(expired));

    assert_eq!(view.state(), RunStatus::Expired);
    assert_eq!(view.rows().len(), 1);
    assert_eq!(view.rows()[0].summary, "run deadline exceeded");
    assert_eq!(view.rows()[0].severity, TuiRowSeverity::Error);
}

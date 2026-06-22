use graphblocks_runtime_core::journal::{ExecutionJournal, JournalError, JournalMetadata};
use serde_json::json;

#[test]
fn journal_appends_records_with_monotonic_run_sequence() -> Result<(), JournalError> {
    let mut journal = ExecutionJournal::new("run-000001");

    let first = journal.append("run_admitted", json!({"graphHash": "abc"}))?;
    let second = journal.append("node_started", json!({"node": "prompt"}))?;

    assert_eq!(first.record_id, "run-000001:1");
    assert_eq!(first.run_id, "run-000001");
    assert_eq!(first.run_sequence, 1);
    assert_eq!(first.kind, "run_admitted");
    assert_eq!(second.record_id, "run-000001:2");
    assert_eq!(second.run_sequence, 2);
    assert_eq!(journal.records(), &[first, second]);
    Ok(())
}

#[test]
fn journal_preserves_metadata_and_payload() -> Result<(), JournalError> {
    let mut journal = ExecutionJournal::new("run-000001");
    let metadata = JournalMetadata::new()
        .with_causation_id("cause-1")
        .with_node_id("model")
        .with_attempt_id("attempt-2")
        .with_lease_epoch(7);

    let record = journal.append_with_metadata(
        "effect_committed",
        metadata,
        Some(json!({"providerRequestId": "req-123"})),
    )?;

    assert_eq!(record.causation_id.as_deref(), Some("cause-1"));
    assert_eq!(record.node_id.as_deref(), Some("model"));
    assert_eq!(record.attempt_id.as_deref(), Some("attempt-2"));
    assert_eq!(record.lease_epoch, Some(7));
    assert_eq!(
        record.payload,
        Some(json!({"providerRequestId": "req-123"})),
    );
    Ok(())
}

#[test]
fn journal_records_terminal_once_and_rejects_late_records() -> Result<(), JournalError> {
    let mut journal = ExecutionJournal::new("run-000001");

    journal.append("node_terminal", json!({"node": "answer"}))?;
    let terminal = journal.append_terminal("run_completed", json!({"status": "completed"}))?;

    assert!(terminal.terminal);
    assert_eq!(journal.records().last(), Some(&terminal));
    assert_eq!(journal.terminal_kind(), Some("run_completed"));
    assert_eq!(
        journal.append("late_node_output", json!({"node": "answer"})),
        Err(JournalError::AppendAfterTerminal {
            terminal_kind: "run_completed".to_owned(),
        }),
    );
    assert_eq!(
        journal.append_terminal("run_failed", json!({"error": "late"})),
        Err(JournalError::TerminalAlreadyRecorded {
            terminal_kind: "run_completed".to_owned(),
        }),
    );
    Ok(())
}

#[test]
fn journal_terminal_record_preserves_metadata() -> Result<(), JournalError> {
    let mut journal = ExecutionJournal::new("run-000001");
    let metadata = JournalMetadata::new()
        .with_causation_id("node-output-3")
        .with_node_id("answer")
        .with_attempt_id("attempt-1")
        .with_lease_epoch(11);

    let terminal = journal.append_terminal_with_metadata(
        "run_completed",
        metadata,
        Some(json!({"status": "completed"})),
    )?;

    assert!(terminal.terminal);
    assert_eq!(terminal.causation_id.as_deref(), Some("node-output-3"));
    assert_eq!(terminal.node_id.as_deref(), Some("answer"));
    assert_eq!(terminal.attempt_id.as_deref(), Some("attempt-1"));
    assert_eq!(terminal.lease_epoch, Some(11));
    assert_eq!(journal.records().last(), Some(&terminal));
    Ok(())
}

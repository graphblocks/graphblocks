use graphblocks_runtime_core::journal::{
    ExecutionJournal, JournalError, JournalMetadata, SqliteExecutionJournal,
};
use rusqlite::params;
use serde_json::json;
use std::sync::{Arc, Barrier};
use std::time::{SystemTime, UNIX_EPOCH};

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

#[test]
fn sqlite_journal_persists_records_across_reopen() -> Result<(), String> {
    let mut path = std::env::temp_dir();
    path.push(format!(
        "graphblocks-sqlite-journal-{}-persist.sqlite3",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);

    let first;
    let terminal;
    {
        let mut journal = SqliteExecutionJournal::open(&path, "run-000001")
            .map_err(|error| format!("{error:?}"))?;
        first = journal
            .append_with_metadata(
                "node_started",
                JournalMetadata::new()
                    .with_causation_id("run")
                    .with_node_id("model")
                    .with_attempt_id("attempt-1")
                    .with_lease_epoch(3),
                Some(json!({"input": "prompt"})),
            )
            .map_err(|error| format!("{error:?}"))?;
        terminal = journal
            .append_terminal("run_completed", json!({"status": "completed"}))
            .map_err(|error| format!("{error:?}"))?;
    }

    let journal =
        SqliteExecutionJournal::open(&path, "run-000001").map_err(|error| format!("{error:?}"))?;
    assert_eq!(
        journal
            .terminal_kind()
            .map_err(|error| format!("{error:?}"))?
            .as_deref(),
        Some("run_completed")
    );
    assert_eq!(
        journal.records().map_err(|error| format!("{error:?}"))?,
        vec![first, terminal],
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn sqlite_journal_rejects_invalid_record_metadata_on_replay() -> Result<(), String> {
    let mut path = std::env::temp_dir();
    path.push(format!(
        "graphblocks-sqlite-journal-{}-invalid-metadata.sqlite3",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);

    {
        let mut journal = SqliteExecutionJournal::open(&path, "run-000001")
            .map_err(|error| format!("{error:?}"))?;
        journal
            .append("node_started", json!({"node": "model"}))
            .map_err(|error| format!("{error:?}"))?;
    }
    {
        let connection = rusqlite::Connection::open(&path).map_err(|error| format!("{error:?}"))?;
        connection
            .execute(
                "UPDATE journal_records SET kind = ?1 WHERE record_id = ?2",
                params![" \t", "run-000001:1"],
            )
            .map_err(|error| format!("{error:?}"))?;
    }

    let journal =
        SqliteExecutionJournal::open(&path, "run-000001").map_err(|error| format!("{error:?}"))?;
    let records = journal.records();

    assert_eq!(
        records,
        Err(JournalError::Storage {
            message: "stored journal record kind must not be empty".to_owned(),
        })
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn sqlite_journal_rejects_sequence_gaps_on_replay() -> Result<(), String> {
    let path = std::env::temp_dir().join(format!(
        "graphblocks-sqlite-journal-{}-sequence-gap.sqlite3",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);
    {
        let mut journal = SqliteExecutionJournal::open(&path, "run-000001")
            .map_err(|error| format!("{error:?}"))?;
        journal
            .append("node_started", json!({}))
            .map_err(|error| format!("{error:?}"))?;
        journal
            .append("node_completed", json!({}))
            .map_err(|error| format!("{error:?}"))?;
    }
    let connection = rusqlite::Connection::open(&path).map_err(|error| format!("{error:?}"))?;
    connection
        .execute(
            "DELETE FROM journal_records WHERE run_id = ?1 AND run_sequence = 1",
            params!["run-000001"],
        )
        .map_err(|error| format!("{error:?}"))?;
    drop(connection);

    let journal =
        SqliteExecutionJournal::open(&path, "run-000001").map_err(|error| format!("{error:?}"))?;
    assert_eq!(
        journal.records(),
        Err(JournalError::Storage {
            message: "stored journal sequence gap: expected 1, found 2".to_owned(),
        })
    );
    assert_eq!(
        journal.terminal_kind(),
        Err(JournalError::Storage {
            message: "stored journal sequence gap: expected 1, found 2".to_owned(),
        })
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn sqlite_journal_rejects_record_id_mismatch_on_replay() -> Result<(), String> {
    let path = std::env::temp_dir().join(format!(
        "graphblocks-sqlite-journal-{}-record-id.sqlite3",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);
    {
        let mut journal = SqliteExecutionJournal::open(&path, "run-000001")
            .map_err(|error| format!("{error:?}"))?;
        journal
            .append("node_started", json!({}))
            .map_err(|error| format!("{error:?}"))?;
    }
    let connection = rusqlite::Connection::open(&path).map_err(|error| format!("{error:?}"))?;
    connection
        .execute(
            "UPDATE journal_records SET record_id = ?1 WHERE run_id = ?2",
            params!["run-other:99", "run-000001"],
        )
        .map_err(|error| format!("{error:?}"))?;
    drop(connection);

    let journal =
        SqliteExecutionJournal::open(&path, "run-000001").map_err(|error| format!("{error:?}"))?;
    assert_eq!(
        journal.records(),
        Err(JournalError::Storage {
            message: "stored journal record_id \"run-other:99\" does not match \"run-000001:1\""
                .to_owned(),
        })
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn sqlite_journal_rejects_noncanonical_terminal_flag_on_replay() -> Result<(), String> {
    let path = std::env::temp_dir().join(format!(
        "graphblocks-sqlite-journal-{}-terminal-flag.sqlite3",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);
    {
        let mut journal = SqliteExecutionJournal::open(&path, "run-000001")
            .map_err(|error| format!("{error:?}"))?;
        journal
            .append("node_started", json!({}))
            .map_err(|error| format!("{error:?}"))?;
    }
    let connection = rusqlite::Connection::open(&path).map_err(|error| format!("{error:?}"))?;
    connection
        .execute(
            "UPDATE journal_records SET terminal = 2 WHERE run_id = ?1",
            params!["run-000001"],
        )
        .map_err(|error| format!("{error:?}"))?;
    drop(connection);

    let journal =
        SqliteExecutionJournal::open(&path, "run-000001").map_err(|error| format!("{error:?}"))?;
    assert_eq!(
        journal.records(),
        Err(JournalError::Storage {
            message: "stored journal record terminal flag must be 0 or 1, found 2".to_owned(),
        })
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn sqlite_journal_rejects_late_records_after_terminal() -> Result<(), String> {
    let mut journal = SqliteExecutionJournal::open_in_memory("run-000001")
        .map_err(|error| format!("{error:?}"))?;

    journal
        .append_terminal("run_completed", json!({"status": "completed"}))
        .map_err(|error| format!("{error:?}"))?;

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
fn sqlite_journal_serializes_concurrent_terminal_and_nonterminal_writers() -> Result<(), String> {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_nanos();
    let path = std::env::temp_dir().join(format!(
        "graphblocks-sqlite-journal-{}-{unique}-concurrent-terminal.sqlite3",
        std::process::id(),
    ));
    let _ = std::fs::remove_file(&path);

    for round in 0..64 {
        let run_id = format!("run-{round:06}");
        let mut terminal_writer =
            SqliteExecutionJournal::open(&path, &run_id).map_err(|error| format!("{error:?}"))?;
        let mut nonterminal_writer =
            SqliteExecutionJournal::open(&path, &run_id).map_err(|error| format!("{error:?}"))?;
        let barrier = Arc::new(Barrier::new(2));
        let terminal_barrier = Arc::clone(&barrier);
        let terminal_thread = std::thread::spawn(move || {
            terminal_barrier.wait();
            terminal_writer.append_terminal("run_completed", json!({"status": "completed"}))
        });
        let nonterminal_thread = std::thread::spawn(move || {
            barrier.wait();
            nonterminal_writer.append("node_completed", json!({"node": "answer"}))
        });

        let terminal = terminal_thread
            .join()
            .map_err(|_| "terminal journal writer panicked".to_owned())?
            .map_err(|error| format!("{error:?}"))?;
        let nonterminal = nonterminal_thread
            .join()
            .map_err(|_| "nonterminal journal writer panicked".to_owned())?;
        match nonterminal {
            Ok(record) => assert!(record.run_sequence < terminal.run_sequence),
            Err(JournalError::AppendAfterTerminal { terminal_kind }) => {
                assert_eq!(terminal_kind, "run_completed");
            }
            Err(error) => return Err(format!("unexpected concurrent append error: {error:?}")),
        }

        let journal =
            SqliteExecutionJournal::open(&path, &run_id).map_err(|error| format!("{error:?}"))?;
        let records = journal.records().map_err(|error| format!("{error:?}"))?;
        assert_eq!(records.last(), Some(&terminal));
        assert!(terminal.terminal);
        assert_eq!(records.iter().filter(|record| record.terminal).count(), 1);
    }
    let _ = std::fs::remove_file(path);
    Ok(())
}

use graphblocks_runtime_core::usage::{
    InMemoryUsageLedger, SqliteUsageLedger, UsageAmount, UsageConfidence, UsageLedgerError,
    UsageRecord, UsageSource,
};
use rusqlite::params;
use std::{
    fs,
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};

fn tokens(amount: i64) -> UsageAmount {
    UsageAmount::new("model_output_tokens", amount, "tokens")
}

fn sqlite_usage_path(test_name: &str) -> PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "graphblocks-{test_name}-{}-{unique}.sqlite",
        std::process::id()
    ))
}

#[test]
fn usage_ledgers_reject_invalid_usage_records() -> Result<(), UsageLedgerError> {
    let mut memory = InMemoryUsageLedger::new();
    let mut sqlite = SqliteUsageLedger::open_in_memory()?;
    let blank_record_id = UsageRecord::new(
        "   ",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(12)],
        1_000,
    );
    let blank_amount_kind = UsageRecord::new(
        "usage-1",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [UsageAmount::new("   ", 12, "tokens")],
        1_000,
    );
    let blank_amount_unit = UsageRecord::new(
        "usage-2",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [UsageAmount::new("model_output_tokens", 12, "   ")],
        1_000,
    );
    let zero_occurred_at = UsageRecord::new(
        "usage-zero-time",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(12)],
        0,
    );
    let empty_amounts = UsageRecord::new(
        "usage-empty-amounts",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [],
        1_000,
    );
    let negative_amount = UsageRecord::new(
        "usage-negative",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(-1)],
        1_000,
    );
    let blank_dimension_key = UsageRecord::new(
        "usage-blank-dimension-key",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(12).with_dimension(" ", "test-model")],
        1_000,
    );
    let blank_dimension_value = UsageRecord::new(
        "usage-blank-dimension-value",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(12).with_dimension("model", " ")],
        1_000,
    );
    let blank_run_id = UsageRecord::new(
        "usage-3",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(12)],
        1_000,
    )
    .with_run_id(" ");
    let blank_provider_response_id = UsageRecord::new(
        "usage-4",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(12)],
        1_000,
    )
    .with_provider_response_id("");
    let blank_metadata_key = UsageRecord::new(
        "usage-blank-metadata-key",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(12)],
        1_000,
    )
    .with_metadata(" ", "generation");
    let blank_metadata_value = UsageRecord::new(
        "usage-blank-metadata-value",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(12)],
        1_000,
    )
    .with_metadata("phase", " ");

    assert_eq!(
        memory.append(blank_record_id.clone()),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage record_id must not be empty".to_string()
        })
    );
    assert_eq!(
        sqlite.append(blank_record_id),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage record_id must not be empty".to_string()
        })
    );
    assert_eq!(
        memory.append(blank_amount_kind),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage amount kind must not be empty".to_string()
        })
    );
    assert_eq!(
        sqlite.append(blank_amount_unit),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage amount unit must not be empty".to_string()
        })
    );
    assert_eq!(
        memory.append(zero_occurred_at.clone()),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage occurred_at_unix_ms must be positive".to_string()
        })
    );
    assert_eq!(
        sqlite.append(zero_occurred_at),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage occurred_at_unix_ms must be positive".to_string()
        })
    );
    assert_eq!(
        memory.append(empty_amounts.clone()),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage amounts must not be empty".to_string()
        })
    );
    assert_eq!(
        sqlite.append(empty_amounts),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage amounts must not be empty".to_string()
        })
    );
    assert_eq!(
        memory.append(negative_amount.clone()),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage amount must be non-negative".to_string()
        })
    );
    assert_eq!(
        sqlite.append(negative_amount),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage amount must be non-negative".to_string()
        })
    );
    assert_eq!(
        memory.append(blank_dimension_key),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage amount dimension keys must not be empty".to_string()
        })
    );
    assert_eq!(
        sqlite.append(blank_dimension_value),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage amount dimension values must not be empty".to_string()
        })
    );
    assert_eq!(
        memory.append(blank_run_id),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage run_id must not be empty".to_string()
        })
    );
    assert_eq!(
        sqlite.append(blank_provider_response_id),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage provider_response_id must not be empty".to_string()
        })
    );
    assert_eq!(
        memory.append(blank_metadata_key),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage metadata keys must not be empty".to_string()
        })
    );
    assert_eq!(
        sqlite.append(blank_metadata_value),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage metadata values must not be empty".to_string()
        })
    );
    Ok(())
}

#[test]
fn usage_ledger_appends_immutable_records_and_queries_by_run() -> Result<(), UsageLedgerError> {
    let mut ledger = InMemoryUsageLedger::new();
    let record = UsageRecord::new(
        "usage-1",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(12)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1");

    let appended = ledger.append(record.clone())?;

    assert_eq!(appended, record);
    assert_eq!(ledger.records_for_run("run-1"), vec![record]);
    assert!(ledger.records_for_run("missing").is_empty());
    Ok(())
}

#[test]
fn usage_ledger_replays_identical_records_without_double_counting() -> Result<(), UsageLedgerError>
{
    let mut ledger = InMemoryUsageLedger::new();
    let record = UsageRecord::new(
        "usage-1",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(12)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1");
    let changed = UsageRecord::new(
        "usage-1",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(13)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1");

    assert_eq!(ledger.append(record.clone())?, record);
    assert_eq!(ledger.append(record.clone())?, record);
    assert_eq!(
        ledger.append(changed),
        Err(UsageLedgerError::RecordConflict {
            record_id: "usage-1".to_string()
        })
    );
    assert_eq!(ledger.records_for_run("run-1"), vec![record]);
    assert_eq!(ledger.totals_for_run("run-1"), vec![tokens(12)]);
    Ok(())
}

#[test]
fn usage_ledger_deduplicates_provider_response_for_same_attempt() -> Result<(), UsageLedgerError> {
    let mut ledger = InMemoryUsageLedger::new();
    let first = UsageRecord::new(
        "usage-1",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(20)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1")
    .with_quota_window_id("tenant-a:2026-06")
    .with_execution_scope("turn:turn-1/tool:call-1")
    .with_metadata("tool_call_id", "call-1")
    .with_metadata("tool_name", "ticket.create");
    let duplicate = UsageRecord::new(
        "usage-duplicate",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(20)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1")
    .with_quota_window_id("tenant-a:2026-06")
    .with_execution_scope("turn:turn-1/tool:call-1")
    .with_metadata("tool_call_id", "call-1")
    .with_metadata("tool_name", "ticket.create");

    assert_eq!(ledger.append(first.clone())?, first);
    assert_eq!(ledger.append(duplicate)?, first);
    assert_eq!(ledger.records_for_run("run-1"), vec![first]);
    Ok(())
}

#[test]
fn usage_ledger_rejects_provider_response_replay_with_conflicting_timestamp()
-> Result<(), UsageLedgerError> {
    let mut ledger = InMemoryUsageLedger::new();
    let first = UsageRecord::new(
        "usage-1",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(20)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1");
    let conflicting_timestamp = UsageRecord::new(
        "usage-conflict",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(20)],
        1_010,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1");

    assert_eq!(ledger.append(first.clone())?, first);
    assert_eq!(
        ledger.append(conflicting_timestamp),
        Err(UsageLedgerError::RecordConflict {
            record_id: "resp-1".to_string()
        })
    );
    assert_eq!(ledger.records_for_run("run-1"), vec![first]);
    Ok(())
}

#[test]
fn usage_ledger_rejects_conflicting_provider_response_replay() -> Result<(), UsageLedgerError> {
    let mut ledger = InMemoryUsageLedger::new();
    let first = UsageRecord::new(
        "usage-1",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(20)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1")
    .with_quota_window_id("tenant-a:2026-06")
    .with_execution_scope("turn:turn-1/tool:call-1")
    .with_metadata("tool_call_id", "call-1")
    .with_metadata("tool_name", "ticket.create");
    let conflicting_replay = UsageRecord::new(
        "usage-conflict",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(21)],
        1_010,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1")
    .with_quota_window_id("tenant-a:2026-06")
    .with_execution_scope("turn:turn-1/tool:call-1")
    .with_metadata("tool_call_id", "call-1")
    .with_metadata("tool_name", "ticket.create");

    assert_eq!(ledger.append(first.clone())?, first);
    assert_eq!(
        ledger.append(conflicting_replay),
        Err(UsageLedgerError::RecordConflict {
            record_id: "resp-1".to_string()
        })
    );
    assert_eq!(ledger.records_for_run("run-1"), vec![first]);
    Ok(())
}

#[test]
fn usage_ledger_reconcile_writes_new_record_for_late_final_usage() -> Result<(), UsageLedgerError> {
    let mut ledger = InMemoryUsageLedger::new();
    let provisional = ledger.append(
        UsageRecord::new(
            "usage-provisional",
            UsageSource::TokenizerEstimated,
            UsageConfidence::Estimated,
            [tokens(18)],
            1_000,
        )
        .with_run_id("run-1")
        .with_attempt_id("attempt-1")
        .with_provider_response_id("resp-1")
        .with_pricing_ref("pricing-2026-06")
        .with_quota_window_id("tenant-a:2026-06")
        .with_execution_scope("turn:turn-1/tool:call-1")
        .with_metadata("tool_call_id", "call-1")
        .with_metadata("tool_name", "knowledge.search"),
    )?;

    let reconciled = ledger.reconcile(
        "usage-provisional",
        [tokens(21)],
        1_500,
        Some("usage-reconciled".to_string()),
    )?;

    assert_eq!(reconciled.source, UsageSource::Reconciled);
    assert_eq!(reconciled.confidence, UsageConfidence::Exact);
    assert_eq!(
        reconciled.reconciliation_of.as_deref(),
        Some("usage-provisional")
    );
    assert_eq!(reconciled.run_id.as_deref(), Some("run-1"));
    assert_eq!(reconciled.attempt_id.as_deref(), Some("attempt-1"));
    assert_eq!(reconciled.provider_response_id.as_deref(), Some("resp-1"));
    assert_eq!(reconciled.pricing_ref.as_deref(), Some("pricing-2026-06"));
    assert_eq!(
        reconciled.quota_window_id.as_deref(),
        Some("tenant-a:2026-06")
    );
    assert_eq!(
        reconciled.execution_scope.as_deref(),
        Some("turn:turn-1/tool:call-1")
    );
    assert_eq!(
        reconciled.metadata.get("tool_call_id").map(String::as_str),
        Some("call-1")
    );
    assert_eq!(
        reconciled.metadata.get("tool_name").map(String::as_str),
        Some("knowledge.search")
    );
    assert_eq!(
        ledger.records_for_run("run-1"),
        vec![provisional, reconciled]
    );
    Ok(())
}

#[test]
fn usage_ledger_totals_replace_provisional_with_reconciled_usage() -> Result<(), UsageLedgerError> {
    let mut ledger = InMemoryUsageLedger::new();
    ledger.append(
        UsageRecord::new(
            "usage-provisional",
            UsageSource::TokenizerEstimated,
            UsageConfidence::Estimated,
            [tokens(18)],
            1_000,
        )
        .with_run_id("run-1")
        .with_attempt_id("attempt-1")
        .with_provider_response_id("resp-1"),
    )?;
    ledger.append(
        UsageRecord::new(
            "usage-runtime",
            UsageSource::RuntimeMeasured,
            UsageConfidence::Estimated,
            [tokens(2)],
            1_010,
        )
        .with_run_id("run-1")
        .with_attempt_id("attempt-2"),
    )?;
    ledger.reconcile(
        "usage-provisional",
        [tokens(21)],
        1_500,
        Some("usage-reconciled".to_string()),
    )?;

    assert_eq!(ledger.totals_for_run("run-1"), vec![tokens(23)]);
    Ok(())
}

#[test]
fn usage_ledger_rejects_multiple_reconciliations_for_same_source_record()
-> Result<(), UsageLedgerError> {
    let mut ledger = InMemoryUsageLedger::new();
    let provisional = ledger.append(
        UsageRecord::new(
            "usage-provisional",
            UsageSource::TokenizerEstimated,
            UsageConfidence::Estimated,
            [tokens(18)],
            1_000,
        )
        .with_run_id("run-1")
        .with_attempt_id("attempt-1")
        .with_provider_response_id("resp-1"),
    )?;
    let first = ledger.reconcile(
        "usage-provisional",
        [tokens(21)],
        1_500,
        Some("usage-reconciled-1".to_string()),
    )?;

    assert_eq!(
        ledger.reconcile(
            "usage-provisional",
            [tokens(22)],
            1_600,
            Some("usage-reconciled-2".to_string()),
        ),
        Err(UsageLedgerError::RecordConflict {
            record_id: "usage-provisional".to_string()
        })
    );
    assert_eq!(ledger.records_for_run("run-1"), vec![provisional, first]);
    assert_eq!(ledger.totals_for_run("run-1"), vec![tokens(21)]);
    Ok(())
}

#[test]
fn usage_ledgers_reject_reconciliation_before_source_usage() -> Result<(), UsageLedgerError> {
    let mut memory = InMemoryUsageLedger::new();
    let mut sqlite = SqliteUsageLedger::open_in_memory()?;
    let provisional = UsageRecord::new(
        "usage-provisional",
        UsageSource::TokenizerEstimated,
        UsageConfidence::Estimated,
        [tokens(18)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1");

    memory.append(provisional.clone())?;
    sqlite.append(provisional)?;

    assert_eq!(
        memory.reconcile(
            "usage-provisional",
            [tokens(21)],
            999,
            Some("usage-reconciled-memory".to_owned()),
        ),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage reconciliation occurred_at must not precede source usage".to_owned(),
        })
    );
    assert_eq!(
        sqlite.reconcile(
            "usage-provisional",
            [tokens(21)],
            999,
            Some("usage-reconciled-sqlite".to_owned()),
        ),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage reconciliation occurred_at must not precede source usage".to_owned(),
        })
    );
    Ok(())
}

#[test]
fn usage_ledgers_reject_reconciliation_record_without_source() -> Result<(), UsageLedgerError> {
    let mut memory = InMemoryUsageLedger::new();
    let mut sqlite = SqliteUsageLedger::open_in_memory()?;
    let mut orphaned_reconciliation = UsageRecord::new(
        "usage-reconciled",
        UsageSource::Reconciled,
        UsageConfidence::Exact,
        [tokens(21)],
        1_500,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1");
    orphaned_reconciliation.reconciliation_of = Some("usage-missing".to_owned());

    assert_eq!(
        memory.append(orphaned_reconciliation.clone()),
        Err(UsageLedgerError::RecordNotFound {
            record_id: "usage-missing".to_owned()
        })
    );
    assert_eq!(
        sqlite.append(orphaned_reconciliation),
        Err(UsageLedgerError::RecordNotFound {
            record_id: "usage-missing".to_owned()
        })
    );
    Ok(())
}

#[test]
fn usage_ledgers_require_reconciled_lineage_only_for_reconciliations()
-> Result<(), UsageLedgerError> {
    let mut memory = InMemoryUsageLedger::new();
    let mut sqlite = SqliteUsageLedger::open_in_memory()?;
    let provisional = UsageRecord::new(
        "usage-provisional",
        UsageSource::TokenizerEstimated,
        UsageConfidence::Estimated,
        [tokens(18)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1");
    let reconciled_without_lineage = UsageRecord::new(
        "usage-reconciled-without-lineage",
        UsageSource::Reconciled,
        UsageConfidence::Exact,
        [tokens(21)],
        1_500,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1");
    let mut non_reconciled_with_lineage = UsageRecord::new(
        "usage-runtime-with-lineage",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(1)],
        1_510,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1");
    non_reconciled_with_lineage.reconciliation_of = Some("usage-provisional".to_owned());

    memory.append(provisional.clone())?;
    sqlite.append(provisional)?;

    assert_eq!(
        memory.append(reconciled_without_lineage.clone()),
        Err(UsageLedgerError::InvalidRecord {
            message: "reconciled usage records must identify reconciliation_of".to_owned(),
        })
    );
    assert_eq!(
        sqlite.append(reconciled_without_lineage),
        Err(UsageLedgerError::InvalidRecord {
            message: "reconciled usage records must identify reconciliation_of".to_owned(),
        })
    );
    assert_eq!(
        memory.append(non_reconciled_with_lineage.clone()),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage reconciliation_of requires reconciled source".to_owned(),
        })
    );
    assert_eq!(
        sqlite.append(non_reconciled_with_lineage),
        Err(UsageLedgerError::InvalidRecord {
            message: "usage reconciliation_of requires reconciled source".to_owned(),
        })
    );
    Ok(())
}

#[test]
fn sqlite_usage_ledger_persists_records_across_reopen() -> Result<(), UsageLedgerError> {
    let path = sqlite_usage_path("usage-persist");
    let record = UsageRecord::new(
        "usage-1",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(12).with_dimension("model", "test-model")],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_quota_window_id("tenant-a:2026-06")
    .with_execution_scope("turn:turn-1/model:generate")
    .with_metadata("phase", "generation");

    {
        let mut ledger = SqliteUsageLedger::open(&path)?;
        assert_eq!(ledger.append(record.clone())?, record);
    }

    let ledger = SqliteUsageLedger::open(&path)?;
    assert_eq!(ledger.records_for_run("run-1")?, vec![record]);
    fs::remove_file(path).ok();
    Ok(())
}

#[test]
fn sqlite_usage_ledger_replays_identical_records_without_double_counting()
-> Result<(), UsageLedgerError> {
    let mut ledger = SqliteUsageLedger::open_in_memory()?;
    let record = UsageRecord::new(
        "usage-1",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(12)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1");
    let changed = UsageRecord::new(
        "usage-1",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(13)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1");

    assert_eq!(ledger.append(record.clone())?, record);
    assert_eq!(ledger.append(record.clone())?, record);
    assert_eq!(
        ledger.append(changed),
        Err(UsageLedgerError::RecordConflict {
            record_id: "usage-1".to_string()
        })
    );
    assert_eq!(ledger.records_for_run("run-1")?, vec![record]);
    assert_eq!(ledger.totals_for_run("run-1")?, vec![tokens(12)]);
    Ok(())
}

#[test]
fn sqlite_usage_ledger_enforces_provider_dedupe_for_null_attempt_at_storage_boundary()
-> Result<(), UsageLedgerError> {
    let path = sqlite_usage_path("usage-provider-dedupe-index");
    {
        let mut ledger = SqliteUsageLedger::open(&path)?;
        let first = UsageRecord::new(
            "usage-1",
            UsageSource::ProviderReported,
            UsageConfidence::ProviderExact,
            [tokens(20)],
            1_000,
        )
        .with_run_id("run-1")
        .with_provider_response_id("resp-1");

        assert_eq!(ledger.append(first.clone())?, first);
    }

    let connection = rusqlite::Connection::open(&path).expect("usage ledger database opens");
    let duplicate = connection.execute(
        "
        INSERT INTO usage_records (
            sequence,
            record_id,
            source,
            confidence,
            amounts_json,
            occurred_at_unix_ms,
            run_id,
            provider_response_id,
            metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ",
        params![
            2_i64,
            "usage-duplicate",
            "provider_reported",
            "provider_exact",
            r#"[{"amount":"20","dimensions":{},"kind":"model_output_tokens","unit":"tokens"}]"#,
            1_010_i64,
            "run-1",
            "resp-1",
            "{}",
        ],
    );

    assert!(duplicate.is_err());
    fs::remove_file(path).ok();
    Ok(())
}

#[test]
fn sqlite_usage_ledger_migrates_existing_usage_tables_for_lineage() -> Result<(), UsageLedgerError>
{
    let path = sqlite_usage_path("usage-lineage-migration");
    {
        let connection =
            rusqlite::Connection::open(&path).expect("old usage ledger database opens");
        connection
            .execute_batch(
                r#"
                CREATE TABLE usage_records (
                    sequence INTEGER PRIMARY KEY,
                    record_id TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    amounts_json TEXT NOT NULL,
                    occurred_at_unix_ms INTEGER NOT NULL,
                    run_id TEXT,
                    attempt_id TEXT,
                    provider_response_id TEXT,
                    pricing_ref TEXT,
                    reconciliation_of TEXT,
                    metadata_json TEXT NOT NULL
                );
                INSERT INTO usage_records (
                    sequence,
                    record_id,
                    source,
                    confidence,
                    amounts_json,
                    occurred_at_unix_ms,
                    run_id,
                    attempt_id,
                    provider_response_id,
                    pricing_ref,
                    reconciliation_of,
                    metadata_json
                )
                VALUES (
                    1,
                    'usage-old',
                    'runtime_measured',
                    'estimated',
                    '[{"kind":"model_output_tokens","amount":12,"unit":"tokens","dimensions":{}}]',
                    1000,
                    'run-1',
                    'attempt-1',
                    NULL,
                    NULL,
                    NULL,
                    '{}'
                );
                "#,
            )
            .expect("old usage ledger schema is created");
    }

    let mut ledger = SqliteUsageLedger::open(&path)?;
    let old = ledger.get("usage-old")?;

    assert_eq!(old.quota_window_id, None);
    assert_eq!(old.execution_scope, None);

    let new = UsageRecord::new(
        "usage-new",
        UsageSource::RuntimeMeasured,
        UsageConfidence::Estimated,
        [tokens(7)],
        1_010,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-2")
    .with_quota_window_id("tenant-a:2026-06")
    .with_execution_scope("turn:turn-1/model:generate");

    assert_eq!(ledger.append(new.clone())?, new);
    assert_eq!(ledger.records_for_run("run-1")?, vec![old, new]);
    fs::remove_file(path).ok();
    Ok(())
}

#[test]
fn sqlite_usage_ledger_deduplicates_provider_response_and_reconciles_late_usage()
-> Result<(), UsageLedgerError> {
    let mut ledger = SqliteUsageLedger::open_in_memory()?;
    let first = UsageRecord::new(
        "usage-1",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(20)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1")
    .with_quota_window_id("tenant-a:2026-06")
    .with_execution_scope("turn:turn-1/tool:call-1")
    .with_metadata("tool_call_id", "call-1")
    .with_metadata("tool_name", "ticket.create");
    let duplicate = UsageRecord::new(
        "usage-duplicate",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(20)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1")
    .with_quota_window_id("tenant-a:2026-06")
    .with_execution_scope("turn:turn-1/tool:call-1")
    .with_metadata("tool_call_id", "call-1")
    .with_metadata("tool_name", "ticket.create");

    assert_eq!(ledger.append(first.clone())?, first);
    assert_eq!(ledger.append(duplicate)?, first);

    let reconciled = ledger.reconcile(
        "usage-1",
        [tokens(21)],
        1_500,
        Some("usage-reconciled".to_string()),
    )?;

    assert_eq!(reconciled.source, UsageSource::Reconciled);
    assert_eq!(reconciled.reconciliation_of.as_deref(), Some("usage-1"));
    assert_eq!(
        reconciled.quota_window_id.as_deref(),
        Some("tenant-a:2026-06")
    );
    assert_eq!(
        reconciled.execution_scope.as_deref(),
        Some("turn:turn-1/tool:call-1")
    );
    assert_eq!(
        reconciled.metadata.get("tool_call_id").map(String::as_str),
        Some("call-1")
    );
    assert_eq!(
        reconciled.metadata.get("tool_name").map(String::as_str),
        Some("ticket.create")
    );
    assert_eq!(ledger.records_for_run("run-1")?, vec![first, reconciled]);
    Ok(())
}

#[test]
fn sqlite_usage_ledger_rejects_provider_response_replay_with_conflicting_timestamp()
-> Result<(), UsageLedgerError> {
    let mut ledger = SqliteUsageLedger::open_in_memory()?;
    let first = UsageRecord::new(
        "usage-1",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(20)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1");
    let conflicting_timestamp = UsageRecord::new(
        "usage-conflict",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(20)],
        1_010,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1");

    assert_eq!(ledger.append(first.clone())?, first);
    assert_eq!(
        ledger.append(conflicting_timestamp),
        Err(UsageLedgerError::RecordConflict {
            record_id: "resp-1".to_string()
        })
    );
    assert_eq!(ledger.records_for_run("run-1")?, vec![first]);
    Ok(())
}

#[test]
fn sqlite_usage_ledger_rejects_conflicting_provider_response_replay() -> Result<(), UsageLedgerError>
{
    let mut ledger = SqliteUsageLedger::open_in_memory()?;
    let first = UsageRecord::new(
        "usage-1",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(20)],
        1_000,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1");
    let conflicting_replay = UsageRecord::new(
        "usage-conflict",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(21)],
        1_010,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1");

    assert_eq!(ledger.append(first.clone())?, first);
    assert_eq!(
        ledger.append(conflicting_replay),
        Err(UsageLedgerError::RecordConflict {
            record_id: "resp-1".to_string()
        })
    );
    assert_eq!(ledger.records_for_run("run-1")?, vec![first]);
    Ok(())
}

#[test]
fn sqlite_usage_ledger_totals_replace_provisional_with_reconciled_usage()
-> Result<(), UsageLedgerError> {
    let mut ledger = SqliteUsageLedger::open_in_memory()?;
    ledger.append(
        UsageRecord::new(
            "usage-provisional",
            UsageSource::TokenizerEstimated,
            UsageConfidence::Estimated,
            [tokens(18)],
            1_000,
        )
        .with_run_id("run-1")
        .with_attempt_id("attempt-1")
        .with_provider_response_id("resp-1"),
    )?;
    ledger.append(
        UsageRecord::new(
            "usage-runtime",
            UsageSource::RuntimeMeasured,
            UsageConfidence::Estimated,
            [tokens(2)],
            1_010,
        )
        .with_run_id("run-1")
        .with_attempt_id("attempt-2"),
    )?;
    ledger.reconcile(
        "usage-provisional",
        [tokens(21)],
        1_500,
        Some("usage-reconciled".to_string()),
    )?;

    assert_eq!(ledger.totals_for_run("run-1")?, vec![tokens(23)]);
    Ok(())
}

#[test]
fn sqlite_usage_ledger_rejects_multiple_reconciliations_for_same_source_record()
-> Result<(), UsageLedgerError> {
    let mut ledger = SqliteUsageLedger::open_in_memory()?;
    let provisional = ledger.append(
        UsageRecord::new(
            "usage-provisional",
            UsageSource::TokenizerEstimated,
            UsageConfidence::Estimated,
            [tokens(18)],
            1_000,
        )
        .with_run_id("run-1")
        .with_attempt_id("attempt-1")
        .with_provider_response_id("resp-1"),
    )?;
    let first = ledger.reconcile(
        "usage-provisional",
        [tokens(21)],
        1_500,
        Some("usage-reconciled-1".to_string()),
    )?;

    assert_eq!(
        ledger.reconcile(
            "usage-provisional",
            [tokens(22)],
            1_600,
            Some("usage-reconciled-2".to_string()),
        ),
        Err(UsageLedgerError::RecordConflict {
            record_id: "usage-provisional".to_string()
        })
    );
    assert_eq!(ledger.records_for_run("run-1")?, vec![provisional, first]);
    assert_eq!(ledger.totals_for_run("run-1")?, vec![tokens(21)]);
    Ok(())
}

use graphblocks_runtime_core::usage::{
    InMemoryUsageLedger, SqliteUsageLedger, UsageAmount, UsageConfidence, UsageLedgerError,
    UsageRecord, UsageSource,
};
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
    .with_provider_response_id("resp-1");
    let duplicate = UsageRecord::new(
        "usage-duplicate",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(20)],
        1_010,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1");

    assert_eq!(ledger.append(first.clone())?, first);
    assert_eq!(ledger.append(duplicate)?, first);
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
    .with_metadata("tool_call_id", "call-1")
    .with_metadata("tool_name", "ticket.create");
    let duplicate = UsageRecord::new(
        "usage-duplicate",
        UsageSource::ProviderReported,
        UsageConfidence::ProviderExact,
        [tokens(20)],
        1_010,
    )
    .with_run_id("run-1")
    .with_attempt_id("attempt-1")
    .with_provider_response_id("resp-1");

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

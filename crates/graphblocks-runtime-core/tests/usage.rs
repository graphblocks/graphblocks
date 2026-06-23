use graphblocks_runtime_core::usage::{
    InMemoryUsageLedger, UsageAmount, UsageConfidence, UsageLedgerError, UsageRecord, UsageSource,
};

fn tokens(amount: i64) -> UsageAmount {
    UsageAmount::new("model_output_tokens", amount, "tokens")
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
        .with_pricing_ref("pricing-2026-06"),
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
        ledger.records_for_run("run-1"),
        vec![provisional, reconciled]
    );
    Ok(())
}

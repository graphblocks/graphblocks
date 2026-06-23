use graphblocks_runtime_core::budget::{
    BudgetError, BudgetStatus, InMemoryBudgetLedger, ReservationPurpose, ReservationStatus,
    UsageAmount,
};

fn tokens(amount: i64) -> UsageAmount {
    UsageAmount::new("model_total_tokens", amount, "tokens")
}

#[test]
fn budget_ledger_reserve_reduces_available_balance() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;

    let reservation = ledger.reserve(
        "budget-1",
        "run:1",
        [tokens(40)],
        ReservationPurpose::ProviderCall,
        "2026-06-22T01:00:00Z",
        None,
    )?;

    let balance = ledger.balance("budget-1")?;
    assert_eq!(reservation.fencing_token, 1);
    assert_eq!(reservation.status, ReservationStatus::Reserved);
    assert_eq!(balance.reserved, vec![tokens(40)]);
    assert_eq!(balance.available, vec![tokens(60)]);
    assert_eq!(balance.revision, 2);
    Ok(())
}

#[test]
fn budget_ledger_rejects_reservation_above_available_balance() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
    ledger.reserve(
        "budget-1",
        "run:1",
        [tokens(80)],
        ReservationPurpose::ProviderCall,
        "later",
        None,
    )?;

    let error = ledger
        .reserve(
            "budget-1",
            "run:2",
            [tokens(30)],
            ReservationPurpose::ProviderCall,
            "later",
            None,
        )
        .expect_err("reservation above available balance is rejected");

    assert_eq!(
        error,
        BudgetError::BudgetExceeded {
            budget_id: "budget-1".to_string(),
            kind: "model_total_tokens".to_string(),
            unit: "tokens".to_string(),
        }
    );
    Ok(())
}

#[test]
fn budget_ledger_commit_releases_unused_reservation() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
    let reservation = ledger.reserve(
        "budget-1",
        "run:1",
        [tokens(40)],
        ReservationPurpose::ProviderCall,
        "later",
        None,
    )?;

    let settlement = ledger.commit(&reservation.reservation_id, [tokens(25)])?;

    let balance = ledger.balance("budget-1")?;
    assert_eq!(settlement.committed, vec![tokens(25)]);
    assert_eq!(settlement.released, vec![tokens(15)]);
    assert_eq!(settlement.status, ReservationStatus::Committed);
    assert_eq!(balance.reserved, Vec::<UsageAmount>::new());
    assert_eq!(balance.committed, vec![tokens(25)]);
    assert_eq!(balance.available, vec![tokens(75)]);
    Ok(())
}

#[test]
fn budget_ledger_release_restores_available_balance() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
    let reservation = ledger.reserve(
        "budget-1",
        "run:1",
        [tokens(40)],
        ReservationPurpose::ProviderCall,
        "later",
        None,
    )?;

    let settlement = ledger.release(&reservation.reservation_id)?;

    assert_eq!(settlement.released, vec![tokens(40)]);
    assert_eq!(settlement.status, ReservationStatus::Released);
    assert_eq!(ledger.balance("budget-1")?.available, vec![tokens(100)]);
    Ok(())
}

#[test]
fn budget_ledger_commit_over_reserved_records_overdraft() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    let account = ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
    assert_eq!(account.status, BudgetStatus::Active);
    let reservation = ledger.reserve(
        "budget-1",
        "run:1",
        [tokens(40)],
        ReservationPurpose::ProviderCall,
        "later",
        None,
    )?;

    let settlement = ledger.commit(&reservation.reservation_id, [tokens(50)])?;

    let balance = ledger.balance("budget-1")?;
    assert_eq!(settlement.overdraft, vec![tokens(10)]);
    assert_eq!(balance.committed, vec![tokens(50)]);
    assert_eq!(balance.overdraft, vec![tokens(10)]);
    assert_eq!(balance.available, vec![tokens(50)]);
    Ok(())
}

#[test]
fn hierarchical_budget_reservation_holds_child_and_parent_balance() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate(
        "tenant-budget",
        "tenant:acme",
        [tokens(100)],
        "tenant-policy",
        None,
    )?;
    ledger.allocate(
        "run-budget",
        "run:1",
        [tokens(80)],
        "run-policy",
        Some("tenant-budget".to_string()),
    )?;

    ledger.reserve(
        "run-budget",
        "attempt:1",
        [tokens(70)],
        ReservationPurpose::ProviderCall,
        "later",
        None,
    )?;

    assert_eq!(ledger.balance("run-budget")?.available, vec![tokens(10)]);
    assert_eq!(ledger.balance("tenant-budget")?.available, vec![tokens(30)]);
    Ok(())
}

#[test]
fn hierarchical_budget_reservation_rejects_when_parent_balance_is_insufficient()
-> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate(
        "tenant-budget",
        "tenant:acme",
        [tokens(100)],
        "tenant-policy",
        None,
    )?;
    ledger.allocate(
        "run-budget",
        "run:1",
        [tokens(120)],
        "run-policy",
        Some("tenant-budget".to_string()),
    )?;
    ledger.reserve(
        "run-budget",
        "attempt:1",
        [tokens(80)],
        ReservationPurpose::ProviderCall,
        "later",
        None,
    )?;

    let error = ledger
        .reserve(
            "run-budget",
            "attempt:2",
            [tokens(30)],
            ReservationPurpose::ProviderCall,
            "later",
            None,
        )
        .expect_err("parent budget must reject oversubscription");

    assert_eq!(
        error,
        BudgetError::BudgetExceeded {
            budget_id: "tenant-budget".to_string(),
            kind: "model_total_tokens".to_string(),
            unit: "tokens".to_string(),
        }
    );
    Ok(())
}

#[test]
fn hierarchical_budget_release_restores_child_and_parent_balance() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate(
        "tenant-budget",
        "tenant:acme",
        [tokens(100)],
        "tenant-policy",
        None,
    )?;
    ledger.allocate(
        "run-budget",
        "run:1",
        [tokens(80)],
        "run-policy",
        Some("tenant-budget".to_string()),
    )?;
    let reservation = ledger.reserve(
        "run-budget",
        "attempt:1",
        [tokens(70)],
        ReservationPurpose::ProviderCall,
        "later",
        None,
    )?;

    ledger.release(&reservation.reservation_id)?;

    assert_eq!(ledger.balance("run-budget")?.available, vec![tokens(80)]);
    assert_eq!(
        ledger.balance("tenant-budget")?.available,
        vec![tokens(100)]
    );
    Ok(())
}

#[test]
fn hierarchical_budget_commit_settles_child_and_parent_balance() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate(
        "tenant-budget",
        "tenant:acme",
        [tokens(100)],
        "tenant-policy",
        None,
    )?;
    ledger.allocate(
        "run-budget",
        "run:1",
        [tokens(80)],
        "run-policy",
        Some("tenant-budget".to_string()),
    )?;
    let reservation = ledger.reserve(
        "run-budget",
        "attempt:1",
        [tokens(70)],
        ReservationPurpose::ProviderCall,
        "later",
        None,
    )?;

    ledger.commit(&reservation.reservation_id, [tokens(55)])?;

    assert_eq!(ledger.balance("run-budget")?.committed, vec![tokens(55)]);
    assert_eq!(ledger.balance("tenant-budget")?.committed, vec![tokens(55)]);
    assert_eq!(ledger.balance("run-budget")?.available, vec![tokens(25)]);
    assert_eq!(ledger.balance("tenant-budget")?.available, vec![tokens(45)]);
    Ok(())
}

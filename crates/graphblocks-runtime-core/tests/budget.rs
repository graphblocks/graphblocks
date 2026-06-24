use graphblocks_runtime_core::budget::{
    BudgetError, BudgetStatus, CompletionReservePurpose, CompletionReserveStatus,
    InMemoryBudgetLedger, ReservationPurpose, ReservationStatus, UsageAmount,
};
use std::collections::BTreeMap;

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
fn budget_ledger_commit_allows_overdraft_within_limit() -> Result<(), BudgetError> {
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

    let settlement = ledger.commit_with_overdraft_limit(
        &reservation.reservation_id,
        [tokens(45)],
        [tokens(5)],
    )?;

    assert_eq!(settlement.overdraft, vec![tokens(5)]);
    assert_eq!(ledger.balance("budget-1")?.committed, vec![tokens(45)]);
    Ok(())
}

#[test]
fn budget_ledger_rejects_commit_above_overdraft_limit_without_mutating_balance()
-> Result<(), BudgetError> {
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

    let error = ledger
        .commit_with_overdraft_limit(&reservation.reservation_id, [tokens(46)], [tokens(5)])
        .expect_err("overdraft above the declared limit is rejected");

    assert_eq!(
        error,
        BudgetError::BudgetExceeded {
            budget_id: "budget-1".to_string(),
            kind: "model_total_tokens".to_string(),
            unit: "tokens".to_string(),
        }
    );
    let balance = ledger.balance("budget-1")?;
    assert_eq!(balance.reserved, vec![tokens(40)]);
    assert_eq!(balance.committed, Vec::<UsageAmount>::new());
    assert_eq!(balance.overdraft, Vec::<UsageAmount>::new());
    assert_eq!(balance.available, vec![tokens(60)]);
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

#[test]
fn budget_ledger_issues_bounded_permit_from_reservations() -> Result<(), BudgetError> {
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

    let permit = ledger.issue_permit(
        "permit-1",
        vec![reservation.reservation_id.clone()],
        "worker:1",
        "turn:1",
        3,
        "finish_current_turn",
        "sha256:policy",
        "2026-06-22T01:00:00Z",
        Vec::new(),
    )?;

    assert_eq!(permit.permit_id, "permit-1");
    assert_eq!(permit.reservation_refs, vec![reservation.reservation_id]);
    assert_eq!(permit.authorized_amounts, vec![tokens(40)]);
    assert_eq!(
        permit.fencing_tokens,
        BTreeMap::from([("budget-1".to_string(), reservation.fencing_token)])
    );
    assert_eq!(permit.owner, "worker:1");
    assert_eq!(permit.atomic_unit, "turn:1");
    assert_eq!(permit.continuation_profile, "finish_current_turn");
    assert_eq!(permit.policy_snapshot_digest, "sha256:policy");
    assert!(permit.allows([tokens(25)]));
    assert!(!permit.allows([tokens(41)]));
    Ok(())
}

#[test]
fn budget_permit_requires_matching_usage_dimensions() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate(
        "budget-1",
        "tenant:acme",
        [tokens(100).with_dimension("model", "small")],
        "policy-1",
        None,
    )?;
    let reservation = ledger.reserve(
        "budget-1",
        "run:1",
        [tokens(40).with_dimension("model", "small")],
        ReservationPurpose::ProviderCall,
        "later",
        None,
    )?;

    let permit = ledger.issue_permit(
        "permit-1",
        vec![reservation.reservation_id],
        "worker:1",
        "turn:1",
        1,
        "finish_current_turn",
        "sha256:policy",
        "later",
        Vec::new(),
    )?;

    assert!(permit.allows([tokens(20).with_dimension("model", "small")]));
    assert!(!permit.allows([tokens(20).with_dimension("model", "large")]));
    Ok(())
}

#[test]
fn budget_ledger_permit_combines_multiple_reservations() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
    let first = ledger.reserve(
        "budget-1",
        "run:1",
        [tokens(25)],
        ReservationPurpose::Task,
        "later",
        None,
    )?;
    let second = ledger.reserve(
        "budget-1",
        "run:1",
        [tokens(15)],
        ReservationPurpose::Finalization,
        "later",
        None,
    )?;

    let permit = ledger.issue_permit(
        "permit-1",
        vec![first.reservation_id.clone(), second.reservation_id.clone()],
        "worker:1",
        "turn:1",
        1,
        "hard_stop",
        "sha256:policy",
        "later",
        Vec::new(),
    )?;

    assert_eq!(permit.authorized_amounts, vec![tokens(40)]);
    assert_eq!(
        permit.fencing_tokens,
        BTreeMap::from([("budget-1".to_string(), second.fencing_token)])
    );
    Ok(())
}

#[test]
fn budget_ledger_permit_includes_parent_fencing_tokens() -> Result<(), BudgetError> {
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
        [tokens(40)],
        ReservationPurpose::ProviderCall,
        "later",
        None,
    )?;

    let permit = ledger.issue_permit(
        "permit-1",
        vec![reservation.reservation_id],
        "worker:1",
        "turn:1",
        1,
        "finish_current_turn",
        "sha256:policy",
        "later",
        vec![tokens(10)],
    )?;

    assert_eq!(permit.low_watermark, vec![tokens(10)]);
    assert_eq!(
        permit.fencing_tokens,
        BTreeMap::from([
            ("run-budget".to_string(), 1),
            ("tenant-budget".to_string(), 1),
        ])
    );
    Ok(())
}

#[test]
fn budget_ledger_rejects_permit_for_released_reservation() -> Result<(), BudgetError> {
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
    ledger.release(&reservation.reservation_id)?;

    let error = ledger
        .issue_permit(
            "permit-1",
            vec![reservation.reservation_id.clone()],
            "worker:1",
            "turn:1",
            1,
            "hard_stop",
            "sha256:policy",
            "later",
            Vec::new(),
        )
        .expect_err("released reservation cannot authorize a permit");

    assert_eq!(
        error,
        BudgetError::ReservationState {
            reservation_id: reservation.reservation_id,
            status: ReservationStatus::Released,
        }
    );
    Ok(())
}

#[test]
fn budget_ledger_rejects_duplicate_permit_ids() -> Result<(), BudgetError> {
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
    let first = ledger.issue_permit(
        "permit-1",
        vec![reservation.reservation_id.clone()],
        "worker:1",
        "turn:1",
        1,
        "hard_stop",
        "sha256:policy",
        "later",
        Vec::new(),
    )?;
    assert_eq!(first.permit_id, "permit-1");

    let error = ledger
        .issue_permit(
            "permit-1",
            vec![reservation.reservation_id],
            "worker:2",
            "turn:2",
            2,
            "hard_stop",
            "sha256:policy",
            "later",
            Vec::<UsageAmount>::new(),
        )
        .expect_err("duplicate permit id is rejected");

    assert_eq!(
        error,
        BudgetError::PermitConflict {
            permit_id: "permit-1".to_string(),
        }
    );
    Ok(())
}

#[test]
fn budget_ledger_commit_with_permit_settles_authorized_reservation() -> Result<(), BudgetError> {
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
    let permit = ledger.issue_permit(
        "permit-1",
        vec![reservation.reservation_id.clone()],
        "worker:1",
        "turn:1",
        1,
        "finish_current_turn",
        "sha256:policy",
        "later",
        Vec::new(),
    )?;

    let settlement =
        ledger.commit_with_permit(&permit.permit_id, &reservation.reservation_id, [tokens(25)])?;

    assert_eq!(settlement.committed, vec![tokens(25)]);
    assert_eq!(settlement.released, vec![tokens(15)]);
    assert_eq!(ledger.balance("budget-1")?.available, vec![tokens(75)]);
    Ok(())
}

#[test]
fn budget_ledger_release_with_permit_restores_authorized_reservation() -> Result<(), BudgetError> {
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
    let permit = ledger.issue_permit(
        "permit-1",
        vec![reservation.reservation_id.clone()],
        "worker:1",
        "turn:1",
        1,
        "finish_current_turn",
        "sha256:policy",
        "later",
        Vec::new(),
    )?;

    let settlement = ledger.release_with_permit(&permit.permit_id, &reservation.reservation_id)?;

    assert_eq!(settlement.released, vec![tokens(40)]);
    assert_eq!(ledger.balance("budget-1")?.available, vec![tokens(100)]);
    Ok(())
}

#[test]
fn budget_ledger_commit_with_permit_rejects_usage_above_authorized_without_mutating()
-> Result<(), BudgetError> {
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
    let permit = ledger.issue_permit(
        "permit-1",
        vec![reservation.reservation_id.clone()],
        "worker:1",
        "turn:1",
        1,
        "finish_current_turn",
        "sha256:policy",
        "later",
        Vec::new(),
    )?;

    let error = ledger
        .commit_with_permit(&permit.permit_id, &reservation.reservation_id, [tokens(41)])
        .expect_err("permit-backed commit must stay within authorized usage");

    assert_eq!(
        error,
        BudgetError::BudgetExceeded {
            budget_id: "budget-1".to_string(),
            kind: "model_total_tokens".to_string(),
            unit: "tokens".to_string(),
        }
    );
    let balance = ledger.balance("budget-1")?;
    assert_eq!(balance.reserved, vec![tokens(40)]);
    assert_eq!(balance.committed, Vec::<UsageAmount>::new());
    assert_eq!(balance.available, vec![tokens(60)]);
    Ok(())
}

#[test]
fn budget_ledger_permit_cannot_settle_unreferenced_reservation() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
    let first = ledger.reserve(
        "budget-1",
        "run:1",
        [tokens(25)],
        ReservationPurpose::Task,
        "later",
        None,
    )?;
    let second = ledger.reserve(
        "budget-1",
        "run:1",
        [tokens(15)],
        ReservationPurpose::Task,
        "later",
        None,
    )?;
    let permit = ledger.issue_permit(
        "permit-1",
        vec![first.reservation_id],
        "worker:1",
        "turn:1",
        1,
        "finish_current_turn",
        "sha256:policy",
        "later",
        Vec::new(),
    )?;

    let error = ledger
        .commit_with_permit(&permit.permit_id, &second.reservation_id, [tokens(10)])
        .expect_err("permit cannot settle reservations it does not name");

    assert_eq!(
        error,
        BudgetError::PermitScope {
            permit_id: "permit-1".to_string(),
            reservation_id: second.reservation_id,
        }
    );
    Ok(())
}

#[test]
fn completion_reserve_holds_finalization_capacity_out_of_general_budget() -> Result<(), BudgetError>
{
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;

    let reserve = ledger.create_completion_reserve(
        "finalization-reserve",
        "budget-1",
        CompletionReservePurpose::Finalization,
        [tokens(20)],
        ["agent.finalize"],
        None,
    )?;

    assert_eq!(reserve.status, CompletionReserveStatus::Available);
    assert_eq!(ledger.balance("budget-1")?.reserved, vec![tokens(20)]);
    assert_eq!(ledger.balance("budget-1")?.available, vec![tokens(80)]);
    assert_eq!(
        ledger
            .reserve(
                "budget-1",
                "planner",
                [tokens(90)],
                ReservationPurpose::Task,
                "later",
                None,
            )
            .expect_err("ordinary planning cannot consume the completion reserve"),
        BudgetError::BudgetExceeded {
            budget_id: "budget-1".to_owned(),
            kind: "model_total_tokens".to_owned(),
            unit: "tokens".to_owned(),
        }
    );
    Ok(())
}

#[test]
fn completion_reserve_can_be_spent_by_authorized_finalization_work() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
    ledger.create_completion_reserve(
        "finalization-reserve",
        "budget-1",
        CompletionReservePurpose::Finalization,
        [tokens(20)],
        ["agent.finalize"],
        None,
    )?;

    let reservation =
        ledger.spend_completion_reserve("finalization-reserve", "agent.finalize", "later")?;
    let reserve = ledger.completion_reserve("finalization-reserve")?;

    assert_eq!(reservation.purpose, ReservationPurpose::Finalization);
    assert_eq!(reservation.amounts, vec![tokens(20)]);
    assert_eq!(reserve.status, CompletionReserveStatus::Spent);

    let settlement = ledger.commit(&reservation.reservation_id, [tokens(15)])?;
    let balance = ledger.balance("budget-1")?;

    assert_eq!(settlement.committed, vec![tokens(15)]);
    assert_eq!(settlement.released, vec![tokens(5)]);
    assert_eq!(balance.reserved, Vec::<UsageAmount>::new());
    assert_eq!(balance.committed, vec![tokens(15)]);
    assert_eq!(balance.available, vec![tokens(85)]);
    Ok(())
}

#[test]
fn completion_reserve_rejects_unauthorized_spender() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
    ledger.create_completion_reserve(
        "cleanup-reserve",
        "budget-1",
        CompletionReservePurpose::Cleanup,
        [tokens(10)],
        ["cleanup.worker"],
        None,
    )?;

    assert_eq!(
        ledger
            .spend_completion_reserve("cleanup-reserve", "planner", "later")
            .expect_err("only declared spenders may consume completion reserves"),
        BudgetError::CompletionReserveUnauthorized {
            reserve_id: "cleanup-reserve".to_owned(),
            spender: "planner".to_owned(),
        }
    );
    assert_eq!(
        ledger.completion_reserve("cleanup-reserve")?.status,
        CompletionReserveStatus::Available,
    );
    Ok(())
}

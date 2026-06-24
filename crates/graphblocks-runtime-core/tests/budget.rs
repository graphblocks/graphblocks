use graphblocks_runtime_core::budget::{
    BudgetError, BudgetStatus, CompletionReservePurpose, CompletionReserveStatus,
    InMemoryBudgetLedger, ReservationPurpose, ReservationStatus, SqliteBudgetLedger, UsageAmount,
};
use std::{
    collections::{BTreeMap, BTreeSet},
    env, fs,
    path::PathBuf,
    sync::{Arc, Barrier},
    thread,
};

fn tokens(amount: i64) -> UsageAmount {
    UsageAmount::new("model_total_tokens", amount, "tokens")
}

fn sqlite_budget_path(label: &str) -> PathBuf {
    let mut path = env::temp_dir();
    path.push(format!(
        "graphblocks-sqlite-budget-{label}-{}.sqlite3",
        std::process::id()
    ));
    fs::remove_file(&path).ok();
    path
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
fn budget_ledger_expire_restores_available_balance() -> Result<(), BudgetError> {
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

    let settlement = ledger.expire(&reservation.reservation_id)?;

    let balance = ledger.balance("budget-1")?;
    assert_eq!(settlement.released, vec![tokens(40)]);
    assert_eq!(settlement.status, ReservationStatus::Expired);
    assert_eq!(balance.reserved, Vec::<UsageAmount>::new());
    assert_eq!(balance.committed, Vec::<UsageAmount>::new());
    assert_eq!(balance.available, vec![tokens(100)]);
    Ok(())
}

#[test]
fn budget_ledger_expired_reservation_cannot_be_settled_or_authorize_permit()
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

    ledger.expire(&reservation.reservation_id)?;

    assert_eq!(
        ledger
            .commit(&reservation.reservation_id, [tokens(1)])
            .expect_err("expired reservation cannot be committed"),
        BudgetError::ReservationState {
            reservation_id: reservation.reservation_id.clone(),
            status: ReservationStatus::Expired,
        }
    );
    assert_eq!(
        ledger
            .release(&reservation.reservation_id)
            .expect_err("expired reservation cannot be released"),
        BudgetError::ReservationState {
            reservation_id: reservation.reservation_id.clone(),
            status: ReservationStatus::Expired,
        }
    );
    assert_eq!(
        ledger
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
            .expect_err("expired reservation cannot authorize a permit"),
        BudgetError::ReservationState {
            reservation_id: reservation.reservation_id,
            status: ReservationStatus::Expired,
        }
    );
    Ok(())
}

#[test]
fn sqlite_budget_ledger_persists_reserved_balance_across_reopen() -> Result<(), BudgetError> {
    let path = sqlite_budget_path("reserve-persist");

    {
        let mut ledger = SqliteBudgetLedger::open(&path)?;
        ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
        let reservation = ledger.reserve(
            "budget-1",
            "run:1",
            [tokens(40)],
            ReservationPurpose::ProviderCall,
            "later",
            None,
        )?;

        assert_eq!(reservation.fencing_token, 1);
        assert_eq!(ledger.balance("budget-1")?.available, vec![tokens(60)]);
    }

    let ledger = SqliteBudgetLedger::open(&path)?;
    let balance = ledger.balance("budget-1")?;

    assert_eq!(balance.reserved, vec![tokens(40)]);
    assert_eq!(balance.committed, Vec::<UsageAmount>::new());
    assert_eq!(balance.available, vec![tokens(60)]);
    assert_eq!(balance.revision, 2);
    fs::remove_file(path).ok();
    Ok(())
}

#[test]
fn sqlite_budget_ledger_hierarchical_reserve_holds_parent_capacity() -> Result<(), BudgetError> {
    let mut ledger = SqliteBudgetLedger::open_in_memory()?;
    ledger.allocate(
        "tenant-budget",
        "tenant:acme",
        [tokens(100)],
        "policy-1",
        None,
    )?;
    ledger.allocate(
        "run-budget",
        "run:1",
        [tokens(80)],
        "policy-1",
        Some("tenant-budget".to_string()),
    )?;

    ledger.reserve(
        "run-budget",
        "run:1",
        [tokens(40)],
        ReservationPurpose::ProviderCall,
        "later",
        None,
    )?;

    assert_eq!(ledger.balance("run-budget")?.reserved, vec![tokens(40)]);
    assert_eq!(ledger.balance("run-budget")?.available, vec![tokens(40)]);
    assert_eq!(ledger.balance("tenant-budget")?.reserved, vec![tokens(40)]);
    assert_eq!(ledger.balance("tenant-budget")?.available, vec![tokens(60)]);

    let error = ledger
        .reserve(
            "run-budget",
            "run:2",
            [tokens(45)],
            ReservationPurpose::ProviderCall,
            "later",
            None,
        )
        .expect_err("child budget reservation cannot exceed its own available balance");

    assert_eq!(
        error,
        BudgetError::BudgetExceeded {
            budget_id: "run-budget".to_string(),
            kind: "model_total_tokens".to_string(),
            unit: "tokens".to_string(),
        }
    );
    Ok(())
}

#[test]
fn sqlite_budget_ledger_commit_releases_unused_reservation_across_reopen() -> Result<(), BudgetError>
{
    let path = sqlite_budget_path("commit-persist");

    {
        let mut ledger = SqliteBudgetLedger::open(&path)?;
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

        assert_eq!(settlement.committed, vec![tokens(25)]);
        assert_eq!(settlement.released, vec![tokens(15)]);
        assert_eq!(settlement.status, ReservationStatus::Committed);
        assert_eq!(ledger.balance("budget-1")?.available, vec![tokens(75)]);
    }

    let ledger = SqliteBudgetLedger::open(&path)?;
    let balance = ledger.balance("budget-1")?;

    assert_eq!(balance.reserved, Vec::<UsageAmount>::new());
    assert_eq!(balance.committed, vec![tokens(25)]);
    assert_eq!(balance.available, vec![tokens(75)]);
    assert_eq!(balance.revision, 3);
    fs::remove_file(path).ok();
    Ok(())
}

#[test]
fn sqlite_budget_ledger_release_and_expire_restore_available_balance() -> Result<(), BudgetError> {
    let mut ledger = SqliteBudgetLedger::open_in_memory()?;
    ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
    let released = ledger.reserve(
        "budget-1",
        "run:1",
        [tokens(40)],
        ReservationPurpose::ProviderCall,
        "later",
        None,
    )?;
    let expired = ledger.reserve(
        "budget-1",
        "run:2",
        [tokens(20)],
        ReservationPurpose::ProviderCall,
        "later",
        None,
    )?;

    let release = ledger.release(&released.reservation_id)?;
    let expiration = ledger.expire(&expired.reservation_id)?;

    assert_eq!(release.released, vec![tokens(40)]);
    assert_eq!(release.status, ReservationStatus::Released);
    assert_eq!(expiration.released, vec![tokens(20)]);
    assert_eq!(expiration.status, ReservationStatus::Expired);
    assert_eq!(
        ledger.balance("budget-1")?.reserved,
        Vec::<UsageAmount>::new()
    );
    assert_eq!(ledger.balance("budget-1")?.available, vec![tokens(100)]);
    assert_eq!(
        ledger
            .commit(&expired.reservation_id, [tokens(1)])
            .expect_err("expired reservation cannot be committed"),
        BudgetError::ReservationState {
            reservation_id: expired.reservation_id,
            status: ReservationStatus::Expired,
        }
    );
    Ok(())
}

#[test]
fn sqlite_budget_ledger_serializes_competing_reservations() -> Result<(), BudgetError> {
    let path = sqlite_budget_path("reservation-race");
    {
        let mut ledger = SqliteBudgetLedger::open(&path)?;
        ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
    }

    let barrier = Arc::new(Barrier::new(3));
    let handles = ["run:1", "run:2"]
        .into_iter()
        .map(|owner| {
            let path = path.clone();
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || -> Result<bool, BudgetError> {
                let mut ledger = SqliteBudgetLedger::open(&path)?;
                barrier.wait();
                match ledger.reserve(
                    "budget-1",
                    owner,
                    [tokens(70)],
                    ReservationPurpose::ProviderCall,
                    "later",
                    None,
                ) {
                    Ok(_) => Ok(true),
                    Err(BudgetError::BudgetExceeded { .. }) => Ok(false),
                    Err(error) => Err(error),
                }
            })
        })
        .collect::<Vec<_>>();

    barrier.wait();
    let outcomes = handles
        .into_iter()
        .map(|handle| handle.join().expect("reservation worker thread joins"))
        .collect::<Result<Vec<_>, _>>()?;

    assert_eq!(outcomes.iter().filter(|outcome| **outcome).count(), 1);
    assert_eq!(outcomes.iter().filter(|outcome| !**outcome).count(), 1);

    let ledger = SqliteBudgetLedger::open(&path)?;
    let balance = ledger.balance("budget-1")?;
    assert_eq!(balance.reserved, vec![tokens(70)]);
    assert_eq!(balance.available, vec![tokens(30)]);
    fs::remove_file(path).ok();
    Ok(())
}

#[test]
fn sqlite_budget_ledger_commit_with_permit_settles_authorized_reservation()
-> Result<(), BudgetError> {
    let mut ledger = SqliteBudgetLedger::open_in_memory()?;
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
        vec![tokens(10)],
    )?;

    let settlement =
        ledger.commit_with_permit(&permit.permit_id, &reservation.reservation_id, [tokens(25)])?;

    assert_eq!(permit.authorized_amounts, vec![tokens(40)]);
    assert_eq!(permit.low_watermark, vec![tokens(10)]);
    assert_eq!(
        permit.fencing_tokens,
        BTreeMap::from([("budget-1".to_string(), 1)])
    );
    assert_eq!(settlement.committed, vec![tokens(25)]);
    assert_eq!(settlement.released, vec![tokens(15)]);
    assert_eq!(ledger.balance("budget-1")?.available, vec![tokens(75)]);
    Ok(())
}

#[test]
fn sqlite_budget_ledger_permit_survives_reopen() -> Result<(), BudgetError> {
    let path = sqlite_budget_path("permit-persist");
    let reservation_id;
    let permit_id;

    {
        let mut ledger = SqliteBudgetLedger::open(&path)?;
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
        reservation_id = reservation.reservation_id;
        permit_id = permit.permit_id;
    }

    let mut ledger = SqliteBudgetLedger::open(&path)?;
    let settlement = ledger.commit_with_permit(&permit_id, &reservation_id, [tokens(30)])?;

    assert_eq!(settlement.committed, vec![tokens(30)]);
    assert_eq!(settlement.released, vec![tokens(10)]);
    assert_eq!(ledger.balance("budget-1")?.committed, vec![tokens(30)]);
    fs::remove_file(path).ok();
    Ok(())
}

#[test]
fn sqlite_budget_ledger_commit_with_permit_rejects_usage_above_authorized_without_mutating()
-> Result<(), BudgetError> {
    let mut ledger = SqliteBudgetLedger::open_in_memory()?;
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
        .expect_err("permit-backed SQLite commit must stay within authorized usage");

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

    let settlement = ledger.release_with_permit(&permit.permit_id, &reservation.reservation_id)?;
    assert_eq!(settlement.released, vec![tokens(40)]);
    assert_eq!(ledger.balance("budget-1")?.available, vec![tokens(100)]);
    Ok(())
}

#[test]
fn sqlite_budget_ledger_commit_with_expired_permit_rejects_without_mutating()
-> Result<(), BudgetError> {
    let mut ledger = SqliteBudgetLedger::open_in_memory()?;
    ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
    let reservation = ledger.reserve(
        "budget-1",
        "run:1",
        [tokens(40)],
        ReservationPurpose::ProviderCall,
        "2026-06-22T00:10:00Z",
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
        "2026-06-22T00:05:00Z",
        Vec::new(),
    )?;

    let error = ledger
        .commit_with_permit_at(
            &permit.permit_id,
            &reservation.reservation_id,
            [tokens(25)],
            "2026-06-22T00:05:00Z",
        )
        .expect_err("expired permit cannot settle a SQLite reservation");

    assert_eq!(
        error,
        BudgetError::PermitExpired {
            permit_id: "permit-1".to_string(),
            expires_at: "2026-06-22T00:05:00Z".to_string(),
            now: "2026-06-22T00:05:00Z".to_string(),
        }
    );
    let balance = ledger.balance("budget-1")?;
    assert_eq!(balance.reserved, vec![tokens(40)]);
    assert_eq!(balance.committed, Vec::<UsageAmount>::new());
    assert_eq!(balance.available, vec![tokens(60)]);
    Ok(())
}

#[test]
fn sqlite_completion_reserve_holds_capacity_across_reopen() -> Result<(), BudgetError> {
    let path = sqlite_budget_path("completion-reserve-persist");

    {
        let mut ledger = SqliteBudgetLedger::open(&path)?;
        ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
        let reserve = ledger.create_completion_reserve(
            "finalization-reserve",
            "budget-1",
            CompletionReservePurpose::Finalization,
            [tokens(20)],
            ["agent.finalize"],
            Some("later".to_string()),
        )?;

        assert_eq!(reserve.status, CompletionReserveStatus::Available);
        assert_eq!(reserve.fencing_token, 1);
        assert_eq!(ledger.balance("budget-1")?.reserved, vec![tokens(20)]);
        assert_eq!(ledger.balance("budget-1")?.available, vec![tokens(80)]);
    }

    let mut ledger = SqliteBudgetLedger::open(&path)?;
    let reserve = ledger.completion_reserve("finalization-reserve")?;

    assert_eq!(reserve.status, CompletionReserveStatus::Available);
    assert_eq!(
        reserve.spendable_by,
        BTreeSet::from(["agent.finalize".to_string()])
    );
    assert_eq!(reserve.expires_at.as_deref(), Some("later"));
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
            .expect_err("completion reserve capacity is not generally available"),
        BudgetError::BudgetExceeded {
            budget_id: "budget-1".to_string(),
            kind: "model_total_tokens".to_string(),
            unit: "tokens".to_string(),
        }
    );
    fs::remove_file(path).ok();
    Ok(())
}

#[test]
fn sqlite_completion_reserve_spend_commits_held_capacity() -> Result<(), BudgetError> {
    let mut ledger = SqliteBudgetLedger::open_in_memory()?;
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
    assert_eq!(reservation.fencing_token, reserve.fencing_token);
    assert_eq!(reserve.status, CompletionReserveStatus::Spent);
    assert_eq!(
        reserve.reservation_id.as_deref(),
        Some(reservation.reservation_id.as_str())
    );
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
fn sqlite_completion_reserve_can_be_spent_after_reopen() -> Result<(), BudgetError> {
    let path = sqlite_budget_path("completion-reserve-spend-reopen");

    {
        let mut ledger = SqliteBudgetLedger::open(&path)?;
        ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
        ledger.create_completion_reserve(
            "finalization-reserve",
            "budget-1",
            CompletionReservePurpose::Finalization,
            [tokens(20)],
            ["agent.finalize"],
            None,
        )?;
    }

    {
        let mut ledger = SqliteBudgetLedger::open(&path)?;
        let reservation =
            ledger.spend_completion_reserve("finalization-reserve", "agent.finalize", "later")?;
        let reserve = ledger.completion_reserve("finalization-reserve")?;
        assert_eq!(reserve.status, CompletionReserveStatus::Spent);
        assert_eq!(
            reserve.reservation_id.as_deref(),
            Some(reservation.reservation_id.as_str())
        );
        ledger.commit(&reservation.reservation_id, [tokens(18)])?;
    }

    let ledger = SqliteBudgetLedger::open(&path)?;
    assert_eq!(ledger.balance("budget-1")?.committed, vec![tokens(18)]);
    assert_eq!(ledger.balance("budget-1")?.available, vec![tokens(82)]);
    fs::remove_file(path).ok();
    Ok(())
}

#[test]
fn sqlite_completion_reserve_serializes_competing_spenders() -> Result<(), BudgetError> {
    let path = sqlite_budget_path("completion-reserve-race");
    {
        let mut ledger = SqliteBudgetLedger::open(&path)?;
        ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
        ledger.create_completion_reserve(
            "finalization-reserve",
            "budget-1",
            CompletionReservePurpose::Finalization,
            [tokens(20)],
            ["agent.finalize"],
            None,
        )?;
    }

    let barrier = Arc::new(Barrier::new(3));
    let handles = ["agent.finalize", "agent.finalize"]
        .into_iter()
        .map(|spender| {
            let path = path.clone();
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || -> Result<bool, BudgetError> {
                let mut ledger = SqliteBudgetLedger::open(&path)?;
                barrier.wait();
                match ledger.spend_completion_reserve("finalization-reserve", spender, "later") {
                    Ok(_) => Ok(true),
                    Err(BudgetError::CompletionReserveState { .. }) => Ok(false),
                    Err(error) => Err(error),
                }
            })
        })
        .collect::<Vec<_>>();

    barrier.wait();
    let outcomes = handles
        .into_iter()
        .map(|handle| handle.join().expect("completion reserve worker joins"))
        .collect::<Result<Vec<_>, _>>()?;

    assert_eq!(outcomes.iter().filter(|outcome| **outcome).count(), 1);
    assert_eq!(outcomes.iter().filter(|outcome| !**outcome).count(), 1);

    let ledger = SqliteBudgetLedger::open(&path)?;
    let reserve = ledger.completion_reserve("finalization-reserve")?;
    assert_eq!(reserve.status, CompletionReserveStatus::Spent);
    assert!(reserve.reservation_id.is_some());
    assert_eq!(ledger.balance("budget-1")?.reserved, vec![tokens(20)]);
    fs::remove_file(path).ok();
    Ok(())
}

#[test]
fn sqlite_completion_reserve_rejects_unauthorized_spender() -> Result<(), BudgetError> {
    let mut ledger = SqliteBudgetLedger::open_in_memory()?;
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
            .expect_err("unauthorized completion reserve spender is rejected"),
        BudgetError::CompletionReserveUnauthorized {
            reserve_id: "cleanup-reserve".to_string(),
            spender: "planner".to_string(),
        }
    );
    assert_eq!(
        ledger.completion_reserve("cleanup-reserve")?.status,
        CompletionReserveStatus::Available,
    );
    assert_eq!(ledger.balance("budget-1")?.reserved, vec![tokens(10)]);
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
fn budget_ledger_commit_with_expired_permit_rejects_without_mutating() -> Result<(), BudgetError> {
    let mut ledger = InMemoryBudgetLedger::new();
    ledger.allocate("budget-1", "tenant:acme", [tokens(100)], "policy-1", None)?;
    let reservation = ledger.reserve(
        "budget-1",
        "run:1",
        [tokens(40)],
        ReservationPurpose::ProviderCall,
        "2026-06-22T00:10:00Z",
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
        "2026-06-22T00:05:00Z",
        Vec::new(),
    )?;

    let error = ledger
        .commit_with_permit_at(
            &permit.permit_id,
            &reservation.reservation_id,
            [tokens(25)],
            "2026-06-22T00:05:00Z",
        )
        .expect_err("expired permit cannot settle a reservation");

    assert_eq!(
        error,
        BudgetError::PermitExpired {
            permit_id: "permit-1".to_string(),
            expires_at: "2026-06-22T00:05:00Z".to_string(),
            now: "2026-06-22T00:05:00Z".to_string(),
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

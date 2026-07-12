use graphblocks_runtime_core::budget::{
    BudgetError, CompletionReservePurpose, CompletionReserveStatus, ReservationPurpose,
    SqliteBudgetLedger, UsageAmount,
};
use serde_json::Value;
use std::{
    env, fs,
    path::PathBuf,
    sync::{Arc, Barrier},
    thread,
};

#[test]
fn rust_budget_race_matches_shared_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("fixtures/budget-race-cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "budget-race TCK root must be an array".to_owned())?;

    for case in cases {
        run_case(case)?;
    }

    Ok(())
}

fn run_case(case: &Value) -> Result<(), String> {
    let name = required_str(case, "name", "budget-race TCK case")?;
    match required_str(case, "kind", name)? {
        "reservation_race" => run_reservation_race(case, name),
        "completion_reserve_race" => run_completion_reserve_race(case, name),
        kind => Err(format!(
            "budget-race TCK case {name} has unknown kind {kind}"
        )),
    }
}

fn run_reservation_race(case: &Value, name: &str) -> Result<(), String> {
    let path = sqlite_budget_path(name);
    let budget_id = required_str(case, "budgetId", name)?.to_owned();
    let allocated = usage_amounts(required_value(case, "allocated", name)?, name)?;
    {
        let mut ledger = SqliteBudgetLedger::open(&path).map_err(budget_error)?;
        ledger
            .allocate(
                &budget_id,
                required_str(case, "scope", name)?,
                allocated,
                required_str(case, "policyRef", name)?,
                None,
            )
            .map_err(budget_error)?;
    }

    let owners = string_array(required_value(case, "owners", name)?, name)?;
    let reservation_amounts =
        usage_amounts(required_value(case, "reservationAmounts", name)?, name)?;
    let expires_at = required_str(case, "expiresAt", name)?.to_owned();
    let purpose = match required_str(case, "reservationPurpose", name)? {
        "provider_call" => ReservationPurpose::ProviderCall,
        "task" => ReservationPurpose::Task,
        "trial" => ReservationPurpose::Trial,
        "tool" => ReservationPurpose::Tool,
        "finalization" => ReservationPurpose::Finalization,
        "cleanup" => ReservationPurpose::Cleanup,
        other => {
            return Err(format!(
                "budget-race TCK case {name} has unknown reservation purpose {other}"
            ));
        }
    };
    let barrier = Arc::new(Barrier::new(owners.len() + 1));
    let handles = owners
        .into_iter()
        .map(|owner| {
            let path = path.clone();
            let budget_id = budget_id.clone();
            let barrier = Arc::clone(&barrier);
            let reservation_amounts = reservation_amounts.clone();
            let expires_at = expires_at.clone();
            thread::spawn(move || -> Result<WorkerOutcome, BudgetError> {
                let mut ledger = SqliteBudgetLedger::open(&path)?;
                barrier.wait();
                match ledger.reserve(
                    &budget_id,
                    owner,
                    reservation_amounts,
                    purpose,
                    expires_at,
                    None,
                ) {
                    Ok(_) => Ok(WorkerOutcome::allowed()),
                    Err(BudgetError::BudgetExceeded { .. }) => {
                        Ok(WorkerOutcome::denied("BudgetExceeded"))
                    }
                    Err(error) => Err(error),
                }
            })
        })
        .collect::<Vec<_>>();

    barrier.wait();
    let outcomes = handles
        .into_iter()
        .map(|handle| handle.join().expect("reservation race worker joins"))
        .collect::<Result<Vec<_>, _>>()
        .map_err(budget_error)?;

    assert_worker_outcomes(name, case, &outcomes)?;
    let ledger = SqliteBudgetLedger::open(&path).map_err(budget_error)?;
    let balance = ledger.balance(&budget_id).map_err(budget_error)?;
    assert_eq!(
        balance.reserved,
        expected_amounts(case, "expectedReserved", name)?,
        "{name}"
    );
    assert_eq!(
        balance.available,
        expected_amounts(case, "expectedAvailable", name)?,
        "{name}"
    );
    fs::remove_file(path).ok();
    Ok(())
}

fn run_completion_reserve_race(case: &Value, name: &str) -> Result<(), String> {
    let path = sqlite_budget_path(name);
    let budget_id = required_str(case, "budgetId", name)?.to_owned();
    let reserve_id = required_str(case, "reserveId", name)?.to_owned();
    let allocated = usage_amounts(required_value(case, "allocated", name)?, name)?;
    {
        let mut ledger = SqliteBudgetLedger::open(&path).map_err(budget_error)?;
        ledger
            .allocate(
                &budget_id,
                required_str(case, "scope", name)?,
                allocated,
                required_str(case, "policyRef", name)?,
                None,
            )
            .map_err(budget_error)?;
        ledger
            .create_completion_reserve(
                &reserve_id,
                &budget_id,
                match required_str(case, "reservePurpose", name)? {
                    "finalization" => CompletionReservePurpose::Finalization,
                    "checkpoint" => CompletionReservePurpose::Checkpoint,
                    "cleanup" => CompletionReservePurpose::Cleanup,
                    "compensation" => CompletionReservePurpose::Compensation,
                    other => {
                        return Err(format!(
                            "budget-race TCK case {name} has unknown completion reserve purpose {other}"
                        ));
                    }
                },
                usage_amounts(required_value(case, "reserveAmounts", name)?, name)?,
                string_array(required_value(case, "spendableBy", name)?, name)?,
                optional_str(case, "reserveExpiresAt").map(str::to_owned),
            )
            .map_err(budget_error)?;
    }

    let spenders = string_array(required_value(case, "spenders", name)?, name)?;
    let expires_at = required_str(case, "expiresAt", name)?.to_owned();
    let barrier = Arc::new(Barrier::new(spenders.len() + 1));
    let handles = spenders
        .into_iter()
        .map(|spender| {
            let path = path.clone();
            let reserve_id = reserve_id.clone();
            let barrier = Arc::clone(&barrier);
            let expires_at = expires_at.clone();
            thread::spawn(move || -> Result<WorkerOutcome, BudgetError> {
                let mut ledger = SqliteBudgetLedger::open(&path)?;
                barrier.wait();
                match ledger.spend_completion_reserve(&reserve_id, spender, expires_at) {
                    Ok(_) => Ok(WorkerOutcome::allowed()),
                    Err(BudgetError::CompletionReserveState { .. }) => {
                        Ok(WorkerOutcome::denied("CompletionReserveState"))
                    }
                    Err(error) => Err(error),
                }
            })
        })
        .collect::<Vec<_>>();

    barrier.wait();
    let outcomes = handles
        .into_iter()
        .map(|handle| handle.join().expect("completion reserve race worker joins"))
        .collect::<Result<Vec<_>, _>>()
        .map_err(budget_error)?;

    assert_worker_outcomes(name, case, &outcomes)?;
    let ledger = SqliteBudgetLedger::open(&path).map_err(budget_error)?;
    let reserve = ledger
        .completion_reserve(&reserve_id)
        .map_err(budget_error)?;
    let reserve_status = match reserve.status {
        CompletionReserveStatus::Available => "available",
        CompletionReserveStatus::Spent => "spent",
        CompletionReserveStatus::Released => "released",
        CompletionReserveStatus::Expired => "expired",
    };
    assert_eq!(
        reserve_status,
        required_str(case, "expectedReserveStatus", name)?,
        "{name}"
    );
    assert_eq!(
        ledger.balance(&budget_id).map_err(budget_error)?.reserved,
        expected_amounts(case, "expectedReserved", name)?,
        "{name}"
    );
    fs::remove_file(path).ok();
    Ok(())
}

fn assert_worker_outcomes(
    name: &str,
    case: &Value,
    outcomes: &[WorkerOutcome],
) -> Result<(), String> {
    let allowed = outcomes.iter().filter(|outcome| outcome.allowed).count() as u64;
    let denied = outcomes.iter().filter(|outcome| !outcome.allowed).count() as u64;
    let expected_allowed = required_value(case, "expectedAllowed", name)?
        .as_u64()
        .ok_or_else(|| {
            format!("budget-race TCK case {name} field expectedAllowed must be an unsigned integer")
        })?;
    let expected_denied = required_value(case, "expectedDenied", name)?
        .as_u64()
        .ok_or_else(|| {
            format!("budget-race TCK case {name} field expectedDenied must be an unsigned integer")
        })?;
    assert_eq!(allowed, expected_allowed, "{name}");
    assert_eq!(denied, expected_denied, "{name}");
    if let Some(error_name) = optional_str(case, "expectedDeniedError") {
        for outcome in outcomes.iter().filter(|outcome| !outcome.allowed) {
            assert_eq!(outcome.error.as_deref(), Some(error_name), "{name}");
        }
    }
    Ok(())
}

fn usage_amounts(value: &Value, name: &str) -> Result<Vec<UsageAmount>, String> {
    let amounts = value
        .as_array()
        .ok_or_else(|| format!("budget-race TCK case {name} usage amounts must be an array"))?;
    amounts
        .iter()
        .map(|amount| {
            let amount_value = required_value(amount, "amount", name)?
                .as_i64()
                .ok_or_else(|| {
                    format!("budget-race TCK case {name} field amount must be an integer")
                })?;
            Ok(UsageAmount::new(
                required_str(amount, "kind", name)?,
                amount_value,
                required_str(amount, "unit", name)?,
            ))
        })
        .collect()
}

fn expected_amounts(case: &Value, field: &str, name: &str) -> Result<Vec<UsageAmount>, String> {
    usage_amounts(required_value(case, field, name)?, name)
}

fn string_array(value: &Value, name: &str) -> Result<Vec<String>, String> {
    let strings = value
        .as_array()
        .ok_or_else(|| format!("budget-race TCK case {name} field must be an array"))?;
    strings
        .iter()
        .map(|value| {
            value
                .as_str()
                .map(str::to_owned)
                .ok_or_else(|| format!("budget-race TCK case {name} expected a string array"))
        })
        .collect()
}

fn sqlite_budget_path(label: &str) -> PathBuf {
    let safe_label = label
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() {
                character
            } else {
                '-'
            }
        })
        .collect::<String>();
    let mut path = env::temp_dir();
    path.push(format!(
        "graphblocks-budget-race-tck-{safe_label}-{}.sqlite3",
        std::process::id()
    ));
    fs::remove_file(&path).ok();
    path
}

fn required_value<'a>(value: &'a Value, field: &str, name: &str) -> Result<&'a Value, String> {
    value
        .get(field)
        .ok_or_else(|| format!("budget-race TCK case {name} is missing {field}"))
}

fn required_str<'a>(value: &'a Value, field: &str, name: &str) -> Result<&'a str, String> {
    required_value(value, field, name)?
        .as_str()
        .ok_or_else(|| format!("budget-race TCK case {name} field {field} must be a string"))
}

fn optional_str<'a>(value: &'a Value, field: &str) -> Option<&'a str> {
    value.get(field).and_then(Value::as_str)
}

fn budget_error(error: BudgetError) -> String {
    format!("{error:?}")
}

#[derive(Debug)]
struct WorkerOutcome {
    allowed: bool,
    error: Option<String>,
}

impl WorkerOutcome {
    fn allowed() -> Self {
        Self {
            allowed: true,
            error: None,
        }
    }

    fn denied(error: impl Into<String>) -> Self {
        Self {
            allowed: false,
            error: Some(error.into()),
        }
    }
}

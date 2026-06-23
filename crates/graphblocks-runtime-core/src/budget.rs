use std::collections::{BTreeMap, BTreeSet};

pub use crate::usage::UsageAmount;

type AmountKey = (String, String, Vec<(String, String)>);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BudgetStatus {
    Active,
    Exhausted,
    Paused,
    Closed,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReservationPurpose {
    ProviderCall,
    Task,
    Trial,
    Tool,
    Finalization,
    Cleanup,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReservationStatus {
    Reserved,
    Committed,
    Released,
    Expired,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum BudgetError {
    BudgetNotFound {
        budget_id: String,
    },
    BudgetConflict {
        budget_id: String,
    },
    ReservationNotFound {
        reservation_id: String,
    },
    ReservationConflict {
        reservation_id: String,
    },
    ReservationState {
        reservation_id: String,
        status: ReservationStatus,
    },
    BudgetExceeded {
        budget_id: String,
        kind: String,
        unit: String,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BudgetAccount {
    pub budget_id: String,
    pub scope: String,
    pub allocated: Vec<UsageAmount>,
    pub parent_budget_id: Option<String>,
    pub status: BudgetStatus,
    pub policy_ref: String,
    pub revision: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BudgetReservation {
    pub reservation_id: String,
    pub budget_id: String,
    pub owner: String,
    pub amounts: Vec<UsageAmount>,
    pub purpose: ReservationPurpose,
    pub expires_at: String,
    pub fencing_token: u64,
    pub status: ReservationStatus,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BudgetBalance {
    pub budget_id: String,
    pub allocated: Vec<UsageAmount>,
    pub reserved: Vec<UsageAmount>,
    pub committed: Vec<UsageAmount>,
    pub available: Vec<UsageAmount>,
    pub overdraft: Vec<UsageAmount>,
    pub revision: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BudgetSettlement {
    pub reservation_id: String,
    pub budget_id: String,
    pub committed: Vec<UsageAmount>,
    pub released: Vec<UsageAmount>,
    pub overdraft: Vec<UsageAmount>,
    pub status: ReservationStatus,
    pub revision: u64,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct InMemoryBudgetLedger {
    accounts: BTreeMap<String, BudgetAccount>,
    allocated: BTreeMap<String, BTreeMap<AmountKey, i64>>,
    reserved: BTreeMap<String, BTreeMap<AmountKey, i64>>,
    committed: BTreeMap<String, BTreeMap<AmountKey, i64>>,
    overdraft: BTreeMap<String, BTreeMap<AmountKey, i64>>,
    reservations: BTreeMap<String, BudgetReservation>,
    reservation_counter: u64,
    fencing_counter: u64,
}

impl InMemoryBudgetLedger {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn allocate(
        &mut self,
        budget_id: impl Into<String>,
        scope: impl Into<String>,
        amounts: impl IntoIterator<Item = UsageAmount>,
        policy_ref: impl Into<String>,
        parent_budget_id: Option<String>,
    ) -> Result<BudgetAccount, BudgetError> {
        let budget_id = budget_id.into();
        if self.accounts.contains_key(&budget_id) {
            return Err(BudgetError::BudgetConflict { budget_id });
        }
        if let Some(parent_budget_id) = &parent_budget_id {
            if !self.accounts.contains_key(parent_budget_id) {
                return Err(BudgetError::BudgetNotFound {
                    budget_id: parent_budget_id.clone(),
                });
            }
        }

        let allocated = amounts_to_map(amounts);
        let account = BudgetAccount {
            budget_id: budget_id.clone(),
            scope: scope.into(),
            allocated: map_to_amounts(&allocated),
            parent_budget_id,
            status: BudgetStatus::Active,
            policy_ref: policy_ref.into(),
            revision: 1,
        };
        self.accounts.insert(budget_id.clone(), account.clone());
        self.allocated.insert(budget_id.clone(), allocated);
        self.reserved.insert(budget_id.clone(), BTreeMap::new());
        self.committed.insert(budget_id.clone(), BTreeMap::new());
        self.overdraft.insert(budget_id, BTreeMap::new());
        Ok(account)
    }

    pub fn reserve(
        &mut self,
        budget_id: impl AsRef<str>,
        owner: impl Into<String>,
        amounts: impl IntoIterator<Item = UsageAmount>,
        purpose: ReservationPurpose,
        expires_at: impl Into<String>,
        reservation_id: Option<String>,
    ) -> Result<BudgetReservation, BudgetError> {
        let budget_id = budget_id.as_ref();
        if !self.accounts.contains_key(budget_id) {
            return Err(BudgetError::BudgetNotFound {
                budget_id: budget_id.to_string(),
            });
        }

        let requested = amounts_to_map(amounts);
        let available = self.available_map(budget_id)?;
        for (key, amount) in &requested {
            if *amount > available.get(key).copied().unwrap_or(0) {
                return Err(BudgetError::BudgetExceeded {
                    budget_id: budget_id.to_string(),
                    kind: key.0.clone(),
                    unit: key.1.clone(),
                });
            }
        }

        self.reservation_counter += 1;
        self.fencing_counter += 1;
        let reservation_id = reservation_id
            .unwrap_or_else(|| format!("reservation-{:06}", self.reservation_counter));
        if self.reservations.contains_key(&reservation_id) {
            return Err(BudgetError::ReservationConflict { reservation_id });
        }

        add_amounts(
            self.reserved
                .get_mut(budget_id)
                .expect("budget has reserved balance map"),
            &requested,
        );
        self.bump_revision(budget_id);

        let reservation = BudgetReservation {
            reservation_id: reservation_id.clone(),
            budget_id: budget_id.to_string(),
            owner: owner.into(),
            amounts: map_to_amounts(&requested),
            purpose,
            expires_at: expires_at.into(),
            fencing_token: self.fencing_counter,
            status: ReservationStatus::Reserved,
        };
        self.reservations
            .insert(reservation_id, reservation.clone());
        Ok(reservation)
    }

    pub fn commit(
        &mut self,
        reservation_id: impl AsRef<str>,
        actual_amounts: impl IntoIterator<Item = UsageAmount>,
    ) -> Result<BudgetSettlement, BudgetError> {
        let reservation_id = reservation_id.as_ref();
        let reservation = self
            .reservations
            .get(reservation_id)
            .cloned()
            .ok_or_else(|| BudgetError::ReservationNotFound {
                reservation_id: reservation_id.to_string(),
            })?;
        if reservation.status != ReservationStatus::Reserved {
            return Err(BudgetError::ReservationState {
                reservation_id: reservation_id.to_string(),
                status: reservation.status,
            });
        }

        let reserved = amounts_to_map(reservation.amounts.clone());
        let actual = amounts_to_map(actual_amounts);
        subtract_amounts(
            self.reserved
                .get_mut(&reservation.budget_id)
                .expect("budget has reserved balance map"),
            &reserved,
        );

        let mut released = BTreeMap::new();
        let mut overdraft = BTreeMap::new();
        for (key, amount) in &reserved {
            let unused = amount - actual.get(key).copied().unwrap_or(0);
            if unused > 0 {
                released.insert(key.clone(), unused);
            }
        }
        for (key, amount) in &actual {
            let extra = amount - reserved.get(key).copied().unwrap_or(0);
            if extra > 0 {
                overdraft.insert(key.clone(), extra);
            }
        }

        add_amounts(
            self.committed
                .get_mut(&reservation.budget_id)
                .expect("budget has committed balance map"),
            &actual,
        );
        add_amounts(
            self.overdraft
                .get_mut(&reservation.budget_id)
                .expect("budget has overdraft balance map"),
            &overdraft,
        );
        self.bump_revision(&reservation.budget_id);

        let updated = BudgetReservation {
            status: ReservationStatus::Committed,
            ..reservation.clone()
        };
        self.reservations
            .insert(reservation_id.to_string(), updated);
        Ok(BudgetSettlement {
            reservation_id: reservation_id.to_string(),
            budget_id: reservation.budget_id.clone(),
            committed: map_to_amounts(&actual),
            released: map_to_amounts(&released),
            overdraft: map_to_amounts(&overdraft),
            status: ReservationStatus::Committed,
            revision: self
                .accounts
                .get(&reservation.budget_id)
                .expect("budget account exists")
                .revision,
        })
    }

    pub fn release(
        &mut self,
        reservation_id: impl AsRef<str>,
    ) -> Result<BudgetSettlement, BudgetError> {
        let reservation_id = reservation_id.as_ref();
        let reservation = self
            .reservations
            .get(reservation_id)
            .cloned()
            .ok_or_else(|| BudgetError::ReservationNotFound {
                reservation_id: reservation_id.to_string(),
            })?;
        if reservation.status != ReservationStatus::Reserved {
            return Err(BudgetError::ReservationState {
                reservation_id: reservation_id.to_string(),
                status: reservation.status,
            });
        }

        let reserved = amounts_to_map(reservation.amounts.clone());
        subtract_amounts(
            self.reserved
                .get_mut(&reservation.budget_id)
                .expect("budget has reserved balance map"),
            &reserved,
        );
        self.bump_revision(&reservation.budget_id);

        let updated = BudgetReservation {
            status: ReservationStatus::Released,
            ..reservation.clone()
        };
        self.reservations
            .insert(reservation_id.to_string(), updated);
        Ok(BudgetSettlement {
            reservation_id: reservation_id.to_string(),
            budget_id: reservation.budget_id.clone(),
            committed: Vec::new(),
            released: map_to_amounts(&reserved),
            overdraft: Vec::new(),
            status: ReservationStatus::Released,
            revision: self
                .accounts
                .get(&reservation.budget_id)
                .expect("budget account exists")
                .revision,
        })
    }

    pub fn balance(&self, budget_id: impl AsRef<str>) -> Result<BudgetBalance, BudgetError> {
        let budget_id = budget_id.as_ref();
        let account = self
            .accounts
            .get(budget_id)
            .ok_or_else(|| BudgetError::BudgetNotFound {
                budget_id: budget_id.to_string(),
            })?;
        let allocated = self
            .allocated
            .get(budget_id)
            .expect("budget has allocated balance map");
        let reserved = self
            .reserved
            .get(budget_id)
            .expect("budget has reserved balance map");
        let committed = self
            .committed
            .get(budget_id)
            .expect("budget has committed balance map");
        let overdraft = self
            .overdraft
            .get(budget_id)
            .expect("budget has overdraft balance map");
        let available = self.available_map(budget_id)?;

        Ok(BudgetBalance {
            budget_id: budget_id.to_string(),
            allocated: map_to_amounts(allocated),
            reserved: map_to_amounts(reserved),
            committed: map_to_amounts(committed),
            available: map_to_amounts(&available),
            overdraft: map_to_amounts(overdraft),
            revision: account.revision,
        })
    }

    fn available_map(&self, budget_id: &str) -> Result<BTreeMap<AmountKey, i64>, BudgetError> {
        let allocated =
            self.allocated
                .get(budget_id)
                .ok_or_else(|| BudgetError::BudgetNotFound {
                    budget_id: budget_id.to_string(),
                })?;
        let reserved = self
            .reserved
            .get(budget_id)
            .expect("budget has reserved balance map");
        let committed = self
            .committed
            .get(budget_id)
            .expect("budget has committed balance map");
        let mut keys = BTreeSet::new();
        keys.extend(allocated.keys().cloned());
        keys.extend(reserved.keys().cloned());
        keys.extend(committed.keys().cloned());
        let mut available = BTreeMap::new();
        for key in keys {
            let remaining = allocated.get(&key).copied().unwrap_or(0)
                - reserved.get(&key).copied().unwrap_or(0)
                - committed.get(&key).copied().unwrap_or(0);
            if remaining > 0 {
                available.insert(key, remaining);
            }
        }
        Ok(available)
    }

    fn bump_revision(&mut self, budget_id: &str) {
        if let Some(account) = self.accounts.get_mut(budget_id) {
            account.revision += 1;
        }
    }
}

fn amount_key(amount: &UsageAmount) -> AmountKey {
    (
        amount.kind.clone(),
        amount.unit.clone(),
        amount
            .dimensions
            .iter()
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect(),
    )
}

fn amounts_to_map(amounts: impl IntoIterator<Item = UsageAmount>) -> BTreeMap<AmountKey, i64> {
    let mut values = BTreeMap::new();
    for amount in amounts {
        let key = amount_key(&amount);
        *values.entry(key).or_insert(0) += amount.amount;
    }
    values.retain(|_, value| *value != 0);
    values
}

fn map_to_amounts(values: &BTreeMap<AmountKey, i64>) -> Vec<UsageAmount> {
    values
        .iter()
        .filter_map(|((kind, unit, dimensions), amount)| {
            if *amount == 0 {
                return None;
            }
            Some(UsageAmount {
                kind: kind.clone(),
                amount: *amount,
                unit: unit.clone(),
                dimensions: dimensions.iter().cloned().collect(),
            })
        })
        .collect()
}

fn add_amounts(target: &mut BTreeMap<AmountKey, i64>, amounts: &BTreeMap<AmountKey, i64>) {
    for (key, amount) in amounts {
        *target.entry(key.clone()).or_insert(0) += amount;
    }
    target.retain(|_, value| *value != 0);
}

fn subtract_amounts(target: &mut BTreeMap<AmountKey, i64>, amounts: &BTreeMap<AmountKey, i64>) {
    for (key, amount) in amounts {
        *target.entry(key.clone()).or_insert(0) -= amount;
    }
    target.retain(|_, value| *value != 0);
}

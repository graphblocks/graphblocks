use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

pub use crate::usage::UsageAmount;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::{Map, Number, Value};

type AmountKey = (String, String, Vec<(String, String)>);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BudgetStatus {
    Active,
    Exhausted,
    Paused,
    Closed,
}

impl BudgetStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::Active => "active",
            Self::Exhausted => "exhausted",
            Self::Paused => "paused",
            Self::Closed => "closed",
        }
    }

    fn from_str(status: &str) -> Option<Self> {
        match status {
            "active" => Some(Self::Active),
            "exhausted" => Some(Self::Exhausted),
            "paused" => Some(Self::Paused),
            "closed" => Some(Self::Closed),
            _ => None,
        }
    }
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

impl ReservationPurpose {
    fn as_str(self) -> &'static str {
        match self {
            Self::ProviderCall => "provider_call",
            Self::Task => "task",
            Self::Trial => "trial",
            Self::Tool => "tool",
            Self::Finalization => "finalization",
            Self::Cleanup => "cleanup",
        }
    }

    fn from_str(purpose: &str) -> Option<Self> {
        match purpose {
            "provider_call" => Some(Self::ProviderCall),
            "task" => Some(Self::Task),
            "trial" => Some(Self::Trial),
            "tool" => Some(Self::Tool),
            "finalization" => Some(Self::Finalization),
            "cleanup" => Some(Self::Cleanup),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CompletionReservePurpose {
    Finalization,
    Checkpoint,
    Cleanup,
    Compensation,
}

impl CompletionReservePurpose {
    fn as_str(self) -> &'static str {
        match self {
            Self::Finalization => "finalization",
            Self::Checkpoint => "checkpoint",
            Self::Cleanup => "cleanup",
            Self::Compensation => "compensation",
        }
    }

    fn from_str(purpose: &str) -> Option<Self> {
        match purpose {
            "finalization" => Some(Self::Finalization),
            "checkpoint" => Some(Self::Checkpoint),
            "cleanup" => Some(Self::Cleanup),
            "compensation" => Some(Self::Compensation),
            _ => None,
        }
    }

    fn reservation_purpose(self) -> ReservationPurpose {
        match self {
            Self::Finalization => ReservationPurpose::Finalization,
            Self::Checkpoint => ReservationPurpose::Finalization,
            Self::Cleanup => ReservationPurpose::Cleanup,
            Self::Compensation => ReservationPurpose::Cleanup,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReservationStatus {
    Reserved,
    Committed,
    Released,
    Expired,
}

impl ReservationStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::Reserved => "reserved",
            Self::Committed => "committed",
            Self::Released => "released",
            Self::Expired => "expired",
        }
    }

    fn from_str(status: &str) -> Option<Self> {
        match status {
            "reserved" => Some(Self::Reserved),
            "committed" => Some(Self::Committed),
            "released" => Some(Self::Released),
            "expired" => Some(Self::Expired),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CompletionReserveStatus {
    Available,
    Spent,
    Released,
    Expired,
}

impl CompletionReserveStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::Available => "available",
            Self::Spent => "spent",
            Self::Released => "released",
            Self::Expired => "expired",
        }
    }

    fn from_str(status: &str) -> Option<Self> {
        match status {
            "available" => Some(Self::Available),
            "spent" => Some(Self::Spent),
            "released" => Some(Self::Released),
            "expired" => Some(Self::Expired),
            _ => None,
        }
    }
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
    PermitNotFound {
        permit_id: String,
    },
    PermitConflict {
        permit_id: String,
    },
    PermitScope {
        permit_id: String,
        reservation_id: String,
    },
    PermitFencing {
        permit_id: String,
        budget_id: String,
        required_token: u64,
        actual_token: Option<u64>,
    },
    PermitExpired {
        permit_id: String,
        expires_at: String,
        now: String,
    },
    CompletionReserveNotFound {
        reserve_id: String,
    },
    CompletionReserveConflict {
        reserve_id: String,
    },
    CompletionReserveUnauthorized {
        reserve_id: String,
        spender: String,
    },
    CompletionReserveState {
        reserve_id: String,
        status: CompletionReserveStatus,
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
    Storage {
        message: String,
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BudgetPermit {
    pub permit_id: String,
    pub reservation_refs: Vec<String>,
    pub owner: String,
    pub atomic_unit: String,
    pub admission_epoch: u64,
    pub authorized_amounts: Vec<UsageAmount>,
    pub continuation_profile: String,
    pub policy_snapshot_digest: String,
    pub expires_at: String,
    pub low_watermark: Vec<UsageAmount>,
    pub fencing_tokens: BTreeMap<String, u64>,
}

impl BudgetPermit {
    pub fn allows<I>(&self, amounts: I) -> bool
    where
        I: IntoIterator<Item = UsageAmount>,
    {
        let authorized = amounts_to_map(self.authorized_amounts.clone());
        let requested = amounts_to_map(amounts);
        requested
            .iter()
            .all(|(key, amount)| *amount <= authorized.get(key).copied().unwrap_or(0))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CompletionReserve {
    pub reserve_id: String,
    pub budget_id: String,
    pub purpose: CompletionReservePurpose,
    pub amounts: Vec<UsageAmount>,
    pub spendable_by: BTreeSet<String>,
    pub expires_at: Option<String>,
    pub status: CompletionReserveStatus,
    pub reservation_id: Option<String>,
    pub fencing_token: u64,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct InMemoryBudgetLedger {
    accounts: BTreeMap<String, BudgetAccount>,
    allocated: BTreeMap<String, BTreeMap<AmountKey, i64>>,
    reserved: BTreeMap<String, BTreeMap<AmountKey, i64>>,
    committed: BTreeMap<String, BTreeMap<AmountKey, i64>>,
    overdraft: BTreeMap<String, BTreeMap<AmountKey, i64>>,
    reservations: BTreeMap<String, BudgetReservation>,
    reservation_holds: BTreeMap<String, Vec<String>>,
    permits: BTreeMap<String, BudgetPermit>,
    permit_spent: BTreeMap<String, BTreeMap<AmountKey, i64>>,
    completion_reserves: BTreeMap<String, CompletionReserve>,
    completion_reserve_holds: BTreeMap<String, Vec<String>>,
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
        let held_budget_ids = self.budget_chain(budget_id)?;
        for held_budget_id in &held_budget_ids {
            let available = self.available_map(held_budget_id)?;
            for (key, amount) in &requested {
                if *amount > available.get(key).copied().unwrap_or(0) {
                    return Err(BudgetError::BudgetExceeded {
                        budget_id: held_budget_id.clone(),
                        kind: key.0.clone(),
                        unit: key.1.clone(),
                    });
                }
            }
        }

        self.reservation_counter += 1;
        self.fencing_counter += 1;
        let reservation_id = reservation_id
            .unwrap_or_else(|| format!("reservation-{:06}", self.reservation_counter));
        if self.reservations.contains_key(&reservation_id) {
            return Err(BudgetError::ReservationConflict { reservation_id });
        }

        for held_budget_id in &held_budget_ids {
            add_amounts(
                self.reserved
                    .get_mut(held_budget_id)
                    .expect("budget has reserved balance map"),
                &requested,
            );
            self.bump_revision(held_budget_id);
        }

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
            .insert(reservation_id.clone(), reservation.clone());
        self.reservation_holds
            .insert(reservation_id, held_budget_ids);
        Ok(reservation)
    }

    pub fn commit(
        &mut self,
        reservation_id: impl AsRef<str>,
        actual_amounts: impl IntoIterator<Item = UsageAmount>,
    ) -> Result<BudgetSettlement, BudgetError> {
        self.commit_inner(
            reservation_id.as_ref(),
            actual_amounts.into_iter().collect(),
            None,
        )
    }

    pub fn commit_with_overdraft_limit(
        &mut self,
        reservation_id: impl AsRef<str>,
        actual_amounts: impl IntoIterator<Item = UsageAmount>,
        max_overdraft: impl IntoIterator<Item = UsageAmount>,
    ) -> Result<BudgetSettlement, BudgetError> {
        self.commit_inner(
            reservation_id.as_ref(),
            actual_amounts.into_iter().collect(),
            Some(max_overdraft.into_iter().collect()),
        )
    }

    fn commit_inner(
        &mut self,
        reservation_id: &str,
        actual_amounts: Vec<UsageAmount>,
        max_overdraft: Option<Vec<UsageAmount>>,
    ) -> Result<BudgetSettlement, BudgetError> {
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
        let held_budget_ids = self
            .reservation_holds
            .get(reservation_id)
            .cloned()
            .unwrap_or_else(|| vec![reservation.budget_id.clone()]);
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
        if let Some(max_overdraft) = max_overdraft {
            let overdraft_limit = amounts_to_map(max_overdraft);
            for (key, amount) in &overdraft {
                if *amount > overdraft_limit.get(key).copied().unwrap_or(0) {
                    return Err(BudgetError::BudgetExceeded {
                        budget_id: reservation.budget_id.clone(),
                        kind: key.0.clone(),
                        unit: key.1.clone(),
                    });
                }
            }
        }

        for held_budget_id in &held_budget_ids {
            subtract_amounts(
                self.reserved
                    .get_mut(held_budget_id)
                    .expect("budget has reserved balance map"),
                &reserved,
            );
            add_amounts(
                self.committed
                    .get_mut(held_budget_id)
                    .expect("budget has committed balance map"),
                &actual,
            );
            add_amounts(
                self.overdraft
                    .get_mut(held_budget_id)
                    .expect("budget has overdraft balance map"),
                &overdraft,
            );
            self.bump_revision(held_budget_id);
        }

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
        let held_budget_ids = self
            .reservation_holds
            .get(reservation_id)
            .cloned()
            .unwrap_or_else(|| vec![reservation.budget_id.clone()]);
        for held_budget_id in &held_budget_ids {
            subtract_amounts(
                self.reserved
                    .get_mut(held_budget_id)
                    .expect("budget has reserved balance map"),
                &reserved,
            );
            self.bump_revision(held_budget_id);
        }

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

    pub fn expire(
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
        let held_budget_ids = self
            .reservation_holds
            .get(reservation_id)
            .cloned()
            .unwrap_or_else(|| vec![reservation.budget_id.clone()]);
        for held_budget_id in &held_budget_ids {
            subtract_amounts(
                self.reserved
                    .get_mut(held_budget_id)
                    .expect("budget has reserved balance map"),
                &reserved,
            );
            self.bump_revision(held_budget_id);
        }

        let updated = BudgetReservation {
            status: ReservationStatus::Expired,
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
            status: ReservationStatus::Expired,
            revision: self
                .accounts
                .get(&reservation.budget_id)
                .expect("budget account exists")
                .revision,
        })
    }

    pub fn commit_with_permit(
        &mut self,
        permit_id: impl AsRef<str>,
        reservation_id: impl AsRef<str>,
        actual_amounts: impl IntoIterator<Item = UsageAmount>,
    ) -> Result<BudgetSettlement, BudgetError> {
        self.commit_with_permit_inner(
            permit_id.as_ref(),
            reservation_id.as_ref(),
            actual_amounts,
            None,
        )
    }

    pub fn commit_with_permit_at(
        &mut self,
        permit_id: impl AsRef<str>,
        reservation_id: impl AsRef<str>,
        actual_amounts: impl IntoIterator<Item = UsageAmount>,
        now: impl AsRef<str>,
    ) -> Result<BudgetSettlement, BudgetError> {
        self.commit_with_permit_inner(
            permit_id.as_ref(),
            reservation_id.as_ref(),
            actual_amounts,
            Some(now.as_ref()),
        )
    }

    fn commit_with_permit_inner(
        &mut self,
        permit_id: &str,
        reservation_id: &str,
        actual_amounts: impl IntoIterator<Item = UsageAmount>,
        now: Option<&str>,
    ) -> Result<BudgetSettlement, BudgetError> {
        if let Some(now) = now {
            self.ensure_permit_not_expired(permit_id, now)?;
        }
        let reservation = self.validate_permit_for_reservation(permit_id, reservation_id)?;
        let actual_amounts = actual_amounts.into_iter().collect::<Vec<_>>();
        let actual = amounts_to_map(actual_amounts.clone());
        self.ensure_permit_allows_additional(permit_id, &actual, &reservation.budget_id)?;
        let settlement = self.commit(reservation_id, actual_amounts)?;
        add_amounts(
            self.permit_spent.entry(permit_id.to_string()).or_default(),
            &actual,
        );
        Ok(settlement)
    }

    pub fn release_with_permit(
        &mut self,
        permit_id: impl AsRef<str>,
        reservation_id: impl AsRef<str>,
    ) -> Result<BudgetSettlement, BudgetError> {
        self.validate_permit_for_reservation(permit_id.as_ref(), reservation_id.as_ref())?;
        self.release(reservation_id)
    }

    pub fn release_with_permit_at(
        &mut self,
        permit_id: impl AsRef<str>,
        reservation_id: impl AsRef<str>,
        now: impl AsRef<str>,
    ) -> Result<BudgetSettlement, BudgetError> {
        let permit_id = permit_id.as_ref();
        self.ensure_permit_not_expired(permit_id, now.as_ref())?;
        self.validate_permit_for_reservation(permit_id, reservation_id.as_ref())?;
        self.release(reservation_id)
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

    pub fn issue_permit(
        &mut self,
        permit_id: impl Into<String>,
        reservation_ids: Vec<String>,
        owner: impl Into<String>,
        atomic_unit: impl Into<String>,
        admission_epoch: u64,
        continuation_profile: impl Into<String>,
        policy_snapshot_digest: impl Into<String>,
        expires_at: impl Into<String>,
        low_watermark: Vec<UsageAmount>,
    ) -> Result<BudgetPermit, BudgetError> {
        let permit_id = permit_id.into();
        if self.permits.contains_key(&permit_id) {
            return Err(BudgetError::PermitConflict { permit_id });
        }

        let mut authorized = BTreeMap::new();
        let mut fencing_tokens = BTreeMap::new();
        for reservation_id in &reservation_ids {
            let reservation = self.reservations.get(reservation_id).ok_or_else(|| {
                BudgetError::ReservationNotFound {
                    reservation_id: reservation_id.clone(),
                }
            })?;
            if reservation.status != ReservationStatus::Reserved {
                return Err(BudgetError::ReservationState {
                    reservation_id: reservation_id.clone(),
                    status: reservation.status,
                });
            }
            add_amounts(
                &mut authorized,
                &amounts_to_map(reservation.amounts.clone()),
            );
            let held_budget_ids = self
                .reservation_holds
                .get(reservation_id)
                .cloned()
                .unwrap_or_else(|| vec![reservation.budget_id.clone()]);
            for held_budget_id in held_budget_ids {
                let current = fencing_tokens.get(&held_budget_id).copied().unwrap_or(0);
                if reservation.fencing_token > current {
                    fencing_tokens.insert(held_budget_id, reservation.fencing_token);
                }
            }
        }

        let permit = BudgetPermit {
            permit_id: permit_id.clone(),
            reservation_refs: reservation_ids,
            owner: owner.into(),
            atomic_unit: atomic_unit.into(),
            admission_epoch,
            authorized_amounts: map_to_amounts(&authorized),
            continuation_profile: continuation_profile.into(),
            policy_snapshot_digest: policy_snapshot_digest.into(),
            expires_at: expires_at.into(),
            low_watermark,
            fencing_tokens,
        };
        self.permits.insert(permit_id, permit.clone());
        self.permit_spent
            .insert(permit.permit_id.clone(), BTreeMap::new());
        Ok(permit)
    }

    pub fn create_completion_reserve<I, S>(
        &mut self,
        reserve_id: impl Into<String>,
        budget_id: impl AsRef<str>,
        purpose: CompletionReservePurpose,
        amounts: impl IntoIterator<Item = UsageAmount>,
        spendable_by: I,
        expires_at: Option<String>,
    ) -> Result<CompletionReserve, BudgetError>
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        let reserve_id = reserve_id.into();
        let budget_id = budget_id.as_ref();
        if self.completion_reserves.contains_key(&reserve_id) {
            return Err(BudgetError::CompletionReserveConflict { reserve_id });
        }
        if !self.accounts.contains_key(budget_id) {
            return Err(BudgetError::BudgetNotFound {
                budget_id: budget_id.to_string(),
            });
        }

        let requested = amounts_to_map(amounts);
        let held_budget_ids = self.budget_chain(budget_id)?;
        for held_budget_id in &held_budget_ids {
            let available = self.available_map(held_budget_id)?;
            for (key, amount) in &requested {
                if *amount > available.get(key).copied().unwrap_or(0) {
                    return Err(BudgetError::BudgetExceeded {
                        budget_id: held_budget_id.clone(),
                        kind: key.0.clone(),
                        unit: key.1.clone(),
                    });
                }
            }
        }

        self.fencing_counter += 1;
        for held_budget_id in &held_budget_ids {
            add_amounts(
                self.reserved
                    .get_mut(held_budget_id)
                    .expect("budget has reserved balance map"),
                &requested,
            );
            self.bump_revision(held_budget_id);
        }

        let reserve = CompletionReserve {
            reserve_id: reserve_id.clone(),
            budget_id: budget_id.to_string(),
            purpose,
            amounts: map_to_amounts(&requested),
            spendable_by: spendable_by.into_iter().map(Into::into).collect(),
            expires_at,
            status: CompletionReserveStatus::Available,
            reservation_id: None,
            fencing_token: self.fencing_counter,
        };
        self.completion_reserves
            .insert(reserve_id.clone(), reserve.clone());
        self.completion_reserve_holds
            .insert(reserve_id, held_budget_ids);
        Ok(reserve)
    }

    pub fn completion_reserve(
        &self,
        reserve_id: impl AsRef<str>,
    ) -> Result<CompletionReserve, BudgetError> {
        let reserve_id = reserve_id.as_ref();
        self.completion_reserves
            .get(reserve_id)
            .cloned()
            .ok_or_else(|| BudgetError::CompletionReserveNotFound {
                reserve_id: reserve_id.to_string(),
            })
    }

    pub fn spend_completion_reserve(
        &mut self,
        reserve_id: impl AsRef<str>,
        spender: impl AsRef<str>,
        expires_at: impl Into<String>,
    ) -> Result<BudgetReservation, BudgetError> {
        let reserve_id = reserve_id.as_ref();
        let spender = spender.as_ref();
        let reserve = self
            .completion_reserves
            .get(reserve_id)
            .cloned()
            .ok_or_else(|| BudgetError::CompletionReserveNotFound {
                reserve_id: reserve_id.to_string(),
            })?;
        if reserve.status != CompletionReserveStatus::Available {
            return Err(BudgetError::CompletionReserveState {
                reserve_id: reserve_id.to_string(),
                status: reserve.status,
            });
        }
        if !reserve.spendable_by.contains(spender) {
            return Err(BudgetError::CompletionReserveUnauthorized {
                reserve_id: reserve_id.to_string(),
                spender: spender.to_string(),
            });
        }

        self.reservation_counter += 1;
        let reservation_id = format!("reservation-{:06}", self.reservation_counter);
        if self.reservations.contains_key(&reservation_id) {
            return Err(BudgetError::ReservationConflict { reservation_id });
        }

        let reservation = BudgetReservation {
            reservation_id: reservation_id.clone(),
            budget_id: reserve.budget_id.clone(),
            owner: spender.to_string(),
            amounts: reserve.amounts.clone(),
            purpose: reserve.purpose.reservation_purpose(),
            expires_at: expires_at.into(),
            fencing_token: reserve.fencing_token,
            status: ReservationStatus::Reserved,
        };
        self.reservations
            .insert(reservation_id.clone(), reservation.clone());
        self.reservation_holds.insert(
            reservation_id.clone(),
            self.completion_reserve_holds
                .get(reserve_id)
                .cloned()
                .unwrap_or_else(|| vec![reserve.budget_id.clone()]),
        );
        self.completion_reserves.insert(
            reserve_id.to_string(),
            CompletionReserve {
                status: CompletionReserveStatus::Spent,
                reservation_id: Some(reservation_id),
                ..reserve
            },
        );
        Ok(reservation)
    }

    pub fn release_completion_reserve(
        &mut self,
        reserve_id: impl AsRef<str>,
    ) -> Result<CompletionReserve, BudgetError> {
        self.settle_completion_reserve(reserve_id.as_ref(), CompletionReserveStatus::Released)
    }

    pub fn expire_completion_reserve(
        &mut self,
        reserve_id: impl AsRef<str>,
    ) -> Result<CompletionReserve, BudgetError> {
        self.settle_completion_reserve(reserve_id.as_ref(), CompletionReserveStatus::Expired)
    }

    fn settle_completion_reserve(
        &mut self,
        reserve_id: &str,
        status: CompletionReserveStatus,
    ) -> Result<CompletionReserve, BudgetError> {
        let reserve = self
            .completion_reserves
            .get(reserve_id)
            .cloned()
            .ok_or_else(|| BudgetError::CompletionReserveNotFound {
                reserve_id: reserve_id.to_string(),
            })?;
        if reserve.status != CompletionReserveStatus::Available {
            return Err(BudgetError::CompletionReserveState {
                reserve_id: reserve_id.to_string(),
                status: reserve.status,
            });
        }

        let reserved = amounts_to_map(reserve.amounts.clone());
        let held_budget_ids = self
            .completion_reserve_holds
            .get(reserve_id)
            .cloned()
            .unwrap_or_else(|| vec![reserve.budget_id.clone()]);
        for held_budget_id in &held_budget_ids {
            subtract_amounts(
                self.reserved
                    .get_mut(held_budget_id)
                    .expect("completion reserve hold points to an existing budget"),
                &reserved,
            );
            self.bump_revision(held_budget_id);
        }

        let updated = CompletionReserve { status, ..reserve };
        self.completion_reserves
            .insert(reserve_id.to_string(), updated.clone());
        Ok(updated)
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

    fn budget_chain(&self, budget_id: &str) -> Result<Vec<String>, BudgetError> {
        let mut chain = Vec::new();
        let mut seen = BTreeSet::new();
        let mut current_id = Some(budget_id.to_string());
        while let Some(id) = current_id {
            if !seen.insert(id.clone()) {
                return Err(BudgetError::BudgetConflict { budget_id: id });
            }
            let account = self
                .accounts
                .get(&id)
                .ok_or_else(|| BudgetError::BudgetNotFound {
                    budget_id: id.clone(),
                })?;
            chain.push(id);
            current_id = account.parent_budget_id.clone();
        }
        Ok(chain)
    }

    fn bump_revision(&mut self, budget_id: &str) {
        if let Some(account) = self.accounts.get_mut(budget_id) {
            account.revision += 1;
        }
    }

    fn validate_permit_for_reservation(
        &self,
        permit_id: &str,
        reservation_id: &str,
    ) -> Result<BudgetReservation, BudgetError> {
        let permit = self
            .permits
            .get(permit_id)
            .ok_or_else(|| BudgetError::PermitNotFound {
                permit_id: permit_id.to_string(),
            })?;
        let reservation = self
            .reservations
            .get(reservation_id)
            .cloned()
            .ok_or_else(|| BudgetError::ReservationNotFound {
                reservation_id: reservation_id.to_string(),
            })?;
        if !permit
            .reservation_refs
            .iter()
            .any(|reference| reference == reservation_id)
        {
            return Err(BudgetError::PermitScope {
                permit_id: permit_id.to_string(),
                reservation_id: reservation_id.to_string(),
            });
        }
        let held_budget_ids = self
            .reservation_holds
            .get(reservation_id)
            .cloned()
            .unwrap_or_else(|| vec![reservation.budget_id.clone()]);
        for budget_id in held_budget_ids {
            let actual_token = permit.fencing_tokens.get(&budget_id).copied();
            if actual_token.is_none_or(|token| token < reservation.fencing_token) {
                return Err(BudgetError::PermitFencing {
                    permit_id: permit_id.to_string(),
                    budget_id,
                    required_token: reservation.fencing_token,
                    actual_token,
                });
            }
        }
        Ok(reservation)
    }

    fn ensure_permit_not_expired(&self, permit_id: &str, now: &str) -> Result<(), BudgetError> {
        let permit = self
            .permits
            .get(permit_id)
            .ok_or_else(|| BudgetError::PermitNotFound {
                permit_id: permit_id.to_string(),
            })?;
        if permit.expires_at.as_str() <= now {
            return Err(BudgetError::PermitExpired {
                permit_id: permit_id.to_string(),
                expires_at: permit.expires_at.clone(),
                now: now.to_string(),
            });
        }
        Ok(())
    }

    fn ensure_permit_allows_additional(
        &self,
        permit_id: &str,
        requested: &BTreeMap<AmountKey, i64>,
        budget_id: &str,
    ) -> Result<(), BudgetError> {
        let permit = self
            .permits
            .get(permit_id)
            .ok_or_else(|| BudgetError::PermitNotFound {
                permit_id: permit_id.to_string(),
            })?;
        let authorized = amounts_to_map(permit.authorized_amounts.clone());
        let spent = self.permit_spent.get(permit_id);
        for (key, amount) in requested {
            let already_spent = spent
                .and_then(|values| values.get(key))
                .copied()
                .unwrap_or(0);
            if already_spent + amount > authorized.get(key).copied().unwrap_or(0) {
                return Err(BudgetError::BudgetExceeded {
                    budget_id: budget_id.to_string(),
                    kind: key.0.clone(),
                    unit: key.1.clone(),
                });
            }
        }
        Ok(())
    }
}

pub struct SqliteBudgetLedger {
    connection: Connection,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct StoredBudgetAccount {
    account: BudgetAccount,
    allocated: BTreeMap<AmountKey, i64>,
    reserved: BTreeMap<AmountKey, i64>,
    committed: BTreeMap<AmountKey, i64>,
    overdraft: BTreeMap<AmountKey, i64>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct StoredBudgetReservation {
    reservation: BudgetReservation,
    held_budget_ids: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct StoredBudgetPermit {
    permit: BudgetPermit,
    spent: BTreeMap<AmountKey, i64>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct StoredCompletionReserve {
    reserve: CompletionReserve,
    held_budget_ids: Vec<String>,
}

impl StoredBudgetAccount {
    fn available(&self) -> BTreeMap<AmountKey, i64> {
        let mut keys = BTreeSet::new();
        keys.extend(self.allocated.keys().cloned());
        keys.extend(self.reserved.keys().cloned());
        keys.extend(self.committed.keys().cloned());
        let mut available = BTreeMap::new();
        for key in keys {
            let remaining = self.allocated.get(&key).copied().unwrap_or(0)
                - self.reserved.get(&key).copied().unwrap_or(0)
                - self.committed.get(&key).copied().unwrap_or(0);
            if remaining > 0 {
                available.insert(key, remaining);
            }
        }
        available
    }

    fn balance(&self) -> BudgetBalance {
        BudgetBalance {
            budget_id: self.account.budget_id.clone(),
            allocated: map_to_amounts(&self.allocated),
            reserved: map_to_amounts(&self.reserved),
            committed: map_to_amounts(&self.committed),
            available: map_to_amounts(&self.available()),
            overdraft: map_to_amounts(&self.overdraft),
            revision: self.account.revision,
        }
    }
}

impl SqliteBudgetLedger {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, BudgetError> {
        let connection = Connection::open(path).map_err(budget_storage_error)?;
        let ledger = Self { connection };
        ledger.initialize()?;
        Ok(ledger)
    }

    pub fn open_in_memory() -> Result<Self, BudgetError> {
        let connection = Connection::open_in_memory().map_err(budget_storage_error)?;
        let ledger = Self { connection };
        ledger.initialize()?;
        Ok(ledger)
    }

    fn initialize(&self) -> Result<(), BudgetError> {
        self.connection
            .execute_batch(
                "
                CREATE TABLE IF NOT EXISTS budget_accounts (
                    budget_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    allocated_json TEXT NOT NULL,
                    reserved_json TEXT NOT NULL,
                    committed_json TEXT NOT NULL,
                    overdraft_json TEXT NOT NULL,
                    parent_budget_id TEXT,
                    status TEXT NOT NULL,
                    policy_ref TEXT NOT NULL,
                    revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    budget_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    amounts_json TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    held_budget_ids_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_counters (
                    counter_name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_permits (
                    permit_id TEXT PRIMARY KEY,
                    reservation_refs_json TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    atomic_unit TEXT NOT NULL,
                    admission_epoch INTEGER NOT NULL,
                    authorized_amounts_json TEXT NOT NULL,
                    continuation_profile TEXT NOT NULL,
                    policy_snapshot_digest TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    low_watermark_json TEXT NOT NULL,
                    fencing_tokens_json TEXT NOT NULL,
                    spent_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_completion_reserves (
                    reserve_id TEXT PRIMARY KEY,
                    budget_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    amounts_json TEXT NOT NULL,
                    spendable_by_json TEXT NOT NULL,
                    expires_at TEXT,
                    status TEXT NOT NULL,
                    reservation_id TEXT,
                    fencing_token INTEGER NOT NULL,
                    held_budget_ids_json TEXT NOT NULL
                );
                ",
            )
            .map_err(budget_storage_error)?;
        Ok(())
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
        let allocated = amounts_to_map(amounts);
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(budget_storage_error)?;
        if sqlite_account_exists(&transaction, &budget_id)? {
            return Err(BudgetError::BudgetConflict { budget_id });
        }
        if let Some(parent_budget_id) = &parent_budget_id {
            if !sqlite_account_exists(&transaction, parent_budget_id)? {
                return Err(BudgetError::BudgetNotFound {
                    budget_id: parent_budget_id.clone(),
                });
            }
        }

        let account = BudgetAccount {
            budget_id: budget_id.clone(),
            scope: scope.into(),
            allocated: map_to_amounts(&allocated),
            parent_budget_id,
            status: BudgetStatus::Active,
            policy_ref: policy_ref.into(),
            revision: 1,
        };
        transaction
            .execute(
                "
                INSERT INTO budget_accounts (
                    budget_id,
                    scope,
                    allocated_json,
                    reserved_json,
                    committed_json,
                    overdraft_json,
                    parent_budget_id,
                    status,
                    policy_ref,
                    revision
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
                params![
                    &account.budget_id,
                    &account.scope,
                    usage_amounts_json(&account.allocated)?,
                    usage_amounts_json(&[])?,
                    usage_amounts_json(&[])?,
                    usage_amounts_json(&[])?,
                    &account.parent_budget_id,
                    account.status.as_str(),
                    &account.policy_ref,
                    budget_u64_to_i64(account.revision, "budget revision")?,
                ],
            )
            .map_err(budget_storage_error)?;
        transaction.commit().map_err(budget_storage_error)?;
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
        let requested = amounts_to_map(amounts);
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(budget_storage_error)?;
        if !sqlite_account_exists(&transaction, budget_id)? {
            return Err(BudgetError::BudgetNotFound {
                budget_id: budget_id.to_string(),
            });
        }

        let held_budget_ids = sqlite_budget_chain(&transaction, budget_id)?;
        for held_budget_id in &held_budget_ids {
            let account = sqlite_load_account(&transaction, held_budget_id)?
                .expect("budget chain only returns existing accounts");
            let available = account.available();
            for (key, amount) in &requested {
                if *amount > available.get(key).copied().unwrap_or(0) {
                    return Err(BudgetError::BudgetExceeded {
                        budget_id: held_budget_id.clone(),
                        kind: key.0.clone(),
                        unit: key.1.clone(),
                    });
                }
            }
        }

        let next_reservation = sqlite_next_counter(&transaction, "reservation_counter")?;
        let fencing_token = sqlite_next_counter(&transaction, "fencing_counter")?;
        let reservation_id =
            reservation_id.unwrap_or_else(|| format!("reservation-{next_reservation:06}"));
        if sqlite_reservation_exists(&transaction, &reservation_id)? {
            return Err(BudgetError::ReservationConflict { reservation_id });
        }

        for held_budget_id in &held_budget_ids {
            let mut account = sqlite_load_account(&transaction, held_budget_id)?
                .expect("budget chain only returns existing accounts");
            add_amounts(&mut account.reserved, &requested);
            account.account.revision += 1;
            sqlite_update_account_balances(&transaction, &account)?;
        }

        let reservation = BudgetReservation {
            reservation_id: reservation_id.clone(),
            budget_id: budget_id.to_string(),
            owner: owner.into(),
            amounts: map_to_amounts(&requested),
            purpose,
            expires_at: expires_at.into(),
            fencing_token,
            status: ReservationStatus::Reserved,
        };
        transaction
            .execute(
                "
                INSERT INTO budget_reservations (
                    reservation_id,
                    budget_id,
                    owner,
                    amounts_json,
                    purpose,
                    expires_at,
                    fencing_token,
                    status,
                    held_budget_ids_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
                params![
                    &reservation.reservation_id,
                    &reservation.budget_id,
                    &reservation.owner,
                    usage_amounts_json(&reservation.amounts)?,
                    reservation.purpose.as_str(),
                    &reservation.expires_at,
                    budget_u64_to_i64(reservation.fencing_token, "reservation fencing token")?,
                    reservation.status.as_str(),
                    string_list_json(&held_budget_ids)?,
                ],
            )
            .map_err(budget_storage_error)?;
        transaction.commit().map_err(budget_storage_error)?;
        Ok(reservation)
    }

    pub fn commit(
        &mut self,
        reservation_id: impl AsRef<str>,
        actual_amounts: impl IntoIterator<Item = UsageAmount>,
    ) -> Result<BudgetSettlement, BudgetError> {
        self.commit_inner(
            reservation_id.as_ref(),
            actual_amounts.into_iter().collect(),
            None,
        )
    }

    pub fn commit_with_overdraft_limit(
        &mut self,
        reservation_id: impl AsRef<str>,
        actual_amounts: impl IntoIterator<Item = UsageAmount>,
        max_overdraft: impl IntoIterator<Item = UsageAmount>,
    ) -> Result<BudgetSettlement, BudgetError> {
        self.commit_inner(
            reservation_id.as_ref(),
            actual_amounts.into_iter().collect(),
            Some(max_overdraft.into_iter().collect()),
        )
    }

    fn commit_inner(
        &mut self,
        reservation_id: &str,
        actual_amounts: Vec<UsageAmount>,
        max_overdraft: Option<Vec<UsageAmount>>,
    ) -> Result<BudgetSettlement, BudgetError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(budget_storage_error)?;
        let settlement =
            sqlite_commit_reserved(&transaction, reservation_id, actual_amounts, max_overdraft)?;
        transaction.commit().map_err(budget_storage_error)?;
        Ok(settlement)
    }

    pub fn release(
        &mut self,
        reservation_id: impl AsRef<str>,
    ) -> Result<BudgetSettlement, BudgetError> {
        self.release_inner(reservation_id.as_ref(), ReservationStatus::Released)
    }

    pub fn expire(
        &mut self,
        reservation_id: impl AsRef<str>,
    ) -> Result<BudgetSettlement, BudgetError> {
        self.release_inner(reservation_id.as_ref(), ReservationStatus::Expired)
    }

    fn release_inner(
        &mut self,
        reservation_id: &str,
        status: ReservationStatus,
    ) -> Result<BudgetSettlement, BudgetError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(budget_storage_error)?;
        let settlement = sqlite_release_reserved(&transaction, reservation_id, status)?;
        transaction.commit().map_err(budget_storage_error)?;
        Ok(settlement)
    }

    pub fn issue_permit(
        &mut self,
        permit_id: impl Into<String>,
        reservation_ids: Vec<String>,
        owner: impl Into<String>,
        atomic_unit: impl Into<String>,
        admission_epoch: u64,
        continuation_profile: impl Into<String>,
        policy_snapshot_digest: impl Into<String>,
        expires_at: impl Into<String>,
        low_watermark: Vec<UsageAmount>,
    ) -> Result<BudgetPermit, BudgetError> {
        let permit_id = permit_id.into();
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(budget_storage_error)?;
        if sqlite_permit_exists(&transaction, &permit_id)? {
            return Err(BudgetError::PermitConflict { permit_id });
        }

        let mut authorized = BTreeMap::new();
        let mut fencing_tokens = BTreeMap::new();
        for reservation_id in &reservation_ids {
            let stored_reservation = sqlite_load_reservation(&transaction, reservation_id)?
                .ok_or_else(|| BudgetError::ReservationNotFound {
                    reservation_id: reservation_id.clone(),
                })?;
            let reservation = stored_reservation.reservation;
            if reservation.status != ReservationStatus::Reserved {
                return Err(BudgetError::ReservationState {
                    reservation_id: reservation_id.clone(),
                    status: reservation.status,
                });
            }
            add_amounts(
                &mut authorized,
                &amounts_to_map(reservation.amounts.clone()),
            );
            for held_budget_id in stored_reservation.held_budget_ids {
                let current = fencing_tokens.get(&held_budget_id).copied().unwrap_or(0);
                if reservation.fencing_token > current {
                    fencing_tokens.insert(held_budget_id, reservation.fencing_token);
                }
            }
        }

        let permit = BudgetPermit {
            permit_id: permit_id.clone(),
            reservation_refs: reservation_ids,
            owner: owner.into(),
            atomic_unit: atomic_unit.into(),
            admission_epoch,
            authorized_amounts: map_to_amounts(&authorized),
            continuation_profile: continuation_profile.into(),
            policy_snapshot_digest: policy_snapshot_digest.into(),
            expires_at: expires_at.into(),
            low_watermark,
            fencing_tokens,
        };
        transaction
            .execute(
                "
                INSERT INTO budget_permits (
                    permit_id,
                    reservation_refs_json,
                    owner,
                    atomic_unit,
                    admission_epoch,
                    authorized_amounts_json,
                    continuation_profile,
                    policy_snapshot_digest,
                    expires_at,
                    low_watermark_json,
                    fencing_tokens_json,
                    spent_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
                params![
                    &permit.permit_id,
                    string_list_json(&permit.reservation_refs)?,
                    &permit.owner,
                    &permit.atomic_unit,
                    budget_u64_to_i64(permit.admission_epoch, "permit admission epoch")?,
                    usage_amounts_json(&permit.authorized_amounts)?,
                    &permit.continuation_profile,
                    &permit.policy_snapshot_digest,
                    &permit.expires_at,
                    usage_amounts_json(&permit.low_watermark)?,
                    u64_map_json(&permit.fencing_tokens)?,
                    usage_amounts_json(&[])?,
                ],
            )
            .map_err(budget_storage_error)?;
        transaction.commit().map_err(budget_storage_error)?;
        Ok(permit)
    }

    pub fn commit_with_permit(
        &mut self,
        permit_id: impl AsRef<str>,
        reservation_id: impl AsRef<str>,
        actual_amounts: impl IntoIterator<Item = UsageAmount>,
    ) -> Result<BudgetSettlement, BudgetError> {
        self.commit_with_permit_inner(
            permit_id.as_ref(),
            reservation_id.as_ref(),
            actual_amounts,
            None,
        )
    }

    pub fn commit_with_permit_at(
        &mut self,
        permit_id: impl AsRef<str>,
        reservation_id: impl AsRef<str>,
        actual_amounts: impl IntoIterator<Item = UsageAmount>,
        now: impl AsRef<str>,
    ) -> Result<BudgetSettlement, BudgetError> {
        self.commit_with_permit_inner(
            permit_id.as_ref(),
            reservation_id.as_ref(),
            actual_amounts,
            Some(now.as_ref()),
        )
    }

    fn commit_with_permit_inner(
        &mut self,
        permit_id: &str,
        reservation_id: &str,
        actual_amounts: impl IntoIterator<Item = UsageAmount>,
        now: Option<&str>,
    ) -> Result<BudgetSettlement, BudgetError> {
        let actual_amounts = actual_amounts.into_iter().collect::<Vec<_>>();
        let actual = amounts_to_map(actual_amounts.clone());
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(budget_storage_error)?;
        let (mut permit, reservation) =
            sqlite_validate_permit_for_reservation(&transaction, permit_id, reservation_id)?;
        if let Some(now) = now {
            sqlite_ensure_permit_not_expired(&permit, now)?;
        }
        sqlite_ensure_permit_allows_additional(
            &permit,
            &actual,
            &reservation.reservation.budget_id,
        )?;
        let settlement =
            sqlite_commit_reserved(&transaction, reservation_id, actual_amounts, None)?;
        add_amounts(&mut permit.spent, &actual);
        sqlite_update_permit_spent(&transaction, permit_id, &permit.spent)?;
        transaction.commit().map_err(budget_storage_error)?;
        Ok(settlement)
    }

    pub fn release_with_permit(
        &mut self,
        permit_id: impl AsRef<str>,
        reservation_id: impl AsRef<str>,
    ) -> Result<BudgetSettlement, BudgetError> {
        let permit_id = permit_id.as_ref();
        let reservation_id = reservation_id.as_ref();
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(budget_storage_error)?;
        sqlite_validate_permit_for_reservation(&transaction, permit_id, reservation_id)?;
        let settlement =
            sqlite_release_reserved(&transaction, reservation_id, ReservationStatus::Released)?;
        transaction.commit().map_err(budget_storage_error)?;
        Ok(settlement)
    }

    pub fn release_with_permit_at(
        &mut self,
        permit_id: impl AsRef<str>,
        reservation_id: impl AsRef<str>,
        now: impl AsRef<str>,
    ) -> Result<BudgetSettlement, BudgetError> {
        let permit_id = permit_id.as_ref();
        let reservation_id = reservation_id.as_ref();
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(budget_storage_error)?;
        let (permit, _) =
            sqlite_validate_permit_for_reservation(&transaction, permit_id, reservation_id)?;
        sqlite_ensure_permit_not_expired(&permit, now.as_ref())?;
        let settlement =
            sqlite_release_reserved(&transaction, reservation_id, ReservationStatus::Released)?;
        transaction.commit().map_err(budget_storage_error)?;
        Ok(settlement)
    }

    pub fn create_completion_reserve<I, S>(
        &mut self,
        reserve_id: impl Into<String>,
        budget_id: impl AsRef<str>,
        purpose: CompletionReservePurpose,
        amounts: impl IntoIterator<Item = UsageAmount>,
        spendable_by: I,
        expires_at: Option<String>,
    ) -> Result<CompletionReserve, BudgetError>
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        let reserve_id = reserve_id.into();
        let budget_id = budget_id.as_ref();
        let requested = amounts_to_map(amounts);
        let spendable_by = spendable_by
            .into_iter()
            .map(Into::into)
            .collect::<BTreeSet<_>>();
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(budget_storage_error)?;
        if sqlite_completion_reserve_exists(&transaction, &reserve_id)? {
            return Err(BudgetError::CompletionReserveConflict { reserve_id });
        }
        if !sqlite_account_exists(&transaction, budget_id)? {
            return Err(BudgetError::BudgetNotFound {
                budget_id: budget_id.to_string(),
            });
        }

        let held_budget_ids = sqlite_budget_chain(&transaction, budget_id)?;
        for held_budget_id in &held_budget_ids {
            let account = sqlite_load_account(&transaction, held_budget_id)?
                .expect("budget chain only returns existing accounts");
            let available = account.available();
            for (key, amount) in &requested {
                if *amount > available.get(key).copied().unwrap_or(0) {
                    return Err(BudgetError::BudgetExceeded {
                        budget_id: held_budget_id.clone(),
                        kind: key.0.clone(),
                        unit: key.1.clone(),
                    });
                }
            }
        }

        let fencing_token = sqlite_next_counter(&transaction, "fencing_counter")?;
        for held_budget_id in &held_budget_ids {
            let mut account = sqlite_load_account(&transaction, held_budget_id)?
                .expect("budget chain only returns existing accounts");
            add_amounts(&mut account.reserved, &requested);
            account.account.revision += 1;
            sqlite_update_account_balances(&transaction, &account)?;
        }

        let reserve = CompletionReserve {
            reserve_id: reserve_id.clone(),
            budget_id: budget_id.to_string(),
            purpose,
            amounts: map_to_amounts(&requested),
            spendable_by,
            expires_at,
            status: CompletionReserveStatus::Available,
            reservation_id: None,
            fencing_token,
        };
        transaction
            .execute(
                "
                INSERT INTO budget_completion_reserves (
                    reserve_id,
                    budget_id,
                    purpose,
                    amounts_json,
                    spendable_by_json,
                    expires_at,
                    status,
                    reservation_id,
                    fencing_token,
                    held_budget_ids_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
                params![
                    &reserve.reserve_id,
                    &reserve.budget_id,
                    reserve.purpose.as_str(),
                    usage_amounts_json(&reserve.amounts)?,
                    string_set_json(&reserve.spendable_by)?,
                    &reserve.expires_at,
                    reserve.status.as_str(),
                    &reserve.reservation_id,
                    budget_u64_to_i64(reserve.fencing_token, "completion reserve fencing token")?,
                    string_list_json(&held_budget_ids)?,
                ],
            )
            .map_err(budget_storage_error)?;
        transaction.commit().map_err(budget_storage_error)?;
        Ok(reserve)
    }

    pub fn completion_reserve(
        &self,
        reserve_id: impl AsRef<str>,
    ) -> Result<CompletionReserve, BudgetError> {
        let reserve_id = reserve_id.as_ref();
        sqlite_load_completion_reserve(&self.connection, reserve_id)?
            .map(|stored| stored.reserve)
            .ok_or_else(|| BudgetError::CompletionReserveNotFound {
                reserve_id: reserve_id.to_string(),
            })
    }

    pub fn spend_completion_reserve(
        &mut self,
        reserve_id: impl AsRef<str>,
        spender: impl AsRef<str>,
        expires_at: impl Into<String>,
    ) -> Result<BudgetReservation, BudgetError> {
        let reserve_id = reserve_id.as_ref();
        let spender = spender.as_ref();
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(budget_storage_error)?;
        let stored_reserve =
            sqlite_load_completion_reserve(&transaction, reserve_id)?.ok_or_else(|| {
                BudgetError::CompletionReserveNotFound {
                    reserve_id: reserve_id.to_string(),
                }
            })?;
        let reserve = stored_reserve.reserve;
        if reserve.status != CompletionReserveStatus::Available {
            return Err(BudgetError::CompletionReserveState {
                reserve_id: reserve_id.to_string(),
                status: reserve.status,
            });
        }
        if !reserve.spendable_by.contains(spender) {
            return Err(BudgetError::CompletionReserveUnauthorized {
                reserve_id: reserve_id.to_string(),
                spender: spender.to_string(),
            });
        }

        let next_reservation = sqlite_next_counter(&transaction, "reservation_counter")?;
        let reservation_id = format!("reservation-{next_reservation:06}");
        if sqlite_reservation_exists(&transaction, &reservation_id)? {
            return Err(BudgetError::ReservationConflict { reservation_id });
        }

        let reservation = BudgetReservation {
            reservation_id: reservation_id.clone(),
            budget_id: reserve.budget_id.clone(),
            owner: spender.to_string(),
            amounts: reserve.amounts.clone(),
            purpose: reserve.purpose.reservation_purpose(),
            expires_at: expires_at.into(),
            fencing_token: reserve.fencing_token,
            status: ReservationStatus::Reserved,
        };
        sqlite_insert_reservation(&transaction, &reservation, &stored_reserve.held_budget_ids)?;
        sqlite_update_completion_reserve_spent(
            &transaction,
            reserve_id,
            &reservation.reservation_id,
        )?;
        transaction.commit().map_err(budget_storage_error)?;
        Ok(reservation)
    }

    pub fn release_completion_reserve(
        &mut self,
        reserve_id: impl AsRef<str>,
    ) -> Result<CompletionReserve, BudgetError> {
        self.settle_completion_reserve(reserve_id.as_ref(), CompletionReserveStatus::Released)
    }

    pub fn expire_completion_reserve(
        &mut self,
        reserve_id: impl AsRef<str>,
    ) -> Result<CompletionReserve, BudgetError> {
        self.settle_completion_reserve(reserve_id.as_ref(), CompletionReserveStatus::Expired)
    }

    fn settle_completion_reserve(
        &mut self,
        reserve_id: &str,
        status: CompletionReserveStatus,
    ) -> Result<CompletionReserve, BudgetError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(budget_storage_error)?;
        let stored_reserve =
            sqlite_load_completion_reserve(&transaction, reserve_id)?.ok_or_else(|| {
                BudgetError::CompletionReserveNotFound {
                    reserve_id: reserve_id.to_string(),
                }
            })?;
        let reserve = stored_reserve.reserve;
        if reserve.status != CompletionReserveStatus::Available {
            return Err(BudgetError::CompletionReserveState {
                reserve_id: reserve_id.to_string(),
                status: reserve.status,
            });
        }

        let reserved = amounts_to_map(reserve.amounts.clone());
        for held_budget_id in &stored_reserve.held_budget_ids {
            let mut account = sqlite_load_account(&transaction, held_budget_id)?
                .expect("completion reserve hold points to an existing budget account");
            subtract_amounts(&mut account.reserved, &reserved);
            account.account.revision += 1;
            sqlite_update_account_balances(&transaction, &account)?;
        }
        sqlite_update_completion_reserve_status(&transaction, reserve_id, status)?;
        transaction.commit().map_err(budget_storage_error)?;
        Ok(CompletionReserve { status, ..reserve })
    }

    pub fn balance(&self, budget_id: impl AsRef<str>) -> Result<BudgetBalance, BudgetError> {
        let budget_id = budget_id.as_ref();
        let account = sqlite_load_account(&self.connection, budget_id)?.ok_or_else(|| {
            BudgetError::BudgetNotFound {
                budget_id: budget_id.to_string(),
            }
        })?;
        Ok(account.balance())
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

fn sqlite_account_exists(connection: &Connection, budget_id: &str) -> Result<bool, BudgetError> {
    connection
        .query_row(
            "SELECT 1 FROM budget_accounts WHERE budget_id = ?",
            params![budget_id],
            |row| row.get::<_, i64>(0),
        )
        .optional()
        .map(|value| value.is_some())
        .map_err(budget_storage_error)
}

fn sqlite_reservation_exists(
    connection: &Connection,
    reservation_id: &str,
) -> Result<bool, BudgetError> {
    connection
        .query_row(
            "SELECT 1 FROM budget_reservations WHERE reservation_id = ?",
            params![reservation_id],
            |row| row.get::<_, i64>(0),
        )
        .optional()
        .map(|value| value.is_some())
        .map_err(budget_storage_error)
}

fn sqlite_load_reservation(
    connection: &Connection,
    reservation_id: &str,
) -> Result<Option<StoredBudgetReservation>, BudgetError> {
    let stored = connection
        .query_row(
            "
            SELECT
                reservation_id,
                budget_id,
                owner,
                amounts_json,
                purpose,
                expires_at,
                fencing_token,
                status,
                held_budget_ids_json
            FROM budget_reservations
            WHERE reservation_id = ?
            ",
            params![reservation_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, i64>(6)?,
                    row.get::<_, String>(7)?,
                    row.get::<_, String>(8)?,
                ))
            },
        )
        .optional()
        .map_err(budget_storage_error)?;

    let Some((
        reservation_id,
        budget_id,
        owner,
        amounts_json,
        purpose,
        expires_at,
        fencing_token,
        status,
        held_budget_ids_json,
    )) = stored
    else {
        return Ok(None);
    };

    let purpose = ReservationPurpose::from_str(&purpose).ok_or_else(|| BudgetError::Storage {
        message: format!("unknown reservation purpose {purpose:?}"),
    })?;
    let status = ReservationStatus::from_str(&status).ok_or_else(|| BudgetError::Storage {
        message: format!("unknown reservation status {status:?}"),
    })?;
    Ok(Some(StoredBudgetReservation {
        reservation: BudgetReservation {
            reservation_id,
            budget_id,
            owner,
            amounts: usage_amounts_from_json(&amounts_json)?,
            purpose,
            expires_at,
            fencing_token: budget_i64_to_u64(fencing_token, "reservation fencing token")?,
            status,
        },
        held_budget_ids: string_list_from_json(&held_budget_ids_json)?,
    }))
}

fn sqlite_insert_reservation(
    connection: &Connection,
    reservation: &BudgetReservation,
    held_budget_ids: &[String],
) -> Result<(), BudgetError> {
    connection
        .execute(
            "
            INSERT INTO budget_reservations (
                reservation_id,
                budget_id,
                owner,
                amounts_json,
                purpose,
                expires_at,
                fencing_token,
                status,
                held_budget_ids_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ",
            params![
                &reservation.reservation_id,
                &reservation.budget_id,
                &reservation.owner,
                usage_amounts_json(&reservation.amounts)?,
                reservation.purpose.as_str(),
                &reservation.expires_at,
                budget_u64_to_i64(reservation.fencing_token, "reservation fencing token")?,
                reservation.status.as_str(),
                string_list_json(held_budget_ids)?,
            ],
        )
        .map_err(budget_storage_error)?;
    Ok(())
}

fn sqlite_update_reservation_status(
    connection: &Connection,
    reservation_id: &str,
    status: ReservationStatus,
) -> Result<(), BudgetError> {
    let updated = connection
        .execute(
            "
            UPDATE budget_reservations
            SET status = ?
            WHERE reservation_id = ?
            ",
            params![status.as_str(), reservation_id],
        )
        .map_err(budget_storage_error)?;
    if updated == 0 {
        return Err(BudgetError::ReservationNotFound {
            reservation_id: reservation_id.to_string(),
        });
    }
    Ok(())
}

fn sqlite_completion_reserve_exists(
    connection: &Connection,
    reserve_id: &str,
) -> Result<bool, BudgetError> {
    connection
        .query_row(
            "SELECT 1 FROM budget_completion_reserves WHERE reserve_id = ?",
            params![reserve_id],
            |row| row.get::<_, i64>(0),
        )
        .optional()
        .map(|value| value.is_some())
        .map_err(budget_storage_error)
}

fn sqlite_load_completion_reserve(
    connection: &Connection,
    reserve_id: &str,
) -> Result<Option<StoredCompletionReserve>, BudgetError> {
    let stored = connection
        .query_row(
            "
            SELECT
                reserve_id,
                budget_id,
                purpose,
                amounts_json,
                spendable_by_json,
                expires_at,
                status,
                reservation_id,
                fencing_token,
                held_budget_ids_json
            FROM budget_completion_reserves
            WHERE reserve_id = ?
            ",
            params![reserve_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, Option<String>>(5)?,
                    row.get::<_, String>(6)?,
                    row.get::<_, Option<String>>(7)?,
                    row.get::<_, i64>(8)?,
                    row.get::<_, String>(9)?,
                ))
            },
        )
        .optional()
        .map_err(budget_storage_error)?;

    let Some((
        reserve_id,
        budget_id,
        purpose,
        amounts_json,
        spendable_by_json,
        expires_at,
        status,
        reservation_id,
        fencing_token,
        held_budget_ids_json,
    )) = stored
    else {
        return Ok(None);
    };
    let purpose =
        CompletionReservePurpose::from_str(&purpose).ok_or_else(|| BudgetError::Storage {
            message: format!("unknown completion reserve purpose {purpose:?}"),
        })?;
    let status =
        CompletionReserveStatus::from_str(&status).ok_or_else(|| BudgetError::Storage {
            message: format!("unknown completion reserve status {status:?}"),
        })?;

    Ok(Some(StoredCompletionReserve {
        reserve: CompletionReserve {
            reserve_id,
            budget_id,
            purpose,
            amounts: usage_amounts_from_json(&amounts_json)?,
            spendable_by: string_set_from_json(&spendable_by_json)?,
            expires_at,
            status,
            reservation_id,
            fencing_token: budget_i64_to_u64(fencing_token, "completion reserve fencing token")?,
        },
        held_budget_ids: string_list_from_json(&held_budget_ids_json)?,
    }))
}

fn sqlite_update_completion_reserve_spent(
    connection: &Connection,
    reserve_id: &str,
    reservation_id: &str,
) -> Result<(), BudgetError> {
    let updated = connection
        .execute(
            "
            UPDATE budget_completion_reserves
            SET status = ?, reservation_id = ?
            WHERE reserve_id = ?
            ",
            params![
                CompletionReserveStatus::Spent.as_str(),
                reservation_id,
                reserve_id,
            ],
        )
        .map_err(budget_storage_error)?;
    if updated == 0 {
        return Err(BudgetError::CompletionReserveNotFound {
            reserve_id: reserve_id.to_string(),
        });
    }
    Ok(())
}

fn sqlite_update_completion_reserve_status(
    connection: &Connection,
    reserve_id: &str,
    status: CompletionReserveStatus,
) -> Result<(), BudgetError> {
    let updated = connection
        .execute(
            "
            UPDATE budget_completion_reserves
            SET status = ?, reservation_id = NULL
            WHERE reserve_id = ?
            ",
            params![status.as_str(), reserve_id],
        )
        .map_err(budget_storage_error)?;
    if updated == 0 {
        return Err(BudgetError::CompletionReserveNotFound {
            reserve_id: reserve_id.to_string(),
        });
    }
    Ok(())
}

fn sqlite_commit_reserved(
    connection: &Connection,
    reservation_id: &str,
    actual_amounts: Vec<UsageAmount>,
    max_overdraft: Option<Vec<UsageAmount>>,
) -> Result<BudgetSettlement, BudgetError> {
    let stored_reservation =
        sqlite_load_reservation(connection, reservation_id)?.ok_or_else(|| {
            BudgetError::ReservationNotFound {
                reservation_id: reservation_id.to_string(),
            }
        })?;
    let reservation = stored_reservation.reservation;
    if reservation.status != ReservationStatus::Reserved {
        return Err(BudgetError::ReservationState {
            reservation_id: reservation_id.to_string(),
            status: reservation.status,
        });
    }

    let reserved = amounts_to_map(reservation.amounts.clone());
    let actual = amounts_to_map(actual_amounts);
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
    if let Some(max_overdraft) = max_overdraft {
        let overdraft_limit = amounts_to_map(max_overdraft);
        for (key, amount) in &overdraft {
            if *amount > overdraft_limit.get(key).copied().unwrap_or(0) {
                return Err(BudgetError::BudgetExceeded {
                    budget_id: reservation.budget_id.clone(),
                    kind: key.0.clone(),
                    unit: key.1.clone(),
                });
            }
        }
    }

    for held_budget_id in &stored_reservation.held_budget_ids {
        let mut account = sqlite_load_account(connection, held_budget_id)?
            .expect("reservation hold points to an existing budget account");
        subtract_amounts(&mut account.reserved, &reserved);
        add_amounts(&mut account.committed, &actual);
        add_amounts(&mut account.overdraft, &overdraft);
        account.account.revision += 1;
        sqlite_update_account_balances(connection, &account)?;
    }
    sqlite_update_reservation_status(connection, reservation_id, ReservationStatus::Committed)?;

    let account = sqlite_load_account(connection, &reservation.budget_id)?.ok_or_else(|| {
        BudgetError::BudgetNotFound {
            budget_id: reservation.budget_id.clone(),
        }
    })?;
    Ok(BudgetSettlement {
        reservation_id: reservation_id.to_string(),
        budget_id: reservation.budget_id,
        committed: map_to_amounts(&actual),
        released: map_to_amounts(&released),
        overdraft: map_to_amounts(&overdraft),
        status: ReservationStatus::Committed,
        revision: account.account.revision,
    })
}

fn sqlite_release_reserved(
    connection: &Connection,
    reservation_id: &str,
    status: ReservationStatus,
) -> Result<BudgetSettlement, BudgetError> {
    let stored_reservation =
        sqlite_load_reservation(connection, reservation_id)?.ok_or_else(|| {
            BudgetError::ReservationNotFound {
                reservation_id: reservation_id.to_string(),
            }
        })?;
    let reservation = stored_reservation.reservation;
    if reservation.status != ReservationStatus::Reserved {
        return Err(BudgetError::ReservationState {
            reservation_id: reservation_id.to_string(),
            status: reservation.status,
        });
    }

    let reserved = amounts_to_map(reservation.amounts.clone());
    for held_budget_id in &stored_reservation.held_budget_ids {
        let mut account = sqlite_load_account(connection, held_budget_id)?
            .expect("reservation hold points to an existing budget account");
        subtract_amounts(&mut account.reserved, &reserved);
        account.account.revision += 1;
        sqlite_update_account_balances(connection, &account)?;
    }
    sqlite_update_reservation_status(connection, reservation_id, status)?;

    let account = sqlite_load_account(connection, &reservation.budget_id)?.ok_or_else(|| {
        BudgetError::BudgetNotFound {
            budget_id: reservation.budget_id.clone(),
        }
    })?;
    Ok(BudgetSettlement {
        reservation_id: reservation_id.to_string(),
        budget_id: reservation.budget_id,
        committed: Vec::new(),
        released: map_to_amounts(&reserved),
        overdraft: Vec::new(),
        status,
        revision: account.account.revision,
    })
}

fn sqlite_permit_exists(connection: &Connection, permit_id: &str) -> Result<bool, BudgetError> {
    connection
        .query_row(
            "SELECT 1 FROM budget_permits WHERE permit_id = ?",
            params![permit_id],
            |row| row.get::<_, i64>(0),
        )
        .optional()
        .map(|value| value.is_some())
        .map_err(budget_storage_error)
}

fn sqlite_load_permit(
    connection: &Connection,
    permit_id: &str,
) -> Result<Option<StoredBudgetPermit>, BudgetError> {
    let stored = connection
        .query_row(
            "
            SELECT
                permit_id,
                reservation_refs_json,
                owner,
                atomic_unit,
                admission_epoch,
                authorized_amounts_json,
                continuation_profile,
                policy_snapshot_digest,
                expires_at,
                low_watermark_json,
                fencing_tokens_json,
                spent_json
            FROM budget_permits
            WHERE permit_id = ?
            ",
            params![permit_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, i64>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, String>(6)?,
                    row.get::<_, String>(7)?,
                    row.get::<_, String>(8)?,
                    row.get::<_, String>(9)?,
                    row.get::<_, String>(10)?,
                    row.get::<_, String>(11)?,
                ))
            },
        )
        .optional()
        .map_err(budget_storage_error)?;

    let Some((
        permit_id,
        reservation_refs_json,
        owner,
        atomic_unit,
        admission_epoch,
        authorized_amounts_json,
        continuation_profile,
        policy_snapshot_digest,
        expires_at,
        low_watermark_json,
        fencing_tokens_json,
        spent_json,
    )) = stored
    else {
        return Ok(None);
    };

    Ok(Some(StoredBudgetPermit {
        permit: BudgetPermit {
            permit_id,
            reservation_refs: string_list_from_json(&reservation_refs_json)?,
            owner,
            atomic_unit,
            admission_epoch: budget_i64_to_u64(admission_epoch, "permit admission epoch")?,
            authorized_amounts: usage_amounts_from_json(&authorized_amounts_json)?,
            continuation_profile,
            policy_snapshot_digest,
            expires_at,
            low_watermark: usage_amounts_from_json(&low_watermark_json)?,
            fencing_tokens: u64_map_from_json(&fencing_tokens_json)?,
        },
        spent: amounts_to_map(usage_amounts_from_json(&spent_json)?),
    }))
}

fn sqlite_update_permit_spent(
    connection: &Connection,
    permit_id: &str,
    spent: &BTreeMap<AmountKey, i64>,
) -> Result<(), BudgetError> {
    let updated = connection
        .execute(
            "
            UPDATE budget_permits
            SET spent_json = ?
            WHERE permit_id = ?
            ",
            params![usage_amounts_json(&map_to_amounts(spent))?, permit_id],
        )
        .map_err(budget_storage_error)?;
    if updated == 0 {
        return Err(BudgetError::PermitNotFound {
            permit_id: permit_id.to_string(),
        });
    }
    Ok(())
}

fn sqlite_validate_permit_for_reservation(
    connection: &Connection,
    permit_id: &str,
    reservation_id: &str,
) -> Result<(StoredBudgetPermit, StoredBudgetReservation), BudgetError> {
    let permit =
        sqlite_load_permit(connection, permit_id)?.ok_or_else(|| BudgetError::PermitNotFound {
            permit_id: permit_id.to_string(),
        })?;
    let reservation = sqlite_load_reservation(connection, reservation_id)?.ok_or_else(|| {
        BudgetError::ReservationNotFound {
            reservation_id: reservation_id.to_string(),
        }
    })?;
    if !permit
        .permit
        .reservation_refs
        .iter()
        .any(|reference| reference == reservation_id)
    {
        return Err(BudgetError::PermitScope {
            permit_id: permit_id.to_string(),
            reservation_id: reservation_id.to_string(),
        });
    }
    for budget_id in &reservation.held_budget_ids {
        let actual_token = permit.permit.fencing_tokens.get(budget_id).copied();
        if actual_token.is_none_or(|token| token < reservation.reservation.fencing_token) {
            return Err(BudgetError::PermitFencing {
                permit_id: permit_id.to_string(),
                budget_id: budget_id.clone(),
                required_token: reservation.reservation.fencing_token,
                actual_token,
            });
        }
    }
    Ok((permit, reservation))
}

fn sqlite_ensure_permit_not_expired(
    permit: &StoredBudgetPermit,
    now: &str,
) -> Result<(), BudgetError> {
    if permit.permit.expires_at.as_str() <= now {
        return Err(BudgetError::PermitExpired {
            permit_id: permit.permit.permit_id.clone(),
            expires_at: permit.permit.expires_at.clone(),
            now: now.to_string(),
        });
    }
    Ok(())
}

fn sqlite_ensure_permit_allows_additional(
    permit: &StoredBudgetPermit,
    requested: &BTreeMap<AmountKey, i64>,
    budget_id: &str,
) -> Result<(), BudgetError> {
    let authorized = amounts_to_map(permit.permit.authorized_amounts.clone());
    for (key, amount) in requested {
        let already_spent = permit.spent.get(key).copied().unwrap_or(0);
        if already_spent + amount > authorized.get(key).copied().unwrap_or(0) {
            return Err(BudgetError::BudgetExceeded {
                budget_id: budget_id.to_string(),
                kind: key.0.clone(),
                unit: key.1.clone(),
            });
        }
    }
    Ok(())
}

fn sqlite_load_account(
    connection: &Connection,
    budget_id: &str,
) -> Result<Option<StoredBudgetAccount>, BudgetError> {
    let stored = connection
        .query_row(
            "
            SELECT
                budget_id,
                scope,
                allocated_json,
                reserved_json,
                committed_json,
                overdraft_json,
                parent_budget_id,
                status,
                policy_ref,
                revision
            FROM budget_accounts
            WHERE budget_id = ?
            ",
            params![budget_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, Option<String>>(6)?,
                    row.get::<_, String>(7)?,
                    row.get::<_, String>(8)?,
                    row.get::<_, i64>(9)?,
                ))
            },
        )
        .optional()
        .map_err(budget_storage_error)?;

    let Some((
        budget_id,
        scope,
        allocated_json,
        reserved_json,
        committed_json,
        overdraft_json,
        parent_budget_id,
        status,
        policy_ref,
        revision,
    )) = stored
    else {
        return Ok(None);
    };

    let status = BudgetStatus::from_str(&status).ok_or_else(|| BudgetError::Storage {
        message: format!("unknown budget status {status:?}"),
    })?;
    let allocated = amounts_to_map(usage_amounts_from_json(&allocated_json)?);
    let reserved = amounts_to_map(usage_amounts_from_json(&reserved_json)?);
    let committed = amounts_to_map(usage_amounts_from_json(&committed_json)?);
    let overdraft = amounts_to_map(usage_amounts_from_json(&overdraft_json)?);

    Ok(Some(StoredBudgetAccount {
        account: BudgetAccount {
            budget_id,
            scope,
            allocated: map_to_amounts(&allocated),
            parent_budget_id,
            status,
            policy_ref,
            revision: budget_i64_to_u64(revision, "budget revision")?,
        },
        allocated,
        reserved,
        committed,
        overdraft,
    }))
}

fn sqlite_update_account_balances(
    connection: &Connection,
    account: &StoredBudgetAccount,
) -> Result<(), BudgetError> {
    let updated = connection
        .execute(
            "
            UPDATE budget_accounts
            SET
                allocated_json = ?,
                reserved_json = ?,
                committed_json = ?,
                overdraft_json = ?,
                revision = ?
            WHERE budget_id = ?
            ",
            params![
                usage_amounts_json(&map_to_amounts(&account.allocated))?,
                usage_amounts_json(&map_to_amounts(&account.reserved))?,
                usage_amounts_json(&map_to_amounts(&account.committed))?,
                usage_amounts_json(&map_to_amounts(&account.overdraft))?,
                budget_u64_to_i64(account.account.revision, "budget revision")?,
                &account.account.budget_id,
            ],
        )
        .map_err(budget_storage_error)?;
    if updated == 0 {
        return Err(BudgetError::BudgetNotFound {
            budget_id: account.account.budget_id.clone(),
        });
    }
    Ok(())
}

fn sqlite_budget_chain(
    connection: &Connection,
    budget_id: &str,
) -> Result<Vec<String>, BudgetError> {
    let mut chain = Vec::new();
    let mut seen = BTreeSet::new();
    let mut current_id = Some(budget_id.to_string());
    while let Some(id) = current_id {
        if !seen.insert(id.clone()) {
            return Err(BudgetError::BudgetConflict { budget_id: id });
        }
        let account =
            sqlite_load_account(connection, &id)?.ok_or_else(|| BudgetError::BudgetNotFound {
                budget_id: id.clone(),
            })?;
        chain.push(id);
        current_id = account.account.parent_budget_id;
    }
    Ok(chain)
}

fn sqlite_next_counter(connection: &Connection, counter_name: &str) -> Result<u64, BudgetError> {
    let current = connection
        .query_row(
            "SELECT value FROM budget_counters WHERE counter_name = ?",
            params![counter_name],
            |row| row.get::<_, i64>(0),
        )
        .optional()
        .map_err(budget_storage_error)?;
    let next = current
        .unwrap_or(0)
        .checked_add(1)
        .ok_or_else(|| BudgetError::Storage {
            message: format!("budget counter {counter_name:?} exceeds SQLite integer range"),
        })?;
    connection
        .execute(
            "
            INSERT INTO budget_counters (counter_name, value)
            VALUES (?, ?)
            ON CONFLICT(counter_name) DO UPDATE SET value = excluded.value
            ",
            params![counter_name, next],
        )
        .map_err(budget_storage_error)?;
    budget_i64_to_u64(next, "budget counter")
}

fn usage_amounts_json(amounts: &[UsageAmount]) -> Result<String, BudgetError> {
    let values = amounts
        .iter()
        .map(|amount| {
            let mut value = Map::new();
            value.insert("kind".to_string(), Value::String(amount.kind.clone()));
            value.insert(
                "amount".to_string(),
                Value::Number(Number::from(amount.amount)),
            );
            value.insert("unit".to_string(), Value::String(amount.unit.clone()));
            value.insert(
                "dimensions".to_string(),
                string_map_value(&amount.dimensions),
            );
            Value::Object(value)
        })
        .collect::<Vec<_>>();
    serde_json::to_string(&values).map_err(budget_storage_error)
}

fn usage_amounts_from_json(value: &str) -> Result<Vec<UsageAmount>, BudgetError> {
    let Value::Array(values) =
        serde_json::from_str::<Value>(value).map_err(budget_storage_error)?
    else {
        return Err(BudgetError::Storage {
            message: "budget usage amounts must be an array".to_string(),
        });
    };

    let mut amounts = Vec::new();
    for value in values {
        let Value::Object(mut object) = value else {
            return Err(BudgetError::Storage {
                message: "budget usage amount must be an object".to_string(),
            });
        };
        let Some(kind) = object.remove("kind").and_then(|value| match value {
            Value::String(value) => Some(value),
            _ => None,
        }) else {
            return Err(BudgetError::Storage {
                message: "budget usage amount kind must be a string".to_string(),
            });
        };
        let Some(amount) = object.remove("amount").and_then(|value| value.as_i64()) else {
            return Err(BudgetError::Storage {
                message: "budget usage amount must be an integer".to_string(),
            });
        };
        let Some(unit) = object.remove("unit").and_then(|value| match value {
            Value::String(value) => Some(value),
            _ => None,
        }) else {
            return Err(BudgetError::Storage {
                message: "budget usage amount unit must be a string".to_string(),
            });
        };
        let dimensions = match object.remove("dimensions") {
            Some(value) => string_map_from_value(value)?,
            None => BTreeMap::new(),
        };
        amounts.push(UsageAmount {
            kind,
            amount,
            unit,
            dimensions,
        });
    }
    Ok(amounts)
}

fn string_list_json(values: &[String]) -> Result<String, BudgetError> {
    let values = values
        .iter()
        .map(|value| Value::String(value.clone()))
        .collect::<Vec<_>>();
    serde_json::to_string(&values).map_err(budget_storage_error)
}

fn string_list_from_json(value: &str) -> Result<Vec<String>, BudgetError> {
    let Value::Array(values) =
        serde_json::from_str::<Value>(value).map_err(budget_storage_error)?
    else {
        return Err(BudgetError::Storage {
            message: "budget string list must be an array".to_string(),
        });
    };

    let mut result = Vec::new();
    for value in values {
        let Value::String(value) = value else {
            return Err(BudgetError::Storage {
                message: "budget string list value must be a string".to_string(),
            });
        };
        result.push(value);
    }
    Ok(result)
}

fn string_set_json(values: &BTreeSet<String>) -> Result<String, BudgetError> {
    let values = values
        .iter()
        .map(|value| Value::String(value.clone()))
        .collect::<Vec<_>>();
    serde_json::to_string(&values).map_err(budget_storage_error)
}

fn string_set_from_json(value: &str) -> Result<BTreeSet<String>, BudgetError> {
    let Value::Array(values) =
        serde_json::from_str::<Value>(value).map_err(budget_storage_error)?
    else {
        return Err(BudgetError::Storage {
            message: "budget string set must be an array".to_string(),
        });
    };

    let mut result = BTreeSet::new();
    for value in values {
        let Value::String(value) = value else {
            return Err(BudgetError::Storage {
                message: "budget string set value must be a string".to_string(),
            });
        };
        result.insert(value);
    }
    Ok(result)
}

fn string_map_value(value: &BTreeMap<String, String>) -> Value {
    Value::Object(
        value
            .iter()
            .map(|(key, value)| (key.clone(), Value::String(value.clone())))
            .collect(),
    )
}

fn string_map_from_value(value: Value) -> Result<BTreeMap<String, String>, BudgetError> {
    let Value::Object(object) = value else {
        return Err(BudgetError::Storage {
            message: "budget string map must be an object".to_string(),
        });
    };
    let mut result = BTreeMap::new();
    for (key, value) in object {
        let Value::String(value) = value else {
            return Err(BudgetError::Storage {
                message: format!("budget string map value for {key:?} must be a string"),
            });
        };
        result.insert(key, value);
    }
    Ok(result)
}

fn u64_map_json(value: &BTreeMap<String, u64>) -> Result<String, BudgetError> {
    let value = Value::Object(
        value
            .iter()
            .map(|(key, value)| (key.clone(), Value::Number(Number::from(*value))))
            .collect(),
    );
    serde_json::to_string(&value).map_err(budget_storage_error)
}

fn u64_map_from_json(value: &str) -> Result<BTreeMap<String, u64>, BudgetError> {
    let Value::Object(object) =
        serde_json::from_str::<Value>(value).map_err(budget_storage_error)?
    else {
        return Err(BudgetError::Storage {
            message: "budget u64 map must be an object".to_string(),
        });
    };
    let mut result = BTreeMap::new();
    for (key, value) in object {
        let Some(value) = value.as_u64() else {
            return Err(BudgetError::Storage {
                message: format!("budget u64 map value for {key:?} must be an unsigned integer"),
            });
        };
        result.insert(key, value);
    }
    Ok(result)
}

fn budget_u64_to_i64(value: u64, label: &'static str) -> Result<i64, BudgetError> {
    i64::try_from(value).map_err(|_| BudgetError::Storage {
        message: format!("{label} exceeds SQLite integer range"),
    })
}

fn budget_i64_to_u64(value: i64, label: &'static str) -> Result<u64, BudgetError> {
    u64::try_from(value).map_err(|_| BudgetError::Storage {
        message: format!("{label} must be non-negative"),
    })
}

fn budget_storage_error(error: impl std::fmt::Display) -> BudgetError {
    BudgetError::Storage {
        message: error.to_string(),
    }
}

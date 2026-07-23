use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::SystemTime;

use serde_json::Value;

#[derive(Debug, Eq, PartialEq)]
pub enum LeaseError {
    InvalidCapacity,
    InvalidUnits,
    InvalidExpiration,
    InvalidIdentity {
        field: &'static str,
    },
    IdentifierOverflow,
    CapacityExhausted {
        pool_id: String,
        requested_units: u64,
        available_units: u64,
    },
    UnknownLease {
        pool_id: String,
        lease_id: String,
    },
    StaleFencingToken {
        pool_id: String,
        lease_id: String,
    },
    NotYetActive {
        pool_id: String,
        lease_id: String,
        acquired_at: SystemTime,
        attempted_at: SystemTime,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LeaseRequest {
    owner: String,
    units: u64,
    attribute_selector: BTreeMap<String, Value>,
    expires_at: Option<SystemTime>,
}

impl LeaseRequest {
    pub fn new(owner: impl Into<String>, units: u64) -> Self {
        Self {
            owner: owner.into(),
            units,
            attribute_selector: BTreeMap::new(),
            expires_at: None,
        }
    }

    pub fn with_attribute_selector(mut self, attributes: BTreeMap<String, Value>) -> Self {
        self.attribute_selector = attributes;
        self
    }

    pub fn with_expires_at(mut self, expires_at: SystemTime) -> Self {
        self.expires_at = Some(expires_at);
        self
    }
}

#[derive(Clone, Debug)]
pub struct LeasePool {
    inner: Arc<Mutex<Inner>>,
}

#[derive(Debug)]
struct Inner {
    id: String,
    capacity_units: u64,
    used_units: u64,
    next_lease_number: u64,
    next_fencing_token: u64,
    active: HashMap<String, ActiveLease>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ActiveLease {
    owner: String,
    units: u64,
    attributes: BTreeMap<String, Value>,
    fencing_token: u64,
    acquired_at: SystemTime,
    expires_at: Option<SystemTime>,
}

#[derive(Debug)]
pub struct ResourceLease {
    inner: Arc<Mutex<Inner>>,
    lease_id: String,
    released: AtomicBool,
}

impl LeasePool {
    pub fn new(id: impl Into<String>, capacity_units: u64) -> Result<Self, LeaseError> {
        if capacity_units == 0 {
            return Err(LeaseError::InvalidCapacity);
        }
        let id = id.into();
        validate_identity("id", &id)?;
        Ok(Self {
            inner: Arc::new(Mutex::new(Inner {
                id,
                capacity_units,
                used_units: 0,
                next_lease_number: 1,
                next_fencing_token: 1,
                active: HashMap::new(),
            })),
        })
    }

    pub fn id(&self) -> String {
        self.lock().id.clone()
    }

    pub fn capacity_units(&self) -> u64 {
        self.lock().capacity_units
    }

    pub fn available_units(&self) -> u64 {
        let inner = self.lock();
        inner.capacity_units - inner.used_units
    }

    pub fn try_acquire(&self, request: LeaseRequest) -> Result<ResourceLease, LeaseError> {
        self.try_acquire_at(request, SystemTime::now())
    }

    pub fn try_acquire_at(
        &self,
        request: LeaseRequest,
        acquired_at: SystemTime,
    ) -> Result<ResourceLease, LeaseError> {
        if request.units == 0 {
            return Err(LeaseError::InvalidUnits);
        }
        validate_identity("owner", &request.owner)?;
        if request
            .expires_at
            .is_some_and(|expires_at| expires_at <= acquired_at)
        {
            return Err(LeaseError::InvalidExpiration);
        }

        let mut inner = self.lock();
        Self::reap_expired_locked(&mut inner, acquired_at);
        let available_units = inner.capacity_units - inner.used_units;
        if request.units > available_units {
            return Err(LeaseError::CapacityExhausted {
                pool_id: inner.id.clone(),
                requested_units: request.units,
                available_units,
            });
        }

        let lease_id = format!("{}-{}", inner.id, inner.next_lease_number);
        let next_lease_number = inner
            .next_lease_number
            .checked_add(1)
            .ok_or(LeaseError::IdentifierOverflow)?;
        let fencing_token = inner.next_fencing_token;
        let next_fencing_token = inner
            .next_fencing_token
            .checked_add(1)
            .ok_or(LeaseError::IdentifierOverflow)?;
        let used_units = inner
            .used_units
            .checked_add(request.units)
            .ok_or(LeaseError::IdentifierOverflow)?;
        inner.next_lease_number = next_lease_number;
        inner.next_fencing_token = next_fencing_token;
        inner.used_units = used_units;
        inner.active.insert(
            lease_id.clone(),
            ActiveLease {
                owner: request.owner,
                units: request.units,
                attributes: request.attribute_selector,
                fencing_token,
                acquired_at,
                expires_at: request.expires_at,
            },
        );

        Ok(ResourceLease {
            inner: Arc::clone(&self.inner),
            lease_id,
            released: AtomicBool::new(false),
        })
    }

    pub fn reap_expired(&self, now: SystemTime) -> usize {
        let mut inner = self.lock();
        Self::reap_expired_locked(&mut inner, now)
    }

    pub fn renew(
        &self,
        lease_id: &str,
        fencing_token: u64,
        expires_at: SystemTime,
    ) -> Result<u64, LeaseError> {
        self.renew_at(lease_id, fencing_token, expires_at, SystemTime::now())
    }

    pub fn renew_at(
        &self,
        lease_id: &str,
        fencing_token: u64,
        expires_at: SystemTime,
        renewed_at: SystemTime,
    ) -> Result<u64, LeaseError> {
        if expires_at <= renewed_at {
            return Err(LeaseError::InvalidExpiration);
        }

        let mut inner = self.lock();
        Self::reap_expired_locked(&mut inner, renewed_at);
        let pool_id = inner.id.clone();
        let Some((current_token, acquired_at, current_expiration)) = inner
            .active
            .get(lease_id)
            .map(|active| (active.fencing_token, active.acquired_at, active.expires_at))
        else {
            return Err(LeaseError::UnknownLease {
                pool_id,
                lease_id: lease_id.to_owned(),
            });
        };
        if renewed_at < acquired_at {
            return Err(LeaseError::NotYetActive {
                pool_id,
                lease_id: lease_id.to_owned(),
                acquired_at,
                attempted_at: renewed_at,
            });
        }
        if current_token != fencing_token {
            return Err(LeaseError::StaleFencingToken {
                pool_id,
                lease_id: lease_id.to_owned(),
            });
        }
        let Some(current_expiration) = current_expiration else {
            return Err(LeaseError::InvalidExpiration);
        };
        if expires_at <= current_expiration {
            return Err(LeaseError::InvalidExpiration);
        }

        let renewed_token = inner.next_fencing_token;
        inner.next_fencing_token = inner
            .next_fencing_token
            .checked_add(1)
            .ok_or(LeaseError::IdentifierOverflow)?;
        if let Some(active) = inner.active.get_mut(lease_id) {
            active.fencing_token = renewed_token;
            active.expires_at = Some(expires_at);
        }
        Ok(renewed_token)
    }

    pub fn validate_fencing_token(
        &self,
        lease_id: &str,
        fencing_token: u64,
    ) -> Result<(), LeaseError> {
        self.validate_fencing_token_at(lease_id, fencing_token, SystemTime::now())
    }

    pub fn validate_fencing_token_at(
        &self,
        lease_id: &str,
        fencing_token: u64,
        validated_at: SystemTime,
    ) -> Result<(), LeaseError> {
        let mut inner = self.lock();
        Self::reap_expired_locked(&mut inner, validated_at);
        let Some(active) = inner.active.get(lease_id) else {
            return Err(LeaseError::UnknownLease {
                pool_id: inner.id.clone(),
                lease_id: lease_id.to_owned(),
            });
        };
        if validated_at < active.acquired_at {
            return Err(LeaseError::NotYetActive {
                pool_id: inner.id.clone(),
                lease_id: lease_id.to_owned(),
                acquired_at: active.acquired_at,
                attempted_at: validated_at,
            });
        }
        if active.fencing_token != fencing_token {
            return Err(LeaseError::StaleFencingToken {
                pool_id: inner.id.clone(),
                lease_id: lease_id.to_owned(),
            });
        }
        Ok(())
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn reap_expired_locked(inner: &mut Inner, now: SystemTime) -> usize {
        let expired_ids = inner
            .active
            .iter()
            .filter_map(|(lease_id, active)| {
                active
                    .expires_at
                    .filter(|expires_at| *expires_at <= now)
                    .map(|_| lease_id.clone())
            })
            .collect::<Vec<_>>();
        let mut reaped = 0;
        for lease_id in expired_ids {
            if let Some(active) = inner.active.remove(&lease_id) {
                inner.used_units = inner.used_units.saturating_sub(active.units);
                reaped += 1;
            }
        }
        reaped
    }
}

fn validate_identity(field: &'static str, value: &str) -> Result<(), LeaseError> {
    if value.trim().is_empty() || value != value.trim() {
        return Err(LeaseError::InvalidIdentity { field });
    }
    Ok(())
}

impl ResourceLease {
    pub fn lease_id(&self) -> &str {
        &self.lease_id
    }

    pub fn owner(&self) -> String {
        self.active()
            .map_or_else(String::new, |active| active.owner)
    }

    pub fn units(&self) -> u64 {
        self.active().map_or(0, |active| active.units)
    }

    pub fn attributes(&self) -> BTreeMap<String, Value> {
        self.active()
            .map_or_else(BTreeMap::new, |active| active.attributes)
    }

    pub fn fencing_token(&self) -> u64 {
        self.active().map_or(0, |active| active.fencing_token)
    }

    pub fn acquired_at(&self) -> Option<SystemTime> {
        self.active().map(|active| active.acquired_at)
    }

    pub fn expires_at(&self) -> Option<SystemTime> {
        self.active().and_then(|active| active.expires_at)
    }

    pub fn release(&self) -> bool {
        if self.released.swap(true, Ordering::AcqRel) {
            return false;
        }
        let mut inner = self.inner.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some(active) = inner.active.remove(&self.lease_id) {
            inner.used_units = inner.used_units.saturating_sub(active.units);
            return true;
        }
        false
    }

    fn active(&self) -> Option<ActiveLease> {
        self.inner
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .active
            .get(&self.lease_id)
            .cloned()
    }
}

impl Drop for ResourceLease {
    fn drop(&mut self) {
        self.release();
    }
}

#[cfg(test)]
mod tests {
    use super::{LeaseError, LeasePool, LeaseRequest};

    #[test]
    fn acquisition_rejects_identifier_counter_overflow_without_reserving_capacity() {
        let pool = LeasePool::new("pool", 1).expect("pool is valid");
        {
            let mut inner = pool.lock();
            inner.next_fencing_token = u64::MAX;
        }

        assert!(matches!(
            pool.try_acquire(LeaseRequest::new("owner", 1)),
            Err(LeaseError::IdentifierOverflow)
        ));
        assert_eq!(pool.available_units(), 1);
    }

    #[test]
    fn renewal_rejects_fencing_counter_overflow_without_mutating_lease() {
        let pool = LeasePool::new("pool", 1).expect("pool is valid");
        let acquired_at = std::time::SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(10);
        let lease = pool
            .try_acquire_at(
                LeaseRequest::new("owner", 1).with_expires_at(
                    std::time::SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(20),
                ),
                acquired_at,
            )
            .expect("lease is valid");
        let original_token = lease.fencing_token();
        {
            let mut inner = pool.lock();
            inner.next_fencing_token = u64::MAX;
        }

        assert_eq!(
            pool.renew_at(
                lease.lease_id(),
                original_token,
                std::time::SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(30),
                acquired_at + std::time::Duration::from_secs(1),
            ),
            Err(LeaseError::IdentifierOverflow)
        );
        assert_eq!(lease.fencing_token(), original_token);
    }
}

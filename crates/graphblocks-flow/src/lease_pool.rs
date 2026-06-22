use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::SystemTime;

use serde_json::Value;

#[derive(Debug, Eq, PartialEq)]
pub enum LeaseError {
    InvalidCapacity,
    InvalidUnits,
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
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LeaseRequest {
    owner: String,
    units: u64,
    attribute_selector: BTreeMap<String, Value>,
}

impl LeaseRequest {
    pub fn new(owner: impl Into<String>, units: u64) -> Self {
        Self {
            owner: owner.into(),
            units,
            attribute_selector: BTreeMap::new(),
        }
    }

    pub fn with_attribute_selector(mut self, attributes: BTreeMap<String, Value>) -> Self {
        self.attribute_selector = attributes;
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
        Ok(Self {
            inner: Arc::new(Mutex::new(Inner {
                id: id.into(),
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
        if request.units == 0 {
            return Err(LeaseError::InvalidUnits);
        }

        let mut inner = self.lock();
        let available_units = inner.capacity_units - inner.used_units;
        if request.units > available_units {
            return Err(LeaseError::CapacityExhausted {
                pool_id: inner.id.clone(),
                requested_units: request.units,
                available_units,
            });
        }

        let lease_id = format!("{}-{}", inner.id, inner.next_lease_number);
        inner.next_lease_number += 1;
        let fencing_token = inner.next_fencing_token;
        inner.next_fencing_token += 1;
        inner.used_units += request.units;
        inner.active.insert(
            lease_id.clone(),
            ActiveLease {
                owner: request.owner,
                units: request.units,
                attributes: request.attribute_selector,
                fencing_token,
                acquired_at: SystemTime::now(),
            },
        );

        Ok(ResourceLease {
            inner: Arc::clone(&self.inner),
            lease_id,
            released: AtomicBool::new(false),
        })
    }

    pub fn validate_fencing_token(
        &self,
        lease_id: &str,
        fencing_token: u64,
    ) -> Result<(), LeaseError> {
        let inner = self.lock();
        let Some(active) = inner.active.get(lease_id) else {
            return Err(LeaseError::UnknownLease {
                pool_id: inner.id.clone(),
                lease_id: lease_id.to_owned(),
            });
        };
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

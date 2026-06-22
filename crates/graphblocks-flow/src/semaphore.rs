use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

#[derive(Debug, Eq, PartialEq)]
pub enum SemaphoreError {
    InvalidLimit,
    CapacityExhausted { semaphore_id: String },
}

#[derive(Clone, Debug)]
pub struct LocalSemaphore {
    inner: Arc<Mutex<Inner>>,
}

#[derive(Debug)]
struct Inner {
    id: String,
    limit: usize,
    used: usize,
}

#[derive(Debug)]
pub struct SemaphorePermit {
    inner: Arc<Mutex<Inner>>,
    released: AtomicBool,
    owner: String,
}

impl LocalSemaphore {
    pub fn new(id: impl Into<String>, limit: usize) -> Result<Self, SemaphoreError> {
        if limit == 0 {
            return Err(SemaphoreError::InvalidLimit);
        }
        Ok(Self {
            inner: Arc::new(Mutex::new(Inner {
                id: id.into(),
                limit,
                used: 0,
            })),
        })
    }

    pub fn id(&self) -> String {
        self.lock().id.clone()
    }

    pub fn limit(&self) -> usize {
        self.lock().limit
    }

    pub fn available(&self) -> usize {
        let inner = self.lock();
        inner.limit - inner.used
    }

    pub fn try_acquire(&self, owner: impl Into<String>) -> Result<SemaphorePermit, SemaphoreError> {
        let mut inner = self.lock();
        if inner.used == inner.limit {
            return Err(SemaphoreError::CapacityExhausted {
                semaphore_id: inner.id.clone(),
            });
        }
        inner.used += 1;
        Ok(SemaphorePermit {
            inner: Arc::clone(&self.inner),
            released: AtomicBool::new(false),
            owner: owner.into(),
        })
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

impl SemaphorePermit {
    pub fn owner(&self) -> &str {
        &self.owner
    }

    pub fn release(&self) -> bool {
        if self.released.swap(true, Ordering::AcqRel) {
            return false;
        }
        let mut inner = self.inner.lock().unwrap_or_else(PoisonError::into_inner);
        inner.used = inner.used.saturating_sub(1);
        true
    }
}

impl Drop for SemaphorePermit {
    fn drop(&mut self) {
        self.release();
    }
}

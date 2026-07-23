use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

#[derive(Debug, Eq, PartialEq)]
pub enum RateLimitError {
    InvalidLimit,
    InvalidWindow,
    InvalidIdentity { field: &'static str },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RateLimitDecision {
    Allowed,
    Limited { retry_after_ms: u64 },
    Rejected { reason: &'static str },
}

#[derive(Clone, Debug)]
pub struct LocalRateLimiter {
    inner: Arc<Mutex<Inner>>,
}

#[derive(Debug)]
struct Inner {
    id: String,
    limit: u64,
    window_ms: u64,
    window_start_ms: u64,
    used: u64,
}

impl LocalRateLimiter {
    pub fn new(id: impl Into<String>, limit: u64, window_ms: u64) -> Result<Self, RateLimitError> {
        if limit == 0 {
            return Err(RateLimitError::InvalidLimit);
        }
        if window_ms == 0 {
            return Err(RateLimitError::InvalidWindow);
        }
        let id = id.into();
        validate_identity("id", &id)?;
        Ok(Self {
            inner: Arc::new(Mutex::new(Inner {
                id,
                limit,
                window_ms,
                window_start_ms: 0,
                used: 0,
            })),
        })
    }

    pub fn id(&self) -> String {
        self.lock().id.clone()
    }

    pub fn limit(&self) -> u64 {
        self.lock().limit
    }

    pub fn window_ms(&self) -> u64 {
        self.lock().window_ms
    }

    pub fn available_at(&self, now_ms: u64) -> u64 {
        let mut inner = self.lock();
        Self::refresh_window(&mut inner, now_ms);
        inner.limit - inner.used
    }

    pub fn check_at(&self, owner: impl Into<String>, now_ms: u64, units: u64) -> RateLimitDecision {
        let owner = owner.into();
        if validate_identity("owner", &owner).is_err() {
            return RateLimitDecision::Rejected {
                reason: "invalid_owner",
            };
        }
        if units == 0 {
            return RateLimitDecision::Rejected {
                reason: "invalid_units",
            };
        }

        let mut inner = self.lock();
        Self::refresh_window(&mut inner, now_ms);
        if units > inner.limit {
            return RateLimitDecision::Rejected {
                reason: "units_exceed_limit",
            };
        }
        let retry_after_ms = now_ms.checked_sub(inner.window_start_ms).map_or_else(
            || {
                inner
                    .window_start_ms
                    .saturating_sub(now_ms)
                    .saturating_add(inner.window_ms)
            },
            |elapsed| inner.window_ms.saturating_sub(elapsed),
        );
        let Some(next_used) = inner.used.checked_add(units) else {
            return RateLimitDecision::Limited { retry_after_ms };
        };
        if next_used > inner.limit {
            return RateLimitDecision::Limited { retry_after_ms };
        }

        inner.used = next_used;
        RateLimitDecision::Allowed
    }

    fn refresh_window(inner: &mut Inner, now_ms: u64) {
        if now_ms
            .checked_sub(inner.window_start_ms)
            .is_some_and(|elapsed| elapsed >= inner.window_ms)
        {
            inner.window_start_ms = now_ms;
            inner.used = 0;
        }
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

fn validate_identity(field: &'static str, value: &str) -> Result<(), RateLimitError> {
    if value.trim().is_empty() || value != value.trim() {
        return Err(RateLimitError::InvalidIdentity { field });
    }
    Ok(())
}

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, SystemTime};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AdmissionTicketState {
    Queued,
    Admitted,
    Running,
    Completed,
    Failed,
    Cancelled,
    Expired,
}

impl AdmissionTicketState {
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Completed | Self::Failed | Self::Cancelled | Self::Expired
        )
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AdmissionTicketQueueConfig {
    max_concurrent: usize,
    rate_limit: usize,
    rate_window: Duration,
    max_pending: usize,
    ticket_ttl: Duration,
}

impl AdmissionTicketQueueConfig {
    pub fn new(
        max_concurrent: usize,
        rate_limit: usize,
        rate_window: Duration,
        max_pending: usize,
        ticket_ttl: Duration,
    ) -> Result<Self, AdmissionTicketError> {
        if max_concurrent == 0 {
            return Err(AdmissionTicketError::InvalidMaxConcurrent);
        }
        if rate_limit == 0 {
            return Err(AdmissionTicketError::InvalidRateLimit);
        }
        if rate_window.is_zero() {
            return Err(AdmissionTicketError::InvalidRateWindow);
        }
        if ticket_ttl.is_zero() {
            return Err(AdmissionTicketError::InvalidTicketTtl);
        }
        Ok(Self {
            max_concurrent,
            rate_limit,
            rate_window,
            max_pending,
            ticket_ttl,
        })
    }

    pub fn max_concurrent(self) -> usize {
        self.max_concurrent
    }

    pub fn rate_limit(self) -> usize {
        self.rate_limit
    }

    pub fn rate_window(self) -> Duration {
        self.rate_window
    }

    pub fn max_pending(self) -> usize {
        self.max_pending
    }

    pub fn ticket_ttl(self) -> Duration {
        self.ticket_ttl
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AdmissionTicket {
    ticket_id: String,
    request_id: String,
    owner: String,
    state: AdmissionTicketState,
    created_at: SystemTime,
    expires_at: SystemTime,
    admitted_at: Option<SystemTime>,
    running_at: Option<SystemTime>,
    terminal_at: Option<SystemTime>,
    fencing_token: Option<u64>,
}

impl AdmissionTicket {
    pub fn ticket_id(&self) -> &str {
        &self.ticket_id
    }

    pub fn request_id(&self) -> &str {
        &self.request_id
    }

    pub fn owner(&self) -> &str {
        &self.owner
    }

    pub fn state(&self) -> AdmissionTicketState {
        self.state
    }

    pub fn created_at(&self) -> SystemTime {
        self.created_at
    }

    pub fn expires_at(&self) -> SystemTime {
        self.expires_at
    }

    pub fn admitted_at(&self) -> Option<SystemTime> {
        self.admitted_at
    }

    pub fn running_at(&self) -> Option<SystemTime> {
        self.running_at
    }

    pub fn terminal_at(&self) -> Option<SystemTime> {
        self.terminal_at
    }

    pub fn fencing_token(&self) -> Option<u64> {
        self.fencing_token
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AdmissionTicketReceipt {
    ticket: AdmissionTicket,
    duplicate: bool,
}

impl AdmissionTicketReceipt {
    pub fn ticket(&self) -> &AdmissionTicket {
        &self.ticket
    }

    pub fn into_ticket(self) -> AdmissionTicket {
        self.ticket
    }

    pub fn duplicate(&self) -> bool {
        self.duplicate
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AdmissionTicketClaim {
    ticket_id: String,
    request_id: String,
    owner: String,
    fencing_token: u64,
    claimed_at: SystemTime,
    expires_at: SystemTime,
}

impl AdmissionTicketClaim {
    pub fn ticket_id(&self) -> &str {
        &self.ticket_id
    }

    pub fn request_id(&self) -> &str {
        &self.request_id
    }

    pub fn owner(&self) -> &str {
        &self.owner
    }

    pub fn fencing_token(&self) -> u64 {
        self.fencing_token
    }

    pub fn claimed_at(&self) -> SystemTime {
        self.claimed_at
    }

    pub fn expires_at(&self) -> SystemTime {
        self.expires_at
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct AdmissionTicketMaintenance {
    pub expired: usize,
    pub promoted: usize,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct AdmissionTicketQueueCounts {
    pub queued: usize,
    pub admitted: usize,
    pub running: usize,
}

impl AdmissionTicketQueueCounts {
    pub fn concurrent(self) -> usize {
        self.admitted + self.running
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AdmissionTicketError {
    InvalidMaxConcurrent,
    InvalidRateLimit,
    InvalidRateWindow,
    InvalidTicketTtl,
    InvalidIdentity {
        field: &'static str,
    },
    PendingCapacityExhausted {
        max_pending: usize,
    },
    ExpirationOverflow,
    IdentifierOverflow,
    UnknownTicket {
        ticket_id: String,
    },
    UnknownRequest {
        request_id: String,
        owner: String,
    },
    InvalidState {
        ticket_id: String,
        expected: &'static str,
        actual: AdmissionTicketState,
    },
    StaleFencingToken {
        ticket_id: String,
        expected: u64,
        actual: u64,
    },
}

#[derive(Clone, Debug)]
pub struct AdmissionTicketQueue {
    inner: Arc<Mutex<Inner>>,
}

#[derive(Debug)]
struct Inner {
    config: AdmissionTicketQueueConfig,
    tickets: HashMap<String, AdmissionTicket>,
    tickets_by_request: HashMap<(String, String), String>,
    queued: VecDeque<String>,
    admitted: VecDeque<String>,
    concurrent: usize,
    next_ticket_number: u64,
    next_fencing_token: u64,
    rate_window_started_at: SystemTime,
    rate_window_used: usize,
}

impl AdmissionTicketQueue {
    pub fn new(config: AdmissionTicketQueueConfig) -> Self {
        Self::new_at(config, SystemTime::now())
    }

    pub fn new_at(config: AdmissionTicketQueueConfig, now: SystemTime) -> Self {
        Self {
            inner: Arc::new(Mutex::new(Inner {
                config,
                tickets: HashMap::new(),
                tickets_by_request: HashMap::new(),
                queued: VecDeque::new(),
                admitted: VecDeque::new(),
                concurrent: 0,
                next_ticket_number: 1,
                next_fencing_token: 1,
                rate_window_started_at: now,
                rate_window_used: 0,
            })),
        }
    }

    pub fn config(&self) -> AdmissionTicketQueueConfig {
        self.lock().config
    }

    pub fn admit(
        &self,
        request_id: impl Into<String>,
        owner: impl Into<String>,
    ) -> Result<AdmissionTicketReceipt, AdmissionTicketError> {
        self.admit_at(request_id, owner, SystemTime::now())
    }

    pub fn admit_at(
        &self,
        request_id: impl Into<String>,
        owner: impl Into<String>,
        now: SystemTime,
    ) -> Result<AdmissionTicketReceipt, AdmissionTicketError> {
        let request_id = request_id.into();
        let owner = owner.into();
        validate_identity("request_id", &request_id)?;
        validate_identity("owner", &owner)?;

        let mut inner = self.lock();
        inner.maintain_at(now);
        let request_key = (request_id.clone(), owner.clone());
        if let Some(ticket_id) = inner.tickets_by_request.get(&request_key)
            && let Some(ticket) = inner.tickets.get(ticket_id)
        {
            return Ok(AdmissionTicketReceipt {
                ticket: ticket.clone(),
                duplicate: true,
            });
        }

        let expires_at = now
            .checked_add(inner.config.ticket_ttl)
            .ok_or(AdmissionTicketError::ExpirationOverflow)?;
        let admit_now = inner.concurrent < inner.config.max_concurrent
            && inner.rate_window_used < inner.config.rate_limit;
        if !admit_now && inner.queued.len() >= inner.config.max_pending {
            return Err(AdmissionTicketError::PendingCapacityExhausted {
                max_pending: inner.config.max_pending,
            });
        }

        let ticket_number = inner.next_ticket_number;
        inner.next_ticket_number = inner
            .next_ticket_number
            .checked_add(1)
            .ok_or(AdmissionTicketError::IdentifierOverflow)?;
        let ticket_id = format!("ticket-{ticket_number:020}");
        let state = if admit_now {
            AdmissionTicketState::Admitted
        } else {
            AdmissionTicketState::Queued
        };
        let ticket = AdmissionTicket {
            ticket_id: ticket_id.clone(),
            request_id,
            owner,
            state,
            created_at: now,
            expires_at,
            admitted_at: admit_now.then_some(now),
            running_at: None,
            terminal_at: None,
            fencing_token: None,
        };
        if admit_now {
            inner.concurrent += 1;
            inner.rate_window_used += 1;
            inner.admitted.push_back(ticket_id.clone());
        } else {
            inner.queued.push_back(ticket_id.clone());
        }
        inner.tickets_by_request.insert(
            (ticket.request_id.clone(), ticket.owner.clone()),
            ticket_id.clone(),
        );
        inner.tickets.insert(ticket_id, ticket.clone());

        Ok(AdmissionTicketReceipt {
            ticket,
            duplicate: false,
        })
    }

    pub fn ticket(
        &self,
        ticket_id: impl AsRef<str>,
    ) -> Result<AdmissionTicket, AdmissionTicketError> {
        self.ticket_at(ticket_id, SystemTime::now())
    }

    pub fn ticket_at(
        &self,
        ticket_id: impl AsRef<str>,
        now: SystemTime,
    ) -> Result<AdmissionTicket, AdmissionTicketError> {
        let ticket_id = ticket_id.as_ref();
        let mut inner = self.lock();
        inner.maintain_at(now);
        inner
            .tickets
            .get(ticket_id)
            .cloned()
            .ok_or_else(|| AdmissionTicketError::UnknownTicket {
                ticket_id: ticket_id.to_owned(),
            })
    }

    pub fn ticket_for_at(
        &self,
        request_id: impl Into<String>,
        owner: impl Into<String>,
        now: SystemTime,
    ) -> Result<AdmissionTicket, AdmissionTicketError> {
        let request_id = request_id.into();
        let owner = owner.into();
        validate_identity("request_id", &request_id)?;
        validate_identity("owner", &owner)?;
        let mut inner = self.lock();
        inner.maintain_at(now);
        let ticket_id = inner
            .tickets_by_request
            .get(&(request_id.clone(), owner.clone()))
            .ok_or_else(|| AdmissionTicketError::UnknownRequest {
                request_id: request_id.clone(),
                owner: owner.clone(),
            })?;
        inner
            .tickets
            .get(ticket_id)
            .cloned()
            .ok_or(AdmissionTicketError::UnknownRequest { request_id, owner })
    }

    pub fn claim_next(&self) -> Result<Option<AdmissionTicketClaim>, AdmissionTicketError> {
        self.claim_next_at(SystemTime::now())
    }

    pub fn claim_next_at(
        &self,
        now: SystemTime,
    ) -> Result<Option<AdmissionTicketClaim>, AdmissionTicketError> {
        let mut inner = self.lock();
        inner.maintain_at(now);
        while let Some(ticket_id) = inner.admitted.pop_front() {
            let Some(ticket) = inner.tickets.get(&ticket_id) else {
                continue;
            };
            if ticket.state != AdmissionTicketState::Admitted {
                continue;
            }
            let fencing_token = inner.next_fencing_token;
            inner.next_fencing_token = inner
                .next_fencing_token
                .checked_add(1)
                .ok_or(AdmissionTicketError::IdentifierOverflow)?;
            let ticket = inner
                .tickets
                .get_mut(&ticket_id)
                .expect("admitted ticket was checked before claim");
            ticket.state = AdmissionTicketState::Running;
            ticket.running_at = Some(now);
            ticket.fencing_token = Some(fencing_token);
            return Ok(Some(AdmissionTicketClaim {
                ticket_id: ticket.ticket_id.clone(),
                request_id: ticket.request_id.clone(),
                owner: ticket.owner.clone(),
                fencing_token,
                claimed_at: now,
                expires_at: ticket.expires_at,
            }));
        }
        Ok(None)
    }

    pub fn complete(
        &self,
        ticket_id: impl AsRef<str>,
        fencing_token: u64,
    ) -> Result<AdmissionTicket, AdmissionTicketError> {
        self.complete_at(ticket_id, fencing_token, SystemTime::now())
    }

    pub fn complete_at(
        &self,
        ticket_id: impl AsRef<str>,
        fencing_token: u64,
        now: SystemTime,
    ) -> Result<AdmissionTicket, AdmissionTicketError> {
        self.transition_claimed_at(
            ticket_id.as_ref(),
            fencing_token,
            AdmissionTicketState::Completed,
            now,
        )
    }

    pub fn fail(
        &self,
        ticket_id: impl AsRef<str>,
        fencing_token: u64,
    ) -> Result<AdmissionTicket, AdmissionTicketError> {
        self.fail_at(ticket_id, fencing_token, SystemTime::now())
    }

    pub fn fail_at(
        &self,
        ticket_id: impl AsRef<str>,
        fencing_token: u64,
        now: SystemTime,
    ) -> Result<AdmissionTicket, AdmissionTicketError> {
        self.transition_claimed_at(
            ticket_id.as_ref(),
            fencing_token,
            AdmissionTicketState::Failed,
            now,
        )
    }

    pub fn cancel_claimed(
        &self,
        ticket_id: impl AsRef<str>,
        fencing_token: u64,
    ) -> Result<AdmissionTicket, AdmissionTicketError> {
        self.cancel_claimed_at(ticket_id, fencing_token, SystemTime::now())
    }

    pub fn cancel_claimed_at(
        &self,
        ticket_id: impl AsRef<str>,
        fencing_token: u64,
        now: SystemTime,
    ) -> Result<AdmissionTicket, AdmissionTicketError> {
        self.transition_claimed_at(
            ticket_id.as_ref(),
            fencing_token,
            AdmissionTicketState::Cancelled,
            now,
        )
    }

    pub fn cancel(
        &self,
        ticket_id: impl AsRef<str>,
    ) -> Result<AdmissionTicket, AdmissionTicketError> {
        self.cancel_at(ticket_id, SystemTime::now())
    }

    pub fn cancel_at(
        &self,
        ticket_id: impl AsRef<str>,
        now: SystemTime,
    ) -> Result<AdmissionTicket, AdmissionTicketError> {
        let ticket_id = ticket_id.as_ref();
        let mut inner = self.lock();
        inner.maintain_at(now);
        let current_state = inner
            .tickets
            .get(ticket_id)
            .map(|ticket| ticket.state)
            .ok_or_else(|| AdmissionTicketError::UnknownTicket {
                ticket_id: ticket_id.to_owned(),
            })?;
        if current_state == AdmissionTicketState::Cancelled {
            return inner.tickets.get(ticket_id).cloned().ok_or_else(|| {
                AdmissionTicketError::UnknownTicket {
                    ticket_id: ticket_id.to_owned(),
                }
            });
        }
        if current_state.is_terminal() {
            return Err(AdmissionTicketError::InvalidState {
                ticket_id: ticket_id.to_owned(),
                expected: "queued, admitted, or running",
                actual: current_state,
            });
        }
        if current_state == AdmissionTicketState::Running {
            return Err(AdmissionTicketError::InvalidState {
                ticket_id: ticket_id.to_owned(),
                expected: "queued or admitted; use cancel_claimed after the worker exits",
                actual: current_state,
            });
        }
        if current_state == AdmissionTicketState::Admitted {
            inner.concurrent = inner.concurrent.saturating_sub(1);
        }
        let ticket = inner
            .tickets
            .get_mut(ticket_id)
            .expect("nonterminal ticket was checked before cancellation");
        ticket.state = AdmissionTicketState::Cancelled;
        ticket.terminal_at = Some(now);
        let cancelled = ticket.clone();
        inner.remove_terminal_from_dispatch_queues();
        inner.promote_at(now);
        Ok(cancelled)
    }

    pub fn refresh(&self) -> AdmissionTicketMaintenance {
        self.refresh_at(SystemTime::now())
    }

    pub fn refresh_at(&self, now: SystemTime) -> AdmissionTicketMaintenance {
        self.lock().maintain_at(now)
    }

    pub fn counts(&self) -> AdmissionTicketQueueCounts {
        self.counts_at(SystemTime::now())
    }

    pub fn counts_at(&self, now: SystemTime) -> AdmissionTicketQueueCounts {
        let mut inner = self.lock();
        inner.maintain_at(now);
        inner.counts()
    }

    fn transition_claimed_at(
        &self,
        ticket_id: &str,
        fencing_token: u64,
        terminal_state: AdmissionTicketState,
        now: SystemTime,
    ) -> Result<AdmissionTicket, AdmissionTicketError> {
        let mut inner = self.lock();
        inner.maintain_at(now);
        let ticket =
            inner
                .tickets
                .get(ticket_id)
                .ok_or_else(|| AdmissionTicketError::UnknownTicket {
                    ticket_id: ticket_id.to_owned(),
                })?;
        if ticket.state != AdmissionTicketState::Running {
            return Err(AdmissionTicketError::InvalidState {
                ticket_id: ticket_id.to_owned(),
                expected: "running",
                actual: ticket.state,
            });
        }
        let expected = ticket
            .fencing_token
            .expect("running admission ticket has a fencing token");
        if fencing_token != expected {
            return Err(AdmissionTicketError::StaleFencingToken {
                ticket_id: ticket_id.to_owned(),
                expected,
                actual: fencing_token,
            });
        }
        let ticket = inner
            .tickets
            .get_mut(ticket_id)
            .expect("running ticket was checked before terminal transition");
        ticket.state = terminal_state;
        ticket.terminal_at = Some(now);
        let terminal = ticket.clone();
        inner.concurrent = inner.concurrent.saturating_sub(1);
        inner.promote_at(now);
        Ok(terminal)
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

impl Inner {
    fn maintain_at(&mut self, now: SystemTime) -> AdmissionTicketMaintenance {
        if now
            .duration_since(self.rate_window_started_at)
            .is_ok_and(|elapsed| elapsed >= self.config.rate_window)
        {
            self.rate_window_started_at = now;
            self.rate_window_used = 0;
        }

        let mut expired = 0;
        let mut released_capacity = 0;
        for ticket in self.tickets.values_mut() {
            if !ticket.state.is_terminal() && ticket.expires_at <= now {
                if matches!(
                    ticket.state,
                    AdmissionTicketState::Admitted | AdmissionTicketState::Running
                ) {
                    released_capacity += 1;
                }
                ticket.state = AdmissionTicketState::Expired;
                ticket.terminal_at = Some(now);
                expired += 1;
            }
        }
        self.concurrent = self.concurrent.saturating_sub(released_capacity);
        self.remove_terminal_from_dispatch_queues();
        self.prune_expired_terminal_tickets(now);
        let promoted = self.promote_at(now);
        AdmissionTicketMaintenance { expired, promoted }
    }

    fn promote_at(&mut self, now: SystemTime) -> usize {
        let mut promoted = 0;
        while self.concurrent < self.config.max_concurrent
            && self.rate_window_used < self.config.rate_limit
        {
            let Some(ticket_id) = self.queued.pop_front() else {
                break;
            };
            let Some(ticket) = self.tickets.get_mut(&ticket_id) else {
                continue;
            };
            if ticket.state != AdmissionTicketState::Queued {
                continue;
            }
            if ticket.expires_at <= now {
                ticket.state = AdmissionTicketState::Expired;
                ticket.terminal_at = Some(now);
                continue;
            }
            ticket.state = AdmissionTicketState::Admitted;
            ticket.admitted_at = Some(now);
            self.concurrent += 1;
            self.rate_window_used += 1;
            self.admitted.push_back(ticket_id);
            promoted += 1;
        }
        promoted
    }

    fn remove_terminal_from_dispatch_queues(&mut self) {
        let tickets = &self.tickets;
        self.queued.retain(|ticket_id| {
            tickets
                .get(ticket_id)
                .is_some_and(|ticket| ticket.state == AdmissionTicketState::Queued)
        });
        self.admitted.retain(|ticket_id| {
            tickets
                .get(ticket_id)
                .is_some_and(|ticket| ticket.state == AdmissionTicketState::Admitted)
        });
    }

    fn prune_expired_terminal_tickets(&mut self, now: SystemTime) {
        let ticket_ids = self
            .tickets
            .iter()
            .filter(|(_, ticket)| ticket.state.is_terminal() && ticket.expires_at <= now)
            .map(|(ticket_id, _)| ticket_id.clone())
            .collect::<Vec<_>>();
        for ticket_id in ticket_ids {
            let Some(ticket) = self.tickets.remove(&ticket_id) else {
                continue;
            };
            let request_key = (ticket.request_id, ticket.owner);
            if self
                .tickets_by_request
                .get(&request_key)
                .is_some_and(|current_ticket_id| current_ticket_id == &ticket_id)
            {
                self.tickets_by_request.remove(&request_key);
            }
        }
    }

    fn counts(&self) -> AdmissionTicketQueueCounts {
        let mut counts = AdmissionTicketQueueCounts::default();
        for ticket in self.tickets.values() {
            match ticket.state {
                AdmissionTicketState::Queued => counts.queued += 1,
                AdmissionTicketState::Admitted => counts.admitted += 1,
                AdmissionTicketState::Running => counts.running += 1,
                AdmissionTicketState::Completed
                | AdmissionTicketState::Failed
                | AdmissionTicketState::Cancelled
                | AdmissionTicketState::Expired => {}
            }
        }
        counts
    }
}

fn validate_identity(field: &'static str, value: &str) -> Result<(), AdmissionTicketError> {
    if value.trim().is_empty() || value != value.trim() {
        return Err(AdmissionTicketError::InvalidIdentity { field });
    }
    Ok(())
}

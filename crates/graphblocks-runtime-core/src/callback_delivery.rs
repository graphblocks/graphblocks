use std::collections::BTreeSet;

use crate::application_event::{ApplicationProtocolEvent, ApplicationProtocolEventKind};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EventFilter {
    pub types: Option<BTreeSet<ApplicationProtocolEventKind>>,
    pub include_terminal_events: bool,
}

impl EventFilter {
    pub fn new() -> Self {
        Self {
            types: None,
            include_terminal_events: true,
        }
    }

    pub fn with_types(
        mut self,
        types: impl IntoIterator<Item = ApplicationProtocolEventKind>,
    ) -> Self {
        self.types = Some(types.into_iter().collect());
        self
    }

    pub fn with_terminal_events(mut self, include_terminal_events: bool) -> Self {
        self.include_terminal_events = include_terminal_events;
        self
    }

    pub fn matches(&self, event: &ApplicationProtocolEvent) -> bool {
        if self.include_terminal_events
            && matches!(
                event.kind,
                ApplicationProtocolEventKind::RunCompleted
                    | ApplicationProtocolEventKind::RunFailed
                    | ApplicationProtocolEventKind::RunCancelled
            )
        {
            return true;
        }

        self.types
            .as_ref()
            .is_none_or(|types| types.contains(&event.kind))
    }
}

impl Default for EventFilter {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CallbackFailurePolicy {
    BestEffort,
    RetryThenDeadLetter,
    PauseRunOnFailure,
    FailRunOnFailure,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CallbackSubscriptionStatus {
    Active,
    Paused,
    Expired,
    Revoked,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CallbackSubscription {
    pub subscription_id: String,
    pub owner: String,
    pub scope: String,
    pub scope_id: String,
    pub event_filter: EventFilter,
    pub delivery_target: String,
    pub status: CallbackSubscriptionStatus,
    pub created_at_unix_ms: u64,
    pub expires_at_unix_ms: Option<u64>,
    pub replay_from_cursor: Option<String>,
    pub failure_policy: CallbackFailurePolicy,
}

impl CallbackSubscription {
    pub fn new(
        subscription_id: impl Into<String>,
        owner: impl Into<String>,
        scope: impl Into<String>,
        scope_id: impl Into<String>,
        event_filter: EventFilter,
        delivery_target: impl Into<String>,
        failure_policy: CallbackFailurePolicy,
        created_at_unix_ms: u64,
    ) -> Result<Self, CallbackDeliveryError> {
        let subscription = Self {
            subscription_id: subscription_id.into(),
            owner: owner.into(),
            scope: scope.into(),
            scope_id: scope_id.into(),
            event_filter,
            delivery_target: delivery_target.into(),
            status: CallbackSubscriptionStatus::Active,
            created_at_unix_ms,
            expires_at_unix_ms: None,
            replay_from_cursor: None,
            failure_policy,
        };
        subscription.validate()?;
        Ok(subscription)
    }

    pub fn validate(&self) -> Result<(), CallbackDeliveryError> {
        for (field, value) in [
            ("subscription_id", &self.subscription_id),
            ("owner", &self.owner),
            ("scope", &self.scope),
            ("scope_id", &self.scope_id),
            ("delivery_target", &self.delivery_target),
        ] {
            if value.trim().is_empty() {
                return Err(CallbackDeliveryError::EmptyField {
                    field: field.to_owned(),
                });
            }
        }
        if self
            .replay_from_cursor
            .as_ref()
            .is_some_and(|cursor| cursor.trim().is_empty())
        {
            return Err(CallbackDeliveryError::EmptyField {
                field: "replay_from_cursor".to_owned(),
            });
        }
        Ok(())
    }

    pub fn can_receive(&self, event: &ApplicationProtocolEvent) -> bool {
        self.status == CallbackSubscriptionStatus::Active
            && self
                .expires_at_unix_ms
                .is_none_or(|expires_at| event.metadata.occurred_at_unix_ms <= expires_at)
            && self.event_filter.matches(event)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CallbackDeliveryStatus {
    Pending,
    Delivering,
    Delivered,
    Acknowledged,
    Failed,
    DeadLettered,
    Cancelled,
    Expired,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CallbackDelivery {
    pub delivery_id: String,
    pub subscription_id: String,
    pub event_id: String,
    pub run_id: String,
    pub sequence: u64,
    pub cursor: String,
    pub attempt: u32,
    pub idempotency_key: String,
    pub failure_policy: CallbackFailurePolicy,
    pub status: CallbackDeliveryStatus,
    pub next_retry_at_unix_ms: Option<u64>,
    pub delivered_at_unix_ms: Option<u64>,
    pub acknowledged_at_unix_ms: Option<u64>,
    pub last_error: Option<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CallbackRetryPolicy {
    pub max_attempts: u32,
    pub base_delay_ms: u64,
    pub max_delay_ms: u64,
}

impl CallbackRetryPolicy {
    pub fn new(max_attempts: u32, base_delay_ms: u64, max_delay_ms: u64) -> Self {
        Self {
            max_attempts: max_attempts.max(1),
            base_delay_ms,
            max_delay_ms,
        }
    }

    pub fn delay_for_attempt(self, attempt: u32) -> u64 {
        let exponent = attempt.saturating_sub(1).min(32);
        let multiplier = 1_u64.checked_shl(exponent).unwrap_or(u64::MAX);
        self.base_delay_ms
            .saturating_mul(multiplier)
            .min(self.max_delay_ms)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CallbackDeliveryResponse {
    Success,
    DuplicateAlreadyProcessed,
    TargetGone,
    RateLimited { retry_after_ms: Option<u64> },
    ServerError(u16),
    ClientError(u16),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CallbackDeliveryError {
    EmptyField { field: String },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CallbackDeliveryScheduler {
    retry_policy: CallbackRetryPolicy,
}

impl CallbackDeliveryScheduler {
    pub fn new(retry_policy: CallbackRetryPolicy) -> Self {
        Self { retry_policy }
    }

    pub fn schedule_event(
        &self,
        subscription: &CallbackSubscription,
        event: &ApplicationProtocolEvent,
    ) -> Option<CallbackDelivery> {
        if !subscription.can_receive(event) {
            return None;
        }

        Some(CallbackDelivery {
            delivery_id: format!(
                "del_{}_{}",
                subscription.subscription_id, event.metadata.event_id
            ),
            subscription_id: subscription.subscription_id.clone(),
            event_id: event.metadata.event_id.clone(),
            run_id: event.metadata.run_id.clone(),
            sequence: event.metadata.sequence,
            cursor: event
                .metadata
                .cursor
                .clone()
                .unwrap_or_else(|| event.metadata.sequence.to_string()),
            attempt: 1,
            idempotency_key: format!(
                "{}:{}",
                subscription.subscription_id, event.metadata.event_id
            ),
            failure_policy: subscription.failure_policy,
            status: CallbackDeliveryStatus::Pending,
            next_retry_at_unix_ms: None,
            delivered_at_unix_ms: None,
            acknowledged_at_unix_ms: None,
            last_error: None,
        })
    }

    pub fn record_response(
        &self,
        mut delivery: CallbackDelivery,
        response: CallbackDeliveryResponse,
        now_unix_ms: u64,
    ) -> CallbackDelivery {
        match response {
            CallbackDeliveryResponse::Success => {
                delivery.status = CallbackDeliveryStatus::Delivered;
                delivery.delivered_at_unix_ms = Some(now_unix_ms);
                delivery.next_retry_at_unix_ms = None;
                delivery.last_error = None;
            }
            CallbackDeliveryResponse::DuplicateAlreadyProcessed => {
                delivery.status = CallbackDeliveryStatus::Acknowledged;
                delivery.acknowledged_at_unix_ms = Some(now_unix_ms);
                delivery.next_retry_at_unix_ms = None;
                delivery.last_error = None;
            }
            CallbackDeliveryResponse::TargetGone => {
                delivery.status = CallbackDeliveryStatus::Cancelled;
                delivery.next_retry_at_unix_ms = None;
                delivery.last_error = Some("target_gone".to_owned());
            }
            CallbackDeliveryResponse::RateLimited { retry_after_ms } => {
                self.retry_or_finish(
                    &mut delivery,
                    now_unix_ms,
                    retry_after_ms,
                    "rate_limited".to_owned(),
                );
            }
            CallbackDeliveryResponse::ServerError(status) => {
                self.retry_or_finish(
                    &mut delivery,
                    now_unix_ms,
                    None,
                    format!("server_error:{status}"),
                );
            }
            CallbackDeliveryResponse::ClientError(status) => {
                delivery.status = CallbackDeliveryStatus::Failed;
                delivery.next_retry_at_unix_ms = None;
                delivery.last_error = Some(format!("client_error:{status}"));
            }
        }
        delivery
    }

    fn retry_or_finish(
        &self,
        delivery: &mut CallbackDelivery,
        now_unix_ms: u64,
        retry_after_ms: Option<u64>,
        error: String,
    ) {
        if delivery.failure_policy == CallbackFailurePolicy::BestEffort {
            delivery.status = CallbackDeliveryStatus::Failed;
            delivery.next_retry_at_unix_ms = None;
            delivery.last_error = Some(error);
            return;
        }

        if delivery.attempt >= self.retry_policy.max_attempts {
            delivery.status = CallbackDeliveryStatus::DeadLettered;
            delivery.next_retry_at_unix_ms = None;
            delivery.last_error = Some(error);
            return;
        }

        delivery.attempt += 1;
        delivery.status = CallbackDeliveryStatus::Pending;
        let delay_ms = retry_after_ms
            .unwrap_or_else(|| self.retry_policy.delay_for_attempt(delivery.attempt - 1));
        delivery.next_retry_at_unix_ms = Some(now_unix_ms.saturating_add(delay_ms));
        delivery.last_error = Some(error);
    }
}

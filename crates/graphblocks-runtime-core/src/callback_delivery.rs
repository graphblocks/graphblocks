use std::collections::{BTreeMap, BTreeSet};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use std::path::Path;
use std::sync::Mutex;

use crate::application_event::{
    ApplicationProtocolEvent, ApplicationProtocolEventKind, ApplicationProtocolLog,
};
use hmac::{Hmac, Mac};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EventFilter {
    pub types: Option<BTreeSet<ApplicationProtocolEventKind>>,
    pub visibility: Option<BTreeSet<String>>,
    pub node_ids: Option<BTreeSet<String>>,
    pub operation_ids: Option<BTreeSet<String>>,
    pub severity_min: Option<String>,
    pub include_terminal_events: bool,
}

impl EventFilter {
    pub fn new() -> Self {
        Self {
            types: None,
            visibility: None,
            node_ids: None,
            operation_ids: None,
            severity_min: None,
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

    pub fn with_visibility(
        mut self,
        visibility: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        self.visibility = Some(visibility.into_iter().map(Into::into).collect());
        self
    }

    pub fn with_node_ids(mut self, node_ids: impl IntoIterator<Item = impl Into<String>>) -> Self {
        self.node_ids = Some(node_ids.into_iter().map(Into::into).collect());
        self
    }

    pub fn with_operation_ids(
        mut self,
        operation_ids: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        self.operation_ids = Some(operation_ids.into_iter().map(Into::into).collect());
        self
    }

    pub fn with_severity_min(
        mut self,
        severity_min: impl Into<String>,
    ) -> Result<Self, CallbackDeliveryError> {
        let severity_min = severity_min.into();
        if severity_rank(&severity_min).is_none() {
            return Err(CallbackDeliveryError::EmptyField {
                field: "severity_min".to_owned(),
            });
        }
        self.severity_min = Some(severity_min);
        Ok(self)
    }

    pub fn with_terminal_events(mut self, include_terminal_events: bool) -> Self {
        self.include_terminal_events = include_terminal_events;
        self
    }

    pub fn matches(&self, event: &ApplicationProtocolEvent) -> bool {
        if !self.payload_field_matches(event, "visibility", &self.visibility)
            || !self.payload_field_matches(event, "node_id", &self.node_ids)
            || !self.payload_field_matches(event, "operation_id", &self.operation_ids)
        {
            return false;
        }

        if let Some(severity_min) = &self.severity_min {
            let Some(event_severity) = event.payload.get("severity").and_then(Value::as_str) else {
                return false;
            };
            let Some(event_rank) = severity_rank(event_severity) else {
                return false;
            };
            let Some(min_rank) = severity_rank(severity_min) else {
                return false;
            };
            if event_rank < min_rank {
                return false;
            }
        }

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

    fn payload_field_matches(
        &self,
        event: &ApplicationProtocolEvent,
        field: &str,
        allowed: &Option<BTreeSet<String>>,
    ) -> bool {
        allowed.as_ref().is_none_or(|allowed| {
            event
                .payload
                .get(field)
                .and_then(Value::as_str)
                .is_some_and(|value| allowed.contains(value))
        })
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
pub enum CallbackDeliveryTarget {
    Webhook {
        url: String,
    },
    WebSocket {
        connection_id: String,
        require_ack: bool,
    },
    Sse {
        connection_id: String,
        require_ack: bool,
    },
    PushNotification {
        channel: String,
    },
    Email {
        address: String,
    },
    LocalCallback {
        callback_name: String,
        process_bound: bool,
    },
}

impl CallbackDeliveryTarget {
    pub fn webhook(url: impl Into<String>) -> Self {
        Self::Webhook { url: url.into() }
    }

    pub fn websocket(
        connection_id: impl Into<String>,
        require_ack: bool,
    ) -> Result<Self, CallbackDeliveryError> {
        let connection_id = non_empty_target_field(connection_id.into(), "connection_id")?;
        Ok(Self::WebSocket {
            connection_id,
            require_ack,
        })
    }

    pub fn sse(
        connection_id: impl Into<String>,
        require_ack: bool,
    ) -> Result<Self, CallbackDeliveryError> {
        let connection_id = non_empty_target_field(connection_id.into(), "connection_id")?;
        Ok(Self::Sse {
            connection_id,
            require_ack,
        })
    }

    pub fn push_notification(channel: impl Into<String>) -> Result<Self, CallbackDeliveryError> {
        let channel = non_empty_target_field(channel.into(), "channel")?;
        Ok(Self::PushNotification { channel })
    }

    pub fn email(address: impl Into<String>) -> Result<Self, CallbackDeliveryError> {
        let address = non_empty_target_field(address.into(), "address")?;
        Ok(Self::Email { address })
    }

    pub fn local_callback(
        callback_name: impl Into<String>,
        process_bound: bool,
    ) -> Result<Self, CallbackDeliveryError> {
        let callback_name = non_empty_target_field(callback_name.into(), "callback_name")?;
        Ok(Self::LocalCallback {
            callback_name,
            process_bound,
        })
    }

    pub fn parse_address(address: impl Into<String>) -> Result<Self, CallbackDeliveryError> {
        let address = address.into();
        let (kind, value) =
            address
                .split_once(':')
                .ok_or_else(|| CallbackDeliveryError::EmptyField {
                    field: "delivery_target".to_owned(),
                })?;
        match kind {
            "webhook" => Ok(Self::webhook(non_empty_target_field(
                value.to_owned(),
                "url",
            )?)),
            "websocket" => Self::websocket(value, false),
            "sse" => Self::sse(value, false),
            "push" | "push_notification" => Self::push_notification(value),
            "email" => Self::email(value),
            "local_callback" => Self::local_callback(value, true),
            _ => Ok(Self::LocalCallback {
                callback_name: non_empty_target_field(address, "delivery_target")?,
                process_bound: true,
            }),
        }
    }

    pub fn kind(&self) -> &'static str {
        match self {
            Self::Webhook { .. } => "webhook",
            Self::WebSocket { .. } => "websocket",
            Self::Sse { .. } => "sse",
            Self::PushNotification { .. } => "push_notification",
            Self::Email { .. } => "email",
            Self::LocalCallback { .. } => "local_callback",
        }
    }

    pub fn address(&self) -> String {
        match self {
            Self::Webhook { url } => format!("webhook:{url}"),
            Self::WebSocket { connection_id, .. } => format!("websocket:{connection_id}"),
            Self::Sse { connection_id, .. } => format!("sse:{connection_id}"),
            Self::PushNotification { channel } => format!("push_notification:{channel}"),
            Self::Email { address } => format!("email:{address}"),
            Self::LocalCallback { callback_name, .. } => format!("local_callback:{callback_name}"),
        }
    }

    pub fn supports_ordered_delivery(&self) -> bool {
        matches!(
            self,
            Self::Webhook { .. } | Self::WebSocket { .. } | Self::Sse { .. }
        )
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum CallbackAuthoritativeUse {
    RunCorrectness,
    Billing,
    Quota,
    Audit,
    EffectCommit,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CallbackSubscription {
    pub subscription_id: String,
    pub owner: String,
    pub scope: String,
    pub scope_id: String,
    pub event_filter: EventFilter,
    pub delivery_target: CallbackDeliveryTarget,
    pub status: CallbackSubscriptionStatus,
    pub created_at_unix_ms: u64,
    pub expires_at_unix_ms: Option<u64>,
    pub replay_from_cursor: Option<String>,
    pub failure_policy: CallbackFailurePolicy,
    pub ordered_delivery: bool,
    pub authoritative_uses: BTreeSet<CallbackAuthoritativeUse>,
    pub mandatory_delivery: bool,
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
        Self::new_with_target(
            subscription_id,
            owner,
            scope,
            scope_id,
            event_filter,
            CallbackDeliveryTarget::parse_address(delivery_target)?,
            failure_policy,
            created_at_unix_ms,
        )
    }

    pub fn new_with_target(
        subscription_id: impl Into<String>,
        owner: impl Into<String>,
        scope: impl Into<String>,
        scope_id: impl Into<String>,
        event_filter: EventFilter,
        delivery_target: CallbackDeliveryTarget,
        failure_policy: CallbackFailurePolicy,
        created_at_unix_ms: u64,
    ) -> Result<Self, CallbackDeliveryError> {
        let subscription = Self {
            subscription_id: subscription_id.into(),
            owner: owner.into(),
            scope: scope.into(),
            scope_id: scope_id.into(),
            event_filter,
            delivery_target,
            status: CallbackSubscriptionStatus::Active,
            created_at_unix_ms,
            expires_at_unix_ms: None,
            replay_from_cursor: None,
            failure_policy,
            ordered_delivery: false,
            authoritative_uses: BTreeSet::new(),
            mandatory_delivery: false,
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
        ] {
            if value.trim().is_empty() {
                return Err(CallbackDeliveryError::EmptyField {
                    field: field.to_owned(),
                });
            }
        }
        if !matches!(
            self.scope.as_str(),
            "run" | "conversation" | "project" | "tenant" | "deployment"
        ) {
            return Err(CallbackDeliveryError::EmptyField {
                field: "scope".to_owned(),
            });
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
        if self.event_filter.visibility.as_ref().is_some_and(|values| {
            values.iter().any(|value| {
                !matches!(
                    value.as_str(),
                    "client" | "operator" | "internal" | "audit_only"
                )
            })
        }) {
            return Err(CallbackDeliveryError::EmptyField {
                field: "event_filter.visibility".to_owned(),
            });
        }
        Ok(())
    }

    pub fn with_replay_from_cursor(
        mut self,
        cursor: impl Into<String>,
    ) -> Result<Self, CallbackDeliveryError> {
        let cursor = cursor.into();
        if cursor.trim().is_empty() {
            return Err(CallbackDeliveryError::EmptyField {
                field: "replay_from_cursor".to_owned(),
            });
        }
        self.replay_from_cursor = Some(cursor);
        Ok(self)
    }

    pub fn with_ordered_delivery(mut self) -> Self {
        self.ordered_delivery = true;
        self
    }

    pub fn with_mandatory_delivery(mut self) -> Self {
        self.mandatory_delivery = true;
        self
    }

    pub fn with_authoritative_use(mut self, authoritative_use: CallbackAuthoritativeUse) -> Self {
        self.authoritative_uses.insert(authoritative_use);
        self
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

impl CallbackDeliveryStatus {
    fn blocks_ordered_delivery(self) -> bool {
        matches!(self, Self::Pending | Self::Delivering)
    }
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
    pub redrive_count: u32,
    pub last_redrive_operator: Option<String>,
    pub last_redrive_reason: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CallbackDeadLetter {
    pub original_delivery_id: String,
    pub subscription_id: String,
    pub event_id: String,
    pub run_id: String,
    pub sequence: u64,
    pub cursor: String,
    pub idempotency_key: String,
    pub failure_policy: CallbackFailurePolicy,
    pub attempt_history: Vec<u32>,
    pub last_error: Option<String>,
    pub dead_lettered_at_unix_ms: u64,
    pub redrive_count: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CallbackDeliveryRunAction {
    PauseRun {
        run_id: String,
        subscription_id: String,
        delivery_id: String,
        reason: String,
    },
    FailRun {
        run_id: String,
        subscription_id: String,
        delivery_id: String,
        reason: String,
    },
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct OrderedDeliveryState {
    blocking_by_subscription_run: BTreeMap<(String, String), CallbackDelivery>,
}

impl OrderedDeliveryState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn is_blocked(&self, subscription_id: &str, run_id: &str) -> bool {
        self.blocking_by_subscription_run
            .get(&(subscription_id.to_owned(), run_id.to_owned()))
            .is_some_and(|delivery| delivery.status.blocks_ordered_delivery())
    }

    pub fn blocking_delivery(&self, subscription_id: &str, run_id: &str) -> Option<String> {
        self.blocking_by_subscription_run
            .get(&(subscription_id.to_owned(), run_id.to_owned()))
            .and_then(|delivery| {
                delivery
                    .status
                    .blocks_ordered_delivery()
                    .then(|| delivery.delivery_id.clone())
            })
    }

    pub fn record_scheduled(&mut self, delivery: &CallbackDelivery) {
        if delivery.status.blocks_ordered_delivery() {
            self.blocking_by_subscription_run.insert(
                (delivery.subscription_id.clone(), delivery.run_id.clone()),
                delivery.clone(),
            );
        }
    }

    pub fn record_delivery_status(&mut self, delivery: &CallbackDelivery) {
        let key = (delivery.subscription_id.clone(), delivery.run_id.clone());
        if delivery.status.blocks_ordered_delivery() {
            self.blocking_by_subscription_run
                .insert(key, delivery.clone());
        } else {
            self.blocking_by_subscription_run.remove(&key);
        }
    }
}

impl CallbackDeadLetter {
    pub fn from_delivery(
        delivery: CallbackDelivery,
        dead_lettered_at_unix_ms: u64,
    ) -> Result<Self, CallbackDeliveryError> {
        if delivery.status != CallbackDeliveryStatus::DeadLettered {
            return Err(CallbackDeliveryError::InvalidDeliveryStatus {
                delivery_id: delivery.delivery_id,
                status: delivery.status,
            });
        }
        Ok(Self {
            original_delivery_id: delivery.delivery_id,
            subscription_id: delivery.subscription_id,
            event_id: delivery.event_id,
            run_id: delivery.run_id,
            sequence: delivery.sequence,
            cursor: delivery.cursor,
            idempotency_key: delivery.idempotency_key,
            failure_policy: delivery.failure_policy,
            attempt_history: (1..=delivery.attempt).collect(),
            last_error: delivery.last_error,
            dead_lettered_at_unix_ms,
            redrive_count: delivery.redrive_count,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CallbackRetryPolicy {
    pub max_attempts: u32,
    pub base_delay_ms: u64,
    pub max_delay_ms: u64,
}

impl CallbackRetryPolicy {
    pub fn new(max_attempts: u32, base_delay_ms: u64, max_delay_ms: u64) -> Self {
        let base_delay_ms = base_delay_ms.max(1);
        let max_delay_ms = max_delay_ms.max(base_delay_ms);
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

    pub fn delay_for_attempt_with_jitter(self, attempt: u32, jitter_key: &str) -> u64 {
        let base_delay_ms = self.delay_for_attempt(attempt);
        let remaining_delay_ms = self.max_delay_ms.saturating_sub(base_delay_ms);
        if remaining_delay_ms == 0 {
            return base_delay_ms;
        }

        let jitter_window_ms = remaining_delay_ms.min(base_delay_ms).max(1);
        let mut hash = 14_695_981_039_346_656_037_u64;
        for byte in jitter_key
            .as_bytes()
            .iter()
            .copied()
            .chain(attempt.to_le_bytes())
        {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(1_099_511_628_211);
        }
        let jitter_ms = (hash % jitter_window_ms) + 1;
        base_delay_ms
            .saturating_add(jitter_ms)
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
    EmptyField {
        field: String,
    },
    InvalidDeliveryStatus {
        delivery_id: String,
        status: CallbackDeliveryStatus,
    },
    DeadLetterNotFound {
        original_delivery_id: String,
    },
    EventNotFound {
        event_id: String,
    },
    WebhookSigning {
        error: WebhookSignatureError,
    },
    Storage {
        message: String,
    },
}

#[derive(Debug)]
pub struct SqliteCallbackDeadLetterStore {
    connection: Mutex<Connection>,
}

#[derive(Debug)]
pub struct SqliteCallbackDeliveryQueue {
    connection: Mutex<Connection>,
}

impl SqliteCallbackDeliveryQueue {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, CallbackDeliveryError> {
        let connection = Connection::open(path).map_err(callback_storage_error)?;
        let queue = Self {
            connection: Mutex::new(connection),
        };
        queue.initialize()?;
        Ok(queue)
    }

    pub fn open_in_memory() -> Result<Self, CallbackDeliveryError> {
        let connection = Connection::open_in_memory().map_err(callback_storage_error)?;
        let queue = Self {
            connection: Mutex::new(connection),
        };
        queue.initialize()?;
        Ok(queue)
    }

    fn initialize(&self) -> Result<(), CallbackDeliveryError> {
        let connection = self
            .connection
            .lock()
            .expect("sqlite callback delivery queue lock poisoned");
        connection
            .execute_batch(
                "
                CREATE TABLE IF NOT EXISTS callback_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    next_retry_at_unix_ms INTEGER,
                    sequence INTEGER NOT NULL,
                    delivery_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS callback_deliveries_due_idx
                ON callback_deliveries(status, next_retry_at_unix_ms, sequence, delivery_id);
                ",
            )
            .map_err(callback_storage_error)?;
        Ok(())
    }

    pub fn upsert_delivery(&self, delivery: CallbackDelivery) -> Result<(), CallbackDeliveryError> {
        let connection = self
            .connection
            .lock()
            .expect("sqlite callback delivery queue lock poisoned");
        connection
            .execute(
                "
                INSERT INTO callback_deliveries (
                    delivery_id,
                    status,
                    next_retry_at_unix_ms,
                    sequence,
                    delivery_json
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(delivery_id) DO UPDATE SET
                    status = excluded.status,
                    next_retry_at_unix_ms = excluded.next_retry_at_unix_ms,
                    sequence = excluded.sequence,
                    delivery_json = excluded.delivery_json
                ",
                params![
                    &delivery.delivery_id,
                    callback_delivery_status_as_str(delivery.status),
                    optional_callback_u64_to_i64(
                        delivery.next_retry_at_unix_ms,
                        "callback delivery next retry",
                    )?,
                    callback_u64_to_i64(delivery.sequence, "callback delivery sequence")?,
                    callback_storage_json(&delivery_to_value(&delivery))?,
                ],
            )
            .map_err(callback_storage_error)?;
        Ok(())
    }

    pub fn get_delivery(
        &self,
        delivery_id: &str,
    ) -> Result<Option<CallbackDelivery>, CallbackDeliveryError> {
        let connection = self
            .connection
            .lock()
            .expect("sqlite callback delivery queue lock poisoned");
        let mut statement = connection
            .prepare(
                "
                SELECT delivery_json
                FROM callback_deliveries
                WHERE delivery_id = ?
                ",
            )
            .map_err(callback_storage_error)?;
        let mut rows = statement
            .query(params![delivery_id])
            .map_err(callback_storage_error)?;
        let Some(row) = rows.next().map_err(callback_storage_error)? else {
            return Ok(None);
        };
        let delivery_json = row.get::<_, String>(0).map_err(callback_storage_error)?;
        delivery_from_value(callback_parse_json(&delivery_json)?).map(Some)
    }

    pub fn due_deliveries(
        &self,
        now_unix_ms: u64,
        limit: usize,
    ) -> Result<Vec<CallbackDelivery>, CallbackDeliveryError> {
        if limit == 0 {
            return Ok(Vec::new());
        }

        let connection = self
            .connection
            .lock()
            .expect("sqlite callback delivery queue lock poisoned");
        let mut statement = connection
            .prepare(
                "
                SELECT delivery_json
                FROM callback_deliveries
                WHERE status = ?
                  AND (next_retry_at_unix_ms IS NULL OR next_retry_at_unix_ms <= ?)
                ORDER BY sequence, delivery_id
                LIMIT ?
                ",
            )
            .map_err(callback_storage_error)?;
        let rows = statement
            .query_map(
                params![
                    callback_delivery_status_as_str(CallbackDeliveryStatus::Pending),
                    callback_u64_to_i64(now_unix_ms, "callback delivery due time")?,
                    callback_usize_to_i64(limit, "callback delivery due limit")?,
                ],
                |row| row.get::<_, String>(0),
            )
            .map_err(callback_storage_error)?;
        rows.map(|row| {
            delivery_from_value(callback_parse_json(&row.map_err(callback_storage_error)?)?)
        })
        .collect()
    }

    pub fn recover_in_flight_deliveries(
        &self,
        now_unix_ms: u64,
    ) -> Result<usize, CallbackDeliveryError> {
        let connection = self
            .connection
            .lock()
            .expect("sqlite callback delivery queue lock poisoned");
        let mut deliveries = Vec::new();
        {
            let mut statement = connection
                .prepare(
                    "
                    SELECT delivery_json
                    FROM callback_deliveries
                    WHERE status = ?
                    ORDER BY sequence, delivery_id
                    ",
                )
                .map_err(callback_storage_error)?;
            let rows = statement
                .query_map(
                    params![callback_delivery_status_as_str(
                        CallbackDeliveryStatus::Delivering
                    )],
                    |row| row.get::<_, String>(0),
                )
                .map_err(callback_storage_error)?;
            for row in rows {
                let mut delivery = delivery_from_value(callback_parse_json(
                    &row.map_err(callback_storage_error)?,
                )?)?;
                delivery.status = CallbackDeliveryStatus::Pending;
                delivery.next_retry_at_unix_ms = Some(now_unix_ms);
                delivery.last_error = Some("delivery_recovered_after_worker_restart".to_owned());
                deliveries.push(delivery);
            }
        }

        for delivery in &deliveries {
            connection
                .execute(
                    "
                    UPDATE callback_deliveries
                    SET status = ?,
                        next_retry_at_unix_ms = ?,
                        sequence = ?,
                        delivery_json = ?
                    WHERE delivery_id = ?
                    ",
                    params![
                        callback_delivery_status_as_str(delivery.status),
                        optional_callback_u64_to_i64(
                            delivery.next_retry_at_unix_ms,
                            "callback delivery next retry",
                        )?,
                        callback_u64_to_i64(delivery.sequence, "callback delivery sequence")?,
                        callback_storage_json(&delivery_to_value(delivery))?,
                        &delivery.delivery_id,
                    ],
                )
                .map_err(callback_storage_error)?;
        }

        Ok(deliveries.len())
    }

    pub fn cancel_pending_for_subscription(
        &self,
        subscription_id: &str,
        reason: impl Into<String>,
    ) -> Result<usize, CallbackDeliveryError> {
        if subscription_id.trim().is_empty() {
            return Err(CallbackDeliveryError::EmptyField {
                field: "subscription_id".to_owned(),
            });
        }
        let reason = reason.into();
        if reason.trim().is_empty() {
            return Err(CallbackDeliveryError::EmptyField {
                field: "reason".to_owned(),
            });
        }

        let connection = self
            .connection
            .lock()
            .expect("sqlite callback delivery queue lock poisoned");
        let mut deliveries = Vec::new();
        {
            let mut statement = connection
                .prepare(
                    "
                    SELECT delivery_json
                    FROM callback_deliveries
                    WHERE status = ?
                    ORDER BY sequence, delivery_id
                    ",
                )
                .map_err(callback_storage_error)?;
            let rows = statement
                .query_map(
                    params![callback_delivery_status_as_str(
                        CallbackDeliveryStatus::Pending
                    )],
                    |row| row.get::<_, String>(0),
                )
                .map_err(callback_storage_error)?;
            for row in rows {
                let mut delivery = delivery_from_value(callback_parse_json(
                    &row.map_err(callback_storage_error)?,
                )?)?;
                if delivery.subscription_id == subscription_id {
                    delivery.status = CallbackDeliveryStatus::Cancelled;
                    delivery.next_retry_at_unix_ms = None;
                    delivery.last_error = Some(reason.clone());
                    deliveries.push(delivery);
                }
            }
        }

        for delivery in &deliveries {
            connection
                .execute(
                    "
                    UPDATE callback_deliveries
                    SET status = ?,
                        next_retry_at_unix_ms = ?,
                        sequence = ?,
                        delivery_json = ?
                    WHERE delivery_id = ?
                    ",
                    params![
                        callback_delivery_status_as_str(delivery.status),
                        optional_callback_u64_to_i64(
                            delivery.next_retry_at_unix_ms,
                            "callback delivery next retry",
                        )?,
                        callback_u64_to_i64(delivery.sequence, "callback delivery sequence")?,
                        callback_storage_json(&delivery_to_value(delivery))?,
                        &delivery.delivery_id,
                    ],
                )
                .map_err(callback_storage_error)?;
        }

        Ok(deliveries.len())
    }
}

impl SqliteCallbackDeadLetterStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, CallbackDeliveryError> {
        let connection = Connection::open(path).map_err(callback_storage_error)?;
        let store = Self {
            connection: Mutex::new(connection),
        };
        store.initialize()?;
        Ok(store)
    }

    pub fn open_in_memory() -> Result<Self, CallbackDeliveryError> {
        let connection = Connection::open_in_memory().map_err(callback_storage_error)?;
        let store = Self {
            connection: Mutex::new(connection),
        };
        store.initialize()?;
        Ok(store)
    }

    fn initialize(&self) -> Result<(), CallbackDeliveryError> {
        let connection = self
            .connection
            .lock()
            .expect("sqlite callback dead-letter store lock poisoned");
        connection
            .execute_batch(
                "
                CREATE TABLE IF NOT EXISTS callback_dead_letters (
                    original_delivery_id TEXT PRIMARY KEY,
                    dead_letter_json TEXT NOT NULL
                );
                ",
            )
            .map_err(callback_storage_error)?;
        Ok(())
    }

    pub fn insert_dead_letter(
        &self,
        dead_letter: CallbackDeadLetter,
    ) -> Result<(), CallbackDeliveryError> {
        let connection = self
            .connection
            .lock()
            .expect("sqlite callback dead-letter store lock poisoned");
        connection
            .execute(
                "
                INSERT OR REPLACE INTO callback_dead_letters (
                    original_delivery_id,
                    dead_letter_json
                )
                VALUES (?, ?)
                ",
                params![
                    &dead_letter.original_delivery_id,
                    callback_storage_json(&dead_letter_to_value(&dead_letter))?,
                ],
            )
            .map_err(callback_storage_error)?;
        Ok(())
    }

    pub fn get_dead_letter(
        &self,
        original_delivery_id: &str,
    ) -> Result<Option<CallbackDeadLetter>, CallbackDeliveryError> {
        let connection = self
            .connection
            .lock()
            .expect("sqlite callback dead-letter store lock poisoned");
        let mut statement = connection
            .prepare(
                "
                SELECT dead_letter_json
                FROM callback_dead_letters
                WHERE original_delivery_id = ?
                ",
            )
            .map_err(callback_storage_error)?;
        let mut rows = statement
            .query(params![original_delivery_id])
            .map_err(callback_storage_error)?;
        let Some(row) = rows.next().map_err(callback_storage_error)? else {
            return Ok(None);
        };
        let dead_letter_json = row.get::<_, String>(0).map_err(callback_storage_error)?;
        dead_letter_from_value(callback_parse_json(&dead_letter_json)?).map(Some)
    }

    pub fn redrive_dead_letter(
        &self,
        scheduler: &CallbackDeliveryScheduler,
        original_delivery_id: &str,
        operator: impl Into<String>,
        reason: impl Into<String>,
        redriven_at_unix_ms: u64,
    ) -> Result<CallbackDelivery, CallbackDeliveryError> {
        let mut dead_letter = self.get_dead_letter(original_delivery_id)?.ok_or_else(|| {
            CallbackDeliveryError::DeadLetterNotFound {
                original_delivery_id: original_delivery_id.to_owned(),
            }
        })?;
        let redriven =
            scheduler.redrive_dead_letter(&dead_letter, operator, reason, redriven_at_unix_ms)?;
        dead_letter.redrive_count = redriven.redrive_count;
        dead_letter.attempt_history.push(redriven.attempt);
        self.insert_dead_letter(dead_letter)?;
        Ok(redriven)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WebhookEgressPolicy {
    allowed_hosts: BTreeSet<String>,
}

impl WebhookEgressPolicy {
    pub fn default_deny_internal() -> Self {
        Self {
            allowed_hosts: BTreeSet::new(),
        }
    }

    pub fn with_allowed_host(mut self, host: impl Into<String>) -> Self {
        self.allowed_hosts.insert(normalize_host(&host.into()));
        self
    }

    pub fn validate_resolved_addresses(
        &self,
        target: &WebhookDeliveryTarget,
        addresses: impl IntoIterator<Item = IpAddr>,
    ) -> Result<(), WebhookEndpointError> {
        if self.host_allowed(&target.host) {
            return Ok(());
        }

        for address in addresses {
            let unsafe_address = match address {
                IpAddr::V4(address) => is_forbidden_ipv4(address),
                IpAddr::V6(address) => is_forbidden_ipv6(address),
            };
            if unsafe_address {
                return Err(WebhookEndpointError::UnsafeResolvedAddress {
                    host: target.host.clone(),
                    address,
                });
            }
        }

        Ok(())
    }

    fn host_allowed(&self, host: &str) -> bool {
        self.allowed_hosts.contains(&normalize_host(host))
    }
}

impl Default for WebhookEgressPolicy {
    fn default() -> Self {
        Self::default_deny_internal()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WebhookDeliveryTarget {
    pub url: String,
    pub scheme: String,
    pub host: String,
    pub max_payload_bytes: usize,
}

impl WebhookDeliveryTarget {
    pub const DEFAULT_MAX_PAYLOAD_BYTES: usize = 262_144;

    pub fn new(
        url: impl Into<String>,
        policy: &WebhookEgressPolicy,
    ) -> Result<Self, WebhookEndpointError> {
        let url = url.into();
        if url.trim().is_empty() {
            return Err(WebhookEndpointError::EmptyUrl);
        }
        if url.trim() != url {
            return Err(WebhookEndpointError::MalformedUrl);
        }

        let (scheme, rest) = url
            .split_once("://")
            .ok_or(WebhookEndpointError::MalformedUrl)?;
        if !matches!(scheme, "http" | "https") {
            return Err(WebhookEndpointError::UnsupportedScheme {
                scheme: scheme.to_owned(),
            });
        }
        let scheme = scheme.to_owned();
        let authority = rest
            .split(&['/', '?', '#'][..])
            .next()
            .ok_or(WebhookEndpointError::MissingHost)?;
        if authority.is_empty() {
            return Err(WebhookEndpointError::MissingHost);
        }
        let host = parse_authority_host(authority)?;
        if host.is_empty() {
            return Err(WebhookEndpointError::MissingHost);
        }
        if !policy.host_allowed(&host) && is_forbidden_webhook_host(&host) {
            return Err(WebhookEndpointError::UnsafeEndpoint { host });
        }

        Ok(Self {
            url,
            scheme,
            host,
            max_payload_bytes: Self::DEFAULT_MAX_PAYLOAD_BYTES,
        })
    }

    pub fn with_max_payload_bytes(
        mut self,
        max_payload_bytes: usize,
    ) -> Result<Self, WebhookEndpointError> {
        if max_payload_bytes == 0 {
            return Err(WebhookEndpointError::InvalidPayloadLimit { max_payload_bytes });
        }
        self.max_payload_bytes = max_payload_bytes;
        Ok(self)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WebhookEndpointError {
    EmptyUrl,
    MalformedUrl,
    MissingHost,
    UserInfoUnsupported { host: String },
    UnsupportedScheme { scheme: String },
    UnsafeEndpoint { host: String },
    UnsafeResolvedAddress { host: String, address: IpAddr },
    InvalidPayloadLimit { max_payload_bytes: usize },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CallbackConfigurationDiagnostic {
    pub code: &'static str,
    pub field: &'static str,
    pub message: String,
}

impl CallbackConfigurationDiagnostic {
    pub fn subscription(subscription: &CallbackSubscription) -> Vec<Self> {
        let mut diagnostics = Vec::new();
        if subscription.ordered_delivery
            && !subscription.delivery_target.supports_ordered_delivery()
        {
            diagnostics.push(Self {
                code: "GB6012",
                field: "delivery.ordering",
                message: format!(
                    "callback subscription {} requests ordered delivery for target {} that cannot guarantee ordering",
                    subscription.subscription_id,
                    subscription.delivery_target.address()
                ),
            });
        }
        if subscription.mandatory_delivery
            && subscription.failure_policy == CallbackFailurePolicy::BestEffort
        {
            diagnostics.push(Self {
                code: "GB6006",
                field: "failure_policy",
                message: format!(
                    "mandatory callback subscription {} has no retry, dead-letter, or fallback policy",
                    subscription.subscription_id
                ),
            });
        }
        if matches!(
            subscription.failure_policy,
            CallbackFailurePolicy::PauseRunOnFailure | CallbackFailurePolicy::FailRunOnFailure
        ) {
            diagnostics.push(Self {
                code: "GB6014",
                field: "failure_policy",
                message: format!(
                    "callback subscription {} uses mandatory failure policy without dead-letter behavior",
                    subscription.subscription_id
                ),
            });
        }
        if !subscription.authoritative_uses.is_empty() {
            let uses = subscription
                .authoritative_uses
                .iter()
                .map(|authoritative_use| match authoritative_use {
                    CallbackAuthoritativeUse::RunCorrectness => "run_correctness",
                    CallbackAuthoritativeUse::Billing => "billing",
                    CallbackAuthoritativeUse::Quota => "quota",
                    CallbackAuthoritativeUse::Audit => "audit",
                    CallbackAuthoritativeUse::EffectCommit => "effect_commit",
                })
                .collect::<Vec<_>>()
                .join(", ");
            diagnostics.push(Self {
                code: "GB6004",
                field: "authoritative_uses",
                message: format!(
                    "callback subscription {} uses callback delivery as a source of truth for {uses}",
                    subscription.subscription_id
                ),
            });
        }
        diagnostics
    }

    pub fn webhook_subscription(
        subscription: &CallbackSubscription,
        signing: Option<&WebhookSigningConfig>,
        endpoint_error: Option<&WebhookEndpointError>,
    ) -> Vec<Self> {
        let mut diagnostics = Vec::new();
        if signing.is_none() {
            diagnostics.push(Self {
                code: "GB6002",
                field: "delivery.signing",
                message: format!(
                    "callback subscription {} uses webhook delivery without signing",
                    subscription.subscription_id
                ),
            });
        }
        if let Some(diagnostic) = endpoint_error.and_then(|error| {
            Self::webhook_endpoint_error(&subscription.delivery_target.address(), error)
        }) {
            diagnostics.push(diagnostic);
        }
        diagnostics
    }

    pub fn webhook_endpoint_error(url: &str, error: &WebhookEndpointError) -> Option<Self> {
        match error {
            WebhookEndpointError::UnsafeEndpoint { host } => Some(Self {
                code: "GB6011",
                field: "delivery.url",
                message: format!("callback webhook target {url} reaches forbidden host {host}"),
            }),
            WebhookEndpointError::UnsafeResolvedAddress { host, address } => Some(Self {
                code: "GB6011",
                field: "delivery.url",
                message: format!(
                    "callback webhook target {url} resolves {host} to forbidden address {address}"
                ),
            }),
            WebhookEndpointError::UnsupportedScheme { scheme } => Some(Self {
                code: "GB6011",
                field: "delivery.url",
                message: format!("callback webhook target {url} uses forbidden scheme {scheme}"),
            }),
            WebhookEndpointError::UserInfoUnsupported { host } => Some(Self {
                code: "GB6011",
                field: "delivery.url",
                message: format!(
                    "callback webhook target {url} contains unsupported userinfo before host {host}"
                ),
            }),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WebhookSigningConfig {
    pub secret_ref: String,
    secret: Vec<u8>,
    pub timestamp_header: String,
    pub signature_header: String,
    pub algorithm_header: String,
    pub replay_window_ms: u64,
}

impl WebhookSigningConfig {
    pub fn hmac_sha256(
        secret_ref: impl Into<String>,
        secret: impl AsRef<[u8]>,
        replay_window_ms: u64,
    ) -> Result<Self, WebhookSignatureError> {
        let secret_ref = secret_ref.into();
        if secret_ref.trim().is_empty() {
            return Err(WebhookSignatureError::EmptyField {
                field: "secret_ref".to_owned(),
            });
        }
        let secret = secret.as_ref();
        if secret.is_empty() {
            return Err(WebhookSignatureError::EmptyField {
                field: "secret".to_owned(),
            });
        }
        Ok(Self {
            secret_ref,
            secret: secret.to_vec(),
            timestamp_header: "GraphBlocks-Timestamp".to_owned(),
            signature_header: "GraphBlocks-Signature".to_owned(),
            algorithm_header: "GraphBlocks-Signature-Algorithm".to_owned(),
            replay_window_ms,
        })
    }

    pub fn sign_delivery(
        &self,
        delivery: &CallbackDelivery,
        event: &ApplicationProtocolEvent,
        delivered_at_unix_ms: u64,
    ) -> Result<SignedWebhookDelivery, WebhookSignatureError> {
        self.build_signed_delivery(delivery, event, delivered_at_unix_ms, None)
    }

    pub fn sign_delivery_for_target(
        &self,
        target: &WebhookDeliveryTarget,
        delivery: &CallbackDelivery,
        event: &ApplicationProtocolEvent,
        delivered_at_unix_ms: u64,
    ) -> Result<SignedWebhookDelivery, WebhookSignatureError> {
        self.build_signed_delivery(
            delivery,
            event,
            delivered_at_unix_ms,
            Some(target.max_payload_bytes),
        )
    }

    fn build_signed_delivery(
        &self,
        delivery: &CallbackDelivery,
        event: &ApplicationProtocolEvent,
        delivered_at_unix_ms: u64,
        max_payload_bytes: Option<usize>,
    ) -> Result<SignedWebhookDelivery, WebhookSignatureError> {
        let body = json!({
            "delivery_id": &delivery.delivery_id,
            "subscription_id": &delivery.subscription_id,
            "event_id": &delivery.event_id,
            "run_id": &delivery.run_id,
            "sequence": delivery.sequence,
            "cursor": &delivery.cursor,
            "type": event.kind.as_str(),
            "payload": &event.payload,
            "idempotency_key": &delivery.idempotency_key,
            "occurred_at_unix_ms": event.metadata.occurred_at_unix_ms,
            "delivered_at_unix_ms": delivered_at_unix_ms,
            "protocol_version": &event.metadata.protocol_version,
        });
        let body_size_bytes = canonical_body_size_bytes(&body);
        if let Some(max_payload_bytes) = max_payload_bytes {
            if body_size_bytes > max_payload_bytes {
                return Err(WebhookSignatureError::PayloadTooLarge {
                    max_payload_bytes,
                    actual_payload_bytes: body_size_bytes,
                });
            }
        }
        let signature = self.compute_signature(delivered_at_unix_ms, &body)?;
        let mut headers = BTreeMap::new();
        headers.insert(
            "GraphBlocks-Delivery-Id".to_owned(),
            delivery.delivery_id.clone(),
        );
        headers.insert("GraphBlocks-Event-Id".to_owned(), delivery.event_id.clone());
        headers.insert("GraphBlocks-Run-Id".to_owned(), delivery.run_id.clone());
        headers.insert("GraphBlocks-Cursor".to_owned(), delivery.cursor.clone());
        headers.insert(
            "GraphBlocks-Idempotency-Key".to_owned(),
            delivery.idempotency_key.clone(),
        );
        headers.insert(
            self.timestamp_header.clone(),
            delivered_at_unix_ms.to_string(),
        );
        headers.insert(self.signature_header.clone(), signature);
        headers.insert(self.algorithm_header.clone(), "hmac-sha256".to_owned());

        Ok(SignedWebhookDelivery { body, headers })
    }

    pub fn verify_signed_delivery(
        &self,
        signed: &SignedWebhookDelivery,
        now_unix_ms: u64,
    ) -> Result<(), WebhookSignatureError> {
        let timestamp = signed
            .headers
            .get(&self.timestamp_header)
            .ok_or_else(|| WebhookSignatureError::MissingHeader {
                header: self.timestamp_header.clone(),
            })?
            .parse::<u64>()
            .map_err(|_| WebhookSignatureError::InvalidTimestamp)?;
        if now_unix_ms.abs_diff(timestamp) > self.replay_window_ms {
            return Err(WebhookSignatureError::TimestampOutsideReplayWindow {
                timestamp_unix_ms: timestamp,
                now_unix_ms,
                replay_window_ms: self.replay_window_ms,
            });
        }
        let algorithm = signed.headers.get(&self.algorithm_header).ok_or_else(|| {
            WebhookSignatureError::MissingHeader {
                header: self.algorithm_header.clone(),
            }
        })?;
        if algorithm != "hmac-sha256" {
            return Err(WebhookSignatureError::UnsupportedAlgorithm {
                algorithm: algorithm.clone(),
            });
        }
        for (header, field) in [
            ("GraphBlocks-Delivery-Id", "delivery_id"),
            ("GraphBlocks-Event-Id", "event_id"),
            ("GraphBlocks-Run-Id", "run_id"),
            ("GraphBlocks-Cursor", "cursor"),
            ("GraphBlocks-Idempotency-Key", "idempotency_key"),
        ] {
            let header_value =
                signed
                    .headers
                    .get(header)
                    .ok_or_else(|| WebhookSignatureError::MissingHeader {
                        header: header.to_owned(),
                    })?;
            let body_value = signed
                .body
                .get(field)
                .and_then(Value::as_str)
                .ok_or_else(|| WebhookSignatureError::MissingBodyField {
                    field: field.to_owned(),
                })?;
            if header_value != body_value {
                return Err(WebhookSignatureError::HeaderBodyMismatch {
                    header: header.to_owned(),
                    field: field.to_owned(),
                });
            }
        }
        let expected = self.compute_signature(timestamp, &signed.body)?;
        let signature = signed.headers.get(&self.signature_header).ok_or_else(|| {
            WebhookSignatureError::MissingHeader {
                header: self.signature_header.clone(),
            }
        })?;
        if !constant_time_eq(signature.as_bytes(), expected.as_bytes()) {
            return Err(WebhookSignatureError::SignatureMismatch);
        }
        Ok(())
    }

    fn compute_signature(
        &self,
        timestamp_unix_ms: u64,
        body: &Value,
    ) -> Result<String, WebhookSignatureError> {
        let body = graphblocks_compiler::canonical::canonical_json(body);
        let mut mac = HmacSha256::new_from_slice(&self.secret)
            .map_err(|_| WebhookSignatureError::InvalidSecret)?;
        mac.update(timestamp_unix_ms.to_string().as_bytes());
        mac.update(b".");
        mac.update(body.as_bytes());
        Ok(hex_encode(&mac.finalize().into_bytes()))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct WebhookDeliveryAttempt {
    pub target: WebhookDeliveryTarget,
    pub delivery: CallbackDelivery,
    pub signed: SignedWebhookDelivery,
}

#[derive(Clone, Debug, PartialEq)]
pub struct WebhookHttpRequest {
    pub url: String,
    pub method: String,
    pub headers: BTreeMap<String, String>,
    pub body: Value,
}

impl WebhookHttpRequest {
    pub fn canonical_body(&self) -> String {
        graphblocks_compiler::canonical::canonical_json(&self.body)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct WebhookHttpResponse {
    pub status: u16,
    pub retry_after_ms: Option<u64>,
}

impl WebhookHttpResponse {
    pub fn new(status: u16) -> Self {
        Self {
            status,
            retry_after_ms: None,
        }
    }

    pub fn with_retry_after_ms(mut self, retry_after_ms: u64) -> Self {
        self.retry_after_ms = Some(retry_after_ms);
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WebhookHttpTransport {
    egress_policy: WebhookEgressPolicy,
}

impl WebhookHttpTransport {
    pub fn new(egress_policy: WebhookEgressPolicy) -> Self {
        Self { egress_policy }
    }

    pub fn deliver_with<R, S, ResolveError, SendError>(
        &self,
        attempt: &WebhookDeliveryAttempt,
        mut resolve: R,
        mut send: S,
    ) -> CallbackDeliveryResponse
    where
        R: FnMut(&WebhookDeliveryTarget) -> Result<Vec<IpAddr>, ResolveError>,
        S: FnMut(WebhookHttpRequest) -> Result<WebhookHttpResponse, SendError>,
    {
        let addresses = match resolve(&attempt.target) {
            Ok(addresses) => addresses,
            Err(_) => return CallbackDeliveryResponse::ServerError(599),
        };
        if addresses.is_empty() {
            return CallbackDeliveryResponse::ServerError(599);
        }
        if self
            .egress_policy
            .validate_resolved_addresses(&attempt.target, addresses)
            .is_err()
        {
            return CallbackDeliveryResponse::ClientError(403);
        }

        let request = WebhookHttpRequest {
            url: attempt.target.url.clone(),
            method: "POST".to_owned(),
            headers: attempt.signed.headers.clone(),
            body: attempt.signed.body.clone(),
        };
        match send(request) {
            Ok(response) => webhook_http_response_to_delivery_response(response),
            Err(_) => CallbackDeliveryResponse::ServerError(599),
        }
    }
}

pub struct WebhookDeliveryWorker<'a> {
    scheduler: &'a CallbackDeliveryScheduler,
    queue: &'a SqliteCallbackDeliveryQueue,
    target: &'a WebhookDeliveryTarget,
    signing: &'a WebhookSigningConfig,
}

impl<'a> WebhookDeliveryWorker<'a> {
    pub fn new(
        scheduler: &'a CallbackDeliveryScheduler,
        queue: &'a SqliteCallbackDeliveryQueue,
        target: &'a WebhookDeliveryTarget,
        signing: &'a WebhookSigningConfig,
    ) -> Self {
        Self {
            scheduler,
            queue,
            target,
            signing,
        }
    }

    pub fn process_due<T, E>(
        &self,
        now_unix_ms: u64,
        limit: usize,
        mut transport: T,
        mut event_lookup: E,
    ) -> Result<usize, CallbackDeliveryError>
    where
        T: FnMut(&WebhookDeliveryAttempt) -> CallbackDeliveryResponse,
        E: FnMut(&str) -> Option<ApplicationProtocolEvent>,
    {
        let due = self.queue.due_deliveries(now_unix_ms, limit)?;
        let mut attempts = 0;
        for delivery in due {
            let event = event_lookup(&delivery.event_id).ok_or_else(|| {
                CallbackDeliveryError::EventNotFound {
                    event_id: delivery.event_id.clone(),
                }
            })?;
            let signed = match self.signing.sign_delivery_for_target(
                self.target,
                &delivery,
                &event,
                now_unix_ms,
            ) {
                Ok(signed) => signed,
                Err(WebhookSignatureError::PayloadTooLarge {
                    max_payload_bytes,
                    actual_payload_bytes,
                }) => {
                    let mut updated = delivery;
                    updated.status = CallbackDeliveryStatus::Failed;
                    updated.next_retry_at_unix_ms = None;
                    updated.last_error = Some(format!(
                        "payload_too_large:{actual_payload_bytes}>{max_payload_bytes}"
                    ));
                    self.queue.upsert_delivery(updated)?;
                    attempts += 1;
                    continue;
                }
                Err(error) => return Err(CallbackDeliveryError::WebhookSigning { error }),
            };
            let mut in_flight = delivery;
            in_flight.status = CallbackDeliveryStatus::Delivering;
            in_flight.next_retry_at_unix_ms = None;
            self.queue.upsert_delivery(in_flight.clone())?;
            let attempt = WebhookDeliveryAttempt {
                target: self.target.clone(),
                delivery: in_flight.clone(),
                signed,
            };
            let response = transport(&attempt);
            let updated = self
                .scheduler
                .record_response(in_flight, response, now_unix_ms);
            self.queue.upsert_delivery(updated)?;
            attempts += 1;
        }
        Ok(attempts)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SignedWebhookDelivery {
    pub body: Value,
    pub headers: BTreeMap<String, String>,
}

impl SignedWebhookDelivery {
    pub fn body_size_bytes(&self) -> usize {
        canonical_body_size_bytes(&self.body)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WebhookSignatureError {
    EmptyField {
        field: String,
    },
    MissingHeader {
        header: String,
    },
    MissingBodyField {
        field: String,
    },
    HeaderBodyMismatch {
        header: String,
        field: String,
    },
    InvalidTimestamp,
    TimestampOutsideReplayWindow {
        timestamp_unix_ms: u64,
        now_unix_ms: u64,
        replay_window_ms: u64,
    },
    UnsupportedAlgorithm {
        algorithm: String,
    },
    SignatureMismatch,
    PayloadTooLarge {
        max_payload_bytes: usize,
        actual_payload_bytes: usize,
    },
    InvalidBody,
    InvalidSecret,
}

fn canonical_body_size_bytes(body: &Value) -> usize {
    graphblocks_compiler::canonical::canonical_json(body).len()
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut diff = 0_u8;
    for index in 0..left.len() {
        diff |= left[index] ^ right[index];
    }
    diff == 0
}

fn parse_authority_host(authority: &str) -> Result<String, WebhookEndpointError> {
    if let Some((_userinfo, host_port)) = authority.rsplit_once('@') {
        let host = if host_port.is_empty() {
            String::new()
        } else if let Some(rest) = host_port.strip_prefix('[') {
            rest.split_once(']')
                .map(|(host, _suffix)| normalize_host(host))
                .unwrap_or_else(|| host_port.to_owned())
        } else {
            host_port
                .split_once(':')
                .map_or(host_port, |(host, _port)| host)
                .to_owned()
        };
        return Err(WebhookEndpointError::UserInfoUnsupported { host });
    }
    if let Some(rest) = authority.strip_prefix('[') {
        let (host, suffix) = rest
            .split_once(']')
            .ok_or(WebhookEndpointError::MalformedUrl)?;
        if !suffix.is_empty() && !suffix.starts_with(':') {
            return Err(WebhookEndpointError::MalformedUrl);
        }
        return Ok(normalize_host(host));
    }

    let host = authority
        .split_once(':')
        .map_or(authority, |(host, _port)| host);
    if host.is_empty() || host.contains(':') {
        return Err(WebhookEndpointError::MalformedUrl);
    }
    Ok(normalize_host(host))
}

fn normalize_host(host: &str) -> String {
    host.trim_end_matches('.').to_ascii_lowercase()
}

fn non_empty_target_field(value: String, field: &str) -> Result<String, CallbackDeliveryError> {
    if value.trim().is_empty() {
        return Err(CallbackDeliveryError::EmptyField {
            field: field.to_owned(),
        });
    }
    Ok(value)
}

fn severity_rank(severity: &str) -> Option<u8> {
    match severity {
        "debug" => Some(10),
        "info" => Some(20),
        "notice" => Some(30),
        "warning" | "warn" => Some(40),
        "error" => Some(50),
        "critical" | "fatal" => Some(60),
        _ => None,
    }
}

fn is_forbidden_webhook_host(host: &str) -> bool {
    let host = normalize_host(host);
    if matches!(host.as_str(), "localhost" | "localhost.localdomain")
        || host.ends_with(".localhost")
    {
        return true;
    }

    if let Ok(address) = host.parse::<IpAddr>() {
        return match address {
            IpAddr::V4(address) => is_forbidden_ipv4(address),
            IpAddr::V6(address) => is_forbidden_ipv6(address),
        };
    }

    false
}

fn is_forbidden_ipv4(address: Ipv4Addr) -> bool {
    address.is_private()
        || address.is_loopback()
        || address.is_link_local()
        || address.is_unspecified()
        || address.octets()[0] == 0
}

fn is_forbidden_ipv6(address: Ipv6Addr) -> bool {
    address.is_loopback()
        || address.is_unspecified()
        || matches!(address.segments()[0] & 0xfe00, 0xfc00)
        || matches!(address.segments()[0] & 0xffc0, 0xfe80)
}

fn webhook_http_response_to_delivery_response(
    response: WebhookHttpResponse,
) -> CallbackDeliveryResponse {
    match response.status {
        200..=299 => CallbackDeliveryResponse::Success,
        409 => CallbackDeliveryResponse::DuplicateAlreadyProcessed,
        410 => CallbackDeliveryResponse::TargetGone,
        429 => CallbackDeliveryResponse::RateLimited {
            retry_after_ms: response.retry_after_ms,
        },
        500..=599 => CallbackDeliveryResponse::ServerError(response.status),
        400..=499 => CallbackDeliveryResponse::ClientError(response.status),
        _ => CallbackDeliveryResponse::ServerError(response.status),
    }
}

fn dead_letter_to_value(dead_letter: &CallbackDeadLetter) -> Value {
    json!({
        "original_delivery_id": dead_letter.original_delivery_id,
        "subscription_id": dead_letter.subscription_id,
        "event_id": dead_letter.event_id,
        "run_id": dead_letter.run_id,
        "sequence": dead_letter.sequence,
        "cursor": dead_letter.cursor,
        "idempotency_key": dead_letter.idempotency_key,
        "failure_policy": callback_failure_policy_as_str(dead_letter.failure_policy),
        "attempt_history": dead_letter.attempt_history,
        "last_error": dead_letter.last_error,
        "dead_lettered_at_unix_ms": dead_letter.dead_lettered_at_unix_ms,
        "redrive_count": dead_letter.redrive_count,
    })
}

fn delivery_to_value(delivery: &CallbackDelivery) -> Value {
    json!({
        "delivery_id": delivery.delivery_id,
        "subscription_id": delivery.subscription_id,
        "event_id": delivery.event_id,
        "run_id": delivery.run_id,
        "sequence": delivery.sequence,
        "cursor": delivery.cursor,
        "attempt": delivery.attempt,
        "idempotency_key": delivery.idempotency_key,
        "failure_policy": callback_failure_policy_as_str(delivery.failure_policy),
        "status": callback_delivery_status_as_str(delivery.status),
        "next_retry_at_unix_ms": delivery.next_retry_at_unix_ms,
        "delivered_at_unix_ms": delivery.delivered_at_unix_ms,
        "acknowledged_at_unix_ms": delivery.acknowledged_at_unix_ms,
        "last_error": delivery.last_error,
        "redrive_count": delivery.redrive_count,
        "last_redrive_operator": delivery.last_redrive_operator,
        "last_redrive_reason": delivery.last_redrive_reason,
    })
}

fn delivery_from_value(value: Value) -> Result<CallbackDelivery, CallbackDeliveryError> {
    Ok(CallbackDelivery {
        delivery_id: callback_required_string(&value, "delivery_id")?,
        subscription_id: callback_required_string(&value, "subscription_id")?,
        event_id: callback_required_string(&value, "event_id")?,
        run_id: callback_required_string(&value, "run_id")?,
        sequence: callback_required_u64(&value, "sequence")?,
        cursor: callback_required_string(&value, "cursor")?,
        attempt: callback_required_u32(&value, "attempt")?,
        idempotency_key: callback_required_string(&value, "idempotency_key")?,
        failure_policy: callback_failure_policy_from_str(&callback_required_string(
            &value,
            "failure_policy",
        )?)?,
        status: callback_delivery_status_from_str(&callback_required_string(&value, "status")?)?,
        next_retry_at_unix_ms: callback_optional_u64(&value, "next_retry_at_unix_ms")?,
        delivered_at_unix_ms: callback_optional_u64(&value, "delivered_at_unix_ms")?,
        acknowledged_at_unix_ms: callback_optional_u64(&value, "acknowledged_at_unix_ms")?,
        last_error: callback_optional_string(&value, "last_error")?,
        redrive_count: callback_required_u32(&value, "redrive_count")?,
        last_redrive_operator: callback_optional_string(&value, "last_redrive_operator")?,
        last_redrive_reason: callback_optional_string(&value, "last_redrive_reason")?,
    })
}

fn dead_letter_from_value(value: Value) -> Result<CallbackDeadLetter, CallbackDeliveryError> {
    let attempt_history = value
        .get("attempt_history")
        .and_then(Value::as_array)
        .ok_or_else(|| CallbackDeliveryError::Storage {
            message: "stored callback dead letter has invalid attempt_history".to_owned(),
        })?
        .iter()
        .map(|attempt| {
            attempt
                .as_u64()
                .and_then(|attempt| u32::try_from(attempt).ok())
                .ok_or_else(|| CallbackDeliveryError::Storage {
                    message: "stored callback dead letter has invalid attempt".to_owned(),
                })
        })
        .collect::<Result<Vec<_>, _>>()?;

    Ok(CallbackDeadLetter {
        original_delivery_id: callback_required_string(&value, "original_delivery_id")?,
        subscription_id: callback_required_string(&value, "subscription_id")?,
        event_id: callback_required_string(&value, "event_id")?,
        run_id: callback_required_string(&value, "run_id")?,
        sequence: callback_required_u64(&value, "sequence")?,
        cursor: callback_required_string(&value, "cursor")?,
        idempotency_key: callback_required_string(&value, "idempotency_key")?,
        failure_policy: callback_failure_policy_from_str(&callback_required_string(
            &value,
            "failure_policy",
        )?)?,
        attempt_history,
        last_error: callback_optional_string(&value, "last_error")?,
        dead_lettered_at_unix_ms: callback_required_u64(&value, "dead_lettered_at_unix_ms")?,
        redrive_count: callback_required_u64(&value, "redrive_count")?
            .try_into()
            .map_err(|_| CallbackDeliveryError::Storage {
                message: "stored callback dead letter has oversized redrive_count".to_owned(),
            })?,
    })
}

fn callback_delivery_status_as_str(status: CallbackDeliveryStatus) -> &'static str {
    match status {
        CallbackDeliveryStatus::Pending => "pending",
        CallbackDeliveryStatus::Delivering => "delivering",
        CallbackDeliveryStatus::Delivered => "delivered",
        CallbackDeliveryStatus::Acknowledged => "acknowledged",
        CallbackDeliveryStatus::Failed => "failed",
        CallbackDeliveryStatus::DeadLettered => "dead_lettered",
        CallbackDeliveryStatus::Cancelled => "cancelled",
        CallbackDeliveryStatus::Expired => "expired",
    }
}

fn callback_delivery_status_from_str(
    status: &str,
) -> Result<CallbackDeliveryStatus, CallbackDeliveryError> {
    match status {
        "pending" => Ok(CallbackDeliveryStatus::Pending),
        "delivering" => Ok(CallbackDeliveryStatus::Delivering),
        "delivered" => Ok(CallbackDeliveryStatus::Delivered),
        "acknowledged" => Ok(CallbackDeliveryStatus::Acknowledged),
        "failed" => Ok(CallbackDeliveryStatus::Failed),
        "dead_lettered" => Ok(CallbackDeliveryStatus::DeadLettered),
        "cancelled" => Ok(CallbackDeliveryStatus::Cancelled),
        "expired" => Ok(CallbackDeliveryStatus::Expired),
        _ => Err(CallbackDeliveryError::Storage {
            message: format!("unknown callback delivery status {status}"),
        }),
    }
}

fn callback_failure_policy_as_str(failure_policy: CallbackFailurePolicy) -> &'static str {
    match failure_policy {
        CallbackFailurePolicy::BestEffort => "best_effort",
        CallbackFailurePolicy::RetryThenDeadLetter => "retry_then_dead_letter",
        CallbackFailurePolicy::PauseRunOnFailure => "pause_run_on_failure",
        CallbackFailurePolicy::FailRunOnFailure => "fail_run_on_failure",
    }
}

fn callback_failure_policy_from_str(
    failure_policy: &str,
) -> Result<CallbackFailurePolicy, CallbackDeliveryError> {
    match failure_policy {
        "best_effort" => Ok(CallbackFailurePolicy::BestEffort),
        "retry_then_dead_letter" => Ok(CallbackFailurePolicy::RetryThenDeadLetter),
        "pause_run_on_failure" => Ok(CallbackFailurePolicy::PauseRunOnFailure),
        "fail_run_on_failure" => Ok(CallbackFailurePolicy::FailRunOnFailure),
        _ => Err(CallbackDeliveryError::Storage {
            message: format!("unknown callback failure policy {failure_policy}"),
        }),
    }
}

fn callback_required_string(
    value: &Value,
    field: &'static str,
) -> Result<String, CallbackDeliveryError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .ok_or_else(|| CallbackDeliveryError::Storage {
            message: format!("stored callback dead letter has invalid {field}"),
        })
}

fn callback_optional_string(
    value: &Value,
    field: &'static str,
) -> Result<Option<String>, CallbackDeliveryError> {
    match value.get(field) {
        Some(Value::Null) | None => Ok(None),
        Some(Value::String(value)) => Ok(Some(value.clone())),
        _ => Err(CallbackDeliveryError::Storage {
            message: format!("stored callback dead letter has invalid {field}"),
        }),
    }
}

fn callback_required_u64(value: &Value, field: &'static str) -> Result<u64, CallbackDeliveryError> {
    value
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| CallbackDeliveryError::Storage {
            message: format!("stored callback dead letter has invalid {field}"),
        })
}

fn callback_required_u32(value: &Value, field: &'static str) -> Result<u32, CallbackDeliveryError> {
    callback_required_u64(value, field)?
        .try_into()
        .map_err(|_| CallbackDeliveryError::Storage {
            message: format!("stored callback value has oversized {field}"),
        })
}

fn callback_optional_u64(
    value: &Value,
    field: &'static str,
) -> Result<Option<u64>, CallbackDeliveryError> {
    match value.get(field) {
        Some(Value::Null) | None => Ok(None),
        Some(value) => value
            .as_u64()
            .map(Some)
            .ok_or_else(|| CallbackDeliveryError::Storage {
                message: format!("stored callback value has invalid {field}"),
            }),
    }
}

fn callback_storage_json(value: &Value) -> Result<String, CallbackDeliveryError> {
    serde_json::to_string(value).map_err(|error| CallbackDeliveryError::Storage {
        message: error.to_string(),
    })
}

fn callback_parse_json(value: &str) -> Result<Value, CallbackDeliveryError> {
    serde_json::from_str(value).map_err(|error| CallbackDeliveryError::Storage {
        message: error.to_string(),
    })
}

fn callback_storage_error(error: rusqlite::Error) -> CallbackDeliveryError {
    CallbackDeliveryError::Storage {
        message: error.to_string(),
    }
}

fn callback_u64_to_i64(value: u64, label: &'static str) -> Result<i64, CallbackDeliveryError> {
    i64::try_from(value).map_err(|_| CallbackDeliveryError::Storage {
        message: format!("{label} exceeds sqlite integer range"),
    })
}

fn optional_callback_u64_to_i64(
    value: Option<u64>,
    label: &'static str,
) -> Result<Option<i64>, CallbackDeliveryError> {
    value
        .map(|value| callback_u64_to_i64(value, label))
        .transpose()
}

fn callback_usize_to_i64(value: usize, label: &'static str) -> Result<i64, CallbackDeliveryError> {
    i64::try_from(value).map_err(|_| CallbackDeliveryError::Storage {
        message: format!("{label} exceeds sqlite integer range"),
    })
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
            redrive_count: 0,
            last_redrive_operator: None,
            last_redrive_reason: None,
        })
    }

    pub fn schedule_ordered_event(
        &self,
        subscription: &CallbackSubscription,
        event: &ApplicationProtocolEvent,
        ordering: &mut OrderedDeliveryState,
    ) -> Option<Option<CallbackDelivery>> {
        if !subscription.can_receive(event) {
            return None;
        }
        if subscription.ordered_delivery
            && ordering.is_blocked(&subscription.subscription_id, &event.metadata.run_id)
        {
            return Some(None);
        }

        let delivery = self
            .schedule_event(subscription, event)
            .expect("subscription and event were already checked");
        if subscription.ordered_delivery {
            ordering.record_scheduled(&delivery);
        }
        Some(Some(delivery))
    }

    pub fn schedule_replay(
        &self,
        subscription: &CallbackSubscription,
        log: &ApplicationProtocolLog,
        limit: usize,
    ) -> Vec<CallbackDelivery> {
        if subscription.status != CallbackSubscriptionStatus::Active || limit == 0 {
            return Vec::new();
        }

        let replayed = log.replay_after(subscription.replay_from_cursor.as_deref(), limit);
        if !subscription.ordered_delivery {
            return replayed
                .iter()
                .filter_map(|event| self.schedule_event(subscription, event))
                .collect();
        }

        let mut ordering = OrderedDeliveryState::new();
        let mut deliveries = Vec::new();
        for event in replayed {
            match self.schedule_ordered_event(subscription, &event, &mut ordering) {
                Some(Some(delivery)) => deliveries.push(delivery),
                Some(None) => break,
                None => {}
            }
        }
        deliveries
    }

    pub fn record_response(
        &self,
        mut delivery: CallbackDelivery,
        response: CallbackDeliveryResponse,
        now_unix_ms: u64,
    ) -> CallbackDelivery {
        if callback_delivery_status_is_terminal(delivery.status) {
            return delivery;
        }

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

    pub fn run_action_for_terminal_failure(
        &self,
        delivery: &CallbackDelivery,
    ) -> Option<CallbackDeliveryRunAction> {
        if !matches!(
            delivery.status,
            CallbackDeliveryStatus::Failed
                | CallbackDeliveryStatus::DeadLettered
                | CallbackDeliveryStatus::Cancelled
                | CallbackDeliveryStatus::Expired
        ) {
            return None;
        }
        let reason = delivery
            .last_error
            .clone()
            .unwrap_or_else(|| "callback_delivery_failed".to_owned());
        match delivery.failure_policy {
            CallbackFailurePolicy::PauseRunOnFailure => Some(CallbackDeliveryRunAction::PauseRun {
                run_id: delivery.run_id.clone(),
                subscription_id: delivery.subscription_id.clone(),
                delivery_id: delivery.delivery_id.clone(),
                reason,
            }),
            CallbackFailurePolicy::FailRunOnFailure => Some(CallbackDeliveryRunAction::FailRun {
                run_id: delivery.run_id.clone(),
                subscription_id: delivery.subscription_id.clone(),
                delivery_id: delivery.delivery_id.clone(),
                reason,
            }),
            CallbackFailurePolicy::BestEffort | CallbackFailurePolicy::RetryThenDeadLetter => None,
        }
    }

    pub fn redrive_dead_letter(
        &self,
        dead_letter: &CallbackDeadLetter,
        operator: impl Into<String>,
        reason: impl Into<String>,
        _redriven_at_unix_ms: u64,
    ) -> Result<CallbackDelivery, CallbackDeliveryError> {
        let operator = operator.into();
        if operator.trim().is_empty() {
            return Err(CallbackDeliveryError::EmptyField {
                field: "operator".to_owned(),
            });
        }
        let reason = reason.into();
        if reason.trim().is_empty() {
            return Err(CallbackDeliveryError::EmptyField {
                field: "reason".to_owned(),
            });
        }

        Ok(CallbackDelivery {
            delivery_id: dead_letter.original_delivery_id.clone(),
            subscription_id: dead_letter.subscription_id.clone(),
            event_id: dead_letter.event_id.clone(),
            run_id: dead_letter.run_id.clone(),
            sequence: dead_letter.sequence,
            cursor: dead_letter.cursor.clone(),
            attempt: dead_letter.attempt_history.last().copied().unwrap_or(0) + 1,
            idempotency_key: dead_letter.idempotency_key.clone(),
            failure_policy: dead_letter.failure_policy,
            status: CallbackDeliveryStatus::Pending,
            next_retry_at_unix_ms: None,
            delivered_at_unix_ms: None,
            acknowledged_at_unix_ms: None,
            last_error: dead_letter.last_error.clone(),
            redrive_count: dead_letter.redrive_count + 1,
            last_redrive_operator: Some(operator),
            last_redrive_reason: Some(reason),
        })
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
            .map(|retry_after_ms| retry_after_ms.min(self.retry_policy.max_delay_ms))
            .unwrap_or_else(|| {
                self.retry_policy
                    .delay_for_attempt_with_jitter(delivery.attempt - 1, &delivery.idempotency_key)
            });
        delivery.next_retry_at_unix_ms = Some(now_unix_ms.saturating_add(delay_ms));
        delivery.last_error = Some(error);
    }
}

fn callback_delivery_status_is_terminal(status: CallbackDeliveryStatus) -> bool {
    matches!(
        status,
        CallbackDeliveryStatus::Delivered
            | CallbackDeliveryStatus::Acknowledged
            | CallbackDeliveryStatus::Failed
            | CallbackDeliveryStatus::DeadLettered
            | CallbackDeliveryStatus::Cancelled
            | CallbackDeliveryStatus::Expired
    )
}

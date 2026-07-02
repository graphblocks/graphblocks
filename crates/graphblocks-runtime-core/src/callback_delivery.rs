use std::collections::{BTreeMap, BTreeSet};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use crate::application_event::{
    ApplicationProtocolEvent, ApplicationProtocolEventKind, ApplicationProtocolLog,
};
use hmac::{Hmac, Mac};
use serde_json::{Value, json};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

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
    pub ordered_delivery: bool,
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
            ordered_delivery: false,
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
    EmptyField {
        field: String,
    },
    InvalidDeliveryStatus {
        delivery_id: String,
        status: CallbackDeliveryStatus,
    },
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
    UnsupportedScheme { scheme: String },
    UnsafeEndpoint { host: String },
    InvalidPayloadLimit { max_payload_bytes: usize },
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
    if authority.contains('@') {
        return Err(WebhookEndpointError::MalformedUrl);
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

        log.replay_after(subscription.replay_from_cursor.as_deref(), limit)
            .iter()
            .filter_map(|event| self.schedule_event(subscription, event))
            .collect()
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
            .unwrap_or_else(|| self.retry_policy.delay_for_attempt(delivery.attempt - 1));
        delivery.next_retry_at_unix_ms = Some(now_unix_ms.saturating_add(delay_ms));
        delivery.last_error = Some(error);
    }
}

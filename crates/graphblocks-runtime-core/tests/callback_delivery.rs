use graphblocks_runtime_core::application_event::{
    ApplicationEvent, ApplicationEventKind, ApplicationEventMetadata, ApplicationEventVisibility,
    ApplicationProtocolEvent, ApplicationProtocolEventKind, ApplicationProtocolEventMetadata,
    ApplicationProtocolLog,
};
use graphblocks_runtime_core::callback_delivery::{
    CallbackAuthoritativeUse, CallbackConfigurationDiagnostic, CallbackDeadLetter,
    CallbackDeliveryError, CallbackDeliveryResponse, CallbackDeliveryRunAction,
    CallbackDeliveryScheduler, CallbackDeliveryStatus, CallbackDeliveryTarget,
    CallbackFailurePolicy, CallbackRetryPolicy, CallbackSubscription, CallbackSubscriptionStatus,
    EventFilter, OrderedDeliveryState, SqliteCallbackDeadLetterStore, SqliteCallbackDeliveryQueue,
    WebhookDeliveryAttempt, WebhookDeliveryTarget, WebhookDeliveryWorker, WebhookEgressPolicy,
    WebhookEndpointError, WebhookHttpResponse, WebhookHttpTransport, WebhookSignatureError,
    WebhookSigningConfig,
};
use graphblocks_runtime_core::connectors::{
    InMemorySecretProvider, SecretProviderError, SecretRef,
};
use rusqlite::{Connection, params};
use serde_json::json;
use std::collections::BTreeSet;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

fn sqlite_callback_dead_letter_path(label: &str) -> PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "graphblocks-callback-dead-letter-{label}-{unique}.sqlite3"
    ))
}

fn sqlite_callback_delivery_queue_path(label: &str) -> PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "graphblocks-callback-delivery-queue-{label}-{unique}.sqlite3"
    ))
}

fn protocol_event(
    event_id: &str,
    kind: ApplicationProtocolEventKind,
    sequence: u64,
) -> ApplicationProtocolEvent {
    ApplicationProtocolEvent::new(
        kind,
        ApplicationProtocolEventMetadata {
            event_id: event_id.to_owned(),
            protocol_version: "graphblocks.app.v1".to_owned(),
            run_id: "run-1".to_owned(),
            release_id: "release-1".to_owned(),
            turn_id: None,
            operation_id: None,
            sequence,
            cursor: Some(format!("cursor-{sequence}")),
            occurred_at_unix_ms: 1_000 + sequence,
        },
        json!({"message": "event payload"}),
    )
    .expect("event is valid")
}

fn subscription(
    filter: EventFilter,
    failure_policy: CallbackFailurePolicy,
) -> CallbackSubscription {
    CallbackSubscription::new(
        "sub-1",
        "principal:ide",
        "run",
        "run-1",
        filter,
        "webhook:ide-relay",
        failure_policy,
        900,
    )
    .expect("subscription is valid")
}

#[test]
fn subscription_filter_schedules_matching_and_terminal_events() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new()
            .with_types([ApplicationProtocolEventKind::ReviewRequested])
            .with_terminal_events(true),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let review = protocol_event(
        "event-review",
        ApplicationProtocolEventKind::ReviewRequested,
        7,
    );
    let progress = protocol_event(
        "event-progress",
        ApplicationProtocolEventKind::JobProgress,
        8,
    );
    let completed = protocol_event("event-done", ApplicationProtocolEventKind::RunCompleted, 9);

    let review_delivery = scheduler
        .schedule_event(&subscription, &review)
        .expect("matching event schedules");
    let completed_delivery = scheduler
        .schedule_event(&subscription, &completed)
        .expect("terminal event schedules when included");

    assert!(scheduler.schedule_event(&subscription, &progress).is_none());
    assert_eq!(review_delivery.delivery_id, "del_sub-1_event-review");
    assert_eq!(review_delivery.idempotency_key, "sub-1:event-review");
    assert_eq!(review_delivery.cursor, "cursor-7");
    assert_eq!(review_delivery.status, CallbackDeliveryStatus::Pending);
    assert_eq!(completed_delivery.event_id, "event-done");
}

#[test]
fn subscription_filter_excludes_terminal_events_when_disabled() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new().with_terminal_events(false),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let started = protocol_event("event-start", ApplicationProtocolEventKind::RunStarted, 1);
    let completed = protocol_event("event-done", ApplicationProtocolEventKind::RunCompleted, 2);
    let failed = protocol_event("event-failed", ApplicationProtocolEventKind::RunFailed, 3);
    let cancelled = protocol_event(
        "event-cancelled",
        ApplicationProtocolEventKind::RunCancelled,
        4,
    );
    let policy_stopped = protocol_event(
        "event-policy-stopped",
        ApplicationProtocolEventKind::RunPolicyStopped,
        5,
    );
    let expired = protocol_event("event-expired", ApplicationProtocolEventKind::RunExpired, 6);

    assert!(scheduler.schedule_event(&subscription, &started).is_some());
    assert!(
        scheduler
            .schedule_event(&subscription, &completed)
            .is_none()
    );
    assert!(scheduler.schedule_event(&subscription, &failed).is_none());
    assert!(
        scheduler
            .schedule_event(&subscription, &cancelled)
            .is_none()
    );
    assert!(
        scheduler
            .schedule_event(&subscription, &policy_stopped)
            .is_none()
    );
    assert!(scheduler.schedule_event(&subscription, &expired).is_none());
}

#[test]
fn subscription_filter_matches_visibility_node_operation_and_severity() {
    let mut matching = protocol_event(
        "event-ci-failed",
        ApplicationProtocolEventKind::JobProgress,
        11,
    );
    matching.payload = json!({
        "message": "ci failed",
        "visibility": "operator",
        "node_id": "runChecks",
        "operation_id": "op-ci-1",
        "severity": "error"
    });
    let mut wrong_visibility = matching.clone();
    wrong_visibility.metadata.event_id = "event-client".to_owned();
    wrong_visibility.payload["visibility"] = json!("client");
    let mut wrong_node = matching.clone();
    wrong_node.metadata.event_id = "event-other-node".to_owned();
    wrong_node.payload["node_id"] = json!("review");
    let mut below_severity = matching.clone();
    below_severity.metadata.event_id = "event-warning".to_owned();
    below_severity.payload["severity"] = json!("warning");

    let filter = EventFilter::new()
        .with_visibility(["operator"])
        .with_node_ids(["runChecks"])
        .with_operation_ids(["op-ci-1"])
        .with_severity_min("error")
        .expect("severity is valid");

    assert!(filter.matches(&matching));
    assert!(!filter.matches(&wrong_visibility));
    assert!(!filter.matches(&wrong_node));
    assert!(!filter.matches(&below_severity));
}

#[test]
fn event_filter_visibility_is_constrained_by_subscriber_authorization() {
    let mut client_event = protocol_event(
        "event-client-visible",
        ApplicationProtocolEventKind::RunStarted,
        13,
    );
    client_event.payload["visibility"] = json!("client");
    let mut operator_event = protocol_event(
        "event-operator-visible",
        ApplicationProtocolEventKind::RunStarted,
        14,
    );
    operator_event.payload["visibility"] = json!("operator");
    let requested = EventFilter::new()
        .with_types([ApplicationProtocolEventKind::RunStarted])
        .with_visibility(["client", "operator"]);

    let authorized = requested
        .authorized_for_visibility(["client"])
        .expect("authorized visibility projection is valid");
    let denied = EventFilter::new()
        .with_visibility(["operator"])
        .authorized_for_visibility(["client"])
        .expect("empty authorized visibility is still a deny-all filter");
    let invalid = requested.authorized_for_visibility(["private"]);

    assert_eq!(
        authorized.visibility,
        Some(["client".to_owned()].into_iter().collect())
    );
    assert!(authorized.matches(&client_event));
    assert!(!authorized.matches(&operator_event));
    assert_eq!(denied.visibility, Some(BTreeSet::new()));
    assert!(matches!(
        invalid,
        Err(CallbackDeliveryError::EmptyField { field })
        if field == "authorized_visibility"
    ));
}

#[test]
fn authorized_visibility_treats_missing_protocol_event_visibility_as_client() {
    let default_client_event = protocol_event(
        "event-default-client",
        ApplicationProtocolEventKind::RunStarted,
        15,
    );
    let mut malformed_visibility_event = default_client_event.clone();
    malformed_visibility_event.metadata.event_id = "event-malformed-visibility".to_owned();
    malformed_visibility_event.payload["visibility"] = json!(true);
    let filter = EventFilter::new()
        .with_types([ApplicationProtocolEventKind::RunStarted])
        .authorized_for_visibility(["client"])
        .expect("authorized visibility projection is valid");

    assert!(filter.matches(&default_client_event));
    assert!(!filter.matches(&malformed_visibility_event));
}

#[test]
fn subscription_filter_matches_camel_case_node_and_operation_payload_fields() {
    let mut matching = protocol_event(
        "event-ci-camel-case",
        ApplicationProtocolEventKind::JobProgress,
        12,
    );
    matching.payload = json!({
        "message": "ci failed",
        "visibility": "operator",
        "nodeId": "runChecks",
        "operationId": "op-ci-1",
        "severity": "error"
    });
    let filter = EventFilter::new()
        .with_visibility(["operator"])
        .with_node_ids(["runChecks"])
        .with_operation_ids(["op-ci-1"])
        .with_severity_min("error")
        .expect("severity is valid");

    assert!(filter.matches(&matching));
}

#[test]
fn subscription_filter_matches_operation_metadata_without_payload_duplication() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let matching = ApplicationProtocolEvent::new(
        ApplicationProtocolEventKind::ExternalCallbackReceived,
        ApplicationProtocolEventMetadata {
            event_id: "event-callback-1".to_owned(),
            protocol_version: "graphblocks.app.v1".to_owned(),
            run_id: "run-1".to_owned(),
            release_id: "release-1".to_owned(),
            turn_id: None,
            operation_id: Some("op-ci-1".to_owned()),
            sequence: 1,
            cursor: Some("cursor-1".to_owned()),
            occurred_at_unix_ms: 1_001,
        },
        json!({"callback_id": "callback-1"}),
    )
    .expect("callback event is valid");
    let wrong_operation = ApplicationProtocolEvent::new(
        ApplicationProtocolEventKind::ExternalCallbackReceived,
        ApplicationProtocolEventMetadata {
            event_id: "event-callback-2".to_owned(),
            protocol_version: "graphblocks.app.v1".to_owned(),
            run_id: "run-1".to_owned(),
            release_id: "release-1".to_owned(),
            turn_id: None,
            operation_id: Some("op-other".to_owned()),
            sequence: 2,
            cursor: Some("cursor-2".to_owned()),
            occurred_at_unix_ms: 1_002,
        },
        json!({"callback_id": "callback-2"}),
    )
    .expect("callback event is valid");
    let subscription = subscription(
        EventFilter::new().with_operation_ids(["op-ci-1"]),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );

    let delivery = scheduler
        .schedule_event(&subscription, &matching)
        .expect("operation metadata should match the subscription");

    assert_eq!(delivery.event_id, "event-callback-1");
    assert!(
        scheduler
            .schedule_event(&subscription, &wrong_operation)
            .is_none()
    );
}

#[test]
fn subscription_filter_matches_native_application_event_metadata_without_payload_duplication() {
    let matching = ApplicationEvent::new(
        ApplicationEventKind::ExternalCallbackReceived,
        ApplicationEventMetadata {
            event_id: "event-native-callback-1".to_owned(),
            run_id: "run-1".to_owned(),
            response_id: "response-1".to_owned(),
            turn_id: None,
            cursor: Some("cursor-1".to_owned()),
            graph_id: None,
            node_id: Some("waitCI".to_owned()),
            operation_id: Some("op-ci-1".to_owned()),
            sequence: 1,
            release_id: "release-1".to_owned(),
            policy_snapshot_id: "policy-1".to_owned(),
            occurred_at_unix_ms: 1_001,
            visibility: ApplicationEventVisibility::Operator,
        },
        json!({"callback_id": "callback-1"}),
    )
    .expect("native callback event is valid");
    let wrong_node = ApplicationEvent::new(
        ApplicationEventKind::ExternalCallbackReceived,
        ApplicationEventMetadata {
            event_id: "event-native-callback-2".to_owned(),
            run_id: "run-1".to_owned(),
            response_id: "response-1".to_owned(),
            turn_id: None,
            cursor: Some("cursor-2".to_owned()),
            graph_id: None,
            node_id: Some("review".to_owned()),
            operation_id: Some("op-ci-1".to_owned()),
            sequence: 2,
            release_id: "release-1".to_owned(),
            policy_snapshot_id: "policy-1".to_owned(),
            occurred_at_unix_ms: 1_002,
            visibility: ApplicationEventVisibility::Operator,
        },
        json!({"callback_id": "callback-2"}),
    )
    .expect("native callback event is valid");
    let filter = EventFilter::new()
        .with_types([ApplicationProtocolEventKind::ExternalCallbackReceived])
        .with_visibility(["operator"])
        .with_node_ids(["waitCI"])
        .with_operation_ids(["op-ci-1"]);

    assert!(filter.matches_application_event(&matching));
    assert!(!filter.matches_application_event(&wrong_node));
}

#[test]
fn callback_subscription_rejects_unknown_visibility_filter_literals() {
    let result = CallbackSubscription::new(
        "sub-visibility",
        "principal:ide",
        "run",
        "run-1",
        EventFilter::new().with_visibility(["private"]),
        "webhook:ide-relay",
        CallbackFailurePolicy::RetryThenDeadLetter,
        900,
    );

    assert_eq!(
        result,
        Err(
            graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::EmptyField {
                field: "event_filter.visibility".to_owned(),
            }
        )
    );
}

#[test]
fn callback_subscription_rejects_blank_event_filter_selectors() {
    for (field, filter) in [
        (
            "event_filter.node_ids",
            EventFilter::new().with_node_ids(["runChecks", " "]),
        ),
        (
            "event_filter.operation_ids",
            EventFilter::new().with_operation_ids(["op-ci-1", "\t"]),
        ),
        (
            "event_filter.severity_min",
            EventFilter {
                severity_min: Some(" ".to_owned()),
                ..EventFilter::new()
            },
        ),
    ] {
        let result = CallbackSubscription::new(
            "sub-filter",
            "principal:ide",
            "run",
            "run-1",
            filter,
            "webhook:ide-relay",
            CallbackFailurePolicy::RetryThenDeadLetter,
            900,
        );

        assert_eq!(
            result,
            Err(
                graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::EmptyField {
                    field: field.to_owned(),
                }
            ),
            "{field} should reject blank selector values",
        );
    }
}

#[test]
fn callback_subscription_rejects_unknown_scope_literals() {
    let result = CallbackSubscription::new(
        "sub-scope",
        "principal:ide",
        "workspace",
        "workspace-1",
        EventFilter::new(),
        "webhook:ide-relay",
        CallbackFailurePolicy::RetryThenDeadLetter,
        900,
    );

    assert_eq!(
        result,
        Err(
            graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::EmptyField {
                field: "scope".to_owned(),
            }
        )
    );
}

#[test]
fn callback_subscription_rejects_empty_typed_webhook_target_url() {
    let result = CallbackSubscription::new_with_target(
        "sub-empty-webhook",
        "principal:ide",
        "run",
        "run-1",
        EventFilter::new(),
        CallbackDeliveryTarget::webhook(" "),
        CallbackFailurePolicy::RetryThenDeadLetter,
        900,
    );

    assert_eq!(
        result,
        Err(
            graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::EmptyField {
                field: "url".to_owned(),
            }
        )
    );
}

#[test]
fn callback_subscription_rejects_unknown_delivery_target_kind() {
    let result = CallbackSubscription::new(
        "sub-unknown-target",
        "principal:ide",
        "run",
        "run-1",
        EventFilter::new(),
        "ftp:https://relay.example/events",
        CallbackFailurePolicy::RetryThenDeadLetter,
        900,
    );

    assert_eq!(
        result,
        Err(
            graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::EmptyField {
                field: "delivery_target".to_owned(),
            }
        )
    );
}

#[test]
fn callback_subscription_rejects_zero_creation_timestamp() {
    let result = CallbackSubscription::new(
        "sub-created",
        "principal:ide",
        "run",
        "run-1",
        EventFilter::new(),
        "webhook:ide-relay",
        CallbackFailurePolicy::RetryThenDeadLetter,
        0,
    );

    assert_eq!(
        result,
        Err(
            graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::EmptyField {
                field: "created_at_unix_ms".to_owned(),
            }
        )
    );
}

#[test]
fn inactive_or_expired_subscription_does_not_schedule_delivery() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let mut paused = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    paused.status = CallbackSubscriptionStatus::Paused;
    let mut expired = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    expired.expires_at_unix_ms = Some(999);
    let event = protocol_event("event-1", ApplicationProtocolEventKind::RunStarted, 1);

    assert!(scheduler.schedule_event(&paused, &event).is_none());
    assert!(scheduler.schedule_event(&expired, &event).is_none());
}

#[test]
fn subscription_expiration_is_exclusive_at_event_time() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let mut subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    subscription.expires_at_unix_ms = Some(1_002);
    let before_expiry = protocol_event("event-before", ApplicationProtocolEventKind::RunStarted, 1);
    let at_expiry = protocol_event("event-at", ApplicationProtocolEventKind::RunStarted, 2);

    assert!(
        scheduler
            .schedule_event(&subscription, &before_expiry)
            .is_some()
    );
    assert!(
        scheduler
            .schedule_event(&subscription, &at_expiry)
            .is_none(),
        "expires_at is an exclusive capability boundary"
    );
}

#[test]
fn callback_subscription_rejects_expiration_not_after_creation() {
    for expires_at_unix_ms in [899, 900] {
        let mut subscription = subscription(
            EventFilter::new(),
            CallbackFailurePolicy::RetryThenDeadLetter,
        );
        subscription.expires_at_unix_ms = Some(expires_at_unix_ms);

        assert_eq!(
            subscription.validate(),
            Err(
                graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::EmptyField {
                    field: "expires_at_unix_ms".to_owned(),
                }
            ),
            "expiration {expires_at_unix_ms} should be after creation",
        );
    }
}

#[test]
fn run_scoped_subscription_does_not_receive_other_run_events() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = CallbackSubscription::new(
        "sub-run-2",
        "principal:ide",
        "run",
        "run-2",
        EventFilter::new(),
        "webhook:ide-relay",
        CallbackFailurePolicy::RetryThenDeadLetter,
        900,
    )
    .expect("subscription is valid");
    let event = protocol_event("event-1", ApplicationProtocolEventKind::RunStarted, 1);

    assert!(scheduler.schedule_event(&subscription, &event).is_none());
}

#[test]
fn callback_delivery_identity_components_are_collision_resistant() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let first_subscription = CallbackSubscription::new(
        "sub_a",
        "principal:ide",
        "run",
        "run-1",
        EventFilter::new(),
        "webhook:ide-relay",
        CallbackFailurePolicy::RetryThenDeadLetter,
        900,
    )
    .expect("subscription is valid");
    let second_subscription = CallbackSubscription::new(
        "sub",
        "principal:ide",
        "run",
        "run-1",
        EventFilter::new(),
        "webhook:ide-relay",
        CallbackFailurePolicy::RetryThenDeadLetter,
        900,
    )
    .expect("subscription is valid");
    let first = scheduler
        .schedule_event(
            &first_subscription,
            &protocol_event("b", ApplicationProtocolEventKind::ReviewRequested, 1),
        )
        .expect("first delivery schedules");
    let second = scheduler
        .schedule_event(
            &second_subscription,
            &protocol_event("a_b", ApplicationProtocolEventKind::ReviewRequested, 2),
        )
        .expect("second delivery schedules");

    assert_ne!(first.delivery_id, second.delivery_id);

    let colon_subscription = CallbackSubscription::new(
        "sub:a",
        "principal:ide",
        "run",
        "run-1",
        EventFilter::new(),
        "webhook:ide-relay",
        CallbackFailurePolicy::RetryThenDeadLetter,
        900,
    )
    .expect("subscription is valid");
    let colon_first = scheduler
        .schedule_event(
            &colon_subscription,
            &protocol_event("b", ApplicationProtocolEventKind::ReviewRequested, 3),
        )
        .expect("colon delivery schedules");
    let colon_second = scheduler
        .schedule_event(
            &second_subscription,
            &protocol_event("a:b", ApplicationProtocolEventKind::ReviewRequested, 4),
        )
        .expect("colon delivery schedules");

    assert_ne!(colon_first.idempotency_key, colon_second.idempotency_key);
}

#[test]
fn webhook_server_errors_retry_then_dead_letter_with_bounded_backoff() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 250));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");

    let retry_once =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let retry_twice = scheduler.record_response(
        retry_once.clone(),
        CallbackDeliveryResponse::ServerError(503),
        1_100,
    );
    let dead_lettered = scheduler.record_response(
        retry_twice.clone(),
        CallbackDeliveryResponse::ServerError(503),
        1_300,
    );

    assert_eq!(retry_once.status, CallbackDeliveryStatus::Pending);
    assert_eq!(retry_once.attempt, 2);
    assert!(
        retry_once
            .next_retry_at_unix_ms
            .is_some_and(|retry_at| { retry_at > 1_100 && retry_at <= 1_200 })
    );
    assert_eq!(retry_twice.status, CallbackDeliveryStatus::Pending);
    assert_eq!(retry_twice.attempt, 3);
    assert!(
        retry_twice
            .next_retry_at_unix_ms
            .is_some_and(|retry_at| { retry_at > 1_300 && retry_at <= 1_350 })
    );
    assert_eq!(dead_lettered.status, CallbackDeliveryStatus::DeadLettered);
    assert_eq!(dead_lettered.attempt, 3);
    assert!(
        dead_lettered
            .last_error
            .as_deref()
            .is_some_and(|error| { error.contains("server_error:503") })
    );
}

#[test]
fn webhook_rate_limit_retry_after_is_capped_by_retry_policy() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 250));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");

    let retry = scheduler.record_response(
        delivery,
        CallbackDeliveryResponse::RateLimited {
            retry_after_ms: Some(10_000),
        },
        1_000,
    );

    assert_eq!(retry.status, CallbackDeliveryStatus::Pending);
    assert_eq!(retry.attempt, 2);
    assert_eq!(retry.next_retry_at_unix_ms, Some(1_250));
    assert_eq!(retry.last_error.as_deref(), Some("rate_limited"));
}

#[test]
fn webhook_rate_limit_retry_after_zero_uses_positive_delay() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 250));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");

    let retry = scheduler.record_response(
        delivery,
        CallbackDeliveryResponse::RateLimited {
            retry_after_ms: Some(0),
        },
        1_000,
    );

    assert_eq!(retry.status, CallbackDeliveryStatus::Pending);
    assert_eq!(retry.attempt, 2);
    assert!(
        retry
            .next_retry_at_unix_ms
            .is_some_and(|retry_at| retry_at > 1_000),
        "zero retry-after must not schedule an immediate retry"
    );
    assert_eq!(retry.last_error.as_deref(), Some("rate_limited"));
}

#[test]
fn callback_retry_policy_normalizes_zero_delays_to_positive_bound() {
    let policy = CallbackRetryPolicy::new(3, 0, 0);

    assert_eq!(policy.base_delay_ms, 1);
    assert_eq!(policy.max_delay_ms, 1);
    assert_eq!(policy.delay_for_attempt(1), 1);
    assert_eq!(policy.delay_for_attempt(12), 1);
}

#[test]
fn callback_retry_policy_adds_deterministic_bounded_jitter() {
    let policy = CallbackRetryPolicy::new(3, 100, 250);
    let first = policy.delay_for_attempt_with_jitter(1, "sub-1:event-1");
    let replay = policy.delay_for_attempt_with_jitter(1, "sub-1:event-1");
    let second = policy.delay_for_attempt_with_jitter(2, "sub-1:event-1");

    assert_eq!(first, replay);
    assert!(first > policy.delay_for_attempt(1));
    assert!(first <= 200);
    assert!(second > policy.delay_for_attempt(2));
    assert!(second <= policy.max_delay_ms);
}

#[test]
fn webhook_duplicate_and_success_responses_are_terminal_successes() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");

    let duplicate = scheduler.record_response(
        delivery.clone(),
        CallbackDeliveryResponse::DuplicateAlreadyProcessed,
        1_000,
    );
    let delivered = scheduler.record_response(delivery, CallbackDeliveryResponse::Success, 1_001);

    assert_eq!(duplicate.status, CallbackDeliveryStatus::Acknowledged);
    assert_eq!(duplicate.acknowledged_at_unix_ms, Some(1_000));
    assert_eq!(delivered.status, CallbackDeliveryStatus::Delivered);
    assert_eq!(delivered.delivered_at_unix_ms, Some(1_001));
}

#[test]
fn late_receiver_response_does_not_mutate_terminal_delivery() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");

    let delivered = scheduler.record_response(delivery, CallbackDeliveryResponse::Success, 1_000);
    let after_late_error = scheduler.record_response(
        delivered.clone(),
        CallbackDeliveryResponse::ServerError(503),
        1_250,
    );
    let after_late_duplicate = scheduler.record_response(
        delivered.clone(),
        CallbackDeliveryResponse::DuplicateAlreadyProcessed,
        1_500,
    );

    assert_eq!(after_late_error, delivered);
    assert_eq!(after_late_duplicate, delivered);
}

#[test]
fn best_effort_delivery_drops_retryable_failures_without_dead_letter() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(EventFilter::new(), CallbackFailurePolicy::BestEffort);
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");

    let failed =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(500), 1_000);

    assert_eq!(failed.status, CallbackDeliveryStatus::Failed);
    assert_eq!(failed.next_retry_at_unix_ms, None);
}

#[test]
fn mandatory_callback_failure_policy_pauses_or_fails_run_after_terminal_failure() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let pause_subscription =
        subscription(EventFilter::new(), CallbackFailurePolicy::PauseRunOnFailure);
    let fail_subscription =
        subscription(EventFilter::new(), CallbackFailurePolicy::FailRunOnFailure);

    let pause_delivery = scheduler
        .schedule_event(&pause_subscription, &event)
        .expect("delivery schedules");
    let fail_delivery = scheduler
        .schedule_event(&fail_subscription, &event)
        .expect("delivery schedules");
    let paused = scheduler.record_response(
        pause_delivery,
        CallbackDeliveryResponse::ClientError(403),
        1_000,
    );
    let failed = scheduler.record_response(
        fail_delivery,
        CallbackDeliveryResponse::ClientError(403),
        1_000,
    );

    assert_eq!(
        scheduler.run_action_for_terminal_failure(&paused),
        Some(CallbackDeliveryRunAction::PauseRun {
            run_id: "run-1".to_owned(),
            subscription_id: "sub-1".to_owned(),
            delivery_id: "del_sub-1_event-1".to_owned(),
            reason: "client_error:403".to_owned(),
        })
    );
    assert_eq!(
        scheduler.run_action_for_terminal_failure(&failed),
        Some(CallbackDeliveryRunAction::FailRun {
            run_id: "run-1".to_owned(),
            subscription_id: "sub-1".to_owned(),
            delivery_id: "del_sub-1_event-1".to_owned(),
            reason: "client_error:403".to_owned(),
        })
    );
}

#[test]
fn retry_then_dead_letter_failure_does_not_force_run_terminal_action() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");

    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);

    assert_eq!(dead_lettered.status, CallbackDeliveryStatus::DeadLettered);
    assert_eq!(
        scheduler.run_action_for_terminal_failure(&dead_lettered),
        None
    );
}

#[test]
fn dead_letter_rejects_zero_dead_lettered_timestamp() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);

    let error = CallbackDeadLetter::from_delivery(dead_lettered, 0)
        .expect_err("dead-letter timestamp must be positive");

    assert_eq!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::EmptyField {
            field: "dead_lettered_at_unix_ms".to_owned(),
        }
    );
}

#[test]
fn dead_letter_rejects_blank_source_delivery_identity_fields() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);

    for field in [
        "delivery_id",
        "subscription_id",
        "event_id",
        "run_id",
        "cursor",
        "idempotency_key",
    ] {
        let mut malformed = dead_lettered.clone();
        match field {
            "delivery_id" => malformed.delivery_id = " ".to_owned(),
            "subscription_id" => malformed.subscription_id = " ".to_owned(),
            "event_id" => malformed.event_id = " ".to_owned(),
            "run_id" => malformed.run_id = " ".to_owned(),
            "cursor" => malformed.cursor = " ".to_owned(),
            "idempotency_key" => malformed.idempotency_key = " ".to_owned(),
            _ => unreachable!("test field list is exhaustive"),
        }

        assert_eq!(
            CallbackDeadLetter::from_delivery(malformed, 1_001),
            Err(
                graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::EmptyField {
                    field: format!("callback_delivery.{field}"),
                }
            ),
            "{field} should be required before building a dead-letter"
        );
    }
}

#[test]
fn ordered_delivery_blocks_later_events_until_prior_delivery_is_terminal() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    )
    .with_ordered_delivery();
    let first = protocol_event("event-1", ApplicationProtocolEventKind::JobProgress, 1);
    let second = protocol_event("event-2", ApplicationProtocolEventKind::JobProgress, 2);
    let mut ordering = OrderedDeliveryState::new();

    let first_delivery = scheduler
        .schedule_ordered_event(&subscription, &first, &mut ordering)
        .expect("first event schedules")
        .expect("first event is deliverable");
    let blocked = scheduler
        .schedule_ordered_event(&subscription, &second, &mut ordering)
        .expect("second event matches subscription");

    assert!(blocked.is_none());
    assert_eq!(
        ordering.blocking_delivery("sub-1", "run-1").as_deref(),
        Some("del_sub-1_event-1")
    );

    let delivered =
        scheduler.record_response(first_delivery, CallbackDeliveryResponse::Success, 2_000);
    ordering.record_delivery_status(&delivered);
    let second_delivery = scheduler
        .schedule_ordered_event(&subscription, &second, &mut ordering)
        .expect("second event matches subscription")
        .expect("second event is deliverable after first completes");

    assert_eq!(second_delivery.delivery_id, "del_sub-1_event-2");
    assert_eq!(
        ordering.blocking_delivery("sub-1", "run-1").as_deref(),
        Some("del_sub-1_event-2")
    );
}

#[test]
fn ordered_delivery_allows_gap_after_dead_letter() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    )
    .with_ordered_delivery();
    let first = protocol_event("event-1", ApplicationProtocolEventKind::JobProgress, 1);
    let second = protocol_event("event-2", ApplicationProtocolEventKind::JobProgress, 2);
    let mut ordering = OrderedDeliveryState::new();

    let first_delivery = scheduler
        .schedule_ordered_event(&subscription, &first, &mut ordering)
        .expect("first event matches subscription")
        .expect("first event is deliverable");
    let dead_lettered = scheduler.record_response(
        first_delivery,
        CallbackDeliveryResponse::ServerError(503),
        2_000,
    );
    ordering.record_delivery_status(&dead_lettered);

    let second_delivery = scheduler
        .schedule_ordered_event(&subscription, &second, &mut ordering)
        .expect("second event matches subscription")
        .expect("dead-lettered gap allows next event");

    assert_eq!(dead_lettered.status, CallbackDeliveryStatus::DeadLettered);
    assert_eq!(second_delivery.event_id, "event-2");
}

#[test]
fn dead_letter_redrive_preserves_original_delivery_identity_and_attempt_history() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(2, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let retried =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let dead_lettered =
        scheduler.record_response(retried, CallbackDeliveryResponse::ServerError(503), 1_100);

    let dead_letter = CallbackDeadLetter::from_delivery(dead_lettered, 1_101)
        .expect("dead-letter record is valid");
    let redriven = scheduler
        .redrive_dead_letter(&dead_letter, "operator:alice", "receiver recovered", 2_000)
        .expect("redrive creates a new delivery attempt");

    assert_eq!(dead_letter.original_delivery_id, "del_sub-1_event-1");
    assert_eq!(dead_letter.event_id, "event-1");
    assert_eq!(dead_letter.subscription_id, "sub-1");
    assert_eq!(dead_letter.attempt_history, vec![1, 2]);
    assert_eq!(redriven.delivery_id, "del_sub-1_event-1");
    assert_eq!(redriven.event_id, "event-1");
    assert_eq!(redriven.subscription_id, "sub-1");
    assert_eq!(redriven.idempotency_key, "sub-1:event-1");
    assert_eq!(redriven.attempt, 3);
    assert_eq!(redriven.status, CallbackDeliveryStatus::Pending);
    assert_eq!(redriven.redrive_count, 1);
    assert_eq!(
        redriven.last_redrive_operator.as_deref(),
        Some("operator:alice")
    );
    assert_eq!(
        redriven.last_redrive_reason.as_deref(),
        Some("receiver recovered")
    );
}

#[test]
fn redrive_rejects_empty_operator_or_reason() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let dead_letter = CallbackDeadLetter::from_delivery(dead_lettered, 1_001)
        .expect("dead-letter record is valid");

    assert!(
        scheduler
            .redrive_dead_letter(&dead_letter, " ", "receiver recovered", 2_000)
            .is_err()
    );
    assert!(
        scheduler
            .redrive_dead_letter(&dead_letter, "operator:alice", " ", 2_000)
            .is_err()
    );
}

#[test]
fn redrive_rejects_zero_or_regressed_timestamp() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let dead_letter = CallbackDeadLetter::from_delivery(dead_lettered, 1_001)
        .expect("dead-letter record is valid");

    assert_eq!(
        scheduler.redrive_dead_letter(&dead_letter, "operator:alice", "receiver recovered", 0,),
        Err(
            graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::EmptyField {
                field: "redriven_at_unix_ms".to_owned(),
            }
        )
    );

    let error = scheduler
        .redrive_dead_letter(&dead_letter, "operator:alice", "receiver recovered", 1_000)
        .expect_err("redrive timestamp cannot precede dead-letter timestamp");

    assert!(matches!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
            if message.contains("redrive timestamp precedes dead-letter timestamp")
    ));
}

#[test]
fn sqlite_callback_dead_letter_store_persists_dead_letter_across_reopen() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(2, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let retried =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let dead_lettered =
        scheduler.record_response(retried, CallbackDeliveryResponse::ServerError(503), 1_100);
    let dead_letter = CallbackDeadLetter::from_delivery(dead_lettered, 1_101)
        .expect("dead-letter record is valid");
    let path = sqlite_callback_dead_letter_path("persist");

    {
        let store = SqliteCallbackDeadLetterStore::open(&path).expect("store opens");
        store
            .insert_dead_letter(dead_letter.clone())
            .expect("dead letter persists");
    }

    let store = SqliteCallbackDeadLetterStore::open(&path).expect("store reopens");
    let loaded = store
        .get_dead_letter("del_sub-1_event-1")
        .expect("dead letter loads")
        .expect("dead letter exists");

    assert_eq!(loaded, dead_letter);
    assert_eq!(loaded.attempt_history, vec![1, 2]);
    assert_eq!(loaded.last_error.as_deref(), Some("server_error:503"));
}

#[test]
fn sqlite_callback_dead_letter_store_rejects_blank_identity_fields() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let dead_letter = CallbackDeadLetter::from_delivery(dead_lettered, 1_001)
        .expect("dead-letter record is valid");
    let store = SqliteCallbackDeadLetterStore::open_in_memory().expect("store opens");

    for field in [
        "original_delivery_id",
        "subscription_id",
        "event_id",
        "run_id",
        "cursor",
        "idempotency_key",
    ] {
        let mut malformed = dead_letter.clone();
        match field {
            "original_delivery_id" => malformed.original_delivery_id = " ".to_owned(),
            "subscription_id" => malformed.subscription_id = " ".to_owned(),
            "event_id" => malformed.event_id = " ".to_owned(),
            "run_id" => malformed.run_id = " ".to_owned(),
            "cursor" => malformed.cursor = " ".to_owned(),
            "idempotency_key" => malformed.idempotency_key = " ".to_owned(),
            _ => unreachable!("test field list is exhaustive"),
        }

        assert_eq!(
            store.insert_dead_letter(malformed),
            Err(
                graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::EmptyField {
                    field: format!("callback_dead_letter.{field}"),
                }
            ),
            "{field} should be required"
        );
    }
}

#[test]
fn sqlite_callback_dead_letter_store_rejects_nonconsecutive_attempt_history() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let mut dead_letter = CallbackDeadLetter::from_delivery(dead_lettered, 1_001)
        .expect("dead-letter record is valid");
    let store = SqliteCallbackDeadLetterStore::open_in_memory().expect("store opens");

    for malformed_history in [vec![0], vec![2], vec![1, 3], vec![1, 2, 2]] {
        dead_letter.attempt_history = malformed_history.clone();

        assert_eq!(
            store.insert_dead_letter(dead_letter.clone()),
            Err(
                graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage {
                    message: "callback dead letter attempt history must be consecutive from 1"
                        .to_owned(),
                }
            ),
            "{malformed_history:?} should be rejected"
        );
    }
}

#[test]
fn sqlite_callback_dead_letter_store_rejects_missing_error_reason() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let mut dead_letter = CallbackDeadLetter::from_delivery(dead_lettered, 1_001)
        .expect("dead-letter record is valid");
    dead_letter.last_error = None;
    let store = SqliteCallbackDeadLetterStore::open_in_memory().expect("store opens");

    assert_eq!(
        store.insert_dead_letter(dead_letter),
        Err(
            graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage {
                message: "callback dead letter has no error reason".to_owned(),
            }
        )
    );
}

#[test]
fn sqlite_callback_dead_letter_store_rejects_blank_error_reason() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let mut dead_letter = CallbackDeadLetter::from_delivery(dead_lettered, 1_001)
        .expect("dead-letter record is valid");
    dead_letter.last_error = Some(" \t".to_owned());
    let store = SqliteCallbackDeadLetterStore::open_in_memory().expect("store opens");

    assert_eq!(
        store.insert_dead_letter(dead_letter),
        Err(
            graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage {
                message: "callback dead letter has no error reason".to_owned(),
            }
        )
    );
}

#[test]
fn sqlite_callback_dead_letter_store_rejects_immutable_metadata_overwrite() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let dead_letter = CallbackDeadLetter::from_delivery(dead_lettered, 1_001)
        .expect("dead-letter record is valid");
    let mut mutated = dead_letter.clone();
    mutated.subscription_id = "sub-forged".to_owned();
    let store = SqliteCallbackDeadLetterStore::open_in_memory().expect("store opens");

    store
        .insert_dead_letter(dead_letter.clone())
        .expect("original dead letter persists");
    let error = store
        .insert_dead_letter(mutated)
        .expect_err("dead-letter immutable metadata cannot be overwritten");

    assert!(matches!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
            if message.contains("callback dead letter immutable metadata conflict")
    ));
    assert_eq!(
        store
            .get_dead_letter("del_sub-1_event-1")
            .expect("dead letter loads"),
        Some(dead_letter)
    );
}

#[test]
fn sqlite_callback_dead_letter_store_rejects_redrive_history_regression() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let dead_letter = CallbackDeadLetter::from_delivery(dead_lettered, 1_001)
        .expect("dead-letter record is valid");
    let store = SqliteCallbackDeadLetterStore::open_in_memory().expect("store opens");

    store
        .insert_dead_letter(dead_letter.clone())
        .expect("original dead letter persists");
    let redriven = store
        .redrive_dead_letter(
            &scheduler,
            "del_sub-1_event-1",
            "operator:alice",
            "retry",
            1_500,
        )
        .expect("redrive updates history");
    assert_eq!(redriven.redrive_count, 1);

    let error = store
        .insert_dead_letter(dead_letter)
        .expect_err("old dead-letter snapshots cannot erase redrive history");
    assert!(matches!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
            if message.contains("callback dead letter redrive history regression")
    ));
    assert_eq!(
        store
            .get_dead_letter("del_sub-1_event-1")
            .expect("dead letter loads")
            .expect("dead letter exists")
            .attempt_history,
        vec![1, 2]
    );
}

#[test]
fn sqlite_callback_dead_letter_store_rejects_row_identity_mismatch_on_reopen() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let dead_letter = CallbackDeadLetter::from_delivery(dead_lettered, 1_001)
        .expect("dead-letter record is valid");
    let path = sqlite_callback_dead_letter_path("row-identity");

    {
        let store = SqliteCallbackDeadLetterStore::open(&path).expect("store opens");
        store
            .insert_dead_letter(dead_letter)
            .expect("dead letter persists");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let dead_letter_json: String = connection
            .query_row(
                "SELECT dead_letter_json FROM callback_dead_letters WHERE original_delivery_id = ?1",
                params!["del_sub-1_event-1"],
                |row| row.get(0),
            )
            .expect("dead-letter row exists");
        let mut dead_letter: serde_json::Value =
            serde_json::from_str(&dead_letter_json).expect("dead-letter json parses");
        dead_letter["original_delivery_id"] = json!("del_forged");
        connection
            .execute(
                "UPDATE callback_dead_letters SET dead_letter_json = ?1 WHERE original_delivery_id = ?2",
                params![
                    serde_json::to_string(&dead_letter).expect("dead-letter serializes"),
                    "del_sub-1_event-1"
                ],
            )
            .expect("dead-letter row is tampered");
    }

    let store = SqliteCallbackDeadLetterStore::open(&path).expect("store reopens");
    let error = store
        .get_dead_letter("del_sub-1_event-1")
        .expect_err("dead-letter row identity mismatch must fail durable replay");

    assert!(matches!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
            if message.contains("stored callback dead letter identity does not match row key")
    ));
}

#[test]
fn sqlite_callback_dead_letter_store_redrives_after_reopen_and_updates_redrive_count() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let dead_letter = CallbackDeadLetter::from_delivery(dead_lettered, 1_001)
        .expect("dead-letter record is valid");
    let path = sqlite_callback_dead_letter_path("redrive");

    {
        let store = SqliteCallbackDeadLetterStore::open(&path).expect("store opens");
        store
            .insert_dead_letter(dead_letter)
            .expect("dead letter persists");
    }

    let store = SqliteCallbackDeadLetterStore::open(&path).expect("store reopens");
    let redriven = store
        .redrive_dead_letter(
            &scheduler,
            "del_sub-1_event-1",
            "operator:alice",
            "receiver recovered",
            2_000,
        )
        .expect("redrive succeeds after reopen");
    let loaded = store
        .get_dead_letter("del_sub-1_event-1")
        .expect("dead letter loads")
        .expect("dead letter remains for audit");

    assert_eq!(redriven.delivery_id, "del_sub-1_event-1");
    assert_eq!(redriven.idempotency_key, "sub-1:event-1");
    assert_eq!(redriven.attempt, 2);
    assert_eq!(redriven.redrive_count, 1);
    assert_eq!(loaded.redrive_count, 1);
}

#[test]
fn sqlite_callback_dead_letter_store_redrive_persists_attempt_history() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let dead_letter = CallbackDeadLetter::from_delivery(dead_lettered, 1_001)
        .expect("dead-letter record is valid");
    let path = sqlite_callback_dead_letter_path("redrive-history");

    {
        let store = SqliteCallbackDeadLetterStore::open(&path).expect("store opens");
        store
            .insert_dead_letter(dead_letter)
            .expect("dead letter persists");
        let first = store
            .redrive_dead_letter(
                &scheduler,
                "del_sub-1_event-1",
                "operator:alice",
                "receiver recovered",
                2_000,
            )
            .expect("first redrive succeeds");
        assert_eq!(first.attempt, 2);
    }

    let store = SqliteCallbackDeadLetterStore::open(&path).expect("store reopens");
    let second = store
        .redrive_dead_letter(
            &scheduler,
            "del_sub-1_event-1",
            "operator:bob",
            "second redrive",
            3_000,
        )
        .expect("second redrive succeeds");
    let loaded = store
        .get_dead_letter("del_sub-1_event-1")
        .expect("dead letter loads")
        .expect("dead letter remains for audit");

    assert_eq!(second.attempt, 3);
    assert_eq!(second.redrive_count, 2);
    assert_eq!(loaded.redrive_count, 2);
    assert_eq!(loaded.attempt_history, vec![1, 2, 3]);
}

#[test]
fn sqlite_callback_delivery_queue_persists_pending_delivery_across_reopen() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let path = sqlite_callback_delivery_queue_path("pending");

    {
        let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue opens");
        queue
            .upsert_delivery(delivery.clone())
            .expect("delivery persists");
    }

    let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue reopens");
    let loaded = queue
        .get_delivery("del_sub-1_event-1")
        .expect("delivery loads")
        .expect("delivery exists");
    let due = queue
        .due_deliveries(1_000, 10)
        .expect("due deliveries load");

    assert_eq!(loaded, delivery);
    assert_eq!(due, vec![delivery]);
}

#[test]
fn sqlite_callback_delivery_queue_claims_due_delivery_once_across_workers() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let path = sqlite_callback_delivery_queue_path("atomic-claim");
    SqliteCallbackDeliveryQueue::open(&path)
        .expect("queue opens")
        .upsert_delivery(delivery)
        .expect("delivery persists");
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(3));
    let mut workers = Vec::new();
    for _ in 0..2 {
        let queue = SqliteCallbackDeliveryQueue::open(&path).expect("worker queue opens");
        let barrier = barrier.clone();
        workers.push(std::thread::spawn(move || {
            barrier.wait();
            queue.claim_due_deliveries(2_000, 5_000, 1)
        }));
    }

    barrier.wait();
    let claimed = workers
        .into_iter()
        .map(|worker| worker.join().expect("worker joins"))
        .collect::<Result<Vec<_>, _>>()
        .expect("workers claim without storage errors");

    assert_eq!(claimed.iter().map(Vec::len).sum::<usize>(), 1);
    assert_eq!(
        claimed
            .iter()
            .flatten()
            .next()
            .expect("one delivery is claimed")
            .delivery
            .status,
        CallbackDeliveryStatus::Delivering
    );
}

#[test]
fn sqlite_callback_delivery_claim_lease_and_generation_fence_live_workers() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let path = sqlite_callback_delivery_queue_path("claim-generation-fence");
    let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue opens");
    let event = protocol_event(
        "event-live-claim",
        ApplicationProtocolEventKind::ReviewRequested,
        1,
    );
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    queue
        .upsert_delivery(delivery)
        .expect("pending delivery persists");
    let claim = queue
        .claim_due_deliveries(1_000, 1_000, 1)
        .expect("delivery claims")
        .into_iter()
        .next()
        .expect("one delivery claims");
    let completed = scheduler.record_response(
        claim.delivery.clone(),
        CallbackDeliveryResponse::Success,
        1_500,
    );
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(3));
    let recovery_barrier = barrier.clone();
    let recovery_path = path.clone();
    let recovery = std::thread::spawn(move || {
        let queue = SqliteCallbackDeliveryQueue::open(recovery_path).expect("recovery queue opens");
        recovery_barrier.wait();
        queue.recover_in_flight_deliveries(1_999)
    });
    let completion_barrier = barrier.clone();
    let completion_path = path.clone();
    let completion = std::thread::spawn(move || {
        let queue =
            SqliteCallbackDeliveryQueue::open(completion_path).expect("completion queue opens");
        completion_barrier.wait();
        queue.complete_claimed_delivery(&claim, completed)
    });

    barrier.wait();
    assert_eq!(
        recovery
            .join()
            .expect("recovery worker joins")
            .expect("recovery succeeds"),
        0,
        "an unexpired claim belongs to a live worker",
    );
    completion
        .join()
        .expect("completion worker joins")
        .expect("live claim completes");
    assert_eq!(
        queue
            .get_delivery("del_sub-1_event-live-claim")
            .expect("delivery loads")
            .expect("delivery exists")
            .status,
        CallbackDeliveryStatus::Delivered,
    );

    let event = protocol_event(
        "event-stale-claim",
        ApplicationProtocolEventKind::ReviewRequested,
        2,
    );
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    queue
        .upsert_delivery(delivery)
        .expect("pending delivery persists");
    let stale_claim = queue
        .claim_due_deliveries(3_000, 1_000, 1)
        .expect("first generation claims")
        .into_iter()
        .next()
        .expect("first generation exists");
    assert_eq!(
        queue
            .recover_in_flight_deliveries(4_000)
            .expect("expired claim recovers"),
        1,
    );
    let current_claim = queue
        .claim_due_deliveries(4_000, 1_000, 1)
        .expect("second generation claims")
        .into_iter()
        .next()
        .expect("second generation exists");
    assert!(current_claim.claim_generation > stale_claim.claim_generation);
    let stale_completion = scheduler.record_response(
        stale_claim.delivery.clone(),
        CallbackDeliveryResponse::Success,
        3_500,
    );
    assert!(
        queue
            .complete_claimed_delivery(&stale_claim, stale_completion)
            .is_err(),
        "a recovered generation must be fenced from completion",
    );
    let current_completion = scheduler.record_response(
        current_claim.delivery.clone(),
        CallbackDeliveryResponse::Success,
        4_500,
    );
    queue
        .complete_claimed_delivery(&current_claim, current_completion)
        .expect("current generation completes");
}

#[test]
fn sqlite_callback_delivery_queue_rejects_stale_terminal_overwrite() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let pending = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let delivered =
        scheduler.record_response(pending.clone(), CallbackDeliveryResponse::Success, 2_000);
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");
    queue
        .upsert_delivery(delivered.clone())
        .expect("terminal delivery persists");

    let error = queue
        .upsert_delivery(pending)
        .expect_err("stale pending state must not replace terminal delivery");

    assert!(matches!(
        error,
        CallbackDeliveryError::Storage { message }
            if message.contains("terminal callback delivery state cannot be overwritten")
    ));
    assert_eq!(
        queue
            .get_delivery(&delivered.delivery_id)
            .expect("delivery loads"),
        Some(delivered)
    );
}

#[test]
fn sqlite_callback_delivery_queue_requires_claim_before_state_transition() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let pending = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let retry = scheduler.record_response(
        pending.clone(),
        CallbackDeliveryResponse::ServerError(503),
        2_000,
    );
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");
    queue
        .upsert_delivery(pending.clone())
        .expect("pending delivery persists");

    let error = queue
        .upsert_delivery(retry)
        .expect_err("worker must claim pending delivery before changing its state");

    assert!(matches!(
        error,
        CallbackDeliveryError::Storage { message }
            if message.contains("callback delivery state transition requires an active claim")
    ));
    assert_eq!(
        queue
            .get_delivery(&pending.delivery_id)
            .expect("delivery loads"),
        Some(pending)
    );
}

#[test]
fn sqlite_callback_delivery_queue_rejects_delivery_row_identity_mismatch_on_reopen() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let path = sqlite_callback_delivery_queue_path("delivery-row-identity");

    {
        let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue opens");
        queue.upsert_delivery(delivery).expect("delivery persists");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        let delivery_json: String = connection
            .query_row(
                "SELECT delivery_json FROM callback_deliveries WHERE delivery_id = ?1",
                params!["del_sub-1_event-1"],
                |row| row.get(0),
            )
            .expect("delivery row exists");
        let mut delivery: serde_json::Value =
            serde_json::from_str(&delivery_json).expect("delivery json parses");
        delivery["delivery_id"] = json!("del_forged");
        connection
            .execute(
                "UPDATE callback_deliveries SET delivery_json = ?1 WHERE delivery_id = ?2",
                params![
                    serde_json::to_string(&delivery).expect("delivery serializes"),
                    "del_sub-1_event-1"
                ],
            )
            .expect("delivery row is tampered");
    }

    let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue reopens");
    let error = queue
        .get_delivery("del_sub-1_event-1")
        .expect_err("delivery row identity mismatch must fail durable replay");

    assert!(matches!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
            if message.contains("stored callback delivery identity does not match row key")
    ));
}

#[test]
fn sqlite_callback_delivery_queue_rejects_delivery_row_status_mismatch_on_replay() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let failed =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ClientError(400), 2_000);
    let path = sqlite_callback_delivery_queue_path("delivery-row-status");

    {
        let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue opens");
        queue
            .upsert_delivery(failed)
            .expect("failed delivery persists");
    }

    {
        let connection = Connection::open(&path).expect("sqlite connection opens");
        connection
            .execute(
                "UPDATE callback_deliveries SET status = ?1 WHERE delivery_id = ?2",
                params!["pending", "del_sub-1_event-1"],
            )
            .expect("delivery row is tampered");
    }

    let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue reopens");
    let error = queue
        .due_deliveries(3_000, 10)
        .expect_err("delivery row status mismatch must fail durable replay");

    assert!(matches!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
            if message.contains("stored callback delivery status does not match row status")
    ));
}

#[test]
fn sqlite_callback_delivery_queue_persists_retry_schedule_across_reopen() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let retry =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let path = sqlite_callback_delivery_queue_path("retry");

    {
        let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue opens");
        queue
            .upsert_delivery(retry.clone())
            .expect("retry delivery persists");
    }

    let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue reopens");
    let retry_at = retry
        .next_retry_at_unix_ms
        .expect("retry delivery has a retry timestamp");
    let before_due = queue
        .due_deliveries(retry_at - 1, 10)
        .expect("due deliveries load");
    let after_due = queue
        .due_deliveries(retry_at, 10)
        .expect("due deliveries load");

    assert!(before_due.is_empty());
    assert_eq!(after_due, vec![retry]);
}

#[test]
fn sqlite_callback_delivery_queue_rejects_pending_retry_with_zero_timestamp() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let mut retry =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    retry.next_retry_at_unix_ms = Some(0);
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");

    let error = queue
        .upsert_delivery(retry)
        .expect_err("pending retry must retain a positive retry timestamp");

    assert!(matches!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
            if message.contains("pending delivery has zero retry timestamp")
    ));
}

#[test]
fn sqlite_callback_delivery_queue_rejects_acknowledged_retry_timestamp_conflict() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let mut acknowledged = scheduler.record_response(
        delivery,
        CallbackDeliveryResponse::DuplicateAlreadyProcessed,
        2_000,
    );
    acknowledged.next_retry_at_unix_ms = Some(2_500);
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");

    let error = queue
        .upsert_delivery(acknowledged)
        .expect_err("acknowledged delivery cannot retain a retry timestamp");

    assert!(matches!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
            if message.contains("acknowledged delivery has retry timestamp")
    ));
}

#[test]
fn sqlite_callback_delivery_queue_rejects_blank_delivery_identity_fields() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");

    for field in [
        "delivery_id",
        "subscription_id",
        "event_id",
        "run_id",
        "cursor",
        "idempotency_key",
    ] {
        let mut malformed = delivery.clone();
        match field {
            "delivery_id" => malformed.delivery_id = " ".to_owned(),
            "subscription_id" => malformed.subscription_id = " ".to_owned(),
            "event_id" => malformed.event_id = " ".to_owned(),
            "run_id" => malformed.run_id = " ".to_owned(),
            "cursor" => malformed.cursor = " ".to_owned(),
            "idempotency_key" => malformed.idempotency_key = " ".to_owned(),
            _ => unreachable!("test field list is exhaustive"),
        }

        assert_eq!(
            queue.upsert_delivery(malformed),
            Err(
                graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::EmptyField {
                    field: format!("callback_delivery.{field}"),
                }
            ),
            "{field} should be required"
        );
    }
}

#[test]
fn sqlite_callback_delivery_queue_rejects_delivered_without_delivery_timestamp() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let mut delivered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::Success, 2_000);
    delivered.delivered_at_unix_ms = None;
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");

    let error = queue
        .upsert_delivery(delivered)
        .expect_err("delivered record must retain delivery timestamp");

    assert!(matches!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
            if message.contains("delivered delivery has no delivered timestamp")
    ));
}

#[test]
fn sqlite_callback_delivery_queue_rejects_delivered_with_zero_delivery_timestamp() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let mut delivered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::Success, 2_000);
    delivered.delivered_at_unix_ms = Some(0);
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");

    let error = queue
        .upsert_delivery(delivered)
        .expect_err("delivered record must retain a positive delivery timestamp");

    assert!(matches!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
            if message.contains("delivered delivery has zero delivered timestamp")
    ));
}

#[test]
fn sqlite_callback_delivery_queue_rejects_acknowledged_without_ack_timestamp() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let mut acknowledged = scheduler.record_response(
        delivery,
        CallbackDeliveryResponse::DuplicateAlreadyProcessed,
        2_000,
    );
    acknowledged.acknowledged_at_unix_ms = None;
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");

    let error = queue
        .upsert_delivery(acknowledged)
        .expect_err("acknowledged record must retain acknowledgement timestamp");

    assert!(matches!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
            if message.contains("acknowledged delivery has no acknowledged timestamp")
    ));
}

#[test]
fn sqlite_callback_delivery_queue_rejects_acknowledged_with_zero_ack_timestamp() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let mut acknowledged = scheduler.record_response(
        delivery,
        CallbackDeliveryResponse::DuplicateAlreadyProcessed,
        2_000,
    );
    acknowledged.acknowledged_at_unix_ms = Some(0);
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");

    let error = queue
        .upsert_delivery(acknowledged)
        .expect_err("acknowledged record must retain a positive acknowledgement timestamp");

    assert!(matches!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
            if message.contains("acknowledged delivery has zero acknowledged timestamp")
    ));
}

#[test]
fn sqlite_callback_delivery_queue_rejects_acknowledged_before_delivery_timestamp() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let mut acknowledged = scheduler.record_response(
        delivery,
        CallbackDeliveryResponse::DuplicateAlreadyProcessed,
        2_000,
    );
    acknowledged.delivered_at_unix_ms = Some(2_500);
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");

    let error = queue
        .upsert_delivery(acknowledged)
        .expect_err("acknowledgement cannot precede delivery timestamp");

    assert!(matches!(
        error,
        graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
            if message.contains("acknowledged delivery precedes delivered timestamp")
    ));
}

#[test]
fn sqlite_callback_delivery_queue_rejects_terminal_failure_without_error_reason() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let terminal =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 2_000);
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");

    for status in [
        CallbackDeliveryStatus::Failed,
        CallbackDeliveryStatus::DeadLettered,
        CallbackDeliveryStatus::Cancelled,
        CallbackDeliveryStatus::Expired,
    ] {
        let mut malformed = terminal.clone();
        malformed.status = status;
        malformed.next_retry_at_unix_ms = None;
        malformed.last_error = None;

        let error = queue
            .upsert_delivery(malformed)
            .expect_err("terminal failure record must retain an error reason");

        assert!(
            matches!(
                error,
                graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
                    if message.contains("terminal callback delivery has no error reason")
            ),
            "{status:?} should require last_error"
        );
    }
}

#[test]
fn sqlite_callback_delivery_queue_rejects_terminal_failure_with_blank_error_reason() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let terminal =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 2_000);
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");

    for status in [
        CallbackDeliveryStatus::Failed,
        CallbackDeliveryStatus::DeadLettered,
        CallbackDeliveryStatus::Cancelled,
        CallbackDeliveryStatus::Expired,
    ] {
        let mut malformed = terminal.clone();
        malformed.status = status;
        malformed.next_retry_at_unix_ms = None;
        malformed.last_error = Some(" \t".to_owned());

        let error = queue
            .upsert_delivery(malformed)
            .expect_err("terminal failure record must retain a nonblank error reason");

        assert!(
            matches!(
                error,
                graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
                    if message.contains("terminal callback delivery has no error reason")
            ),
            "{status:?} should require nonblank last_error"
        );
    }
}

#[test]
fn sqlite_callback_delivery_queue_rejects_redrive_without_audit_fields() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let dead_lettered =
        scheduler.record_response(delivery, CallbackDeliveryResponse::ServerError(503), 1_000);
    let dead_letter =
        CallbackDeadLetter::from_delivery(dead_lettered, 1_001).expect("dead letter is created");
    let redriven = scheduler
        .redrive_dead_letter(&dead_letter, "operator:alice", "receiver recovered", 2_000)
        .expect("redrive creates a delivery");
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");

    for field in ["last_redrive_operator", "last_redrive_reason"] {
        let mut malformed = redriven.clone();
        match field {
            "last_redrive_operator" => malformed.last_redrive_operator = None,
            "last_redrive_reason" => malformed.last_redrive_reason = Some(" ".to_owned()),
            _ => unreachable!("test field list is exhaustive"),
        }

        let error = queue
            .upsert_delivery(malformed)
            .expect_err("redriven deliveries must preserve operator audit fields");

        assert!(
            matches!(
                error,
                graphblocks_runtime_core::callback_delivery::CallbackDeliveryError::Storage { message }
                    if message.contains("redriven callback delivery has invalid audit fields")
            ),
            "{field} should be required for redrive audit"
        );
    }
}

#[test]
fn sqlite_callback_delivery_queue_recovers_in_flight_delivery_after_worker_restart() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let path = sqlite_callback_delivery_queue_path("recover-in-flight");

    {
        let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue opens");
        queue
            .upsert_delivery(delivery.clone())
            .expect("pending delivery persists");
        queue
            .claim_due_deliveries(1_000, 500, 1)
            .expect("delivery claims before simulated restart");
    }

    let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue reopens");
    let recovered = queue
        .recover_in_flight_deliveries(2_000)
        .expect("in-flight delivery recovers");
    let due = queue
        .due_deliveries(2_000, 10)
        .expect("recovered delivery is due");

    assert_eq!(recovered, 1);
    assert_eq!(due.len(), 1);
    assert_eq!(due[0].delivery_id, "del_sub-1_event-1");
    assert_eq!(due[0].status, CallbackDeliveryStatus::Pending);
    assert_eq!(due[0].next_retry_at_unix_ms, Some(2_000));
    assert_eq!(
        due[0].last_error.as_deref(),
        Some("delivery_recovered_after_worker_restart")
    );
}

#[test]
fn sqlite_callback_delivery_recovery_does_not_overwrite_concurrent_terminal_updates() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let path = sqlite_callback_delivery_queue_path("recover-terminal-race");
    let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue opens");
    for sequence in 1..=512 {
        let event = protocol_event(
            &format!("event-recover-{sequence}"),
            ApplicationProtocolEventKind::ReviewRequested,
            sequence,
        );
        let delivery = scheduler
            .schedule_event(&subscription, &event)
            .expect("delivery schedules");
        queue
            .upsert_delivery(delivery)
            .expect("pending delivery persists");
    }
    let claims = queue
        .claim_due_deliveries(1_000, 1_000, 512)
        .expect("deliveries claim before simulated worker restart");
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(3));
    let recovery_barrier = barrier.clone();
    let recovery_path = path.clone();
    let recovery = std::thread::spawn(move || {
        let queue = SqliteCallbackDeliveryQueue::open(recovery_path).expect("recovery queue opens");
        recovery_barrier.wait();
        queue.recover_in_flight_deliveries(3_000)
    });
    let update_barrier = barrier.clone();
    let update_path = path.clone();
    let updates = std::thread::spawn(move || {
        let queue = SqliteCallbackDeliveryQueue::open(update_path).expect("worker queue opens");
        update_barrier.wait();
        std::thread::sleep(Duration::from_millis(1));
        let mut committed = Vec::new();
        for claim in claims {
            let mut delivery = claim.delivery.clone();
            delivery.status = CallbackDeliveryStatus::Delivered;
            delivery.delivered_at_unix_ms = Some(2_500);
            if queue
                .complete_claimed_delivery(&claim, delivery.clone())
                .is_ok()
            {
                committed.push(delivery.delivery_id);
            }
        }
        committed
    });

    barrier.wait();
    recovery
        .join()
        .expect("recovery worker joins")
        .expect("recovery succeeds");
    let committed = updates.join().expect("delivery worker joins");
    let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue reopens");
    for delivery_id in committed {
        assert_eq!(
            queue
                .get_delivery(&delivery_id)
                .expect("delivery loads")
                .expect("delivery exists")
                .status,
            CallbackDeliveryStatus::Delivered,
            "recovery must not overwrite a concurrent terminal commit",
        );
    }
}

#[test]
fn sqlite_callback_delivery_queue_cancels_pending_subscription_deliveries() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let first = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let second = protocol_event("event-2", ApplicationProtocolEventKind::ReviewRequested, 2);
    let pending = scheduler
        .schedule_event(&subscription, &first)
        .expect("first delivery schedules");
    let in_flight = scheduler
        .schedule_event(&subscription, &second)
        .expect("second delivery schedules");
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");
    queue
        .upsert_delivery(in_flight)
        .expect("second pending delivery persists");
    queue
        .claim_due_deliveries(1_000, 5_000, 1)
        .expect("second delivery claims");
    queue
        .upsert_delivery(pending)
        .expect("pending delivery persists");

    let cancelled = queue
        .cancel_pending_for_subscription("sub-1", "subscription_revoked")
        .expect("pending deliveries cancel");
    let pending = queue
        .get_delivery("del_sub-1_event-1")
        .expect("pending delivery loads")
        .expect("pending delivery exists");
    let in_flight = queue
        .get_delivery("del_sub-1_event-2")
        .expect("in-flight delivery loads")
        .expect("in-flight delivery exists");

    assert_eq!(cancelled, 1);
    assert_eq!(pending.status, CallbackDeliveryStatus::Cancelled);
    assert_eq!(pending.last_error.as_deref(), Some("subscription_revoked"));
    assert_eq!(in_flight.status, CallbackDeliveryStatus::Delivering);
}

#[test]
fn sqlite_callback_delivery_cancellation_does_not_overwrite_concurrent_terminal_updates() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let path = sqlite_callback_delivery_queue_path("cancel-terminal-race");
    let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue opens");
    for sequence in 1..=512 {
        let event = protocol_event(
            &format!("event-cancel-{sequence}"),
            ApplicationProtocolEventKind::ReviewRequested,
            sequence,
        );
        let delivery = scheduler
            .schedule_event(&subscription, &event)
            .expect("delivery schedules");
        queue
            .upsert_delivery(delivery)
            .expect("pending delivery persists");
    }
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(3));
    let cancellation_barrier = barrier.clone();
    let cancellation_path = path.clone();
    let cancellation = std::thread::spawn(move || {
        let queue =
            SqliteCallbackDeliveryQueue::open(cancellation_path).expect("cancellation queue opens");
        cancellation_barrier.wait();
        queue.cancel_pending_for_subscription("sub-1", "subscription_revoked")
    });
    let update_barrier = barrier.clone();
    let update_path = path.clone();
    let updates = std::thread::spawn(move || {
        let queue = SqliteCallbackDeliveryQueue::open(update_path).expect("worker queue opens");
        update_barrier.wait();
        std::thread::sleep(Duration::from_millis(1));
        let claimed = queue
            .claim_due_deliveries(2_000, 5_000, 512)
            .expect("deliveries claim");
        let mut committed = Vec::new();
        for claim in claimed {
            let mut delivery = claim.delivery.clone();
            delivery.status = CallbackDeliveryStatus::Delivered;
            delivery.delivered_at_unix_ms = Some(2_500);
            if queue
                .complete_claimed_delivery(&claim, delivery.clone())
                .is_ok()
            {
                committed.push(delivery.delivery_id);
            }
        }
        committed
    });

    barrier.wait();
    cancellation
        .join()
        .expect("cancellation worker joins")
        .expect("cancellation succeeds");
    let committed = updates.join().expect("delivery worker joins");
    let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue reopens");
    for delivery_id in committed {
        assert_eq!(
            queue
                .get_delivery(&delivery_id)
                .expect("delivery loads")
                .expect("delivery exists")
                .status,
            CallbackDeliveryStatus::Delivered,
            "cancellation must not overwrite a concurrent terminal commit",
        );
    }
}

#[test]
fn webhook_delivery_worker_signs_due_delivery_and_persists_success() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    queue.upsert_delivery(delivery).expect("delivery persists");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");
    let target = WebhookDeliveryTarget::new(
        "https://hooks.example.com/graphblocks/events",
        &WebhookEgressPolicy::default_deny_internal(),
    )
    .expect("target is valid");
    let worker = WebhookDeliveryWorker::new(&scheduler, &queue, &target, &signing);

    let attempts = worker
        .process_due(
            2_000,
            10,
            |attempt: &WebhookDeliveryAttempt| {
                assert_eq!(
                    attempt.target.url,
                    "https://hooks.example.com/graphblocks/events"
                );
                assert_eq!(attempt.delivery.delivery_id, "del_sub-1_event-1");
                assert_eq!(
                    attempt
                        .signed
                        .headers
                        .get("GraphBlocks-Signature-Algorithm")
                        .map(String::as_str),
                    Some("hmac-sha256")
                );
                CallbackDeliveryResponse::Success
            },
            |event_id| {
                assert_eq!(event_id, "event-1");
                Some(event.clone())
            },
        )
        .expect("worker processes due delivery");
    let stored = queue
        .get_delivery("del_sub-1_event-1")
        .expect("delivery loads")
        .expect("delivery exists");

    assert_eq!(attempts, 1);
    assert_eq!(stored.status, CallbackDeliveryStatus::Delivered);
    assert_eq!(stored.delivered_at_unix_ms, Some(2_000));
}

#[test]
fn webhook_delivery_worker_marks_delivery_in_flight_before_transport() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    queue.upsert_delivery(delivery).expect("delivery persists");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");
    let target = WebhookDeliveryTarget::new(
        "https://hooks.example.com/graphblocks/events",
        &WebhookEgressPolicy::default_deny_internal(),
    )
    .expect("target is valid");
    let worker = WebhookDeliveryWorker::new(&scheduler, &queue, &target, &signing);

    worker
        .process_due(
            2_000,
            10,
            |_| {
                let in_flight = queue
                    .get_delivery("del_sub-1_event-1")
                    .expect("delivery loads during transport")
                    .expect("delivery exists during transport");
                assert_eq!(in_flight.status, CallbackDeliveryStatus::Delivering);
                CallbackDeliveryResponse::Success
            },
            |_| Some(event.clone()),
        )
        .expect("worker processes due delivery");
}

#[test]
fn webhook_delivery_worker_persists_retry_after_server_error() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    queue.upsert_delivery(delivery).expect("delivery persists");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");
    let target = WebhookDeliveryTarget::new(
        "https://hooks.example.com/graphblocks/events",
        &WebhookEgressPolicy::default_deny_internal(),
    )
    .expect("target is valid");
    let worker = WebhookDeliveryWorker::new(&scheduler, &queue, &target, &signing);

    let attempts = worker
        .process_due(
            2_000,
            10,
            |_| CallbackDeliveryResponse::ServerError(503),
            |_| Some(event.clone()),
        )
        .expect("worker processes due delivery");
    let stored = queue
        .get_delivery("del_sub-1_event-1")
        .expect("delivery loads")
        .expect("delivery exists");

    assert_eq!(attempts, 1);
    assert_eq!(stored.status, CallbackDeliveryStatus::Pending);
    assert_eq!(stored.attempt, 2);
    assert!(
        stored
            .next_retry_at_unix_ms
            .is_some_and(|retry_at| { retry_at > 2_100 && retry_at <= 2_200 })
    );
    assert_eq!(stored.last_error.as_deref(), Some("server_error:503"));
}

#[test]
#[allow(
    clippy::panic,
    reason = "the transport callback must remain unreachable for rejected oversized payloads"
)]
fn webhook_delivery_worker_returns_mandatory_terminal_run_actions() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(1, 100, 1_000));
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");
    let subscription = subscription(EventFilter::new(), CallbackFailurePolicy::PauseRunOnFailure);
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    queue.upsert_delivery(delivery).expect("delivery persists");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");
    let target = WebhookDeliveryTarget::new(
        "https://hooks.example.com/graphblocks/events",
        &WebhookEgressPolicy::default_deny_internal(),
    )
    .expect("target is valid");
    let worker = WebhookDeliveryWorker::new(&scheduler, &queue, &target, &signing);

    let outcome = worker
        .process_due_with_run_actions(
            2_000,
            10,
            |_| CallbackDeliveryResponse::ClientError(403),
            |_| Some(event.clone()),
        )
        .expect("worker processes due delivery");

    assert_eq!(outcome.attempts, 1);
    assert_eq!(
        outcome.run_actions,
        vec![CallbackDeliveryRunAction::PauseRun {
            run_id: "run-1".to_owned(),
            subscription_id: "sub-1".to_owned(),
            delivery_id: "del_sub-1_event-1".to_owned(),
            reason: "client_error:403".to_owned(),
        }]
    );

    let oversized_queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");
    let oversized_event = ApplicationProtocolEvent::new(
        ApplicationProtocolEventKind::ReviewRequested,
        ApplicationProtocolEventMetadata {
            event_id: "event-oversized-action".to_owned(),
            protocol_version: "graphblocks.app.v1".to_owned(),
            run_id: "run-1".to_owned(),
            release_id: "release-1".to_owned(),
            turn_id: None,
            operation_id: None,
            sequence: 11,
            cursor: Some("cursor-11".to_owned()),
            occurred_at_unix_ms: 1_011,
        },
        json!({"message": "x".repeat(512)}),
    )
    .expect("event is valid");
    let oversized_delivery = scheduler
        .schedule_event(&subscription, &oversized_event)
        .expect("delivery schedules");
    oversized_queue
        .upsert_delivery(oversized_delivery)
        .expect("delivery persists");
    let tiny_target = WebhookDeliveryTarget::new(
        "https://hooks.example.com/graphblocks/events",
        &WebhookEgressPolicy::default_deny_internal(),
    )
    .expect("target is valid")
    .with_max_payload_bytes(128)
    .expect("payload limit is valid");
    let oversized_worker =
        WebhookDeliveryWorker::new(&scheduler, &oversized_queue, &tiny_target, &signing);

    let oversized_outcome = oversized_worker
        .process_due_with_run_actions(
            2_000,
            10,
            |_| panic!("oversized payload must not be sent"),
            |_| Some(oversized_event.clone()),
        )
        .expect("worker records oversized payload failure");

    assert_eq!(oversized_outcome.attempts, 1);
    assert_eq!(oversized_outcome.run_actions.len(), 1);
    assert!(matches!(
        &oversized_outcome.run_actions[0],
        CallbackDeliveryRunAction::PauseRun {
            delivery_id,
            reason,
            ..
        } if delivery_id == "del_sub-1_event-oversized-action"
            && reason.starts_with("payload_too_large:")
    ));
}

#[test]
fn webhook_http_transport_blocks_delivery_when_dns_resolution_is_unsafe() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");
    let target = WebhookDeliveryTarget::new(
        "https://hooks.example.com/graphblocks/events",
        &WebhookEgressPolicy::default_deny_internal(),
    )
    .expect("target is valid");
    let signed = signing
        .sign_delivery_for_target(&target, &delivery, &event, 2_000)
        .expect("delivery signs");
    let attempt = WebhookDeliveryAttempt {
        target,
        delivery,
        signed,
    };
    let transport = WebhookHttpTransport::new(WebhookEgressPolicy::default_deny_internal());
    let mut sent = false;

    let response = transport.deliver_with(
        &attempt,
        |_| Ok::<Vec<IpAddr>, ()>(vec![IpAddr::V4(Ipv4Addr::new(169, 254, 169, 254))]),
        |_, _| {
            sent = true;
            Ok::<WebhookHttpResponse, ()>(WebhookHttpResponse::new(200))
        },
    );

    assert_eq!(response, CallbackDeliveryResponse::ClientError(403));
    assert!(!sent, "unsafe DNS resolution must stop before send");
}

#[test]
fn webhook_http_transport_passes_validated_addresses_to_sender() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");
    let target = WebhookDeliveryTarget::new(
        "https://hooks.example.com/graphblocks/events",
        &WebhookEgressPolicy::default_deny_internal(),
    )
    .expect("target is valid");
    let signed = signing
        .sign_delivery_for_target(&target, &delivery, &event, 2_000)
        .expect("delivery signs");
    let attempt = WebhookDeliveryAttempt {
        target,
        delivery,
        signed,
    };
    let transport = WebhookHttpTransport::new(WebhookEgressPolicy::default_deny_internal());
    let resolved = vec![IpAddr::V4(Ipv4Addr::new(93, 184, 216, 34))];

    let response = transport.deliver_with(
        &attempt,
        |_| Ok::<Vec<IpAddr>, ()>(resolved.clone()),
        |_, addresses| {
            assert_eq!(addresses, resolved.as_slice());
            Ok::<WebhookHttpResponse, ()>(WebhookHttpResponse::new(200))
        },
    );

    assert_eq!(response, CallbackDeliveryResponse::Success);
}

#[test]
fn webhook_http_transport_retries_when_dns_resolution_returns_no_addresses() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");
    let target = WebhookDeliveryTarget::new(
        "https://hooks.example.com/graphblocks/events",
        &WebhookEgressPolicy::default_deny_internal(),
    )
    .expect("target is valid");
    let signed = signing
        .sign_delivery_for_target(&target, &delivery, &event, 2_000)
        .expect("delivery signs");
    let attempt = WebhookDeliveryAttempt {
        target,
        delivery,
        signed,
    };
    let transport = WebhookHttpTransport::new(WebhookEgressPolicy::default_deny_internal());
    let mut sent = false;

    let response = transport.deliver_with(
        &attempt,
        |_| Ok::<Vec<IpAddr>, ()>(Vec::new()),
        |_, _| {
            sent = true;
            Ok::<WebhookHttpResponse, ()>(WebhookHttpResponse::new(200))
        },
    );

    assert_eq!(response, CallbackDeliveryResponse::ServerError(599));
    assert!(!sent, "empty DNS resolution must stop before send");
}

#[test]
fn webhook_http_transport_maps_receiver_status_codes_to_delivery_responses() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");
    let target = WebhookDeliveryTarget::new(
        "https://hooks.example.com/graphblocks/events",
        &WebhookEgressPolicy::default_deny_internal(),
    )
    .expect("target is valid");
    let signed = signing
        .sign_delivery_for_target(&target, &delivery, &event, 2_000)
        .expect("delivery signs");
    let attempt = WebhookDeliveryAttempt {
        target,
        delivery,
        signed,
    };
    let transport = WebhookHttpTransport::new(WebhookEgressPolicy::default_deny_internal());

    for (status, retry_after_ms, expected) in [
        (200, None, CallbackDeliveryResponse::Success),
        (202, None, CallbackDeliveryResponse::Success),
        (
            409,
            None,
            CallbackDeliveryResponse::DuplicateAlreadyProcessed,
        ),
        (410, None, CallbackDeliveryResponse::TargetGone),
        (
            429,
            Some(1_500),
            CallbackDeliveryResponse::RateLimited {
                retry_after_ms: Some(1_500),
            },
        ),
        (503, None, CallbackDeliveryResponse::ServerError(503)),
        (404, None, CallbackDeliveryResponse::ClientError(404)),
    ] {
        let response = transport.deliver_with(
            &attempt,
            |_| Ok::<Vec<IpAddr>, ()>(vec![IpAddr::V4(Ipv4Addr::new(93, 184, 216, 34))]),
            |request, addresses| {
                assert_eq!(addresses, [IpAddr::V4(Ipv4Addr::new(93, 184, 216, 34))]);
                assert_eq!(request.method, "POST");
                assert_eq!(request.url, "https://hooks.example.com/graphblocks/events");
                assert_eq!(
                    request
                        .headers
                        .get("GraphBlocks-Delivery-Id")
                        .map(String::as_str),
                    Some("del_sub-1_event-1")
                );
                Ok::<WebhookHttpResponse, ()>(WebhookHttpResponse {
                    status,
                    retry_after_ms,
                })
            },
        );
        assert_eq!(response, expected);
    }
}

#[test]
fn webhook_envelope_signing_adds_required_headers_and_verifies() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");

    let signed = signing
        .sign_delivery(&delivery, &event, 2_000)
        .expect("delivery signs");

    assert_eq!(
        signed
            .headers
            .get("GraphBlocks-Delivery-Id")
            .map(String::as_str),
        Some("del_sub-1_event-1")
    );
    assert_eq!(
        signed
            .headers
            .get("GraphBlocks-Event-Id")
            .map(String::as_str),
        Some("event-1")
    );
    assert_eq!(
        signed.headers.get("GraphBlocks-Run-Id").map(String::as_str),
        Some("run-1")
    );
    assert_eq!(
        signed
            .headers
            .get("GraphBlocks-Idempotency-Key")
            .map(String::as_str),
        Some("sub-1:event-1")
    );
    assert_eq!(
        signed
            .headers
            .get("GraphBlocks-Signature-Algorithm")
            .map(String::as_str),
        Some("hmac-sha256")
    );
    signing
        .verify_signed_delivery(&signed, 2_050)
        .expect("fresh signature verifies");
}

#[test]
fn webhook_signing_resolves_registered_secret_without_leaking_raw_value() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let mut provider = InMemorySecretProvider::new("test-secrets");
    provider.insert(
        SecretRef::new("secret://callbacks/ide-relay").with_version("2026-07"),
        "registered-secret-value",
    );

    let signing = WebhookSigningConfig::hmac_sha256_registered_secret(
        SecretRef::new("secret://callbacks/ide-relay").with_version("2026-07"),
        &provider,
        "callback.delivery.worker",
        300,
    )
    .expect("registered secret resolves");
    let signed = signing
        .sign_delivery(&delivery, &event, 2_000)
        .expect("delivery signs");

    signing
        .verify_signed_delivery(&signed, 2_050)
        .expect("fresh signature verifies");
    assert_eq!(signing.secret_ref, "secret://callbacks/ide-relay");
    assert_eq!(signing.secret_version.as_deref(), Some("2026-07"));
    assert_eq!(
        signing.secret_provider_kind.as_deref(),
        Some("test-secrets")
    );
    let debug = format!("{signing:?}");
    assert!(debug.contains("<redacted>"));
    assert!(debug.contains("secret://callbacks/ide-relay"));
    assert!(!debug.contains("registered-secret-value"));
}

#[test]
fn webhook_signing_rejects_missing_registered_secret() {
    let provider = InMemorySecretProvider::new("test-secrets");

    assert_eq!(
        WebhookSigningConfig::hmac_sha256_registered_secret(
            SecretRef::new("secret://callbacks/missing"),
            &provider,
            "callback.delivery.worker",
            300,
        ),
        Err(WebhookSignatureError::SecretResolution(
            SecretProviderError::NotFound {
                uri: "secret://callbacks/missing".to_owned(),
                version: None,
            }
        ))
    );
}

#[test]
fn webhook_envelope_signing_includes_operation_id_when_present() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let mut event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    event.metadata.operation_id = Some("op-ci-1".to_owned());
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");

    let signed = signing
        .sign_delivery(&delivery, &event, 2_000)
        .expect("delivery signs");

    assert_eq!(
        signed
            .body
            .get("operation_id")
            .and_then(|value| value.as_str()),
        Some("op-ci-1")
    );
    signing
        .verify_signed_delivery(&signed, 2_050)
        .expect("fresh signature verifies");
}

#[test]
fn webhook_envelope_signing_includes_release_id() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let mut event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    event.metadata.release_id = "release-2026-07-08".to_owned();
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");

    let signed = signing
        .sign_delivery(&delivery, &event, 2_000)
        .expect("delivery signs");

    assert_eq!(
        signed
            .body
            .get("release_id")
            .and_then(|value| value.as_str()),
        Some("release-2026-07-08")
    );
    signing
        .verify_signed_delivery(&signed, 2_050)
        .expect("fresh signature verifies");
}

#[test]
fn webhook_signature_verification_rejects_stale_or_tampered_payloads() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");
    let mut signed = signing
        .sign_delivery(&delivery, &event, 2_000)
        .expect("delivery signs");

    assert_eq!(
        signing.verify_signed_delivery(&signed, 2_301),
        Err(WebhookSignatureError::TimestampOutsideReplayWindow {
            timestamp_unix_ms: 2_000,
            now_unix_ms: 2_301,
            replay_window_ms: 300,
        })
    );

    signed
        .headers
        .insert("GraphBlocks-Signature".to_owned(), "00".repeat(32));
    assert_eq!(
        signing.verify_signed_delivery(&signed, 2_050),
        Err(WebhookSignatureError::SignatureMismatch)
    );
}

#[test]
fn webhook_target_rejects_oversized_signed_payload_before_delivery() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = ApplicationProtocolEvent::new(
        ApplicationProtocolEventKind::ReviewRequested,
        ApplicationProtocolEventMetadata {
            event_id: "event-oversized".to_owned(),
            protocol_version: "graphblocks.app.v1".to_owned(),
            run_id: "run-1".to_owned(),
            release_id: "release-1".to_owned(),
            turn_id: None,
            operation_id: None,
            sequence: 11,
            cursor: Some("cursor-11".to_owned()),
            occurred_at_unix_ms: 1_011,
        },
        json!({"message": "x".repeat(512)}),
    )
    .expect("event is valid");
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");
    let target = WebhookDeliveryTarget::new(
        "https://hooks.example.com/graphblocks/events",
        &WebhookEgressPolicy::default_deny_internal(),
    )
    .expect("target is valid")
    .with_max_payload_bytes(128)
    .expect("payload limit is valid");

    let error = signing
        .sign_delivery_for_target(&target, &delivery, &event, 2_000)
        .expect_err("oversized payload is rejected");

    assert!(matches!(
        error,
        WebhookSignatureError::PayloadTooLarge {
            max_payload_bytes: 128,
            actual_payload_bytes
        } if actual_payload_bytes > 128
    ));
}

#[test]
#[allow(
    clippy::panic,
    reason = "the transport callback must remain unreachable for rejected oversized payloads"
)]
fn webhook_delivery_worker_persists_oversized_payload_failure() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = ApplicationProtocolEvent::new(
        ApplicationProtocolEventKind::ReviewRequested,
        ApplicationProtocolEventMetadata {
            event_id: "event-oversized".to_owned(),
            protocol_version: "graphblocks.app.v1".to_owned(),
            run_id: "run-1".to_owned(),
            release_id: "release-1".to_owned(),
            turn_id: None,
            operation_id: None,
            sequence: 11,
            cursor: Some("cursor-11".to_owned()),
            occurred_at_unix_ms: 1_011,
        },
        json!({"message": "x".repeat(512)}),
    )
    .expect("event is valid");
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    queue.upsert_delivery(delivery).expect("delivery persists");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");
    let target = WebhookDeliveryTarget::new(
        "https://hooks.example.com/graphblocks/events",
        &WebhookEgressPolicy::default_deny_internal(),
    )
    .expect("target is valid")
    .with_max_payload_bytes(128)
    .expect("payload limit is valid");
    let worker = WebhookDeliveryWorker::new(&scheduler, &queue, &target, &signing);

    let attempts = worker
        .process_due(
            2_000,
            10,
            |_| panic!("oversized payload must not be sent"),
            |_| Some(event.clone()),
        )
        .expect("worker records oversized payload failure");
    let stored = queue
        .get_delivery("del_sub-1_event-oversized")
        .expect("delivery loads")
        .expect("delivery exists");

    assert_eq!(attempts, 1);
    assert_eq!(stored.status, CallbackDeliveryStatus::Failed);
    assert_eq!(stored.next_retry_at_unix_ms, None);
    assert!(
        stored
            .last_error
            .as_deref()
            .is_some_and(|error| error.contains("payload_too_large")),
        "unexpected error: {:?}",
        stored.last_error
    );
}

#[test]
fn webhook_target_default_payload_limit_allows_normal_signed_payload() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    let signing =
        WebhookSigningConfig::hmac_sha256("secret://callbacks/ide-relay", b"top-secret", 300)
            .expect("signing config is valid");
    let target = WebhookDeliveryTarget::new(
        "https://hooks.example.com/graphblocks/events",
        &WebhookEgressPolicy::default_deny_internal(),
    )
    .expect("target is valid");

    let signed = signing
        .sign_delivery_for_target(&target, &delivery, &event, 2_000)
        .expect("normal payload signs under default limit");

    assert!(
        signed.body_size_bytes() <= target.max_payload_bytes,
        "signed webhook body should fit target payload limit"
    );
}

#[test]
fn subscription_replay_schedules_matching_events_after_cursor() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let mut subscription = subscription(
        EventFilter::new()
            .with_types([ApplicationProtocolEventKind::ReviewRequested])
            .with_terminal_events(true),
        CallbackFailurePolicy::RetryThenDeadLetter,
    )
    .with_replay_from_cursor("cursor-1")
    .expect("replay cursor is valid");
    let mut log = ApplicationProtocolLog::new();
    log.append(protocol_event(
        "event-1",
        ApplicationProtocolEventKind::RunStarted,
        1,
    ))
    .expect("event appends");
    log.append(protocol_event(
        "event-2",
        ApplicationProtocolEventKind::ReviewRequested,
        2,
    ))
    .expect("event appends");
    log.append(protocol_event(
        "event-3",
        ApplicationProtocolEventKind::JobProgress,
        3,
    ))
    .expect("event appends");
    log.append(protocol_event(
        "event-4",
        ApplicationProtocolEventKind::RunCompleted,
        4,
    ))
    .expect("event appends");

    let deliveries = scheduler.schedule_replay(&subscription, &log, 10);

    assert_eq!(
        deliveries
            .iter()
            .map(|delivery| delivery.event_id.as_str())
            .collect::<Vec<_>>(),
        vec!["event-2", "event-4"]
    );
    assert_eq!(deliveries[0].delivery_id, "del_sub-1_event-2");
    assert_eq!(deliveries[0].cursor, "cursor-2");
    assert_eq!(deliveries[1].idempotency_key, "sub-1:event-4");

    subscription.replay_from_cursor = Some("cursor-2".to_owned());
    let deliveries = scheduler.schedule_replay(&subscription, &log, 10);
    assert_eq!(
        deliveries
            .iter()
            .map(|delivery| delivery.event_id.as_str())
            .collect::<Vec<_>>(),
        vec!["event-4"]
    );
}

#[test]
fn subscription_replay_with_unknown_cursor_schedules_no_deliveries() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new().with_types([ApplicationProtocolEventKind::ReviewRequested]),
        CallbackFailurePolicy::RetryThenDeadLetter,
    )
    .with_replay_from_cursor("cursor-missing")
    .expect("replay cursor is valid");
    let mut log = ApplicationProtocolLog::new();
    log.append(protocol_event(
        "event-1",
        ApplicationProtocolEventKind::ReviewRequested,
        1,
    ))
    .expect("event appends");
    log.append(protocol_event(
        "event-2",
        ApplicationProtocolEventKind::ReviewRequested,
        2,
    ))
    .expect("event appends");

    let deliveries = scheduler.schedule_replay(&subscription, &log, 10);

    assert!(
        deliveries.is_empty(),
        "callback replay must not treat an unknown cursor as replay-from-beginning"
    );
}

#[test]
fn ordered_subscription_replay_schedules_only_first_unblocked_event() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new().with_types([ApplicationProtocolEventKind::JobProgress]),
        CallbackFailurePolicy::RetryThenDeadLetter,
    )
    .with_ordered_delivery();
    let mut log = ApplicationProtocolLog::new();
    log.append(protocol_event(
        "event-1",
        ApplicationProtocolEventKind::JobProgress,
        1,
    ))
    .expect("event appends");
    log.append(protocol_event(
        "event-2",
        ApplicationProtocolEventKind::JobProgress,
        2,
    ))
    .expect("event appends");
    log.append(protocol_event(
        "event-3",
        ApplicationProtocolEventKind::JobProgress,
        3,
    ))
    .expect("event appends");

    let deliveries = scheduler.schedule_replay(&subscription, &log, 10);

    assert_eq!(
        deliveries
            .iter()
            .map(|delivery| delivery.event_id.as_str())
            .collect::<Vec<_>>(),
        vec!["event-1"]
    );
}

#[test]
fn subscription_replay_respects_limit_and_inactive_subscriptions() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let mut subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    )
    .with_replay_from_cursor("cursor-1")
    .expect("replay cursor is valid");
    let mut log = ApplicationProtocolLog::new();
    for sequence in 1..=4 {
        log.append(protocol_event(
            &format!("event-{sequence}"),
            ApplicationProtocolEventKind::ReviewRequested,
            sequence,
        ))
        .expect("event appends");
    }

    assert_eq!(scheduler.schedule_replay(&subscription, &log, 2).len(), 2);

    subscription.status = CallbackSubscriptionStatus::Revoked;
    assert!(
        scheduler
            .schedule_replay(&subscription, &log, 10)
            .is_empty()
    );
}

#[test]
fn webhook_target_rejects_forbidden_internal_endpoints_by_default() {
    let policy = WebhookEgressPolicy::default_deny_internal();

    for url in [
        "http://localhost/callback",
        "https://127.0.0.1/callback",
        "https://10.0.0.8/callback",
        "https://172.16.0.1/callback",
        "https://192.168.1.10/callback",
        "https://169.254.169.254/latest/meta-data",
        "file:///tmp/callback",
        "unix:///var/run/socket",
    ] {
        assert!(
            matches!(
                WebhookDeliveryTarget::new(url, &policy),
                Err(WebhookEndpointError::UnsafeEndpoint { .. })
                    | Err(WebhookEndpointError::UnsupportedScheme { .. })
            ),
            "{url} should be rejected"
        );
    }
}

#[test]
fn webhook_target_rejects_reserved_ipv4_destinations_by_default() {
    let policy = WebhookEgressPolicy::default_deny_internal();

    for url in [
        "https://224.0.0.1/callback",
        "https://239.255.255.250/callback",
        "https://255.255.255.255/callback",
    ] {
        assert!(
            matches!(
                WebhookDeliveryTarget::new(url, &policy),
                Err(WebhookEndpointError::UnsafeEndpoint { .. })
            ),
            "{url} should be rejected before delivery"
        );
    }
}

#[test]
fn webhook_target_rejects_alternate_numeric_loopback_literals() {
    let policy = WebhookEgressPolicy::default_deny_internal();

    for url in ["http://2130706433/callback", "http://0x7f000001/callback"] {
        assert_eq!(
            WebhookDeliveryTarget::new(url, &policy),
            Err(WebhookEndpointError::UnsafeEndpoint {
                host: url
                    .trim_start_matches("http://")
                    .trim_end_matches("/callback")
                    .to_owned()
            }),
            "{url} should be rejected before delivery"
        );
    }
}

#[test]
fn webhook_target_rejects_ipv4_mapped_ipv6_internal_literals() {
    let policy = WebhookEgressPolicy::default_deny_internal();

    for url in [
        "http://[::ffff:127.0.0.1]/callback",
        "http://[::ffff:169.254.169.254]/callback",
        "http://[::ffff:10.0.0.4]/callback",
    ] {
        assert!(
            matches!(
                WebhookDeliveryTarget::new(url, &policy),
                Err(WebhookEndpointError::UnsafeEndpoint { .. })
            ),
            "{url} should be rejected before delivery"
        );
    }
}

#[test]
fn webhook_target_rejects_ipv4_compatible_ipv6_internal_literals() {
    let policy = WebhookEgressPolicy::default_deny_internal();

    for url in [
        "http://[::127.0.0.1]/callback",
        "http://[::169.254.169.254]/callback",
        "http://[::10.0.0.4]/callback",
    ] {
        assert!(
            matches!(
                WebhookDeliveryTarget::new(url, &policy),
                Err(WebhookEndpointError::UnsafeEndpoint { .. })
            ),
            "{url} should be rejected before delivery"
        );
    }
}

#[test]
fn webhook_target_rejects_ipv6_multicast_destinations_by_default() {
    let policy = WebhookEgressPolicy::default_deny_internal();

    for url in ["https://[ff02::1]/callback", "https://[ff05::2]/callback"] {
        assert!(
            matches!(
                WebhookDeliveryTarget::new(url, &policy),
                Err(WebhookEndpointError::UnsafeEndpoint { .. })
            ),
            "{url} should be rejected before delivery"
        );
    }
}

#[test]
fn webhook_target_rejects_scoped_ipv6_literals_by_default() {
    let policy = WebhookEgressPolicy::default_deny_internal();

    for url in [
        "https://[fe80::1%25eth0]/callback",
        "https://[ff02::1%25eth0]/callback",
    ] {
        assert!(
            matches!(
                WebhookDeliveryTarget::new(url, &policy),
                Err(WebhookEndpointError::UnsafeEndpoint { .. })
                    | Err(WebhookEndpointError::MalformedUrl)
            ),
            "{url} should be rejected before delivery"
        );
    }
}

#[test]
fn webhook_target_accepts_public_https_and_explicit_allowlist() {
    let policy = WebhookEgressPolicy::default_deny_internal();
    let target =
        WebhookDeliveryTarget::new("https://hooks.example.com/graphblocks/events", &policy)
            .expect("public https endpoint is valid");

    assert_eq!(target.url, "https://hooks.example.com/graphblocks/events");
    assert_eq!(target.host, "hooks.example.com");
    assert_eq!(target.scheme, "https");

    let policy = WebhookEgressPolicy::default_deny_internal().with_allowed_host("localhost");
    let local = WebhookDeliveryTarget::new("http://localhost:8080/events", &policy)
        .expect("allowlisted local endpoint is valid");
    assert_eq!(local.host, "localhost");
}

#[test]
fn webhook_egress_policy_rejects_public_hostname_resolving_to_internal_addresses() {
    let policy = WebhookEgressPolicy::default_deny_internal();
    let target =
        WebhookDeliveryTarget::new("https://hooks.example.com/graphblocks/events", &policy)
            .expect("public hostname syntax is valid before DNS resolution");

    assert_eq!(
        policy.validate_resolved_addresses(
            &target,
            [
                IpAddr::V4(Ipv4Addr::new(93, 184, 216, 34)),
                IpAddr::V4(Ipv4Addr::new(169, 254, 169, 254)),
            ],
        ),
        Err(WebhookEndpointError::UnsafeResolvedAddress {
            host: "hooks.example.com".to_owned(),
            address: IpAddr::V4(Ipv4Addr::new(169, 254, 169, 254)),
        })
    );
    assert_eq!(
        policy.validate_resolved_addresses(&target, [IpAddr::V4(Ipv4Addr::new(10, 0, 0, 4))],),
        Err(WebhookEndpointError::UnsafeResolvedAddress {
            host: "hooks.example.com".to_owned(),
            address: IpAddr::V4(Ipv4Addr::new(10, 0, 0, 4)),
        })
    );
}

#[test]
fn webhook_target_rejects_non_global_special_addresses_by_default() {
    let policy = WebhookEgressPolicy::default_deny_internal();

    for url in [
        "https://100.64.0.1/callback",
        "https://198.18.0.1/callback",
        "https://192.0.2.1/callback",
        "https://[2001:db8::1]/callback",
        "https://[64:ff9b:1::1]/callback",
    ] {
        assert!(
            matches!(
                WebhookDeliveryTarget::new(url, &policy),
                Err(WebhookEndpointError::UnsafeEndpoint { .. })
            ),
            "{url} should be rejected before delivery",
        );
    }
}

#[test]
fn webhook_egress_policy_rejects_non_global_special_resolved_addresses() {
    let policy = WebhookEgressPolicy::default_deny_internal();
    let target =
        WebhookDeliveryTarget::new("https://hooks.example.com/graphblocks/events", &policy)
            .expect("public hostname syntax is valid before DNS resolution");

    for address in [
        IpAddr::V4(Ipv4Addr::new(100, 64, 0, 1)),
        IpAddr::V4(Ipv4Addr::new(198, 18, 0, 1)),
        IpAddr::V4(Ipv4Addr::new(192, 0, 2, 1)),
        IpAddr::V6(Ipv6Addr::new(0x2001, 0x0db8, 0, 0, 0, 0, 0, 1)),
        IpAddr::V6(Ipv6Addr::new(0x0064, 0xff9b, 1, 0, 0, 0, 0, 1)),
    ] {
        assert_eq!(
            policy.validate_resolved_addresses(&target, [address]),
            Err(WebhookEndpointError::UnsafeResolvedAddress {
                host: "hooks.example.com".to_owned(),
                address,
            })
        );
    }
}

#[test]
fn webhook_egress_policy_rejects_public_hostname_resolving_to_reserved_ipv4_addresses() {
    let policy = WebhookEgressPolicy::default_deny_internal();
    let target =
        WebhookDeliveryTarget::new("https://hooks.example.com/graphblocks/events", &policy)
            .expect("public hostname syntax is valid before DNS resolution");

    for address in [
        IpAddr::V4(Ipv4Addr::new(224, 0, 0, 1)),
        IpAddr::V4(Ipv4Addr::new(255, 255, 255, 255)),
    ] {
        assert_eq!(
            policy.validate_resolved_addresses(&target, [address]),
            Err(WebhookEndpointError::UnsafeResolvedAddress {
                host: "hooks.example.com".to_owned(),
                address,
            })
        );
    }
}

#[test]
fn webhook_egress_policy_rejects_public_hostname_resolving_to_ipv6_multicast() {
    let policy = WebhookEgressPolicy::default_deny_internal();
    let target =
        WebhookDeliveryTarget::new("https://hooks.example.com/graphblocks/events", &policy)
            .expect("public hostname syntax is valid before DNS resolution");
    let multicast_address = IpAddr::V6(Ipv6Addr::new(0xff02, 0, 0, 0, 0, 0, 0, 1));

    assert_eq!(
        policy.validate_resolved_addresses(&target, [multicast_address]),
        Err(WebhookEndpointError::UnsafeResolvedAddress {
            host: "hooks.example.com".to_owned(),
            address: multicast_address,
        })
    );
}

#[test]
fn webhook_egress_policy_rejects_resolved_ipv4_mapped_ipv6_internal_addresses() {
    let policy = WebhookEgressPolicy::default_deny_internal();
    let target =
        WebhookDeliveryTarget::new("https://hooks.example.com/graphblocks/events", &policy)
            .expect("public hostname syntax is valid before DNS resolution");
    let metadata_address = IpAddr::V6(Ipv6Addr::new(0, 0, 0, 0, 0, 0xffff, 0xa9fe, 0xa9fe));

    assert_eq!(
        policy.validate_resolved_addresses(&target, [metadata_address]),
        Err(WebhookEndpointError::UnsafeResolvedAddress {
            host: "hooks.example.com".to_owned(),
            address: metadata_address,
        })
    );
}

#[test]
fn webhook_egress_policy_rejects_resolved_ipv4_compatible_ipv6_internal_addresses() {
    let policy = WebhookEgressPolicy::default_deny_internal();
    let target =
        WebhookDeliveryTarget::new("https://hooks.example.com/graphblocks/events", &policy)
            .expect("public hostname syntax is valid before DNS resolution");
    let metadata_address = IpAddr::V6(Ipv6Addr::new(0, 0, 0, 0, 0, 0, 0xa9fe, 0xa9fe));

    assert_eq!(
        policy.validate_resolved_addresses(&target, [metadata_address]),
        Err(WebhookEndpointError::UnsafeResolvedAddress {
            host: "hooks.example.com".to_owned(),
            address: metadata_address,
        })
    );
}

#[test]
fn webhook_egress_policy_allows_safe_resolved_addresses_and_explicit_host_allowlist() {
    let policy = WebhookEgressPolicy::default_deny_internal();
    let target =
        WebhookDeliveryTarget::new("https://hooks.example.com/graphblocks/events", &policy)
            .expect("public hostname is valid");

    policy
        .validate_resolved_addresses(
            &target,
            [
                IpAddr::V4(Ipv4Addr::new(93, 184, 216, 34)),
                IpAddr::V4(Ipv4Addr::new(8, 8, 8, 8)),
            ],
        )
        .expect("public resolved addresses are allowed");

    let policy =
        WebhookEgressPolicy::default_deny_internal().with_allowed_host("hooks.example.com");
    policy
        .validate_resolved_addresses(&target, [IpAddr::V4(Ipv4Addr::new(10, 0, 0, 4))])
        .expect("explicit host allowlist permits private resolved addresses");
}

#[test]
fn webhook_target_rejects_malformed_or_empty_urls() {
    let policy = WebhookEgressPolicy::default_deny_internal();

    assert_eq!(
        WebhookDeliveryTarget::new(" ", &policy),
        Err(WebhookEndpointError::EmptyUrl)
    );
    assert_eq!(
        WebhookDeliveryTarget::new("https:///missing-host", &policy),
        Err(WebhookEndpointError::MissingHost)
    );
    assert_eq!(
        WebhookDeliveryTarget::new("not-a-url", &policy),
        Err(WebhookEndpointError::MalformedUrl)
    );
    for url in [
        "https://hooks.example.com:/events",
        "https://hooks.example.com:abc/events",
        "https://hooks.example.com:65536/events",
        "https://[2001:4860:4860::8888]:abc/events",
        "https://[2001:4860:4860::8888]:/events",
        "https://[not-ipv6]/events",
        "https://hooks example.com/events",
        "https://hooks.example.com\t/events",
        "https://hooks.example.com%2fevil.test/events",
    ] {
        assert_eq!(
            WebhookDeliveryTarget::new(url, &policy),
            Err(WebhookEndpointError::MalformedUrl),
            "{url} should reject malformed port syntax"
        );
    }
    assert_eq!(
        WebhookDeliveryTarget::new("https://hooks.example.com/events", &policy)
            .expect("target is valid")
            .with_max_payload_bytes(0),
        Err(WebhookEndpointError::InvalidPayloadLimit {
            max_payload_bytes: 0
        })
    );
}

#[test]
fn callback_delivery_targets_preserve_typed_contract_and_ordering_capability() {
    let webhook = CallbackDeliveryTarget::webhook("https://hooks.example.com/events");
    assert_eq!(webhook.kind(), "webhook");
    assert_eq!(
        webhook.address(),
        "webhook:https://hooks.example.com/events"
    );
    assert!(webhook.supports_ordered_delivery());

    let websocket =
        CallbackDeliveryTarget::websocket("conn-1", true).expect("websocket target is valid");
    assert_eq!(websocket.kind(), "websocket");
    assert_eq!(websocket.address(), "websocket:conn-1");
    assert!(websocket.supports_ordered_delivery());

    let sse = CallbackDeliveryTarget::sse("stream-1", true).expect("sse target is valid");
    assert_eq!(sse.kind(), "sse");
    assert_eq!(sse.address(), "sse:stream-1");
    assert!(sse.supports_ordered_delivery());

    let local = CallbackDeliveryTarget::local_callback("test-hook", true)
        .expect("local callback target is valid");
    assert_eq!(local.kind(), "local_callback");
    assert_eq!(local.address(), "local_callback:test-hook");
    assert!(!local.supports_ordered_delivery());

    let email = CallbackDeliveryTarget::email("ops@example.com").expect("email target is valid");
    assert_eq!(email.kind(), "email");
    assert_eq!(email.address(), "email:ops@example.com");
    assert!(!email.supports_ordered_delivery());
}

#[test]
fn callback_subscription_accepts_typed_delivery_target() {
    let subscription = CallbackSubscription::new_with_target(
        "sub-ws",
        "principal:ide",
        "run",
        "run-1",
        EventFilter::new(),
        CallbackDeliveryTarget::websocket("conn-1", true).expect("target is valid"),
        CallbackFailurePolicy::RetryThenDeadLetter,
        1_000,
    )
    .expect("subscription is valid")
    .with_dead_letter_behavior()
    .with_ordered_delivery();

    assert_eq!(subscription.delivery_target.kind(), "websocket");
    assert_eq!(subscription.delivery_target.address(), "websocket:conn-1");
    assert!(CallbackConfigurationDiagnostic::subscription(&subscription).is_empty());
}

#[test]
fn callback_diagnostics_report_webhook_without_authentication() {
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );

    let diagnostics =
        CallbackConfigurationDiagnostic::webhook_subscription(&subscription, None, None);

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6002");
    assert_eq!(
        diagnostics[0].message,
        "callback subscription sub-1 uses webhook delivery without signing"
    );
}

#[test]
fn callback_diagnostics_map_unsafe_endpoint_to_compiler_code() {
    let policy = WebhookEgressPolicy::default_deny_internal();
    let endpoint_error = WebhookDeliveryTarget::new("http://127.0.0.1/events", &policy)
        .expect_err("loopback webhook target is unsafe");

    let diagnostic = CallbackConfigurationDiagnostic::webhook_endpoint_error(
        "http://127.0.0.1/events",
        &endpoint_error,
    )
    .expect("unsafe endpoint maps to a diagnostic");

    assert_eq!(diagnostic.code, "GB6011");
    assert_eq!(diagnostic.field, "delivery.url");
}

#[test]
fn callback_diagnostics_map_userinfo_webhook_url_to_compiler_code() {
    let policy = WebhookEgressPolicy::default_deny_internal();
    let endpoint_error =
        WebhookDeliveryTarget::new("https://token@hooks.example.com/events", &policy)
            .expect_err("userinfo-bearing webhook target is unsafe");

    let diagnostic = CallbackConfigurationDiagnostic::webhook_endpoint_error(
        "https://token@hooks.example.com/events",
        &endpoint_error,
    )
    .expect("userinfo-bearing endpoint maps to a diagnostic");

    assert_eq!(diagnostic.code, "GB6011");
    assert_eq!(diagnostic.field, "delivery.url");
    assert!(
        diagnostic.message.contains("userinfo"),
        "diagnostic should identify the unsafe userinfo component"
    );
}

#[test]
fn callback_diagnostics_report_impossible_ordering_for_unordered_targets() {
    let subscription = CallbackSubscription::new(
        "sub-local",
        "principal:dev",
        "run",
        "run-1",
        EventFilter::new(),
        "local_callback:test-hook",
        CallbackFailurePolicy::RetryThenDeadLetter,
        900,
    )
    .expect("subscription is valid")
    .with_dead_letter_behavior()
    .with_ordered_delivery();

    let diagnostics = CallbackConfigurationDiagnostic::subscription(&subscription);

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6012");
    assert_eq!(diagnostics[0].field, "delivery.ordering");
}

#[test]
fn callback_diagnostics_report_missing_dead_letter_policy_for_retrying_callbacks() {
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );

    let diagnostics = CallbackConfigurationDiagnostic::subscription(&subscription);

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6014");
    assert_eq!(diagnostics[0].field, "failure_policy");
}

#[test]
fn callback_diagnostics_report_mandatory_callback_without_failure_policy() {
    let subscription = subscription(EventFilter::new(), CallbackFailurePolicy::BestEffort)
        .with_mandatory_delivery();

    let diagnostics = CallbackConfigurationDiagnostic::subscription(&subscription);

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6006");
    assert_eq!(diagnostics[0].field, "failure_policy");
    assert!(diagnostics[0].message.contains("sub-1"));
}

#[test]
fn callback_diagnostics_allow_mandatory_retry_then_dead_letter() {
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    )
    .with_dead_letter_behavior()
    .with_mandatory_delivery();

    assert_eq!(
        CallbackConfigurationDiagnostic::subscription(&subscription),
        Vec::new()
    );
}

#[test]
fn callback_diagnostics_report_callback_as_source_of_truth() {
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    )
    .with_dead_letter_behavior()
    .with_authoritative_use(CallbackAuthoritativeUse::Billing)
    .with_authoritative_use(CallbackAuthoritativeUse::EffectCommit);

    let diagnostics = CallbackConfigurationDiagnostic::subscription(&subscription);

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6004");
    assert_eq!(diagnostics[0].field, "authoritative_uses");
    assert!(
        diagnostics[0].message.contains("billing"),
        "diagnostic identifies the forbidden billing use"
    );
    assert!(
        diagnostics[0].message.contains("effect_commit"),
        "diagnostic identifies the forbidden effect commit use"
    );
}

#[test]
fn callback_diagnostics_allow_projection_only_subscriptions() {
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    )
    .with_dead_letter_behavior();

    let diagnostics = CallbackConfigurationDiagnostic::subscription(&subscription);

    assert!(diagnostics.is_empty());
}

use graphblocks_runtime_core::application_event::{
    ApplicationProtocolEvent, ApplicationProtocolEventKind, ApplicationProtocolEventMetadata,
    ApplicationProtocolLog,
};
use graphblocks_runtime_core::callback_delivery::{
    CallbackAuthoritativeUse, CallbackConfigurationDiagnostic, CallbackDeadLetter,
    CallbackDeliveryResponse, CallbackDeliveryRunAction, CallbackDeliveryScheduler,
    CallbackDeliveryStatus, CallbackDeliveryTarget, CallbackFailurePolicy, CallbackRetryPolicy,
    CallbackSubscription, CallbackSubscriptionStatus, EventFilter, OrderedDeliveryState,
    SqliteCallbackDeadLetterStore, SqliteCallbackDeliveryQueue, WebhookDeliveryAttempt,
    WebhookDeliveryTarget, WebhookDeliveryWorker, WebhookEgressPolicy, WebhookEndpointError,
    WebhookHttpResponse, WebhookHttpTransport, WebhookSignatureError, WebhookSigningConfig,
};
use serde_json::json;
use std::net::{IpAddr, Ipv4Addr};
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

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
            turn_id: None,
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
    assert!(retry_once
        .next_retry_at_unix_ms
        .is_some_and(|retry_at| { retry_at > 1_100 && retry_at <= 1_200 }));
    assert_eq!(retry_twice.status, CallbackDeliveryStatus::Pending);
    assert_eq!(retry_twice.attempt, 3);
    assert!(retry_twice
        .next_retry_at_unix_ms
        .is_some_and(|retry_at| { retry_at > 1_300 && retry_at <= 1_350 }));
    assert_eq!(dead_lettered.status, CallbackDeliveryStatus::DeadLettered);
    assert_eq!(dead_lettered.attempt, 3);
    assert!(dead_lettered
        .last_error
        .as_deref()
        .is_some_and(|error| { error.contains("server_error:503") }));
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

    assert!(scheduler
        .redrive_dead_letter(&dead_letter, " ", "receiver recovered", 2_000)
        .is_err());
    assert!(scheduler
        .redrive_dead_letter(&dead_letter, "operator:alice", " ", 2_000)
        .is_err());
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
fn sqlite_callback_delivery_queue_recovers_in_flight_delivery_after_worker_restart() {
    let scheduler = CallbackDeliveryScheduler::new(CallbackRetryPolicy::new(3, 100, 1_000));
    let subscription = subscription(
        EventFilter::new(),
        CallbackFailurePolicy::RetryThenDeadLetter,
    );
    let event = protocol_event("event-1", ApplicationProtocolEventKind::ReviewRequested, 1);
    let mut delivery = scheduler
        .schedule_event(&subscription, &event)
        .expect("delivery schedules");
    delivery.status = CallbackDeliveryStatus::Delivering;
    let path = sqlite_callback_delivery_queue_path("recover-in-flight");

    {
        let queue = SqliteCallbackDeliveryQueue::open(&path).expect("queue opens");
        queue
            .upsert_delivery(delivery.clone())
            .expect("in-flight delivery persists");
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
    let mut in_flight = scheduler
        .schedule_event(&subscription, &second)
        .expect("second delivery schedules");
    in_flight.status = CallbackDeliveryStatus::Delivering;
    let queue = SqliteCallbackDeliveryQueue::open_in_memory().expect("queue opens");
    queue
        .upsert_delivery(pending)
        .expect("pending delivery persists");
    queue
        .upsert_delivery(in_flight)
        .expect("in-flight delivery persists");

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
    assert!(stored
        .next_retry_at_unix_ms
        .is_some_and(|retry_at| { retry_at > 2_100 && retry_at <= 2_200 }));
    assert_eq!(stored.last_error.as_deref(), Some("server_error:503"));
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
        |_| {
            sent = true;
            Ok::<WebhookHttpResponse, ()>(WebhookHttpResponse::new(200))
        },
    );

    assert_eq!(response, CallbackDeliveryResponse::ClientError(403));
    assert!(!sent, "unsafe DNS resolution must stop before send");
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
        |_| {
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
            |_| Ok::<Vec<IpAddr>, ()>(vec![IpAddr::V4(Ipv4Addr::new(203, 0, 113, 10))]),
            |request| {
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
            turn_id: None,
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
            turn_id: None,
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
    assert!(scheduler
        .schedule_replay(&subscription, &log, 10)
        .is_empty());
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
                IpAddr::V4(Ipv4Addr::new(203, 0, 113, 10)),
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
fn webhook_egress_policy_allows_safe_resolved_addresses_and_explicit_host_allowlist() {
    let policy = WebhookEgressPolicy::default_deny_internal();
    let target =
        WebhookDeliveryTarget::new("https://hooks.example.com/graphblocks/events", &policy)
            .expect("public hostname is valid");

    policy
        .validate_resolved_addresses(
            &target,
            [
                IpAddr::V4(Ipv4Addr::new(203, 0, 113, 10)),
                IpAddr::V4(Ipv4Addr::new(198, 51, 100, 8)),
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
    .with_ordered_delivery();

    let diagnostics = CallbackConfigurationDiagnostic::subscription(&subscription);

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6012");
    assert_eq!(diagnostics[0].field, "delivery.ordering");
}

#[test]
fn callback_diagnostics_report_missing_dead_letter_policy_for_retrying_callbacks() {
    let subscription = subscription(EventFilter::new(), CallbackFailurePolicy::PauseRunOnFailure);

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
    );

    let diagnostics = CallbackConfigurationDiagnostic::subscription(&subscription);

    assert!(diagnostics.is_empty());
}

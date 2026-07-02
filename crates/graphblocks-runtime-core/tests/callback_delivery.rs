use graphblocks_runtime_core::application_event::{
    ApplicationProtocolEvent, ApplicationProtocolEventKind, ApplicationProtocolEventMetadata,
};
use graphblocks_runtime_core::callback_delivery::{
    CallbackDeliveryResponse, CallbackDeliveryScheduler, CallbackDeliveryStatus,
    CallbackFailurePolicy, CallbackRetryPolicy, CallbackSubscription, CallbackSubscriptionStatus,
    EventFilter,
};
use serde_json::json;

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
    assert_eq!(retry_once.next_retry_at_unix_ms, Some(1_100));
    assert_eq!(retry_twice.status, CallbackDeliveryStatus::Pending);
    assert_eq!(retry_twice.attempt, 3);
    assert_eq!(retry_twice.next_retry_at_unix_ms, Some(1_300));
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

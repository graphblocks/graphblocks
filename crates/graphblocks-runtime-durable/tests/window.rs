use graphblocks_runtime_durable::{
    AccumulationMode, DurableError, SourceCursor, SourceEvent, Watermark, WindowAccumulator,
    WindowPolicy,
};
use serde_json::json;

fn event(offset: u64, event_time_unix_ms: u64) -> SourceEvent {
    SourceEvent::new(
        SourceCursor::new("orders", 0, offset),
        json!({"offset": offset}),
        Some(event_time_unix_ms),
    )
}

#[test]
fn event_time_window_waits_for_watermark_plus_allowed_lateness() {
    let policy = WindowPolicy::tumbling_event_time(1_000, 250, AccumulationMode::Discarding)
        .expect("policy should be valid");
    let mut windows = WindowAccumulator::new(policy);

    windows
        .ingest(event(1, 1_820_000_000_100))
        .expect("event should be accepted");
    windows
        .ingest(event(2, 1_820_000_000_900))
        .expect("event should be accepted");
    assert!(
        windows
            .advance_watermark(Watermark::event_time(1_820_000_001_249))
            .is_empty()
    );

    let closed = windows.advance_watermark(Watermark::event_time(1_820_000_001_250));

    assert_eq!(closed.len(), 1);
    assert_eq!(closed[0].start_unix_ms, 1_820_000_000_000);
    assert_eq!(closed[0].end_unix_ms, 1_820_000_001_000);
    assert_eq!(closed[0].events.len(), 2);
}

#[test]
fn event_time_window_rejects_event_after_lateness_deadline() {
    let policy = WindowPolicy::tumbling_event_time(1_000, 250, AccumulationMode::Discarding)
        .expect("policy should be valid");
    let mut windows = WindowAccumulator::new(policy);

    windows.advance_watermark(Watermark::event_time(1_820_000_001_250));

    assert_eq!(
        windows.ingest(event(1, 1_820_000_000_999)),
        Err(DurableError::LateEvent {
            event_time_unix_ms: 1_820_000_000_999,
            watermark_unix_ms: 1_820_000_001_250,
            allowed_lateness_ms: 250,
        }),
    );
}

#[test]
fn event_time_watermark_never_moves_backward() {
    let policy = WindowPolicy::tumbling_event_time(1_000, 250, AccumulationMode::Discarding)
        .expect("policy should be valid");
    let mut windows = WindowAccumulator::new(policy);

    windows.advance_watermark(Watermark::event_time(1_820_000_001_250));
    windows.advance_watermark(Watermark::event_time(1_820_000_000_500));

    assert_eq!(
        windows.ingest(event(1, 1_820_000_000_999)),
        Err(DurableError::LateEvent {
            event_time_unix_ms: 1_820_000_000_999,
            watermark_unix_ms: 1_820_000_001_250,
            allowed_lateness_ms: 250,
        }),
    );
}

#[test]
fn window_policy_rejects_size_without_event_time() {
    assert_eq!(
        WindowPolicy::tumbling_event_time(0, 250, AccumulationMode::Accumulating),
        Err(DurableError::InvalidWindowSize),
    );
}

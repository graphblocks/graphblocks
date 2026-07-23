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
fn event_time_window_rejects_events_without_event_time() {
    let policy = WindowPolicy::tumbling_event_time(1_000, 250, AccumulationMode::Discarding)
        .expect("policy should be valid");
    let mut windows = WindowAccumulator::new(policy);
    let missing_event_time = SourceEvent::new(
        SourceCursor::new("orders", 0, 1),
        json!({"offset": 1}),
        None,
    );

    assert_eq!(
        windows.ingest(missing_event_time),
        Err(DurableError::MissingEventTime {
            cursor: SourceCursor::new("orders", 0, 1),
        }),
    );
}

#[test]
fn processing_time_watermark_does_not_advance_event_time_window() {
    let policy = WindowPolicy::tumbling_event_time(100, 0, AccumulationMode::Discarding)
        .expect("policy should be valid");
    let mut windows = WindowAccumulator::new(policy);
    windows.ingest(event(1, 110)).expect("first event accepted");

    assert!(
        windows
            .advance_watermark(Watermark::processing_time(1_000))
            .is_empty()
    );
    windows
        .ingest(event(2, 150))
        .expect("processing time must not make event-time data late");

    let closed = windows.advance_watermark(Watermark::event_time(200));
    assert_eq!(closed.len(), 1);
    assert_eq!(closed[0].events.len(), 2);
}

#[test]
fn window_policy_rejects_size_without_event_time() {
    assert_eq!(
        WindowPolicy::tumbling_event_time(0, 250, AccumulationMode::Accumulating),
        Err(DurableError::InvalidWindowSize),
    );
}

#[test]
fn window_accumulator_rejects_bypassed_zero_size_policy() {
    let policy = WindowPolicy {
        size_ms: 0,
        allowed_lateness_ms: 250,
        accumulation_mode: AccumulationMode::Accumulating,
    };
    let mut windows = WindowAccumulator::new(policy);

    assert_eq!(
        windows.ingest(event(1, 1_000)),
        Err(DurableError::InvalidWindowSize)
    );
}

#[test]
fn accumulating_window_emits_on_time_and_final_replacement() {
    let policy = WindowPolicy::tumbling_event_time(100, 50, AccumulationMode::Accumulating)
        .expect("policy should be valid");
    let mut windows = WindowAccumulator::new(policy);
    windows.ingest(event(900, 90)).expect("event accepted");
    windows.ingest(event(100, 10)).expect("event accepted");

    let on_time = windows.advance_watermark(Watermark::event_time(100));

    assert_eq!(on_time.len(), 1);
    assert_eq!(
        on_time[0]
            .events
            .iter()
            .map(|event| event.cursor.offset)
            .collect::<Vec<_>>(),
        vec![100, 900]
    );
    assert_eq!(on_time[0].revision, 0);
    assert!(!on_time[0].is_final);
    assert!(
        windows
            .advance_watermark(Watermark::event_time(100))
            .is_empty()
    );
    assert!(
        windows
            .advance_watermark(Watermark::event_time(90))
            .is_empty()
    );

    windows
        .ingest(event(500, 10))
        .expect("window remains open until its deadline");
    assert!(
        windows
            .advance_watermark(Watermark::event_time(149))
            .is_empty()
    );

    let final_pane = windows.advance_watermark(Watermark::event_time(150));

    assert_eq!(final_pane.len(), 1);
    assert_eq!(
        final_pane[0]
            .events
            .iter()
            .map(|event| event.cursor.offset)
            .collect::<Vec<_>>(),
        vec![100, 500, 900]
    );
    assert_eq!(final_pane[0].revision, 1);
    assert!(final_pane[0].is_final);
    assert!(matches!(
        windows.ingest(event(999, 99)),
        Err(DurableError::LateEvent { .. })
    ));
}

#[test]
fn accumulating_window_direct_final_watermark_coalesces_panes() {
    for (allowed_lateness_ms, watermark_unix_ms) in [(50, 150), (0, 100)] {
        let policy = WindowPolicy::tumbling_event_time(
            100,
            allowed_lateness_ms,
            AccumulationMode::Accumulating,
        )
        .expect("policy should be valid");
        let mut windows = WindowAccumulator::new(policy);
        windows.ingest(event(900, 90)).expect("event accepted");
        windows.ingest(event(100, 10)).expect("event accepted");

        let panes = windows.advance_watermark(Watermark::event_time(watermark_unix_ms));

        assert_eq!(panes.len(), 1);
        assert_eq!(
            panes[0]
                .events
                .iter()
                .map(|event| event.cursor.offset)
                .collect::<Vec<_>>(),
            vec![100, 900]
        );
        assert_eq!(panes[0].revision, 0);
        assert!(panes[0].is_final);
    }
}

#[test]
fn discarding_window_keeps_single_final_snapshot_and_deadline_admission() {
    let policy = WindowPolicy::tumbling_event_time(100, 50, AccumulationMode::Discarding)
        .expect("policy should be valid");
    let mut windows = WindowAccumulator::new(policy);
    windows.ingest(event(900, 90)).expect("event accepted");
    windows.ingest(event(100, 10)).expect("event accepted");

    assert!(
        windows
            .advance_watermark(Watermark::event_time(100))
            .is_empty()
    );
    windows
        .ingest(event(500, 10))
        .expect("window remains open until its deadline");

    let panes = windows.advance_watermark(Watermark::event_time(150));

    assert_eq!(panes.len(), 1);
    assert_eq!(
        panes[0]
            .events
            .iter()
            .map(|event| event.cursor.offset)
            .collect::<Vec<_>>(),
        vec![100, 500, 900]
    );
    assert_eq!(panes[0].revision, 0);
    assert!(panes[0].is_final);
}

#[test]
fn event_time_window_rejects_unrepresentable_half_open_bounds() {
    for (size_ms, allowed_lateness_ms, event_time_unix_ms) in
        [(10, 0, u64::MAX), (10, 10, u64::MAX - 15)]
    {
        let policy = WindowPolicy::tumbling_event_time(
            size_ms,
            allowed_lateness_ms,
            AccumulationMode::Discarding,
        )
        .expect("policy should be valid");
        let mut windows = WindowAccumulator::new(policy);

        assert_eq!(
            windows.ingest(event(1, event_time_unix_ms)),
            Err(DurableError::WindowBoundaryOverflow {
                event_time_unix_ms,
                size_ms,
                allowed_lateness_ms,
            })
        );
        assert!(
            windows
                .advance_watermark(Watermark::event_time(u64::MAX))
                .is_empty()
        );
    }
}

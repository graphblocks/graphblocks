use graphblocks_runtime_durable::{
    DeliveryGuarantee, DurableError, SourceBatch, SourceCursor, SourceEvent, Watermark,
};
use serde_json::json;

#[test]
fn source_cursor_orders_by_partition_and_offset() {
    let early = SourceCursor::new("orders", 0, 41);
    let late = SourceCursor::new("orders", 0, 42);
    let other_partition = SourceCursor::new("orders", 1, 1);

    assert!(early < late);
    assert!(late < other_partition);
    assert_eq!(late.partition_key(), "orders:0");
}

#[test]
fn source_batch_rejects_empty_demand_and_preserves_high_watermark() {
    assert_eq!(
        SourceBatch::new(
            DeliveryGuarantee::AtLeastOnce,
            [],
            Some(Watermark::event_time(1_820_000_000_000)),
            0,
        ),
        Err(DurableError::InvalidDemand),
    );

    let first = SourceEvent::new(
        SourceCursor::new("orders", 0, 10),
        json!({"orderId": "ord-1"}),
        Some(1_820_000_000_000),
    );
    let second = SourceEvent::new(
        SourceCursor::new("orders", 0, 11),
        json!({"orderId": "ord-2"}),
        Some(1_820_000_000_500),
    );
    let batch = SourceBatch::new(
        DeliveryGuarantee::AtLeastOnce,
        [first, second],
        Some(Watermark::event_time(1_820_000_001_000)),
        2,
    )
    .expect("batch demand should admit two events");

    assert_eq!(batch.events.len(), 2);
    assert_eq!(
        batch.high_cursor(),
        Some(&SourceCursor::new("orders", 0, 11))
    );
    assert_eq!(
        batch.watermark,
        Some(Watermark::event_time(1_820_000_001_000)),
    );
}

#[test]
fn source_batch_rejects_more_events_than_requested_demand() {
    let events = [
        SourceEvent::new(SourceCursor::new("orders", 0, 10), json!({"n": 1}), None),
        SourceEvent::new(SourceCursor::new("orders", 0, 11), json!({"n": 2}), None),
    ];

    assert_eq!(
        SourceBatch::new(DeliveryGuarantee::AtMostOnce, events, None, 1),
        Err(DurableError::DemandExceeded {
            demand: 1,
            actual: 2,
        }),
    );
}

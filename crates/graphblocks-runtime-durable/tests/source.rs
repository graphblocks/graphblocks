use graphblocks_runtime_durable::{
    DeliveryGuarantee, DurableError, InMemoryDurableSource, SourceBatch, SourceCursor, SourceEvent,
    Watermark,
};
use serde_json::json;

fn order_event(offset: u64) -> SourceEvent {
    SourceEvent::new(
        SourceCursor::new("orders", 0, offset),
        json!({"orderId": format!("ord-{offset}")}),
        Some(1_820_000_000_000 + offset),
    )
}

fn partition_event(partition: u32, offset: u64) -> SourceEvent {
    SourceEvent::new(
        SourceCursor::new("orders", partition, offset),
        json!({"partition": partition, "offset": offset}),
        Some(1_820_000_000_000 + offset),
    )
}

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
fn in_memory_source_replays_from_committed_or_explicit_cursor() {
    let mut source = InMemoryDurableSource::new(
        DeliveryGuarantee::AtLeastOnce,
        [order_event(10), order_event(11), order_event(12)],
    );

    let first = source.poll(None, 2).expect("source should poll");
    assert_eq!(
        first
            .events
            .iter()
            .map(|event| event.cursor.offset)
            .collect::<Vec<_>>(),
        vec![10, 11],
    );
    source
        .commit(SourceCursor::new("orders", 0, 11))
        .expect("cursor commit should advance");

    let after_commit = source
        .poll(None, 2)
        .expect("source should resume after commit");
    assert_eq!(
        after_commit
            .events
            .iter()
            .map(|event| event.cursor.offset)
            .collect::<Vec<_>>(),
        vec![12],
    );

    let replay = source
        .poll(Some(SourceCursor::new("orders", 0, 10)), 2)
        .expect("explicit checkpoint cursor should replay after that cursor");
    assert_eq!(
        replay
            .events
            .iter()
            .map(|event| event.cursor.offset)
            .collect::<Vec<_>>(),
        vec![11, 12],
    );
}

#[test]
fn in_memory_source_rejects_conflicting_offset_reuse() {
    let cursor = SourceCursor::new("orders", 0, 10);
    let mut source = InMemoryDurableSource::new(
        DeliveryGuarantee::AtLeastOnce,
        [
            SourceEvent::new(cursor.clone(), json!({"orderId": "ord-a"}), Some(100)),
            SourceEvent::new(cursor.clone(), json!({"orderId": "ord-b"}), Some(100)),
        ],
    );

    assert_eq!(
        source.poll(None, 1),
        Err(DurableError::ConflictingSourceOffset {
            cursor: cursor.clone(),
        })
    );
    assert_eq!(
        source.commit(cursor.clone()),
        Err(DurableError::ConflictingSourceOffset { cursor })
    );
}

#[test]
fn in_memory_source_watermark_never_regresses_between_polls() {
    let source = InMemoryDurableSource::new(
        DeliveryGuarantee::AtLeastOnce,
        [
            SourceEvent::new(
                SourceCursor::new("orders", 0, 10),
                json!({"orderId": "ord-10"}),
                Some(100),
            ),
            SourceEvent::new(
                SourceCursor::new("orders", 0, 11),
                json!({"orderId": "ord-11"}),
                Some(50),
            ),
        ],
    );

    assert_eq!(
        source.poll(None, 1).expect("first poll").watermark,
        Some(Watermark::event_time(100))
    );
    assert_eq!(
        source
            .poll(Some(SourceCursor::new("orders", 0, 10)), 1)
            .expect("second poll")
            .watermark,
        Some(Watermark::event_time(100))
    );
}

#[test]
fn in_memory_source_commits_are_partition_scoped() {
    let mut source = InMemoryDurableSource::new(
        DeliveryGuarantee::AtLeastOnce,
        [
            partition_event(0, 1),
            partition_event(0, 2),
            partition_event(1, 10),
            partition_event(1, 11),
        ],
    );

    source
        .commit(SourceCursor::new("orders", 1, 10))
        .expect("partition 1 commit should advance only partition 1");

    let after_commit = source
        .poll(None, 10)
        .expect("source should retain uncommitted partition 0 events");

    assert_eq!(
        after_commit
            .events
            .iter()
            .map(|event| (event.cursor.partition, event.cursor.offset))
            .collect::<Vec<_>>(),
        vec![(0, 1), (0, 2), (1, 11)],
    );
}

#[test]
fn in_memory_source_explicit_replay_cursor_filters_only_its_partition() {
    let source = InMemoryDurableSource::new(
        DeliveryGuarantee::AtLeastOnce,
        [
            partition_event(0, 1),
            partition_event(0, 2),
            partition_event(1, 1),
            partition_event(1, 2),
        ],
    );

    let replay = source
        .poll(Some(SourceCursor::new("orders", 1, 1)), 10)
        .expect("explicit partition cursor should replay across other partitions");

    assert_eq!(
        replay
            .events
            .iter()
            .map(|event| (event.cursor.partition, event.cursor.offset))
            .collect::<Vec<_>>(),
        vec![(0, 1), (0, 2), (1, 2)],
    );
}

#[test]
fn in_memory_source_pause_blocks_poll_until_resume() {
    let mut source = InMemoryDurableSource::new(DeliveryGuarantee::BestEffort, [order_event(10)]);

    source.pause();
    assert_eq!(source.poll(None, 1), Err(DurableError::SourcePaused));

    source.resume();
    assert_eq!(
        source
            .poll(None, 1)
            .expect("source should resume")
            .events
            .len(),
        1
    );
}

#[test]
fn in_memory_source_rejects_stale_cursor_commit() {
    let mut source = InMemoryDurableSource::new(
        DeliveryGuarantee::AtLeastOnce,
        [order_event(10), order_event(11)],
    );

    source
        .commit(SourceCursor::new("orders", 0, 11))
        .expect("first commit should advance");

    assert_eq!(
        source.commit(SourceCursor::new("orders", 0, 10)),
        Err(DurableError::StaleCommit {
            current: SourceCursor::new("orders", 0, 11),
            attempted: SourceCursor::new("orders", 0, 10),
        }),
    );
}

#[test]
fn in_memory_source_rejects_unknown_cursor_stream() {
    let mut source = InMemoryDurableSource::new(DeliveryGuarantee::AtLeastOnce, [order_event(10)]);
    let unknown_cursor = SourceCursor::new("payments", 0, 10);

    assert_eq!(
        source.commit(unknown_cursor.clone()),
        Err(DurableError::UnknownSourceCursor {
            cursor: unknown_cursor.clone(),
        }),
    );
    assert_eq!(
        source.poll(Some(unknown_cursor.clone()), 1),
        Err(DurableError::UnknownSourceCursor {
            cursor: unknown_cursor,
        }),
    );
}

#[test]
fn in_memory_source_rejects_unknown_cursor_partition() {
    let mut source = InMemoryDurableSource::new(DeliveryGuarantee::AtLeastOnce, [order_event(10)]);
    let unknown_cursor = SourceCursor::new("orders", 1, 10);

    assert_eq!(
        source.commit(unknown_cursor.clone()),
        Err(DurableError::UnknownSourceCursor {
            cursor: unknown_cursor.clone(),
        }),
    );
    assert_eq!(
        source.poll(Some(unknown_cursor.clone()), 1),
        Err(DurableError::UnknownSourceCursor {
            cursor: unknown_cursor,
        }),
    );
}

#[test]
fn in_memory_source_rejects_forged_offset_in_known_partition() {
    let mut source = InMemoryDurableSource::new(
        DeliveryGuarantee::AtLeastOnce,
        [order_event(10), order_event(12)],
    );
    let forged_cursor = SourceCursor::new("orders", 0, 11);

    assert_eq!(
        source.commit(forged_cursor.clone()),
        Err(DurableError::UnknownSourceCursor {
            cursor: forged_cursor.clone(),
        }),
    );
    assert_eq!(
        source.poll(Some(forged_cursor.clone()), 1),
        Err(DurableError::UnknownSourceCursor {
            cursor: forged_cursor,
        }),
    );
}

#[test]
fn empty_in_memory_source_rejects_explicit_cursor() {
    let mut source = InMemoryDurableSource::new(DeliveryGuarantee::AtLeastOnce, []);
    let forged_cursor = SourceCursor::new("orders", 0, 1);

    assert_eq!(
        source.commit(forged_cursor.clone()),
        Err(DurableError::UnknownSourceCursor {
            cursor: forged_cursor.clone(),
        }),
    );
    assert_eq!(
        source.poll(Some(forged_cursor.clone()), 1),
        Err(DurableError::UnknownSourceCursor {
            cursor: forged_cursor,
        }),
    );
}

#[test]
fn in_memory_source_rejects_empty_cursor_stream() {
    let mut source = InMemoryDurableSource::new(DeliveryGuarantee::AtLeastOnce, [order_event(10)]);
    let empty_cursor = SourceCursor::new(" ", 0, 10);

    assert_eq!(
        source.commit(empty_cursor.clone()),
        Err(DurableError::InvalidSourceCursor { field: "stream" }),
    );
    assert_eq!(
        source.poll(Some(empty_cursor), 1),
        Err(DurableError::InvalidSourceCursor { field: "stream" }),
    );
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
fn source_batch_rejects_events_with_empty_cursor_stream() {
    let event = SourceEvent::new(SourceCursor::new(" ", 0, 10), json!({"n": 1}), None);

    assert_eq!(
        SourceBatch::new(DeliveryGuarantee::AtMostOnce, [event], None, 1),
        Err(DurableError::InvalidSourceCursor { field: "stream" }),
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

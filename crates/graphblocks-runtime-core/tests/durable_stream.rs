use graphblocks_runtime_core::durable_stream::{
    CheckpointBarrier, DeliveryGuarantee, DurableStreamError, SourceCursor, SourceRecord,
    StreamWatermark, SinkCommitError, SinkCommitLog, SinkCommitRecord,
};
use serde_json::json;

#[test]
fn source_cursor_orders_partition_offsets_and_filters_replay() {
    let records = vec![
        SourceRecord::new(
            "topic-a",
            SourceCursor::new("partition-1", 3),
            1_000,
            json!({"id": 3}),
        )
        .expect("record is valid"),
        SourceRecord::new(
            "topic-a",
            SourceCursor::new("partition-0", 2),
            900,
            json!({"id": 2}),
        )
        .expect("record is valid"),
        SourceRecord::new(
            "topic-a",
            SourceCursor::new("partition-1", 4),
            1_010,
            json!({"id": 4}),
        )
        .expect("record is valid"),
    ];
    let replay = SourceRecord::replay_after(&records, &SourceCursor::new("partition-1", 3));

    assert_eq!(
        records
            .iter()
            .map(|record| record.cursor.clone())
            .collect::<Vec<_>>(),
        vec![
            SourceCursor::new("partition-1", 3),
            SourceCursor::new("partition-0", 2),
            SourceCursor::new("partition-1", 4),
        ]
    );
    assert_eq!(replay.len(), 1);
    assert_eq!(replay[0].cursor, SourceCursor::new("partition-1", 4));
    assert_eq!(
        SourceCursor::new("partition-1", 3).commit_key(),
        "partition-1:3"
    );
}

#[test]
fn stream_watermark_classifies_allowed_and_late_events() {
    let watermark = StreamWatermark::new("orders", 10_000, 500).expect("watermark is valid");

    assert!(watermark.accepts_event_time(9_500));
    assert!(watermark.accepts_event_time(10_000));
    assert!(!watermark.accepts_event_time(9_499));
    assert_eq!(watermark.min_allowed_event_time_ms(), 9_500);
    assert!(StreamWatermark::new("orders", 10_000, 0).is_ok());
}

#[test]
fn checkpoint_barrier_digest_is_stable_and_includes_required_state() {
    let barrier = CheckpointBarrier::new("checkpoint-1", "sha256:plan", 42)
        .with_source_cursor("orders", SourceCursor::new("partition-0", 12))
        .with_source_cursor("orders", SourceCursor::new("partition-1", 5))
        .with_operator_state_digest("window.orders", "sha256:state")
        .with_pending_effect_digest("effect-log", "sha256:effects")
        .with_sink_commit_digest("warehouse", "sha256:sink")
        .with_schema_version("schemas/Order@1", 1);
    let reversed = CheckpointBarrier::new("checkpoint-other", "sha256:plan", 42)
        .with_schema_version("schemas/Order@1", 1)
        .with_sink_commit_digest("warehouse", "sha256:sink")
        .with_pending_effect_digest("effect-log", "sha256:effects")
        .with_operator_state_digest("window.orders", "sha256:state")
        .with_source_cursor("orders", SourceCursor::new("partition-1", 5))
        .with_source_cursor("orders", SourceCursor::new("partition-0", 12));

    assert_eq!(barrier.source_cursors["orders"].len(), 2);
    assert_eq!(barrier.content_digest(), reversed.content_digest());
    assert_eq!(
        barrier.content_digest(),
        "sha256:a1d296d22a2858fd633c0b2b53e53657fc0780532beedf6c2f718dda6b51b7e0"
    );
}

#[test]
fn checkpoint_barrier_rejects_zero_schema_version() {
    let mut barrier = CheckpointBarrier::new("checkpoint-1", "sha256:plan", 1)
        .with_schema_version("schemas/Order@1", 0);

    assert_eq!(
        barrier.validate(),
        Err(DurableStreamError::InvalidOffset {
            field: "schema_version",
        })
    );
}

#[test]
fn sink_commit_log_is_idempotent_and_rejects_mutated_replay() {
    let log = SinkCommitLog::new("warehouse", DeliveryGuarantee::AtLeastOnce)
        .expect("sink log is valid");
    let record = SinkCommitRecord::new(
        "commit-1",
        "warehouse",
        "orders:partition-0:12",
        json!({"rows": 10}),
    )
    .expect("commit record is valid");

    let (log, first) = log.commit(record.clone()).expect("first commit succeeds");
    let (log, duplicate) = log.commit(record.clone()).expect("duplicate is idempotent");
    let conflict = log.commit(
        SinkCommitRecord::new(
            "commit-2",
            "warehouse",
            "orders:partition-0:12",
            json!({"rows": 11}),
        )
        .expect("conflicting record is valid"),
    );

    assert!(first.committed);
    assert!(!first.duplicate);
    assert!(!duplicate.committed);
    assert!(duplicate.duplicate);
    assert_eq!(log.records().len(), 1);
    assert_eq!(
        conflict,
        Err(SinkCommitError::IdempotencyConflict {
            sink_id: "warehouse".to_owned(),
            idempotency_key: "orders:partition-0:12".to_owned(),
        })
    );
    assert_eq!(
        log.content_digest(),
        "sha256:44f3d946c8929c37202f95f3b38faec719b3bee986ec7d8f403c385a60cb2c7f"
    );
}

#[test]
fn sink_commit_record_rejects_non_object_metadata() {
    assert_eq!(
        SinkCommitRecord::new(
            "commit-1",
            "warehouse",
            "orders:partition-0:12",
            json!("committed"),
        ),
        Err(SinkCommitError::InvalidMetadata {
            field: "metadata",
        })
    );
}

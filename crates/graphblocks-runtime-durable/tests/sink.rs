use graphblocks_runtime_durable::{
    InMemoryDurableSink, SinkCommitError, SinkCommitRequest, SinkCommitResult,
};
use serde_json::json;

#[test]
fn sink_commit_records_metadata_and_replays_duplicate_idempotency_key() {
    let mut sink = InMemoryDurableSink::new("warehouse");
    let request = SinkCommitRequest::new(
        "run-000001",
        "load",
        "load-attempt-1",
        "warehouse-tx-1",
        json!({"rows": 2}),
    );

    let committed = sink.commit(request.clone()).expect("sink should commit");
    let duplicate = sink
        .commit(request)
        .expect("duplicate idempotency key should replay metadata");

    assert_eq!(committed.sequence, duplicate.sequence);
    assert_eq!(committed.metadata, duplicate.metadata);
    assert_eq!(committed.idempotency_key, duplicate.idempotency_key);
    assert!(duplicate.replayed);
    assert_eq!(
        committed,
        SinkCommitResult {
            sink_id: "warehouse".to_owned(),
            idempotency_key: "warehouse-tx-1".to_owned(),
            sequence: 1,
            metadata: json!({"rows": 2}),
            replayed: false,
        },
    );
    assert_eq!(sink.committed_count(), 1);
}

#[test]
fn sink_commit_rejects_idempotency_key_reuse_with_different_payload() {
    let mut sink = InMemoryDurableSink::new("warehouse");
    sink.commit(SinkCommitRequest::new(
        "run-000001",
        "load",
        "load-attempt-1",
        "warehouse-tx-1",
        json!({"rows": 2}),
    ))
    .expect("initial commit should succeed");

    assert_eq!(
        sink.commit(SinkCommitRequest::new(
            "run-000001",
            "load",
            "load-attempt-1",
            "warehouse-tx-1",
            json!({"rows": 3}),
        )),
        Err(SinkCommitError::IdempotencyConflict {
            idempotency_key: "warehouse-tx-1".to_owned(),
        }),
    );
}

#[test]
fn sink_commit_requires_stable_identity_fields() {
    let mut sink = InMemoryDurableSink::new("warehouse");

    assert_eq!(
        sink.commit(SinkCommitRequest::new(
            "",
            "load",
            "attempt",
            "tx",
            json!({})
        )),
        Err(SinkCommitError::MissingRunId),
    );
    assert_eq!(
        sink.commit(SinkCommitRequest::new(
            "run",
            "",
            "attempt",
            "tx",
            json!({})
        )),
        Err(SinkCommitError::MissingNodeId),
    );
    assert_eq!(
        sink.commit(SinkCommitRequest::new("run", "load", "", "tx", json!({}))),
        Err(SinkCommitError::MissingNodeAttemptId),
    );
    assert_eq!(
        sink.commit(SinkCommitRequest::new(
            "run",
            "load",
            "attempt",
            "",
            json!({})
        )),
        Err(SinkCommitError::MissingIdempotencyKey),
    );
}

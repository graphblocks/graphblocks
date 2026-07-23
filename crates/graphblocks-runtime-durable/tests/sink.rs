use graphblocks_runtime_durable::{
    InMemoryDurableSink, SinkCommitError, SinkCommitRequest, SinkCommitResult,
};
use serde_json::json;

#[test]
fn default_sink_starts_commit_sequences_at_one() {
    let mut sink = InMemoryDurableSink::default();
    let result = sink
        .commit(SinkCommitRequest::new(
            "run",
            "node",
            "attempt",
            "key",
            json!(null),
        ))
        .expect("default sink should commit");

    assert_eq!(result.sequence, 1);
    assert_eq!(result.sink_id, "default");
}

#[test]
fn sink_commit_rejects_noncanonical_sink_identity() {
    let mut sink = InMemoryDurableSink::new(" sink ");

    assert_eq!(
        sink.commit(SinkCommitRequest::new(
            "run",
            "node",
            "attempt",
            "key",
            json!(null),
        )),
        Err(SinkCommitError::MissingSinkId)
    );
    assert_eq!(sink.committed_count(), 0);
}

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
            precondition_digest: None,
            sequence: 1,
            metadata: json!({"rows": 2}),
            replayed: false,
        },
    );
    assert_eq!(sink.committed_count(), 1);
}

#[test]
fn sink_commit_records_effect_precondition_and_replays_matching_request() {
    let mut sink = InMemoryDurableSink::new("ticket-system");
    let request = SinkCommitRequest::new(
        "run-000001",
        "ticket-create",
        "ticket-create-attempt-1",
        "ticket-create-tx-1",
        json!({"ticketId": "ticket-1"}),
    )
    .with_precondition_digest("sha256:conversation-revision-1");

    let committed = sink.commit(request.clone()).expect("sink should commit");
    let duplicate = sink
        .commit(request)
        .expect("duplicate precondition should replay");

    assert_eq!(
        committed.precondition_digest.as_deref(),
        Some("sha256:conversation-revision-1")
    );
    assert_eq!(duplicate.precondition_digest, committed.precondition_digest);
    assert!(duplicate.replayed);
    assert_eq!(sink.committed_count(), 1);
}

#[test]
fn sink_commit_rejects_idempotency_reuse_with_different_precondition() {
    let mut sink = InMemoryDurableSink::new("ticket-system");
    sink.commit(
        SinkCommitRequest::new(
            "run-000001",
            "ticket-create",
            "ticket-create-attempt-1",
            "ticket-create-tx-1",
            json!({"ticketId": "ticket-1"}),
        )
        .with_precondition_digest("sha256:conversation-revision-1"),
    )
    .expect("initial commit should succeed");

    assert_eq!(
        sink.commit(
            SinkCommitRequest::new(
                "run-000001",
                "ticket-create",
                "ticket-create-attempt-1",
                "ticket-create-tx-1",
                json!({"ticketId": "ticket-1"}),
            )
            .with_precondition_digest("sha256:conversation-revision-2"),
        ),
        Err(SinkCommitError::IdempotencyConflict {
            idempotency_key: "ticket-create-tx-1".to_owned(),
        }),
    );
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

#[test]
fn sink_commit_rejects_whitespace_identity_fields() {
    let mut sink = InMemoryDurableSink::new("warehouse");

    assert_eq!(
        sink.commit(SinkCommitRequest::new(
            " run ",
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
            " load ",
            "attempt",
            "tx",
            json!({})
        )),
        Err(SinkCommitError::MissingNodeId),
    );
    assert_eq!(
        sink.commit(SinkCommitRequest::new(
            "run",
            "load",
            " attempt ",
            "tx",
            json!({})
        )),
        Err(SinkCommitError::MissingNodeAttemptId),
    );
    assert_eq!(
        sink.commit(SinkCommitRequest::new(
            "run",
            "load",
            "attempt",
            " tx ",
            json!({})
        )),
        Err(SinkCommitError::MissingIdempotencyKey),
    );
}

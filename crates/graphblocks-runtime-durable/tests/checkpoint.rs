use std::collections::BTreeMap;

use graphblocks_runtime_durable::{
    CheckpointBarrier, CheckpointBarrierError, SourceCursor, SourceCursorCommitPlan,
};
use serde_json::json;

#[test]
fn checkpoint_barrier_requires_plan_hash_and_schema_versions() {
    let barrier = CheckpointBarrier {
        checkpoint_id: "checkpoint-000001".to_owned(),
        run_id: "run-000001".to_owned(),
        plan_hash: "".to_owned(),
        source_cursors: BTreeMap::from([("orders".to_owned(), SourceCursor::new("orders", 0, 42))]),
        operator_state: BTreeMap::new(),
        sink_commit_metadata: BTreeMap::new(),
        schema_versions: BTreeMap::new(),
    };

    assert_eq!(
        barrier.validate(),
        Err(CheckpointBarrierError::MissingPlanHash),
    );
}

#[test]
fn checkpoint_barrier_builds_deterministic_source_commit_plan() {
    let barrier = CheckpointBarrier {
        checkpoint_id: "checkpoint-000001".to_owned(),
        run_id: "run-000001".to_owned(),
        plan_hash: "sha256:plan".to_owned(),
        source_cursors: BTreeMap::from([
            ("payments".to_owned(), SourceCursor::new("payments", 1, 7)),
            ("orders".to_owned(), SourceCursor::new("orders", 0, 42)),
        ]),
        operator_state: BTreeMap::from([("dedupe".to_owned(), json!({"seen": 2}))]),
        sink_commit_metadata: BTreeMap::from([("warehouse".to_owned(), json!({"tx": "tx-1"}))]),
        schema_versions: BTreeMap::from([("checkpoint".to_owned(), 1)]),
    };

    assert_eq!(barrier.validate(), Ok(()));
    assert_eq!(
        barrier.source_commit_plan(),
        SourceCursorCommitPlan {
            cursors: vec![
                ("orders".to_owned(), SourceCursor::new("orders", 0, 42)),
                ("payments".to_owned(), SourceCursor::new("payments", 1, 7)),
            ],
        },
    );
}

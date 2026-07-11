use std::collections::BTreeMap;

use graphblocks_runtime_durable::{
    CheckpointBarrier, CheckpointBarrierError, CheckpointStoreError, InMemoryCheckpointStore,
    SchemaRef, SourceCursor, SourceCursorCommitPlan,
};
use serde_json::json;

fn checkpoint(checkpoint_id: &str, state_revision: u64, plan_hash: &str) -> CheckpointBarrier {
    CheckpointBarrier {
        checkpoint_id: checkpoint_id.to_owned(),
        run_id: "run-000001".to_owned(),
        release_id: "release-2026-06-23".to_owned(),
        deployment_revision_id: "deployment-rev-1".to_owned(),
        plan_hash: plan_hash.to_owned(),
        checkpoint_schema: SchemaRef::new("graphblocks.ai/Checkpoint", 1),
        state_revision,
        completed_nodes: vec!["extract".to_owned()],
        pending_nodes: vec!["load".to_owned()],
        source_cursors: BTreeMap::from([("orders".to_owned(), SourceCursor::new("orders", 0, 42))]),
        operator_state: BTreeMap::from([("dedupe".to_owned(), json!({"seen": state_revision}))]),
        sink_commit_metadata: BTreeMap::from([(
            "warehouse".to_owned(),
            json!({"tx": checkpoint_id}),
        )]),
        schema_versions: BTreeMap::from([("checkpoint".to_owned(), 1)]),
        created_at_unix_ms: 1_820_000_000_000 + state_revision,
    }
}

#[test]
fn checkpoint_barrier_requires_plan_hash_and_schema_versions() {
    let mut barrier = checkpoint("checkpoint-000001", 1, "");

    assert_eq!(
        barrier.validate(),
        Err(CheckpointBarrierError::MissingPlanHash),
    );

    barrier.plan_hash = "sha256:plan".to_owned();
    barrier.checkpoint_schema = SchemaRef::new("", 1);

    assert_eq!(
        barrier.validate(),
        Err(CheckpointBarrierError::InvalidCheckpointSchema),
    );

    barrier.checkpoint_schema = SchemaRef::new("graphblocks.ai/Checkpoint", 1);
    barrier.schema_versions.clear();

    assert_eq!(
        barrier.validate(),
        Err(CheckpointBarrierError::MissingSchemaVersions),
    );
}

#[test]
fn checkpoint_barrier_rejects_whitespace_identity_fields() {
    let mut barrier = checkpoint(" ", 1, "sha256:plan");

    assert_eq!(
        barrier.validate(),
        Err(CheckpointBarrierError::MissingCheckpointId),
    );

    barrier = checkpoint("checkpoint-000001", 1, "sha256:plan");
    barrier.run_id = "\t".to_owned();
    assert_eq!(
        barrier.validate(),
        Err(CheckpointBarrierError::MissingRunId)
    );

    barrier = checkpoint("checkpoint-000001", 1, "sha256:plan");
    barrier.release_id = "\n".to_owned();
    assert_eq!(
        barrier.validate(),
        Err(CheckpointBarrierError::MissingReleaseId),
    );

    barrier = checkpoint("checkpoint-000001", 1, "sha256:plan");
    barrier.deployment_revision_id = " ".to_owned();
    assert_eq!(
        barrier.validate(),
        Err(CheckpointBarrierError::MissingDeploymentRevisionId),
    );

    barrier = checkpoint("checkpoint-000001", 1, " ");
    assert_eq!(
        barrier.validate(),
        Err(CheckpointBarrierError::MissingPlanHash),
    );

    barrier = checkpoint("checkpoint-000001", 1, "sha256:plan");
    barrier.checkpoint_schema = SchemaRef::new(" ", 1);
    assert_eq!(
        barrier.validate(),
        Err(CheckpointBarrierError::InvalidCheckpointSchema),
    );
}

#[test]
fn checkpoint_barrier_builds_deterministic_source_commit_plan() {
    let mut barrier = checkpoint("checkpoint-000001", 1, "sha256:plan");
    barrier
        .source_cursors
        .insert("payments".to_owned(), SourceCursor::new("payments", 1, 7));

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

#[test]
fn checkpoint_store_replays_latest_compatible_checkpoint() {
    let mut store = InMemoryCheckpointStore::new();
    store
        .put(checkpoint("checkpoint-000001", 1, "sha256:plan"))
        .expect("initial checkpoint should be accepted");
    store
        .put(checkpoint("checkpoint-000002", 2, "sha256:plan"))
        .expect("newer checkpoint should be accepted");
    store
        .put(checkpoint("checkpoint-000003", 3, "sha256:other-plan"))
        .expect("checkpoint from a different plan should be accepted");

    let replay = store
        .latest_compatible(
            "run-000001",
            "release-2026-06-23",
            "deployment-rev-1",
            "sha256:plan",
        )
        .expect("compatible checkpoint should exist");

    assert_eq!(replay.checkpoint_id, "checkpoint-000002");
    assert_eq!(replay.state_revision, 2);
    assert!(
        store
            .latest_compatible(
                "run-000001",
                "release-2026-06-23",
                "deployment-rev-2",
                "sha256:plan",
            )
            .is_none()
    );
}

#[test]
fn checkpoint_store_rejects_stale_state_revision() {
    let mut store = InMemoryCheckpointStore::new();
    store
        .put(checkpoint("checkpoint-000002", 2, "sha256:plan"))
        .expect("newer checkpoint should be accepted");

    assert_eq!(
        store.put(checkpoint("checkpoint-000001", 1, "sha256:plan")),
        Err(CheckpointStoreError::StaleStateRevision {
            run_id: "run-000001".to_owned(),
            current: 2,
            attempted: 1,
        }),
    );
}

#[test]
fn checkpoint_store_claims_latest_compatible_checkpoint_with_fencing() {
    let mut store = InMemoryCheckpointStore::new();
    store
        .put(checkpoint("checkpoint-000001", 1, "sha256:plan"))
        .expect("initial checkpoint should be accepted");
    store
        .put(checkpoint("checkpoint-000002", 2, "sha256:plan"))
        .expect("newer checkpoint should be accepted");

    let first = store
        .claim_latest_compatible(
            "run-000001",
            "release-2026-06-23",
            "deployment-rev-1",
            "sha256:plan",
            "worker-a",
            "lease-a",
            1_000,
            2_000,
        )
        .expect("first worker should claim the latest compatible checkpoint");

    assert_eq!(first.checkpoint.checkpoint_id, "checkpoint-000002");
    assert_eq!(first.claim.run_id, "run-000001");
    assert_eq!(first.claim.checkpoint_id, "checkpoint-000002");
    assert_eq!(first.claim.worker_id, "worker-a");
    assert_eq!(first.claim.lease_id, "lease-a");
    assert_eq!(first.claim.fencing_epoch, 1);

    assert_eq!(
        store.claim_latest_compatible(
            "run-000001",
            "release-2026-06-23",
            "deployment-rev-1",
            "sha256:plan",
            "worker-b",
            "lease-b",
            1_500,
            2_500,
        ),
        Err(CheckpointStoreError::ActiveRecoveryClaim {
            run_id: "run-000001".to_owned(),
            worker_id: "worker-a".to_owned(),
            lease_id: "lease-a".to_owned(),
            expires_at_unix_ms: 2_000,
        })
    );

    store
        .complete_claim(&first.claim, 1_600)
        .expect("active claim should complete");
    let second = store
        .claim_latest_compatible(
            "run-000001",
            "release-2026-06-23",
            "deployment-rev-1",
            "sha256:plan",
            "worker-b",
            "lease-b",
            1_700,
            2_700,
        )
        .expect("worker should claim after previous claim completes");

    assert_eq!(second.claim.fencing_epoch, 2);
    assert_eq!(
        store.complete_claim(&first.claim, 1_800),
        Err(CheckpointStoreError::RecoveryClaimMismatch {
            run_id: "run-000001".to_owned(),
            expected_lease_id: "lease-a".to_owned(),
            expected_fencing_epoch: 1,
            actual_lease_id: "lease-b".to_owned(),
            actual_fencing_epoch: 2,
        })
    );
}

#[test]
fn checkpoint_store_reclaims_expired_checkpoint_claim_with_new_fence() {
    let mut store = InMemoryCheckpointStore::new();
    store
        .put(checkpoint("checkpoint-000001", 1, "sha256:plan"))
        .expect("checkpoint should be accepted");

    let expired = store
        .claim_latest_compatible(
            "run-000001",
            "release-2026-06-23",
            "deployment-rev-1",
            "sha256:plan",
            "worker-a",
            "lease-a",
            1_000,
            1_100,
        )
        .expect("initial claim should be accepted");
    let replacement = store
        .claim_latest_compatible(
            "run-000001",
            "release-2026-06-23",
            "deployment-rev-1",
            "sha256:plan",
            "worker-b",
            "lease-b",
            1_101,
            2_000,
        )
        .expect("expired claim should be replaceable");

    assert_eq!(replacement.claim.fencing_epoch, 2);
    assert_eq!(
        store.complete_claim(&expired.claim, 1_200),
        Err(CheckpointStoreError::RecoveryClaimMismatch {
            run_id: "run-000001".to_owned(),
            expected_lease_id: "lease-a".to_owned(),
            expected_fencing_epoch: 1,
            actual_lease_id: "lease-b".to_owned(),
            actual_fencing_epoch: 2,
        })
    );
}

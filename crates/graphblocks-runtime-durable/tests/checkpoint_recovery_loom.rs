use std::collections::BTreeMap;

use graphblocks_runtime_durable::{
    CheckpointBarrier, CheckpointRecoveryClaim, CheckpointStoreError, InMemoryCheckpointStore,
    SchemaRef, SourceCursor,
};
use loom::sync::{Arc, Mutex};
use loom::thread;
use serde_json::json;

type SharedStore = Arc<Mutex<InMemoryCheckpointStore>>;

fn seeded_store() -> (SharedStore, CheckpointRecoveryClaim) {
    let mut store = InMemoryCheckpointStore::new();
    store
        .put(CheckpointBarrier {
            checkpoint_id: "checkpoint-000001".to_owned(),
            run_id: "run-000001".to_owned(),
            release_id: "release-2026-06-23".to_owned(),
            deployment_revision_id: "deployment-rev-1".to_owned(),
            plan_hash: "sha256:plan".to_owned(),
            checkpoint_schema: SchemaRef::new("graphblocks.ai/Checkpoint", 1),
            state_revision: 1,
            completed_nodes: vec!["extract".to_owned()],
            pending_nodes: vec!["load".to_owned()],
            source_cursors: BTreeMap::from([(
                "orders".to_owned(),
                SourceCursor::new("orders", 0, 42),
            )]),
            operator_state: BTreeMap::from([("dedupe".to_owned(), json!({"seen": 1}))]),
            sink_commit_metadata: BTreeMap::from([(
                "warehouse".to_owned(),
                json!({"tx": "checkpoint-000001"}),
            )]),
            schema_versions: BTreeMap::from([("checkpoint".to_owned(), 1)]),
            created_at_unix_ms: 1_820_000_000_001,
        })
        .expect("checkpoint should be valid");
    let claim = store
        .claim_latest_compatible(
            "run-000001",
            "release-2026-06-23",
            "deployment-rev-1",
            "sha256:plan",
            "worker-a",
            "lease-a",
            100,
            200,
        )
        .expect("initial claim should succeed")
        .claim;
    (Arc::new(Mutex::new(store)), claim)
}

#[test]
fn renewal_and_takeover_interleavings_preserve_a_single_fenced_owner() {
    loom::model(|| {
        let (store, initial_claim) = seeded_store();
        let renewal_store = Arc::clone(&store);
        let renewal_claim = initial_claim.clone();
        let renewal = thread::spawn(move || {
            renewal_store
                .lock()
                .expect("checkpoint store mutex should not be poisoned")
                .renew_claim(&renewal_claim, 150, 300)
        });
        let takeover_store = Arc::clone(&store);
        let takeover = thread::spawn(move || {
            takeover_store
                .lock()
                .expect("checkpoint store mutex should not be poisoned")
                .claim_latest_compatible(
                    "run-000001",
                    "release-2026-06-23",
                    "deployment-rev-1",
                    "sha256:plan",
                    "worker-b",
                    "lease-b",
                    201,
                    400,
                )
                .map(|recovery| recovery.claim)
        });

        let renewal_result = renewal.join().expect("renewal thread should finish");
        let takeover_result = takeover.join().expect("takeover thread should finish");
        assert_ne!(renewal_result.is_ok(), takeover_result.is_ok());
        match renewal_result {
            Ok(renewed) => {
                let takeover_error =
                    takeover_result.expect_err("takeover must lose when renewal linearizes first");
                assert!(matches!(
                    takeover_error,
                    CheckpointStoreError::ActiveRecoveryClaim {
                        ref worker_id,
                        ref lease_id,
                        expires_at_unix_ms: 300,
                        ..
                    } if worker_id == "worker-a" && lease_id == "lease-a"
                ));
                assert_eq!(renewed.fencing_epoch, 1);
                let mut locked = store
                    .lock()
                    .expect("checkpoint store mutex should not be poisoned");
                locked
                    .complete_claim(&renewed, 250)
                    .expect("renewed owner should complete");
                let next = locked
                    .claim_latest_compatible(
                        "run-000001",
                        "release-2026-06-23",
                        "deployment-rev-1",
                        "sha256:plan",
                        "worker-c",
                        "lease-c",
                        260,
                        500,
                    )
                    .expect("completion should release the claim");
                assert_eq!(next.claim.fencing_epoch, 2);
            }
            Err(renewal_error) => {
                assert!(matches!(
                    renewal_error,
                    CheckpointStoreError::RecoveryClaimMismatch { .. }
                ));
                let replacement =
                    takeover_result.expect("takeover must win when it linearizes before renewal");
                assert_eq!(replacement.fencing_epoch, 2);
                let mut locked = store
                    .lock()
                    .expect("checkpoint store mutex should not be poisoned");
                assert!(matches!(
                    locked.complete_claim(&initial_claim, 250),
                    Err(CheckpointStoreError::RecoveryClaimMismatch { .. })
                ));
                locked
                    .complete_claim(&replacement, 250)
                    .expect("replacement owner should complete");
                let next = locked
                    .claim_latest_compatible(
                        "run-000001",
                        "release-2026-06-23",
                        "deployment-rev-1",
                        "sha256:plan",
                        "worker-c",
                        "lease-c",
                        260,
                        500,
                    )
                    .expect("replacement completion should release the claim");
                assert_eq!(next.claim.fencing_epoch, 3);
            }
        }
    });
}

#[test]
fn completion_and_takeover_interleavings_reject_the_stale_owner() {
    loom::model(|| {
        let (store, initial_claim) = seeded_store();
        let completion_store = Arc::clone(&store);
        let completion_claim = initial_claim.clone();
        let completion = thread::spawn(move || {
            completion_store
                .lock()
                .expect("checkpoint store mutex should not be poisoned")
                .complete_claim(&completion_claim, 150)
        });
        let takeover_store = Arc::clone(&store);
        let takeover = thread::spawn(move || {
            takeover_store
                .lock()
                .expect("checkpoint store mutex should not be poisoned")
                .claim_latest_compatible(
                    "run-000001",
                    "release-2026-06-23",
                    "deployment-rev-1",
                    "sha256:plan",
                    "worker-b",
                    "lease-b",
                    201,
                    400,
                )
                .map(|recovery| recovery.claim)
        });

        let completion_result = completion.join().expect("completion thread should finish");
        let replacement = takeover
            .join()
            .expect("takeover thread should finish")
            .expect("takeover should succeed after completion or expiry");
        assert_eq!(replacement.fencing_epoch, 2);
        assert!(
            completion_result.is_ok()
                || matches!(
                    completion_result,
                    Err(CheckpointStoreError::RecoveryClaimMismatch { .. })
                )
        );

        let mut locked = store
            .lock()
            .expect("checkpoint store mutex should not be poisoned");
        assert!(matches!(
            locked.complete_claim(&initial_claim, 250),
            Err(CheckpointStoreError::RecoveryClaimMismatch { .. })
        ));
        locked
            .complete_claim(&replacement, 250)
            .expect("replacement owner should complete");
    });
}

#[test]
fn concurrent_initial_claims_select_exactly_one_owner_and_advance_the_fence() {
    loom::model(|| {
        let (store, initial_claim) = seeded_store();
        store
            .lock()
            .expect("checkpoint store mutex should not be poisoned")
            .complete_claim(&initial_claim, 150)
            .expect("fixture claim should complete");

        let worker_a_store = Arc::clone(&store);
        let worker_a = thread::spawn(move || {
            worker_a_store
                .lock()
                .expect("checkpoint store mutex should not be poisoned")
                .claim_latest_compatible(
                    "run-000001",
                    "release-2026-06-23",
                    "deployment-rev-1",
                    "sha256:plan",
                    "worker-b",
                    "lease-b",
                    160,
                    300,
                )
                .map(|recovery| recovery.claim)
        });
        let worker_b_store = Arc::clone(&store);
        let worker_b = thread::spawn(move || {
            worker_b_store
                .lock()
                .expect("checkpoint store mutex should not be poisoned")
                .claim_latest_compatible(
                    "run-000001",
                    "release-2026-06-23",
                    "deployment-rev-1",
                    "sha256:plan",
                    "worker-c",
                    "lease-c",
                    160,
                    300,
                )
                .map(|recovery| recovery.claim)
        });

        let results = [
            worker_a.join().expect("worker-b thread should finish"),
            worker_b.join().expect("worker-c thread should finish"),
        ];
        assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(
            results
                .iter()
                .filter(|result| {
                    matches!(
                        result,
                        Err(CheckpointStoreError::ActiveRecoveryClaim { .. })
                    )
                })
                .count(),
            1
        );
        let winner = results
            .into_iter()
            .find_map(Result::ok)
            .expect("one worker should own the claim");
        assert_eq!(winner.fencing_epoch, 2);

        let mut locked = store
            .lock()
            .expect("checkpoint store mutex should not be poisoned");
        locked
            .complete_claim(&winner, 200)
            .expect("winning owner should complete");
        let next = locked
            .claim_latest_compatible(
                "run-000001",
                "release-2026-06-23",
                "deployment-rev-1",
                "sha256:plan",
                "worker-d",
                "lease-d",
                210,
                400,
            )
            .expect("completion should allow another owner");
        assert_eq!(next.claim.fencing_epoch, 3);
    });
}

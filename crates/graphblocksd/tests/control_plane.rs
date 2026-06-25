use graphblocks_protocol::{
    BlockCapability, WORKER_PROTOCOL_VERSION, WorkerAdvertisement, WorkerState,
};
use graphblocksd::{DaemonConfig, DaemonConfigError, WorkerRegistry};

#[test]
fn daemon_config_validates_identity_protocol_and_capacity() {
    assert_eq!(
        DaemonConfig::new(" ", "127.0.0.1:8080").validate(),
        Err(DaemonConfigError::EmptyDaemonId)
    );
    assert_eq!(
        DaemonConfig::new("daemon-1", " ").validate(),
        Err(DaemonConfigError::EmptyBindAddress)
    );
    assert_eq!(
        DaemonConfig::new("daemon-1", "127.0.0.1:8080")
            .with_max_workers(0)
            .validate(),
        Err(DaemonConfigError::ZeroMaxWorkers)
    );
    assert_eq!(
        DaemonConfig::new("daemon-1", "127.0.0.1:8080")
            .with_protocol_version(WORKER_PROTOCOL_VERSION + 1)
            .validate(),
        Err(DaemonConfigError::UnsupportedProtocolVersion {
            expected: WORKER_PROTOCOL_VERSION,
            actual: WORKER_PROTOCOL_VERSION + 1,
        })
    );
}

#[test]
fn worker_registry_admits_ready_workers_and_reports_status() -> Result<(), DaemonConfigError> {
    let config = DaemonConfig::new("daemon-1", "127.0.0.1:8080")
        .require_package_lock_hash("sha256:package-lock")
        .with_max_workers(4);
    let mut registry = WorkerRegistry::new(config)?;
    let advertisement = WorkerAdvertisement::new(
        "worker-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("document.parse@1")],
    );

    let decision = registry.admit_worker(advertisement);
    let status = registry.status();

    assert!(decision.admitted);
    assert_eq!(registry.ready_worker_ids(), vec!["worker-1"]);
    assert_eq!(status.daemon_id, "daemon-1");
    assert_eq!(status.ready_workers, 1);
    assert_eq!(status.admitted_workers, 1);
    assert_eq!(status.rejected_workers, 0);
    assert_eq!(status.protocol_version, WORKER_PROTOCOL_VERSION);
    Ok(())
}

#[test]
fn worker_registry_rejects_unready_or_mismatched_workers() -> Result<(), DaemonConfigError> {
    let config = DaemonConfig::new("daemon-1", "127.0.0.1:8080")
        .require_package_lock_hash("sha256:package-lock");
    let mut registry = WorkerRegistry::new(config)?;
    let mismatched = WorkerAdvertisement::new(
        "worker-mismatch",
        "doc-cpu",
        "sha256:other-lock",
        "sha256:image",
        [BlockCapability::new("document.parse@1")],
    );
    let draining = WorkerAdvertisement::new(
        "worker-draining",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("document.parse@1")],
    )
    .with_state(WorkerState::Draining);

    let mismatch_decision = registry.admit_worker(mismatched);
    let draining_decision = registry.admit_worker(draining);
    let status = registry.status();

    assert!(!mismatch_decision.admitted);
    assert_eq!(
        mismatch_decision.reason_codes,
        vec!["worker.incompatible_package_lock"]
    );
    assert!(!draining_decision.admitted);
    assert_eq!(draining_decision.reason_codes, vec!["worker.not_ready"]);
    assert!(registry.ready_worker_ids().is_empty());
    assert_eq!(status.admitted_workers, 0);
    assert_eq!(status.rejected_workers, 2);
    Ok(())
}

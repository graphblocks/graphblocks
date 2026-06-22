use std::collections::BTreeMap;

use graphblocks_flow::lease_pool::{LeaseError, LeasePool, LeaseRequest};
use serde_json::json;

#[test]
fn lease_pool_reserves_units_and_releases_on_drop() -> Result<(), LeaseError> {
    let pool = LeasePool::new("licensed-tool", 8)?;
    let first = pool.try_acquire(LeaseRequest::new("run-1", 5))?;

    assert_eq!(pool.available_units(), 3);
    assert_eq!(
        pool.try_acquire(LeaseRequest::new("run-2", 4)).err(),
        Some(LeaseError::CapacityExhausted {
            pool_id: "licensed-tool".to_owned(),
            requested_units: 4,
            available_units: 3,
        }),
    );

    drop(first);
    assert_eq!(pool.available_units(), 8);
    Ok(())
}

#[test]
fn lease_pool_assigns_monotonic_fencing_tokens() -> Result<(), LeaseError> {
    let pool = LeasePool::new("licensed-tool", 2)?;
    let first = pool.try_acquire(LeaseRequest::new("run-1", 1))?;
    let first_token = first.fencing_token();
    drop(first);
    let second = pool.try_acquire(LeaseRequest::new("run-1", 1))?;

    assert!(second.fencing_token() > first_token);
    Ok(())
}

#[test]
fn stale_fencing_token_cannot_commit() -> Result<(), LeaseError> {
    let pool = LeasePool::new("licensed-tool", 1)?;
    let first = pool.try_acquire(LeaseRequest::new("worker", 1))?;
    let stale_token = first.fencing_token();
    drop(first);
    let current = pool.try_acquire(LeaseRequest::new("worker", 1))?;

    assert_eq!(
        pool.validate_fencing_token(current.lease_id(), stale_token),
        Err(LeaseError::StaleFencingToken {
            pool_id: "licensed-tool".to_owned(),
            lease_id: current.lease_id().to_owned(),
        }),
    );
    assert!(
        pool.validate_fencing_token(current.lease_id(), current.fencing_token())
            .is_ok()
    );
    Ok(())
}

#[test]
fn lease_request_preserves_owner_and_attribute_selector() -> Result<(), LeaseError> {
    let pool = LeasePool::new("licensed-tool", 2)?;
    let request = LeaseRequest::new("run-1", 1)
        .with_attribute_selector(BTreeMap::from([("region".to_owned(), json!("us-east-1"))]));
    let lease = pool.try_acquire(request)?;

    assert_eq!(lease.owner(), "run-1");
    assert_eq!(lease.attributes().get("region"), Some(&json!("us-east-1")),);
    Ok(())
}

#[test]
fn lease_pool_rejects_invalid_capacity_and_units() {
    assert!(matches!(
        LeasePool::new("bad", 0),
        Err(LeaseError::InvalidCapacity),
    ));
    assert!(matches!(
        LeasePool::new("licensed-tool", 1)
            .expect("valid pool")
            .try_acquire(LeaseRequest::new("run", 0)),
        Err(LeaseError::InvalidUnits),
    ));
}

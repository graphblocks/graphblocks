use std::collections::BTreeMap;
use std::time::{Duration, SystemTime};

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
fn lease_renewal_extends_expiration_and_rotates_fencing_token() -> Result<(), LeaseError> {
    let pool = LeasePool::new("licensed-tool", 1)?;
    let acquired_at = SystemTime::UNIX_EPOCH + Duration::from_secs(10);
    let expires_at = acquired_at + Duration::from_secs(5);
    let lease = pool.try_acquire_at(
        LeaseRequest::new("run-1", 1).with_expires_at(expires_at),
        acquired_at,
    )?;
    let stale_token = lease.fencing_token();
    let renewed_expires_at = expires_at + Duration::from_secs(10);

    let renewed_token = pool.renew_at(
        lease.lease_id(),
        stale_token,
        renewed_expires_at,
        acquired_at + Duration::from_secs(3),
    )?;

    assert!(renewed_token > stale_token);
    assert_eq!(lease.fencing_token(), renewed_token);
    assert_eq!(lease.expires_at(), Some(renewed_expires_at));
    assert_eq!(
        pool.validate_fencing_token_at(
            lease.lease_id(),
            stale_token,
            acquired_at + Duration::from_secs(4),
        ),
        Err(LeaseError::StaleFencingToken {
            pool_id: "licensed-tool".to_owned(),
            lease_id: lease.lease_id().to_owned(),
        }),
    );
    assert_eq!(
        pool.renew_at(
            lease.lease_id(),
            stale_token,
            renewed_expires_at + Duration::from_secs(5),
            acquired_at + Duration::from_secs(4),
        ),
        Err(LeaseError::StaleFencingToken {
            pool_id: "licensed-tool".to_owned(),
            lease_id: lease.lease_id().to_owned(),
        }),
    );
    assert!(
        pool.validate_fencing_token_at(
            lease.lease_id(),
            renewed_token,
            acquired_at + Duration::from_secs(4),
        )
        .is_ok()
    );
    Ok(())
}

#[test]
fn expired_lease_fencing_token_is_not_valid_authority() -> Result<(), LeaseError> {
    let pool = LeasePool::new("licensed-tool", 1)?;
    let acquired_at = SystemTime::UNIX_EPOCH + Duration::from_secs(10);
    let expires_at = acquired_at + Duration::from_secs(5);
    let lease = pool.try_acquire_at(
        LeaseRequest::new("run-1", 1).with_expires_at(expires_at),
        acquired_at,
    )?;
    let lease_id = lease.lease_id().to_owned();
    let fencing_token = lease.fencing_token();

    assert!(
        pool.validate_fencing_token_at(
            &lease_id,
            fencing_token,
            expires_at - Duration::from_millis(1),
        )
        .is_ok()
    );
    assert_eq!(
        pool.validate_fencing_token_at(&lease_id, fencing_token, expires_at),
        Err(LeaseError::UnknownLease {
            pool_id: "licensed-tool".to_owned(),
            lease_id,
        }),
    );
    assert_eq!(pool.available_units(), 1);
    assert!(!lease.release());
    Ok(())
}

#[test]
fn expired_lease_cannot_be_renewed() -> Result<(), LeaseError> {
    let pool = LeasePool::new("licensed-tool", 1)?;
    let acquired_at = SystemTime::UNIX_EPOCH + Duration::from_secs(10);
    let expires_at = acquired_at + Duration::from_secs(5);
    let lease = pool.try_acquire_at(
        LeaseRequest::new("run-1", 1).with_expires_at(expires_at),
        acquired_at,
    )?;

    assert_eq!(
        pool.renew_at(
            lease.lease_id(),
            lease.fencing_token(),
            expires_at + Duration::from_secs(10),
            expires_at,
        ),
        Err(LeaseError::UnknownLease {
            pool_id: "licensed-tool".to_owned(),
            lease_id: lease.lease_id().to_owned(),
        }),
    );
    assert_eq!(pool.available_units(), 1);
    assert!(!lease.release());
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

#[test]
fn expired_leases_are_reaped_without_reusing_fencing_tokens() -> Result<(), LeaseError> {
    let pool = LeasePool::new("licensed-tool", 1)?;
    let acquired_at = SystemTime::UNIX_EPOCH + Duration::from_secs(10);
    let expires_at = acquired_at + Duration::from_secs(5);
    let first = pool.try_acquire_at(
        LeaseRequest::new("run-1", 1).with_expires_at(expires_at),
        acquired_at,
    )?;
    let first_token = first.fencing_token();

    assert_eq!(first.expires_at(), Some(expires_at));
    assert_eq!(pool.available_units(), 0);
    assert_eq!(pool.reap_expired(acquired_at + Duration::from_secs(4)), 0);
    assert_eq!(pool.available_units(), 0);
    assert_eq!(pool.reap_expired(expires_at), 1);
    assert_eq!(pool.available_units(), 1);

    let second = pool.try_acquire_at(
        LeaseRequest::new("run-2", 1),
        expires_at + Duration::from_secs(1),
    )?;

    assert!(second.fencing_token() > first_token);
    assert!(!first.release());
    Ok(())
}

#[test]
fn lease_pool_rejects_expiration_not_after_acquisition() -> Result<(), LeaseError> {
    let pool = LeasePool::new("licensed-tool", 1)?;
    let acquired_at = SystemTime::UNIX_EPOCH + Duration::from_secs(10);

    assert!(matches!(
        pool.try_acquire_at(
            LeaseRequest::new("run-1", 1).with_expires_at(acquired_at),
            acquired_at,
        ),
        Err(LeaseError::InvalidExpiration),
    ));
    Ok(())
}

#[test]
fn lease_pool_rejects_authority_checks_before_lease_acquisition() -> Result<(), LeaseError> {
    let pool = LeasePool::new("licensed-tool", 1)?;
    let acquired_at = SystemTime::UNIX_EPOCH + Duration::from_secs(10);
    let attempted_at = acquired_at - Duration::from_millis(1);
    let expires_at = acquired_at + Duration::from_secs(10);
    let lease = pool.try_acquire_at(
        LeaseRequest::new("run-1", 1).with_expires_at(expires_at),
        acquired_at,
    )?;
    let expected = || LeaseError::NotYetActive {
        pool_id: "licensed-tool".to_owned(),
        lease_id: lease.lease_id().to_owned(),
        acquired_at,
        attempted_at,
    };

    assert_eq!(
        pool.validate_fencing_token_at(lease.lease_id(), lease.fencing_token(), attempted_at),
        Err(expected())
    );
    assert_eq!(
        pool.renew_at(
            lease.lease_id(),
            lease.fencing_token(),
            expires_at + Duration::from_secs(10),
            attempted_at,
        ),
        Err(expected())
    );
    Ok(())
}

#[test]
fn lease_pool_rejects_renewal_that_does_not_extend_expiration() -> Result<(), LeaseError> {
    let pool = LeasePool::new("licensed-tool", 1)?;
    let acquired_at = SystemTime::UNIX_EPOCH + Duration::from_secs(10);
    let expires_at = acquired_at + Duration::from_secs(20);
    let lease = pool.try_acquire_at(
        LeaseRequest::new("run-1", 1).with_expires_at(expires_at),
        acquired_at,
    )?;
    let fencing_token = lease.fencing_token();

    for shortened_expiration in [expires_at, expires_at - Duration::from_millis(1)] {
        assert_eq!(
            pool.renew_at(
                lease.lease_id(),
                fencing_token,
                shortened_expiration,
                acquired_at + Duration::from_secs(1),
            ),
            Err(LeaseError::InvalidExpiration)
        );
    }
    assert_eq!(lease.expires_at(), Some(expires_at));
    assert_eq!(lease.fencing_token(), fencing_token);
    Ok(())
}

#[test]
fn lease_pool_rejects_renewal_of_unbounded_lease() -> Result<(), LeaseError> {
    let pool = LeasePool::new("licensed-tool", 1)?;
    let acquired_at = SystemTime::UNIX_EPOCH + Duration::from_secs(10);
    let lease = pool.try_acquire_at(LeaseRequest::new("run-1", 1), acquired_at)?;
    let fencing_token = lease.fencing_token();

    assert_eq!(
        pool.renew_at(
            lease.lease_id(),
            fencing_token,
            acquired_at + Duration::from_secs(20),
            acquired_at + Duration::from_secs(1),
        ),
        Err(LeaseError::InvalidExpiration)
    );
    assert_eq!(lease.expires_at(), None);
    assert_eq!(lease.fencing_token(), fencing_token);
    Ok(())
}

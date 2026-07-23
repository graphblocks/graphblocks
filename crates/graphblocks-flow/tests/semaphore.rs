use graphblocks_flow::semaphore::{LocalSemaphore, SemaphoreError};

#[test]
fn semaphore_enforces_concurrency_limit_and_releases_on_drop() -> Result<(), SemaphoreError> {
    let semaphore = LocalSemaphore::new("document-convert", 2)?;
    let first = semaphore.try_acquire("node-1")?;
    let second = semaphore.try_acquire("node-2")?;

    assert_eq!(semaphore.available(), 0);
    assert_eq!(
        semaphore.try_acquire("node-3").err(),
        Some(SemaphoreError::CapacityExhausted {
            semaphore_id: "document-convert".to_owned()
        }),
    );

    drop(first);
    assert_eq!(semaphore.available(), 1);
    let third = semaphore.try_acquire("node-3")?;
    assert_eq!(semaphore.available(), 0);

    drop(second);
    drop(third);
    assert_eq!(semaphore.available(), 2);
    Ok(())
}

#[test]
fn semaphore_rejects_zero_limit() {
    assert!(matches!(
        LocalSemaphore::new("bad", 0),
        Err(SemaphoreError::InvalidLimit),
    ));
}

#[test]
fn explicit_release_is_idempotent() -> Result<(), SemaphoreError> {
    let semaphore = LocalSemaphore::new("limited", 1)?;
    let permit = semaphore.try_acquire("node")?;

    assert_eq!(semaphore.available(), 0);
    assert!(permit.release());
    assert!(!permit.release());
    assert_eq!(semaphore.available(), 1);
    Ok(())
}

#[test]
fn semaphore_rejects_noncanonical_identities() {
    assert!(matches!(
        LocalSemaphore::new(" semaphore ", 1),
        Err(SemaphoreError::InvalidIdentity { field: "id" })
    ));

    let semaphore = LocalSemaphore::new("semaphore", 1).expect("valid semaphore");
    assert!(matches!(
        semaphore.try_acquire("\t"),
        Err(SemaphoreError::InvalidIdentity { field: "owner" })
    ));
    assert_eq!(semaphore.available(), 1);
}

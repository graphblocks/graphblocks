use graphblocks_runtime_core::record_store::{
    DeleteOptions, InMemoryRecordStore, Record, RecordFilter, RecordQuery, RecordStoreError,
    WriteOptions,
};
use serde_json::json;

#[test]
fn record_store_put_get_assigns_monotonic_revision_and_etag() -> Result<(), RecordStoreError> {
    let mut store = InMemoryRecordStore::new();

    let created = store.put(
        "tickets",
        Record::new("ticket-1", json!({"status": "open", "priority": 2}))
            .with_metadata("tenant", json!("acme")),
        WriteOptions::create_only(),
    )?;
    let updated = store.put(
        "tickets",
        Record::new("ticket-1", json!({"status": "closed", "priority": 2})),
        WriteOptions::new().with_expected_revision(created.revision),
    )?;

    assert_eq!(created.collection, "tickets");
    assert_eq!(created.key, "ticket-1");
    assert_eq!(created.revision, 1);
    assert_eq!(updated.revision, 2);
    assert_ne!(created.etag, updated.etag);
    assert_eq!(store.get("tickets", "ticket-1")?, Some(updated));
    Ok(())
}

#[test]
fn record_store_compare_and_swap_rejects_stale_revision() -> Result<(), RecordStoreError> {
    let mut store = InMemoryRecordStore::new();
    let created = store.put(
        "tickets",
        Record::new("ticket-1", json!({"status": "open"})),
        WriteOptions::new(),
    )?;
    store.put(
        "tickets",
        Record::new("ticket-1", json!({"status": "pending"})),
        WriteOptions::new().with_expected_revision(created.revision),
    )?;

    let error = store
        .put(
            "tickets",
            Record::new("ticket-1", json!({"status": "closed"})),
            WriteOptions::new().with_expected_revision(created.revision),
        )
        .expect_err("stale revision must be rejected");

    assert_eq!(
        error,
        RecordStoreError::RevisionConflict {
            collection: "tickets".to_owned(),
            key: "ticket-1".to_owned(),
            expected_revision: created.revision,
            current_revision: 2,
        }
    );
    assert_eq!(
        store
            .get("tickets", "ticket-1")?
            .expect("record remains")
            .value,
        json!({"status": "pending"})
    );
    Ok(())
}

#[test]
fn record_store_query_filters_collection_and_paginates_by_key() -> Result<(), RecordStoreError> {
    let mut store = InMemoryRecordStore::new();
    for (key, status, priority) in [
        ("ticket-1", "open", 3),
        ("ticket-2", "closed", 1),
        ("ticket-3", "open", 1),
    ] {
        store.put(
            "tickets",
            Record::new(key, json!({"status": status, "priority": priority})),
            WriteOptions::new(),
        )?;
    }
    store.put(
        "users",
        Record::new("ticket-0", json!({"status": "open"})),
        WriteOptions::new(),
    )?;

    let first = store.query(
        RecordQuery::new("tickets")
            .with_filter(RecordFilter::equals(["status"], json!("open")))
            .with_limit(1),
    )?;
    let second = store.query(
        RecordQuery::new("tickets")
            .with_filter(RecordFilter::equals(["status"], json!("open")))
            .with_limit(10)
            .with_cursor(first.next_cursor.clone().expect("first page has cursor")),
    )?;

    assert_eq!(
        first
            .records
            .iter()
            .map(|record| &record.key)
            .collect::<Vec<_>>(),
        vec!["ticket-1"]
    );
    assert_eq!(
        second
            .records
            .iter()
            .map(|record| &record.key)
            .collect::<Vec<_>>(),
        vec!["ticket-3"]
    );
    assert_eq!(second.next_cursor, None);
    Ok(())
}

#[test]
fn record_store_delete_honors_expected_revision_and_allow_missing() -> Result<(), RecordStoreError>
{
    let mut store = InMemoryRecordStore::new();
    let created = store.put(
        "tickets",
        Record::new("ticket-1", json!({"status": "open"})),
        WriteOptions::new(),
    )?;

    assert_eq!(
        store.delete(
            "tickets",
            "ticket-1",
            DeleteOptions::new().with_expected_revision(created.revision + 1),
        ),
        Err(RecordStoreError::RevisionConflict {
            collection: "tickets".to_owned(),
            key: "ticket-1".to_owned(),
            expected_revision: created.revision + 1,
            current_revision: created.revision,
        })
    );

    store.delete(
        "tickets",
        "ticket-1",
        DeleteOptions::new().with_expected_revision(created.revision),
    )?;
    assert_eq!(store.get("tickets", "ticket-1")?, None);
    assert_eq!(
        store.delete("tickets", "ticket-1", DeleteOptions::new()),
        Err(RecordStoreError::NotFound {
            collection: "tickets".to_owned(),
            key: "ticket-1".to_owned(),
        })
    );
    store.delete(
        "tickets",
        "ticket-1",
        DeleteOptions::new().with_allow_missing(true),
    )?;
    Ok(())
}

#[test]
fn record_store_rejects_blank_collection_and_key() {
    let mut store = InMemoryRecordStore::new();

    assert_eq!(
        store.put("   ", Record::new("ticket-1", json!({})), WriteOptions::new()),
        Err(RecordStoreError::InvalidCollection)
    );
    assert_eq!(
        store.put("tickets", Record::new("   ", json!({})), WriteOptions::new()),
        Err(RecordStoreError::InvalidKey)
    );
    assert_eq!(
        store.get("   ", "ticket-1"),
        Err(RecordStoreError::InvalidCollection)
    );
    assert_eq!(store.get("tickets", "   "), Err(RecordStoreError::InvalidKey));
    assert_eq!(
        store.query(RecordQuery::new("   ")),
        Err(RecordStoreError::InvalidCollection)
    );
    assert_eq!(
        store.delete("tickets", "   ", DeleteOptions::new()),
        Err(RecordStoreError::InvalidKey)
    );
}

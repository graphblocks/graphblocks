use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use graphblocks_runtime_core::resource_scope::{ResourceError, ResourceScope};

fn lock_events<'a>(events: &'a Mutex<Vec<&'static str>>) -> MutexGuard<'a, Vec<&'static str>> {
    events.lock().unwrap_or_else(PoisonError::into_inner)
}

#[test]
fn resource_scope_runs_finalizers_once_in_lifo_order() -> Result<(), ResourceError> {
    let events = Arc::new(Mutex::new(Vec::new()));
    let mut scope = ResourceScope::new("node.render");

    {
        let events = Arc::clone(&events);
        scope.defer(move || lock_events(&events).push("first"));
    }
    {
        let events = Arc::clone(&events);
        scope.defer(move || lock_events(&events).push("second"));
    }

    assert!(scope.close());
    assert!(!scope.close());
    assert_eq!(&*lock_events(&events), &["second", "first"]);
    Ok(())
}

#[test]
fn resource_scope_rejects_new_finalizers_after_close() {
    let mut scope = ResourceScope::new("node.closed");

    assert!(scope.close());
    assert_eq!(
        scope.try_defer(|| {}),
        Err(ResourceError::ScopeClosed {
            scope_id: "node.closed".to_owned()
        }),
    );
}

#[test]
fn resource_scope_drop_runs_pending_finalizers() {
    let events = Arc::new(Mutex::new(Vec::new()));

    {
        let mut scope = ResourceScope::new("node.drop");
        let events = Arc::clone(&events);
        scope.defer(move || lock_events(&events).push("released"));
    }

    assert_eq!(&*lock_events(&events), &["released"]);
}

#[test]
fn resource_scope_reports_id_and_closed_state() {
    let mut scope = ResourceScope::new("run.1");

    assert_eq!(scope.id(), "run.1");
    assert!(!scope.is_closed());
    assert!(scope.close());
    assert!(scope.is_closed());
}

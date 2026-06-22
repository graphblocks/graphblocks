use std::sync::{Arc, Mutex, Weak};

use crate::outcome::CancelReason;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CancellationScope {
    ProviderCall,
    Node,
    Branch,
    TaskGroup,
    AgentStep,
    Turn,
    MapItem,
    Task,
    Trial,
    Run,
    Job,
    Session,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub enum CancellationGuarantee {
    ImmediateLocal,
    Cooperative,
    BestEffortRemote,
    NonCancellableAtomicSection,
}

impl CancellationGuarantee {
    pub fn effective(requested: Self, capability: Self) -> Self {
        requested.max(capability)
    }
}

#[derive(Clone, Debug)]
pub struct CancellationToken {
    inner: Arc<Inner>,
}

#[derive(Debug)]
struct Inner {
    scope: CancellationScope,
    guarantee: CancellationGuarantee,
    reason: Mutex<Option<CancelReason>>,
    children: Mutex<Vec<Weak<Inner>>>,
}

impl CancellationToken {
    pub fn new(scope: CancellationScope, guarantee: CancellationGuarantee) -> Self {
        Self {
            inner: Arc::new(Inner {
                scope,
                guarantee,
                reason: Mutex::new(None),
                children: Mutex::new(Vec::new()),
            }),
        }
    }

    pub fn scope(&self) -> CancellationScope {
        self.inner.scope
    }

    pub fn guarantee(&self) -> CancellationGuarantee {
        self.inner.guarantee
    }

    pub fn is_cancelled(&self) -> bool {
        self.reason().is_some()
    }

    pub fn reason(&self) -> Option<CancelReason> {
        self.inner
            .reason
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }

    pub fn child(
        &self,
        scope: CancellationScope,
        guarantee: CancellationGuarantee,
    ) -> CancellationToken {
        let child = Self::new(scope, guarantee);
        self.inner
            .children
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push(Arc::downgrade(&child.inner));
        if let Some(reason) = self.reason() {
            child.cancel(reason);
        }
        child
    }

    pub fn cancel(&self, reason: CancelReason) -> bool {
        let children = {
            let mut stored_reason = self
                .inner
                .reason
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if stored_reason.is_some() {
                return false;
            }
            *stored_reason = Some(reason.clone());

            let mut children = self
                .inner
                .children
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            children.retain(|child| child.strong_count() > 0);
            children
                .iter()
                .filter_map(Weak::upgrade)
                .collect::<Vec<_>>()
        };

        for child in children {
            CancellationToken { inner: child }.cancel(reason.clone());
        }
        true
    }
}

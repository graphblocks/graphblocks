use std::collections::VecDeque;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use graphblocks_runtime_core::outcome::{BlockError, CancelReason};

#[derive(Clone, Debug, PartialEq)]
pub enum SequenceState {
    Open,
    Completed,
    Failed(BlockError),
    Cancelled(CancelReason),
}

impl SequenceState {
    pub fn is_terminal(&self) -> bool {
        !matches!(self, Self::Open)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum SequenceError {
    InvalidCapacity,
    Full { capacity: usize },
    Closed { state: SequenceState },
    AlreadyTerminal { state: SequenceState },
}

#[derive(Clone, Debug)]
pub struct SequenceSender<T> {
    inner: Arc<Mutex<Inner<T>>>,
}

#[derive(Clone, Debug)]
pub struct SequenceReceiver<T> {
    inner: Arc<Mutex<Inner<T>>>,
}

#[derive(Debug)]
struct Inner<T> {
    capacity: usize,
    state: SequenceState,
    items: VecDeque<T>,
}

pub fn bounded_sequence<T>(
    capacity: usize,
) -> Result<(SequenceSender<T>, SequenceReceiver<T>), SequenceError> {
    if capacity == 0 {
        return Err(SequenceError::InvalidCapacity);
    }

    let inner = Arc::new(Mutex::new(Inner {
        capacity,
        state: SequenceState::Open,
        items: VecDeque::with_capacity(capacity),
    }));
    Ok((
        SequenceSender {
            inner: Arc::clone(&inner),
        },
        SequenceReceiver { inner },
    ))
}

impl<T> SequenceSender<T> {
    pub fn capacity(&self) -> usize {
        self.lock().capacity
    }

    pub fn len(&self) -> usize {
        self.lock().items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn state(&self) -> SequenceState {
        self.lock().state.clone()
    }

    pub fn try_send(&self, item: T) -> Result<(), SequenceError> {
        let mut inner = self.lock();
        if inner.state.is_terminal() {
            return Err(SequenceError::Closed {
                state: inner.state.clone(),
            });
        }
        if inner.items.len() == inner.capacity {
            return Err(SequenceError::Full {
                capacity: inner.capacity,
            });
        }
        inner.items.push_back(item);
        Ok(())
    }

    pub fn complete(&self) -> Result<(), SequenceError> {
        self.finish(SequenceState::Completed)
    }

    pub fn fail(&self, error: BlockError) -> Result<(), SequenceError> {
        self.finish(SequenceState::Failed(error))
    }

    pub fn cancel(&self, reason: CancelReason) -> Result<(), SequenceError> {
        self.finish(SequenceState::Cancelled(reason))
    }

    fn finish(&self, state: SequenceState) -> Result<(), SequenceError> {
        let mut inner = self.lock();
        if inner.state.is_terminal() {
            return Err(SequenceError::AlreadyTerminal {
                state: inner.state.clone(),
            });
        }
        inner.state = state;
        Ok(())
    }

    fn lock(&self) -> MutexGuard<'_, Inner<T>> {
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

impl<T> SequenceReceiver<T> {
    pub fn capacity(&self) -> usize {
        self.lock().capacity
    }

    pub fn len(&self) -> usize {
        self.lock().items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn state(&self) -> SequenceState {
        self.lock().state.clone()
    }

    pub fn try_recv(&self) -> Option<T> {
        self.lock().items.pop_front()
    }

    fn lock(&self) -> MutexGuard<'_, Inner<T>> {
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

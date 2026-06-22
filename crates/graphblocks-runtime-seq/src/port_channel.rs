use std::sync::{Arc, Mutex, PoisonError};

use graphblocks_runtime_core::outcome::{BlockError, CancelReason};
use graphblocks_runtime_core::readiness::PortRef;
use graphblocks_runtime_core::typed_value::TypedValue;

use crate::bounded::{
    SequenceError, SequenceReceiver, SequenceSender, SequenceState, bounded_sequence,
};

#[derive(Clone, Debug, PartialEq)]
pub struct PortEnvelope {
    pub port: PortRef,
    pub item_sequence: u64,
    pub value: TypedValue,
}

#[derive(Clone, Debug, PartialEq)]
pub enum PortChannelError {
    Sequence(SequenceError),
}

impl From<SequenceError> for PortChannelError {
    fn from(error: SequenceError) -> Self {
        Self::Sequence(error)
    }
}

#[derive(Clone, Debug)]
pub struct PortSender {
    port: PortRef,
    sender: SequenceSender<PortEnvelope>,
    next_item_sequence: Arc<Mutex<u64>>,
}

#[derive(Clone, Debug)]
pub struct PortReceiver {
    receiver: SequenceReceiver<PortEnvelope>,
}

pub fn typed_port_channel(
    port: PortRef,
    capacity: usize,
) -> Result<(PortSender, PortReceiver), PortChannelError> {
    let (sender, receiver) = bounded_sequence(capacity)?;
    Ok((
        PortSender {
            port,
            sender,
            next_item_sequence: Arc::new(Mutex::new(1)),
        },
        PortReceiver { receiver },
    ))
}

impl PortSender {
    pub fn port(&self) -> &PortRef {
        &self.port
    }

    pub fn try_send(&self, value: TypedValue) -> Result<u64, PortChannelError> {
        let mut next_item_sequence = self
            .next_item_sequence
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        let item_sequence = *next_item_sequence;
        self.sender.try_send(PortEnvelope {
            port: self.port.clone(),
            item_sequence,
            value,
        })?;
        *next_item_sequence = next_item_sequence.saturating_add(1);
        Ok(item_sequence)
    }

    pub fn complete(&self) -> Result<(), PortChannelError> {
        self.sender.complete()?;
        Ok(())
    }

    pub fn fail(&self, error: BlockError) -> Result<(), PortChannelError> {
        self.sender.fail(error)?;
        Ok(())
    }

    pub fn cancel(&self, reason: CancelReason) -> Result<(), PortChannelError> {
        self.sender.cancel(reason)?;
        Ok(())
    }
}

impl PortReceiver {
    pub fn try_recv(&self) -> Option<PortEnvelope> {
        self.receiver.try_recv()
    }

    pub fn state(&self) -> SequenceState {
        self.receiver.state()
    }
}

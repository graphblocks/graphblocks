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
    ItemSequenceOverflow,
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
    next_item_sequence: Arc<Mutex<Option<u64>>>,
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
            next_item_sequence: Arc::new(Mutex::new(Some(1))),
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
        let item_sequence = (*next_item_sequence).ok_or(PortChannelError::ItemSequenceOverflow)?;
        self.sender.try_send(PortEnvelope {
            port: self.port.clone(),
            item_sequence,
            value,
        })?;
        *next_item_sequence = item_sequence.checked_add(1);
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

#[cfg(test)]
mod tests {
    use graphblocks_runtime_core::readiness::PortRef;
    use graphblocks_runtime_core::typed_value::{TypedValue, ValueEncoding};

    use super::{PortChannelError, typed_port_channel};

    fn value() -> TypedValue {
        TypedValue::new(
            "graphblocks.ai/Message",
            1,
            ValueEncoding::Json,
            br#"{"text":"value"}"#.to_vec(),
        )
    }

    #[test]
    fn maximum_item_sequence_is_emitted_once_then_exhausted() {
        let (sender, receiver) =
            typed_port_channel(PortRef::new("model", "response"), 2).expect("channel is valid");
        *sender
            .next_item_sequence
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(u64::MAX);

        assert_eq!(
            sender.try_send(value()),
            Ok(u64::MAX),
            "the last representable sequence remains usable"
        );
        assert_eq!(
            sender.try_send(value()),
            Err(PortChannelError::ItemSequenceOverflow)
        );
        assert_eq!(
            receiver.try_recv().map(|envelope| envelope.item_sequence),
            Some(u64::MAX)
        );
        assert_eq!(receiver.try_recv(), None);
    }
}

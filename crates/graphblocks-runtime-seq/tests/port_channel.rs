use graphblocks_runtime_core::outcome::{CancelCode, CancelReason};
use graphblocks_runtime_core::readiness::PortRef;
use graphblocks_runtime_core::typed_value::{TypedValue, ValueEncoding};
use graphblocks_runtime_seq::bounded::{SequenceError, SequenceState};
use graphblocks_runtime_seq::port_channel::{PortChannelError, PortEnvelope, typed_port_channel};

fn message_value(text: &str) -> TypedValue {
    TypedValue::new(
        "graphblocks.ai/Message",
        1,
        ValueEncoding::Json,
        format!("{{\"text\":\"{text}\"}}").into_bytes(),
    )
}

#[test]
fn port_channel_preserves_port_and_monotonic_item_sequence() -> Result<(), PortChannelError> {
    let port = PortRef::new("model", "response");
    let (sender, receiver) = typed_port_channel(port.clone(), 2)?;

    let first = sender.try_send(message_value("one"))?;
    let second = sender.try_send(message_value("two"))?;

    assert_eq!(first, 1);
    assert_eq!(second, 2);
    assert_eq!(
        receiver.try_recv(),
        Some(PortEnvelope {
            port: port.clone(),
            item_sequence: 1,
            value: message_value("one"),
        }),
    );
    assert_eq!(
        receiver.try_recv(),
        Some(PortEnvelope {
            port,
            item_sequence: 2,
            value: message_value("two"),
        }),
    );
    assert_eq!(receiver.try_recv(), None);
    Ok(())
}

#[test]
fn port_channel_enforces_capacity_and_terminal_state() -> Result<(), PortChannelError> {
    let (sender, receiver) = typed_port_channel(PortRef::new("model", "response"), 1)?;

    sender.try_send(message_value("one"))?;
    assert_eq!(
        sender.try_send(message_value("two")),
        Err(PortChannelError::Sequence(SequenceError::Full {
            capacity: 1
        })),
    );

    assert_eq!(
        receiver.try_recv().map(|envelope| envelope.item_sequence),
        Some(1)
    );
    sender.complete()?;
    assert_eq!(receiver.state(), SequenceState::Completed);
    assert_eq!(
        sender.try_send(message_value("late")),
        Err(PortChannelError::Sequence(SequenceError::Closed {
            state: SequenceState::Completed,
        })),
    );
    Ok(())
}

#[test]
fn port_channel_retains_cancelled_terminal_reason() -> Result<(), PortChannelError> {
    let (sender, receiver) = typed_port_channel(PortRef::new("tool", "result"), 1)?;
    let reason = CancelReason::new(CancelCode::UserCancel);

    sender.cancel(reason.clone())?;

    assert_eq!(receiver.state(), SequenceState::Cancelled(reason));
    Ok(())
}

#[test]
fn port_channel_rejects_zero_capacity() {
    assert_eq!(
        typed_port_channel(PortRef::new("model", "response"), 0).map(|_| ()),
        Err(PortChannelError::Sequence(SequenceError::InvalidCapacity)),
    );
}

use graphblocks_runtime_core::outcome::{BlockError, CancelCode, CancelReason, ErrorCategory};
use graphblocks_runtime_seq::bounded::{SequenceError, SequenceState, bounded_sequence};

#[test]
fn bounded_sequence_enforces_capacity_and_fifo_receive() -> Result<(), SequenceError> {
    let (sender, receiver) = bounded_sequence(2)?;

    sender.try_send("first")?;
    sender.try_send("second")?;
    assert_eq!(
        sender.try_send("third"),
        Err(SequenceError::Full { capacity: 2 })
    );

    assert_eq!(receiver.try_recv(), Some("first"));
    sender.try_send("third")?;
    assert_eq!(receiver.try_recv(), Some("second"));
    assert_eq!(receiver.try_recv(), Some("third"));
    assert_eq!(receiver.try_recv(), None);
    Ok(())
}

#[test]
fn terminal_signal_is_recorded_once_and_rejects_late_send() -> Result<(), SequenceError> {
    let (sender, receiver) = bounded_sequence(2)?;

    sender.try_send(1)?;
    sender.complete()?;

    assert_eq!(receiver.state(), SequenceState::Completed);
    assert_eq!(
        sender.complete(),
        Err(SequenceError::AlreadyTerminal {
            state: SequenceState::Completed
        }),
    );
    assert_eq!(
        sender.try_send(2),
        Err(SequenceError::Closed {
            state: SequenceState::Completed
        }),
    );
    assert_eq!(receiver.try_recv(), Some(1));
    assert_eq!(receiver.try_recv(), None);
    Ok(())
}

#[test]
fn failed_terminal_retains_canonical_error() -> Result<(), SequenceError> {
    let (sender, receiver) = bounded_sequence::<i32>(1)?;
    let error = BlockError::new(
        "provider.timeout",
        ErrorCategory::Timeout,
        "provider timed out",
        true,
    );

    sender.fail(error.clone())?;

    assert_eq!(receiver.state(), SequenceState::Failed(error));
    Ok(())
}

#[test]
fn cancelled_terminal_retains_cancel_reason() -> Result<(), SequenceError> {
    let (sender, receiver) = bounded_sequence::<i32>(1)?;
    let reason = CancelReason::new(CancelCode::UserCancel);

    sender.cancel(reason.clone())?;

    assert_eq!(receiver.state(), SequenceState::Cancelled(reason));
    Ok(())
}

#[test]
fn bounded_sequence_rejects_zero_capacity() {
    assert!(matches!(
        bounded_sequence::<i32>(0),
        Err(SequenceError::InvalidCapacity),
    ));
}

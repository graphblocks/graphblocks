use graphblocks_runtime_core::output_policy::PendingToolCallsDisposition;
use graphblocks_runtime_core::tool_call::{ToolCall, ToolCallDraft, ToolCallStatus};
use graphblocks_runtime_core::tool_execution::{ToolExecutionState, ToolPlanCall};
use graphblocks_runtime_seq::tool_queue::{SequentialToolQueue, SequentialToolQueueError};

fn tool_call(tool_call_id: &str, status: ToolCallStatus) -> ToolCall {
    let mut draft = ToolCallDraft::proposed("response-1", tool_call_id, "ticket.create");
    draft
        .append_argument_fragment("{\"resource_id\":\"ticket-1\"}")
        .expect("test argument fragment appends");
    let mut call = draft
        .into_completed_tool_call("resolved-tool-1", 1_000)
        .expect("test arguments are valid JSON");
    call.status = status;
    if status == ToolCallStatus::Admitted {
        call.admitted_at_unix_ms = Some(1_100);
    }
    call
}

#[test]
fn sequential_tool_queue_rejects_non_admitted_calls() {
    assert_eq!(
        SequentialToolQueue::new(
            "plan-1",
            "response-1",
            [ToolPlanCall::new(tool_call(
                "call-1",
                ToolCallStatus::Validated
            ))],
        )
        .map(|_| ()),
        Err(SequentialToolQueueError::ToolCallNotAdmitted {
            tool_call_id: "call-1".to_owned(),
            status: ToolCallStatus::Validated,
        }),
    );
}

#[test]
fn sequential_tool_queue_rejects_admitted_calls_without_admission_timestamp() {
    let mut call = tool_call("call-1", ToolCallStatus::Admitted);
    call.admitted_at_unix_ms = None;

    assert_eq!(
        SequentialToolQueue::new("plan-1", "response-1", [ToolPlanCall::new(call)]).map(|_| ()),
        Err(
            SequentialToolQueueError::ToolCallMissingAdmissionTimestamp {
                tool_call_id: "call-1".to_owned(),
            }
        ),
    );
}

#[test]
fn sequential_tool_queue_starts_one_ready_call_at_a_time() -> Result<(), SequentialToolQueueError> {
    let mut queue = SequentialToolQueue::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", ToolCallStatus::Admitted)),
            ToolPlanCall::new(tool_call("call-b", ToolCallStatus::Admitted)),
        ],
    )?;

    assert_eq!(queue.start_next_ready()?, Some("call-a".to_owned()));
    assert_eq!(queue.running_call_id(), Some("call-a"));
    assert_eq!(queue.start_next_ready()?, None);

    queue.record_completed("call-a")?;
    assert_eq!(queue.running_call_id(), None);
    assert_eq!(queue.state("call-a"), Some(ToolExecutionState::Completed));
    assert_eq!(queue.start_next_ready()?, Some("call-b".to_owned()));
    assert_eq!(queue.running_call_id(), Some("call-b"));
    Ok(())
}

#[test]
fn sequential_tool_queue_waits_for_dependencies() -> Result<(), SequentialToolQueueError> {
    let mut dependent = tool_call("call-b", ToolCallStatus::Admitted);
    dependent.depends_on = vec!["call-a".to_owned()];
    let mut queue = SequentialToolQueue::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", ToolCallStatus::Admitted)),
            ToolPlanCall::new(dependent),
        ],
    )?;

    assert_eq!(queue.start_next_ready()?, Some("call-a".to_owned()));
    queue.record_completed("call-a")?;
    assert_eq!(queue.start_next_ready()?, Some("call-b".to_owned()));
    Ok(())
}

#[test]
fn sequential_tool_queue_denies_pending_call_and_skips_dependents()
-> Result<(), SequentialToolQueueError> {
    let mut dependent = tool_call("call-b", ToolCallStatus::Admitted);
    dependent.depends_on = vec!["call-a".to_owned()];
    let mut queue = SequentialToolQueue::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", ToolCallStatus::Admitted)),
            ToolPlanCall::new(dependent),
            ToolPlanCall::new(tool_call("call-c", ToolCallStatus::Admitted)),
        ],
    )?;

    queue.record_denied("call-a")?;

    assert_eq!(queue.state("call-a"), Some(ToolExecutionState::Denied));
    assert_eq!(queue.state("call-b"), Some(ToolExecutionState::Skipped));
    assert_eq!(queue.state("call-c"), Some(ToolExecutionState::Pending));
    assert_eq!(queue.start_next_ready()?, Some("call-c".to_owned()));
    Ok(())
}

#[test]
fn sequential_tool_queue_expires_pending_call_and_skips_dependents()
-> Result<(), SequentialToolQueueError> {
    let mut dependent = tool_call("call-b", ToolCallStatus::Admitted);
    dependent.depends_on = vec!["call-a".to_owned()];
    let mut queue = SequentialToolQueue::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", ToolCallStatus::Admitted)),
            ToolPlanCall::new(dependent),
        ],
    )?;

    queue.record_expired("call-a")?;

    assert_eq!(queue.state("call-a"), Some(ToolExecutionState::Expired));
    assert_eq!(queue.state("call-b"), Some(ToolExecutionState::Skipped));
    assert_eq!(queue.start_next_ready()?, None);
    Ok(())
}

#[test]
fn sequential_tool_queue_rejects_terminal_update_for_different_running_call()
-> Result<(), SequentialToolQueueError> {
    let mut queue = SequentialToolQueue::new(
        "plan-1",
        "response-1",
        [ToolPlanCall::new(tool_call(
            "call-a",
            ToolCallStatus::Admitted,
        ))],
    )?;
    queue.start_next_ready()?;

    assert_eq!(
        queue.record_completed("call-b"),
        Err(SequentialToolQueueError::RunningCallMismatch {
            expected: "call-a".to_owned(),
            actual: "call-b".to_owned(),
        }),
    );
    assert_eq!(queue.running_call_id(), Some("call-a"));
    assert_eq!(queue.state("call-a"), Some(ToolExecutionState::Running));
    Ok(())
}

#[test]
fn sequential_tool_queue_policy_stop_clears_cancelled_running_call()
-> Result<(), SequentialToolQueueError> {
    let mut queue = SequentialToolQueue::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", ToolCallStatus::Admitted)),
            ToolPlanCall::new(tool_call("call-b", ToolCallStatus::Admitted)),
        ],
    )?;
    queue.start_next_ready()?;

    assert_eq!(
        queue.apply_policy_stop(PendingToolCallsDisposition::CancelAdmitted),
        vec!["call-a".to_owned(), "call-b".to_owned()],
    );
    assert_eq!(queue.running_call_id(), None);
    assert_eq!(queue.state("call-a"), Some(ToolExecutionState::Cancelled));
    assert_eq!(queue.state("call-b"), Some(ToolExecutionState::Denied));
    assert_eq!(queue.start_next_ready()?, None);
    Ok(())
}

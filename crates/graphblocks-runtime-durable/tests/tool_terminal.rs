use graphblocks_runtime_core::outcome::{BlockError, ErrorCategory};
use graphblocks_runtime_core::output_policy::{DraftDisposition, DurableResult, TerminalReason};
use graphblocks_runtime_core::tool_result::{
    ContentPart, ToolEffectOutcome, ToolResult, ToolResultStatus,
};
use graphblocks_runtime_durable::{
    DurableOutputCutoffDraftDisposition, DurableOutputCutoffDurableResult,
    DurableOutputCutoffTerminalReason, DurableResponsePolicyStopRecord, DurableToolTerminalRecord,
    DurableToolTerminalState, InMemoryDurableToolTerminalStore, ToolTerminalStoreError,
};

fn completed_tool_record() -> DurableToolTerminalRecord {
    DurableToolTerminalRecord::new(
        "run-000001",
        "response-1",
        "call-1",
        1,
        DurableToolTerminalState::Completed,
        "sha256:arguments",
        1_820_000_000_000,
    )
    .with_output_digest("sha256:output")
    .with_idempotency_key("ticket-create:call-1")
    .with_effect_committed()
    .with_durable_result_committed()
}

#[test]
fn tool_terminal_record_projects_completed_tool_result() {
    let result = ToolResult::completed(
        "call-3",
        [ContentPart::text("created")],
        1_820_000_000_100,
        1_820_000_000_200,
    )
    .with_effect_outcome(ToolEffectOutcome::Committed);
    let record = DurableToolTerminalRecord::from_tool_result(
        "run-000001",
        "response-1",
        1,
        "sha256:arguments-3",
        &result,
        1_820_000_000_900,
    )
    .with_idempotency_key("ticket-create:call-3")
    .with_durable_result_committed();

    assert_eq!(record.tool_call_id, "call-3");
    assert_eq!(record.terminal_state, DurableToolTerminalState::Completed);
    assert_eq!(record.arguments_digest, "sha256:arguments-3");
    assert_eq!(record.output_digest, result.output_digest);
    assert!(record.effect_committed);
    assert!(record.durable_result_committed);
    assert_eq!(record.completed_at_unix_ms, 1_820_000_000_200);

    let mut store = InMemoryDurableToolTerminalStore::new();
    let committed = store
        .record_tool_terminal(record)
        .expect("projected tool result should commit");
    assert_eq!(committed.sequence, 1);
    assert!(!committed.replayed);
}

#[test]
fn tool_terminal_record_projects_policy_stopped_committed_effect() {
    let result = ToolResult::policy_stopped(
        "call-4",
        BlockError::new(
            "output_policy_stopped",
            ErrorCategory::Policy,
            "output policy stopped the response",
            false,
        ),
        1_820_000_000_100,
        1_820_000_000_250,
    )
    .with_effect_outcome(ToolEffectOutcome::Committed);
    let record = DurableToolTerminalRecord::from_tool_result(
        "run-000001",
        "response-2",
        1,
        "sha256:arguments-4",
        &result,
        1_820_000_000_900,
    );

    assert_eq!(
        record.terminal_state,
        DurableToolTerminalState::PolicyStopped
    );
    assert_eq!(record.output_digest, None);
    assert!(record.effect_committed);
    assert!(!record.durable_result_committed);

    let mut store = InMemoryDurableToolTerminalStore::new();
    store
        .record_response_policy_stopped("response-2", "decision-1", 7, 1_820_000_000_300)
        .expect("policy stop barrier should commit");
    let committed = store
        .record_tool_terminal(record)
        .expect("late committed effect should remain auditable after policy stop");
    assert_eq!(
        committed.record.terminal_state,
        DurableToolTerminalState::PolicyStopped,
    );
    assert!(committed.record.effect_committed);
    assert!(!committed.record.durable_result_committed);
}

#[test]
fn tool_terminal_record_uses_fallback_completion_time() {
    let result = ToolResult {
        tool_call_id: "call-5".to_owned(),
        status: ToolResultStatus::Incomplete,
        output: Vec::new(),
        output_digest: None,
        artifacts: Vec::new(),
        diagnostics: Vec::new(),
        error: None,
        started_at_unix_ms: Some(1_820_000_000_100),
        completed_at_unix_ms: None,
        effect_outcome: ToolEffectOutcome::Unknown,
    };
    let record = DurableToolTerminalRecord::from_tool_result(
        "run-000001",
        "response-3",
        1,
        "sha256:arguments-5",
        &result,
        1_820_000_000_400,
    );

    assert_eq!(record.terminal_state, DurableToolTerminalState::Incomplete);
    assert_eq!(record.output_digest, None);
    assert!(!record.effect_committed);
    assert_eq!(record.completed_at_unix_ms, 1_820_000_000_400);

    let mut store = InMemoryDurableToolTerminalStore::new();
    store
        .record_tool_terminal(record)
        .expect("fallback completion time should produce a valid terminal record");
}

fn incomplete_tool_record() -> DurableToolTerminalRecord {
    DurableToolTerminalRecord::new(
        "run-000001",
        "response-1",
        "call-2",
        1,
        DurableToolTerminalState::Incomplete,
        "sha256:arguments-incomplete",
        1_820_000_000_200,
    )
}

#[test]
fn tool_terminal_store_replays_matching_terminal_record() {
    let mut store = InMemoryDurableToolTerminalStore::new();
    let record = completed_tool_record();

    let committed = store
        .record_tool_terminal(record.clone())
        .expect("terminal record should commit");
    let duplicate = store
        .record_tool_terminal(record)
        .expect("matching terminal record should replay");

    assert_eq!(committed.sequence, 1);
    assert!(!committed.replayed);
    assert_eq!(duplicate.sequence, committed.sequence);
    assert_eq!(duplicate.record, committed.record);
    assert!(duplicate.replayed);
    assert_eq!(store.tool_terminal_count(), 1);
}

#[test]
fn tool_terminal_store_records_incomplete_terminal_result() {
    let mut store = InMemoryDurableToolTerminalStore::new();
    let record = incomplete_tool_record();

    let committed = store
        .record_tool_terminal(record.clone())
        .expect("incomplete terminal record should commit");
    let duplicate = store
        .record_tool_terminal(record)
        .expect("matching incomplete terminal record should replay");

    assert_eq!(
        committed.record.terminal_state,
        DurableToolTerminalState::Incomplete
    );
    assert_eq!(committed.record.output_digest, None);
    assert!(!committed.record.effect_committed);
    assert!(!committed.record.durable_result_committed);
    assert!(duplicate.replayed);
    assert_eq!(store.tool_terminal_count(), 1);
}

#[test]
fn tool_terminal_store_rejects_terminal_mutation_on_replay() {
    let mut store = InMemoryDurableToolTerminalStore::new();
    store
        .record_tool_terminal(completed_tool_record())
        .expect("initial terminal record should commit");

    let mut conflicting = completed_tool_record();
    conflicting.terminal_state = DurableToolTerminalState::Failed;
    conflicting.output_digest = Some("sha256:error".to_owned());

    assert_eq!(
        store.record_tool_terminal(conflicting),
        Err(ToolTerminalStoreError::TerminalStateConflict {
            response_id: "response-1".to_owned(),
            tool_call_id: "call-1".to_owned(),
            revision: 1,
        }),
    );
    assert_eq!(store.tool_terminal_count(), 1);
}

#[test]
fn policy_stopped_response_rejects_late_durable_tool_result_commit() {
    let mut store = InMemoryDurableToolTerminalStore::new();
    store
        .record_response_policy_stopped("response-1", "decision-1", 7, 1_820_000_000_000)
        .expect("policy stop barrier should commit");

    assert_eq!(
        store.record_tool_terminal(completed_tool_record()),
        Err(ToolTerminalStoreError::ResponsePolicyStopped {
            response_id: "response-1".to_owned(),
        }),
    );

    let audited_late_effect = DurableToolTerminalRecord::new(
        "run-000001",
        "response-1",
        "call-1",
        1,
        DurableToolTerminalState::Cancelled,
        "sha256:arguments",
        1_820_000_000_100,
    )
    .with_effect_committed();
    let committed = store
        .record_tool_terminal(audited_late_effect)
        .expect("late effect outcome should still be auditable without committing a result");

    assert_eq!(
        committed.record.terminal_state,
        DurableToolTerminalState::Cancelled
    );
    assert!(committed.record.effect_committed);
    assert!(!committed.record.durable_result_committed);
    assert_eq!(store.tool_terminal_count(), 1);
}

#[test]
fn response_policy_stop_barrier_replays_matching_record() {
    let mut store = InMemoryDurableToolTerminalStore::new();

    let committed = store
        .record_response_policy_stopped("response-1", "decision-1", 7, 1_820_000_000_000)
        .expect("policy stop barrier should commit");
    let duplicate = store
        .record_response_policy_stopped("response-1", "decision-1", 7, 1_820_000_000_000)
        .expect("matching policy stop barrier should replay");

    assert_eq!(committed.sequence, duplicate.sequence);
    assert_eq!(committed.record.stream_id, "response-1");
    assert_eq!(committed.record.turn_id, None);
    assert_eq!(committed.record.last_generated_sequence, 7);
    assert_eq!(committed.record.last_client_delivered_sequence, 7);
    assert!(!committed.replayed);
    assert!(duplicate.replayed);
}

#[test]
fn response_policy_stop_barrier_persists_full_output_cutoff_state() {
    let mut store = InMemoryDurableToolTerminalStore::new();
    let record =
        DurableResponsePolicyStopRecord::new("response-1", "decision-1", 7, 1_820_000_000_000)
            .with_stream_id("stream-1")
            .with_turn_id("turn-1")
            .with_last_generated_sequence(9)
            .with_last_client_delivered_sequence(6)
            .with_terminal_reason(DurableOutputCutoffTerminalReason::PolicyDenied)
            .with_draft_disposition(DurableOutputCutoffDraftDisposition::Retract)
            .with_durable_result(DurableOutputCutoffDurableResult::None);

    let committed = store
        .record_response_policy_stop(record.clone())
        .expect("full policy stop record should commit");
    let duplicate = store
        .record_response_policy_stop(record)
        .expect("matching full policy stop record should replay");

    assert_eq!(committed.record.stream_id, "stream-1");
    assert_eq!(committed.record.turn_id.as_deref(), Some("turn-1"));
    assert_eq!(committed.record.last_generated_sequence, 9);
    assert_eq!(committed.record.last_policy_accepted_sequence, 7);
    assert_eq!(committed.record.last_client_delivered_sequence, 6);
    assert_eq!(
        committed.record.terminal_reason,
        DurableOutputCutoffTerminalReason::PolicyDenied,
    );
    assert_eq!(
        committed.record.draft_disposition,
        DurableOutputCutoffDraftDisposition::Retract,
    );
    assert_eq!(
        committed.record.durable_result,
        DurableOutputCutoffDurableResult::None,
    );
    assert_eq!(duplicate.sequence, committed.sequence);
    assert!(duplicate.replayed);
}

#[test]
fn response_policy_stop_record_converts_to_output_cutoff() {
    let record =
        DurableResponsePolicyStopRecord::new("response-1", "decision-1", 7, 1_820_000_000_000)
            .with_stream_id("stream-1")
            .with_turn_id("turn-1")
            .with_last_generated_sequence(9)
            .with_last_client_delivered_sequence(6)
            .with_terminal_reason(DurableOutputCutoffTerminalReason::BudgetExhausted)
            .with_draft_disposition(DurableOutputCutoffDraftDisposition::MarkIncomplete)
            .with_durable_result(DurableOutputCutoffDurableResult::Incomplete);

    let cutoff = record
        .to_output_cutoff()
        .expect("valid durable record should convert");

    assert_eq!(cutoff.stream_id, "stream-1");
    assert_eq!(cutoff.response_id, "response-1");
    assert_eq!(cutoff.turn_id.as_deref(), Some("turn-1"));
    assert_eq!(cutoff.last_generated_sequence, 9);
    assert_eq!(cutoff.last_policy_accepted_sequence, 7);
    assert_eq!(cutoff.last_client_delivered_sequence, 6);
    assert_eq!(cutoff.terminal_reason, TerminalReason::BudgetExhausted);
    assert_eq!(cutoff.draft_disposition, DraftDisposition::MarkIncomplete);
    assert_eq!(cutoff.durable_result, DurableResult::Incomplete);
    assert_eq!(cutoff.policy_decision_id.as_deref(), Some("decision-1"));
    assert_eq!(cutoff.occurred_at_unix_ms, 1_820_000_000_000);
}

#[test]
fn policy_stop_barrier_rejects_response_with_committed_tool_result() {
    let mut store = InMemoryDurableToolTerminalStore::new();
    store
        .record_tool_terminal(completed_tool_record())
        .expect("tool result should commit");

    assert_eq!(
        store.record_response_policy_stopped("response-1", "decision-1", 7, 1_820_000_000_000),
        Err(ToolTerminalStoreError::DurableResultAlreadyCommitted {
            response_id: "response-1".to_owned(),
        }),
    );
}

#[test]
fn tool_terminal_record_requires_stable_identity_fields() {
    let mut store = InMemoryDurableToolTerminalStore::new();

    assert_eq!(
        store.record_tool_terminal(DurableToolTerminalRecord::new(
            "",
            "response-1",
            "call-1",
            1,
            DurableToolTerminalState::Completed,
            "sha256:arguments",
            1_820_000_000_000,
        )),
        Err(ToolTerminalStoreError::MissingRunId),
    );
    assert_eq!(
        store.record_tool_terminal(DurableToolTerminalRecord::new(
            "run-000001",
            "",
            "call-1",
            1,
            DurableToolTerminalState::Completed,
            "sha256:arguments",
            1_820_000_000_000,
        )),
        Err(ToolTerminalStoreError::MissingResponseId),
    );
    assert_eq!(
        store.record_tool_terminal(DurableToolTerminalRecord::new(
            "run-000001",
            "response-1",
            "call-1",
            0,
            DurableToolTerminalState::Completed,
            "sha256:arguments",
            1_820_000_000_000,
        )),
        Err(ToolTerminalStoreError::InvalidRevision),
    );
}

#[test]
fn tool_terminal_record_rejects_whitespace_identity_and_digest_fields() {
    let mut store = InMemoryDurableToolTerminalStore::new();

    assert_eq!(
        store.record_tool_terminal(DurableToolTerminalRecord::new(
            " ",
            "response-1",
            "call-1",
            1,
            DurableToolTerminalState::Completed,
            "sha256:arguments",
            1_820_000_000_000,
        )),
        Err(ToolTerminalStoreError::MissingRunId),
    );
    assert_eq!(
        store.record_tool_terminal(DurableToolTerminalRecord::new(
            "run-000001",
            "\t",
            "call-1",
            1,
            DurableToolTerminalState::Completed,
            "sha256:arguments",
            1_820_000_000_000,
        )),
        Err(ToolTerminalStoreError::MissingResponseId),
    );
    assert_eq!(
        store.record_tool_terminal(DurableToolTerminalRecord::new(
            "run-000001",
            "response-1",
            "\n",
            1,
            DurableToolTerminalState::Completed,
            "sha256:arguments",
            1_820_000_000_000,
        )),
        Err(ToolTerminalStoreError::MissingToolCallId),
    );
    assert_eq!(
        store.record_tool_terminal(DurableToolTerminalRecord::new(
            "run-000001",
            "response-1",
            "call-1",
            1,
            DurableToolTerminalState::Completed,
            " ",
            1_820_000_000_000,
        )),
        Err(ToolTerminalStoreError::MissingArgumentsDigest),
    );
    assert_eq!(
        store.record_tool_terminal(completed_tool_record().with_output_digest(" ")),
        Err(ToolTerminalStoreError::MissingOutputDigest),
    );
    assert_eq!(
        store.record_tool_terminal(incomplete_tool_record().with_idempotency_key(" ")),
        Err(ToolTerminalStoreError::MissingIdempotencyKey),
    );
}

#[test]
fn policy_stop_barrier_rejects_whitespace_identity_fields() {
    let mut store = InMemoryDurableToolTerminalStore::new();

    assert_eq!(
        store.record_response_policy_stopped(" ", "decision-1", 7, 1_820_000_000_000),
        Err(ToolTerminalStoreError::MissingResponseId),
    );
    assert_eq!(
        store.record_response_policy_stopped("response-1", "\t", 7, 1_820_000_000_000),
        Err(ToolTerminalStoreError::MissingPolicyDecisionId),
    );
    assert_eq!(
        store.record_response_policy_stop(
            DurableResponsePolicyStopRecord::new("response-1", "decision-1", 7, 1_820_000_000_000,)
                .with_stream_id(" "),
        ),
        Err(ToolTerminalStoreError::MissingStreamId),
    );
    assert_eq!(
        store.record_response_policy_stop(
            DurableResponsePolicyStopRecord::new("response-1", "decision-1", 7, 1_820_000_000_000,)
                .with_turn_id("\n"),
        ),
        Err(ToolTerminalStoreError::MissingTurnId),
    );
    assert_eq!(
        store.record_response_policy_stop(
            DurableResponsePolicyStopRecord::new("response-1", "decision-1", 7, 1_820_000_000_000,)
                .with_last_generated_sequence(6),
        ),
        Err(
            ToolTerminalStoreError::PolicyAcceptedSequenceBeyondGenerated {
                last_generated_sequence: 6,
                last_policy_accepted_sequence: 7,
            }
        ),
    );
    assert_eq!(
        store.record_response_policy_stop(
            DurableResponsePolicyStopRecord::new("response-1", "decision-1", 7, 1_820_000_000_000,)
                .with_last_generated_sequence(8)
                .with_last_client_delivered_sequence(9),
        ),
        Err(
            ToolTerminalStoreError::ClientDeliveredSequenceBeyondGenerated {
                last_generated_sequence: 8,
                last_client_delivered_sequence: 9,
            }
        ),
    );
}

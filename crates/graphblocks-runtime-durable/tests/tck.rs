use std::collections::{BTreeMap, BTreeSet};

use graphblocks_runtime_core::outcome::{BlockError, ErrorCategory};
use graphblocks_runtime_core::tool_result::{ContentPart, ToolEffectOutcome, ToolResult};
use graphblocks_runtime_durable::{
    AccumulationMode, CheckpointBarrier, CheckpointBarrierError, DeliveryGuarantee, DurableError,
    DurableToolTerminalRecord, DurableToolTerminalState, InMemoryCheckpointStore,
    InMemoryDurableSink, InMemoryDurableSource, InMemoryDurableToolTerminalStore, SchemaRef,
    SinkCommitError, SinkCommitRequest, SourceCursor, SourceEvent, ToolTerminalStoreError,
    Watermark, WindowAccumulator, WindowPolicy,
};
use serde_json::{json, Map, Value};

#[test]
fn rust_durable_runtime_matches_shared_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("../../../tck/durable/cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "durable TCK root must be an array".to_owned())?;

    for case in cases {
        run_case(case)?;
    }

    Ok(())
}

fn run_case(case: &Value) -> Result<(), String> {
    let name = required_str(case, "name", "durable TCK case")?;
    let kind = required_str(case, "kind", name)?;
    let expected = case
        .get("expected")
        .and_then(Value::as_object)
        .ok_or_else(|| format!("durable TCK case {name} is missing expected result"))?;
    let expected_diagnostics = case
        .get("expectedDiagnostics")
        .or_else(|| case.get("expected_diagnostics"))
        .and_then(Value::as_array);
    let mut diagnostics = Vec::new();

    let mut observed = match kind {
        "source_replay" => {
            let events = event_list(case, "events", name)?;
            let mut source = InMemoryDurableSource::new(
                guarantee_from(required_str(case, "guarantee", name)?)?,
                events,
            );
            let first_poll = required_object(case, "firstPoll", name)?;
            let first = source
                .poll(None, required_u64_map(first_poll, "demand", name)? as usize)
                .map_err(|error| format!("{name} first poll failed: {error:?}"))?;
            let commit_cursor = cursor_from(required_object(case, "commitCursor", name)?)?;
            source
                .commit(commit_cursor)
                .map_err(|error| format!("{name} commit failed: {error:?}"))?;
            let after_commit_poll = required_object(case, "afterCommitPoll", name)?;
            let after_commit = source
                .poll(
                    None,
                    required_u64_map(after_commit_poll, "demand", name)? as usize,
                )
                .map_err(|error| format!("{name} after commit poll failed: {error:?}"))?;
            let replay_poll = required_object(case, "replayPoll", name)?;
            let replay_cursor = cursor_from(required_object_map(replay_poll, "cursor", name)?)?;
            let replay = source
                .poll(
                    Some(replay_cursor),
                    required_u64_map(replay_poll, "demand", name)? as usize,
                )
                .map_err(|error| format!("{name} replay poll failed: {error:?}"))?;
            let high_cursor = first.high_cursor().cloned();
            json!({
                "firstOffsets": offsets(&first.events),
                "firstHighCursor": high_cursor.map(|cursor| cursor_contract(&cursor)),
                "firstWatermarkUnixMs": first.watermark.map(|watermark| watermark.unix_ms),
                "afterCommitOffsets": offsets(&after_commit.events),
                "replayOffsets": offsets(&replay.events),
            })
        }
        "source_errors" => {
            let events = event_list(case, "events", name)?;
            let mut source = InMemoryDurableSource::new(
                guarantee_from(required_str(case, "guarantee", name)?)?,
                events,
            );
            source.pause();
            let paused_error = match source.poll(None, 1) {
                Err(DurableError::SourcePaused) => Some("source_paused"),
                _ => None,
            };
            source.resume();
            source
                .commit(cursor_from(required_object(
                    case,
                    "committedCursor",
                    name,
                )?)?)
                .map_err(|error| format!("{name} initial commit failed: {error:?}"))?;
            let mut stale_error = None;
            let mut stale_current_offset = None;
            let mut stale_attempted_offset = None;
            if let Err(DurableError::StaleCommit { current, attempted }) =
                source.commit(cursor_from(required_object(case, "staleCursor", name)?)?)
            {
                stale_error = Some("stale_commit");
                stale_current_offset = Some(current.offset);
                stale_attempted_offset = Some(attempted.offset);
            }
            let unknown_cursor = cursor_from(required_object(case, "unknownCursor", name)?)?;
            let unknown_commit_error = match source.commit(unknown_cursor.clone()) {
                Err(DurableError::UnknownSourceCursor { .. }) => Some("unknown_source_cursor"),
                _ => None,
            };
            let unknown_poll_error = match source.poll(Some(unknown_cursor), 1) {
                Err(DurableError::UnknownSourceCursor { .. }) => Some("unknown_source_cursor"),
                _ => None,
            };
            json!({
                "pausedError": paused_error,
                "staleError": stale_error,
                "staleCurrentOffset": stale_current_offset,
                "staleAttemptedOffset": stale_attempted_offset,
                "unknownCommitError": unknown_commit_error,
                "unknownPollError": unknown_poll_error,
            })
        }
        "window_lateness" => {
            let policy = required_object(case, "policy", name)?;
            let policy = WindowPolicy::tumbling_event_time(
                required_u64_map(policy, "sizeMs", name)?,
                required_u64_map(policy, "allowedLatenessMs", name)?,
                accumulation_from(required_str_map(policy, "accumulationMode", name)?)?,
            )
            .map_err(|error| format!("{name} policy failed: {error:?}"))?;
            let mut windows = WindowAccumulator::new(policy);
            for event in event_list(case, "events", name)? {
                windows
                    .ingest(event)
                    .map_err(|error| format!("{name} ingest failed: {error:?}"))?;
            }
            let watermarks = case
                .get("watermarks")
                .and_then(Value::as_array)
                .ok_or_else(|| format!("{name} is missing watermarks"))?;
            let before = windows.advance_watermark(Watermark::event_time(
                watermarks
                    .first()
                    .and_then(Value::as_u64)
                    .ok_or_else(|| format!("{name} has invalid first watermark"))?,
            ));
            let after = windows.advance_watermark(Watermark::event_time(
                watermarks
                    .get(1)
                    .and_then(Value::as_u64)
                    .ok_or_else(|| format!("{name} has invalid second watermark"))?,
            ));
            let mut late_error = None;
            let mut late_watermark_unix_ms = None;
            if let Err(DurableError::LateEvent {
                watermark_unix_ms, ..
            }) = windows.ingest(event_from(required_object(case, "lateEvent", name)?)?)
            {
                late_error = Some("late_event");
                late_watermark_unix_ms = Some(watermark_unix_ms);
            }
            let pane = after.first();
            json!({
                "closedBefore": before.len(),
                "closedAfter": after.len(),
                "paneStartUnixMs": pane.map(|pane| pane.start_unix_ms),
                "paneEndUnixMs": pane.map(|pane| pane.end_unix_ms),
                "paneOffsets": pane.map(|pane| offsets(&pane.events)).unwrap_or_default(),
                "lateError": late_error,
                "lateWatermarkUnixMs": late_watermark_unix_ms,
            })
        }
        "sink_idempotency" => {
            let mut sink = InMemoryDurableSink::new(required_str(case, "sinkId", name)?);
            let request = sink_request_from(required_object(case, "request", name)?)?;
            let first = sink
                .commit(request.clone())
                .map_err(|error| format!("{name} first sink commit failed: {error:?}"))?;
            let replay = sink
                .commit(request.clone())
                .map_err(|error| format!("{name} replay sink commit failed: {error:?}"))?;
            let conflict = SinkCommitRequest {
                payload: case.get("conflictPayload").cloned().unwrap_or(Value::Null),
                ..request
            };
            let conflict_error = match sink.commit(conflict) {
                Err(SinkCommitError::IdempotencyConflict { .. }) => Some("idempotency_conflict"),
                _ => None,
            };
            json!({
                "firstSequence": first.sequence,
                "replaySequence": replay.sequence,
                "replayReplayed": replay.replayed,
                "committedCount": sink.committed_count(),
                "conflictError": conflict_error,
            })
        }
        "checkpoint_replay" => {
            let missing_plan = barrier_from(required_object(case, "missingPlanBarrier", name)?)?;
            let missing_plan_error = match missing_plan.validate() {
                Err(error) => Some(checkpoint_error_name(&error)),
                Ok(()) => None,
            };
            let barrier = barrier_from(required_object(case, "barrier", name)?)?;
            barrier
                .validate()
                .map_err(|error| format!("{name} barrier failed: {error:?}"))?;
            let commit_plan = barrier
                .source_commit_plan()
                .cursors
                .iter()
                .map(|(source_id, cursor)| {
                    format!(
                        "{}:{}:{}:{}",
                        source_id, cursor.stream, cursor.partition, cursor.offset
                    )
                })
                .collect::<Vec<_>>();
            let mut store = InMemoryCheckpointStore::new();
            for raw_checkpoint in required_array(case, "checkpoints", name)? {
                store
                    .put(barrier_from(raw_checkpoint.as_object().ok_or_else(
                        || format!("{name} checkpoint entry must be an object"),
                    )?)?)
                    .map_err(|error| format!("{name} checkpoint store put failed: {error:?}"))?;
            }
            let lookup = required_object(case, "lookup", name)?;
            let latest = store.latest_compatible(
                required_str_map(lookup, "runId", name)?,
                required_str_map(lookup, "releaseId", name)?,
                required_str_map(lookup, "deploymentRevisionId", name)?,
                required_str_map(lookup, "planHash", name)?,
            );
            let missing_lookup = required_object(case, "missingLookup", name)?;
            let missing = store.latest_compatible(
                required_str_map(missing_lookup, "runId", name)?,
                required_str_map(missing_lookup, "releaseId", name)?,
                required_str_map(missing_lookup, "deploymentRevisionId", name)?,
                required_str_map(missing_lookup, "planHash", name)?,
            );
            json!({
                "missingPlanError": missing_plan_error,
                "commitPlan": commit_plan,
                "latestCheckpointId": latest.as_ref().map(|checkpoint| checkpoint.checkpoint_id.clone()),
                "latestStateRevision": latest.as_ref().map(|checkpoint| checkpoint.state_revision),
                "missingCompatible": missing.is_none(),
            })
        }
        "tool_terminal_from_tool_result" => {
            let mut store = InMemoryDurableToolTerminalStore::new();
            let raw_result = required_object(case, "toolResult", name)?;
            let raw_record = required_object(case, "record", name)?;
            let status = required_str_map(raw_result, "status", name)?;
            let tool_call_id = required_str_map(raw_result, "toolCallId", name)?;
            let started_at_unix_ms = required_u64_map(raw_result, "startedAtUnixMs", name)?;
            let completed_at_unix_ms = required_u64_map(raw_result, "completedAtUnixMs", name)?;
            let record_completed_at_unix_ms =
                required_u64_map(raw_record, "completedAtUnixMs", name)?;
            let raw_output = raw_result
                .get("output")
                .and_then(Value::as_array)
                .ok_or_else(|| format!("{name} toolResult output must be an array"))?;
            let mut output = Vec::new();
            for (part_index, raw_part) in raw_output.iter().enumerate() {
                let raw_part = raw_part
                    .as_object()
                    .ok_or_else(|| format!("{name} output part {part_index} must be an object"))?;
                let metadata = raw_part
                    .get("metadata")
                    .and_then(Value::as_object)
                    .map(|metadata| {
                        metadata
                            .iter()
                            .map(|(key, value)| (key.clone(), value.clone()))
                            .collect::<BTreeMap<_, _>>()
                    })
                    .unwrap_or_default();
                let mut part = match required_str_map(raw_part, "kind", name)? {
                    "text" => ContentPart::text(required_str_map(raw_part, "text", name)?),
                    "json" => ContentPart::json(
                        raw_part
                            .get("data")
                            .cloned()
                            .ok_or_else(|| format!("{name} json output part requires data"))?,
                    ),
                    other => {
                        return Err(format!(
                            "{name} output part {part_index} has unsupported kind {other:?}"
                        ));
                    }
                };
                part.metadata = metadata;
                output.push(part);
            }
            let error = raw_result.get("error").and_then(Value::as_object);
            let error_code = error
                .and_then(|error| error.get("code"))
                .and_then(Value::as_str)
                .unwrap_or(status);
            let error_message = error
                .and_then(|error| error.get("message"))
                .and_then(Value::as_str)
                .unwrap_or(status);
            let mut tool_result = match status {
                "completed" => ToolResult::completed(
                    tool_call_id,
                    output,
                    started_at_unix_ms,
                    completed_at_unix_ms,
                ),
                "failed" => ToolResult::failed(
                    tool_call_id,
                    BlockError::new(error_code, ErrorCategory::Permanent, error_message, false),
                    started_at_unix_ms,
                    completed_at_unix_ms,
                ),
                "denied" => ToolResult::denied(
                    tool_call_id,
                    BlockError::new(
                        error_code,
                        ErrorCategory::Authorization,
                        error_message,
                        false,
                    ),
                    completed_at_unix_ms,
                ),
                "cancelled" => {
                    ToolResult::cancelled(tool_call_id, started_at_unix_ms, completed_at_unix_ms)
                }
                "policy_stopped" => ToolResult::policy_stopped(
                    tool_call_id,
                    BlockError::new(error_code, ErrorCategory::Policy, error_message, false),
                    started_at_unix_ms,
                    completed_at_unix_ms,
                ),
                "incomplete" => {
                    ToolResult::incomplete(tool_call_id, started_at_unix_ms, completed_at_unix_ms)
                }
                other => {
                    return Err(format!(
                        "{name} has unsupported tool result status {other:?}"
                    ));
                }
            };
            if let Some(effect_outcome) = raw_result.get("effectOutcome").and_then(Value::as_str) {
                let effect_outcome = match effect_outcome {
                    "no_external_effect" => ToolEffectOutcome::NoExternalEffect,
                    "committed" => ToolEffectOutcome::Committed,
                    "not_committed" => ToolEffectOutcome::NotCommitted,
                    "unknown" => ToolEffectOutcome::Unknown,
                    other => {
                        return Err(format!("{name} has unsupported effect outcome {other:?}"));
                    }
                };
                tool_result = tool_result.with_effect_outcome(effect_outcome);
            }
            let mut record = DurableToolTerminalRecord::from_tool_result(
                required_str_map(raw_record, "runId", name)?,
                required_str_map(raw_record, "responseId", name)?,
                required_u64_map(raw_record, "revision", name)? as u32,
                required_str_map(raw_record, "argumentsDigest", name)?,
                &tool_result,
                record_completed_at_unix_ms,
            );
            if let Some(idempotency_key) = raw_record.get("idempotencyKey").and_then(Value::as_str)
            {
                record = record.with_idempotency_key(idempotency_key);
            }
            if raw_record
                .get("durableResultCommitted")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                record = record.with_durable_result_committed();
            }
            let committed = store
                .record_tool_terminal(record)
                .map_err(|error| format!("{name} projected terminal failed: {error:?}"))?;
            json!({
                "commitSequence": committed.sequence,
                "toolCallId": committed.record.tool_call_id,
                "terminalState": terminal_state_name(&committed.record.terminal_state),
                "outputDigestMatchesResult": committed.record.output_digest == tool_result.output_digest,
                "outputDigestPrefix": committed.record.output_digest.as_ref().map(|digest| digest.chars().take(7).collect::<String>()),
                "idempotencyKey": committed.record.idempotency_key,
                "effectCommitted": committed.record.effect_committed,
                "durableResultCommitted": committed.record.durable_result_committed,
                "toolTerminalCount": store.tool_terminal_count(),
            })
        }
        "tool_terminal_policy_stop" => {
            let mut store = InMemoryDurableToolTerminalStore::new();
            let policy_stop = required_object(case, "policyStop", name)?;
            let committed = store
                .record_response_policy_stopped(
                    required_str_map(policy_stop, "responseId", name)?,
                    required_str_map(policy_stop, "policyDecisionId", name)?,
                    required_u64_map(policy_stop, "lastPolicyAcceptedSequence", name)?,
                    required_u64_map(policy_stop, "occurredAtUnixMs", name)?,
                )
                .map_err(|error| format!("{name} policy stop failed: {error:?}"))?;
            let replay = store
                .record_response_policy_stopped(
                    required_str_map(policy_stop, "responseId", name)?,
                    required_str_map(policy_stop, "policyDecisionId", name)?,
                    required_u64_map(policy_stop, "lastPolicyAcceptedSequence", name)?,
                    required_u64_map(policy_stop, "occurredAtUnixMs", name)?,
                )
                .map_err(|error| format!("{name} policy stop replay failed: {error:?}"))?;
            let late_result_error = match store.record_tool_terminal(tool_terminal_from(
                required_object(case, "lateDurableResult", name)?,
            )?) {
                Err(ToolTerminalStoreError::ResponsePolicyStopped { .. }) => {
                    Some("response_policy_stopped")
                }
                _ => None,
            };
            let audited = store
                .record_tool_terminal(tool_terminal_from(required_object(
                    case,
                    "auditedLateEffect",
                    name,
                )?)?)
                .map_err(|error| format!("{name} audited terminal failed: {error:?}"))?;
            json!({
                "policyStopSequence": committed.sequence,
                "policyStopReplaySequence": replay.sequence,
                "policyStopReplayReplayed": replay.replayed,
                "lateDurableResultError": late_result_error,
                "auditedTerminalState": terminal_state_name(&audited.record.terminal_state),
                "auditedEffectCommitted": audited.record.effect_committed,
                "auditedDurableResultCommitted": audited.record.durable_result_committed,
                "toolTerminalCount": store.tool_terminal_count(),
            })
        }
        "tool_terminal_effect_invariant" => {
            let mut store = InMemoryDurableToolTerminalStore::new();
            let record_error = match store
                .record_tool_terminal(tool_terminal_from(required_object(case, "record", name)?)?)
            {
                Err(ToolTerminalStoreError::DeniedEffectCommitted { .. }) => {
                    Some("denied_effect_committed")
                }
                Err(ToolTerminalStoreError::ExpiredEffectCommitted { .. }) => {
                    Some("expired_effect_committed")
                }
                Err(other) => return Err(format!("{name} unexpected terminal error: {other:?}")),
                Ok(_) => None,
            };
            json!({
                "recordError": record_error,
                "toolTerminalCount": store.tool_terminal_count(),
            })
        }
        "background_run_event_stream" => {
            let raw_events = required_array(case, "events", name)?;
            let raw_attach = required_object(case, "attach", name)?;
            let empty_detach = Map::new();
            let raw_detach = case
                .get("detach")
                .and_then(Value::as_object)
                .unwrap_or(&empty_detach);
            let empty_retention = Map::new();
            let raw_retention = case
                .get("retention")
                .and_then(Value::as_object)
                .unwrap_or(&empty_retention);
            let initial_cursor_for_events = case
                .get("initialResponse")
                .or_else(|| case.get("initial_response"))
                .and_then(Value::as_object)
                .and_then(|response| {
                    response
                        .get("initialCursor")
                        .or_else(|| response.get("initial_cursor"))
                })
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|cursor| !cursor.is_empty());
            let mut event_records = Vec::new();
            let mut previous_event_sequence = None;
            let mut event_ids = BTreeSet::new();
            let mut event_cursors = BTreeSet::new();
            for (index, raw_event) in raw_events.iter().enumerate() {
                let Some(event) = raw_event.as_object() else {
                    diagnostics.push(json!({
                        "code": "DurableBackgroundRunInvalid",
                        "message": "background run event must be object",
                        "path": format!("$.events[{index}]"),
                    }));
                    continue;
                };
                let mut event_valid = true;
                let event_id_path =
                    if event.contains_key("eventId") || !event.contains_key("event_id") {
                        "eventId"
                    } else {
                        "event_id"
                    };
                if event
                    .get("eventId")
                    .or_else(|| event.get("event_id"))
                    .and_then(Value::as_str)
                    .is_none_or(|event_id| event_id.trim().is_empty())
                {
                    event_valid = false;
                    diagnostics.push(json!({
                        "code": "DurableBackgroundRunInvalid",
                        "message": "background run event requires eventId",
                        "path": format!("$.events[{index}].{event_id_path}"),
                    }));
                } else if let Some(event_id) = event
                    .get("eventId")
                    .or_else(|| event.get("event_id"))
                    .and_then(Value::as_str)
                    .map(str::trim)
                {
                    if !event_ids.insert(event_id.to_owned()) {
                        event_valid = false;
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run eventId must be unique",
                            "path": format!("$.events[{index}].{event_id_path}"),
                        }));
                    }
                }
                if event
                    .get("cursor")
                    .and_then(Value::as_str)
                    .is_none_or(|cursor| cursor.trim().is_empty())
                {
                    event_valid = false;
                    diagnostics.push(json!({
                        "code": "DurableBackgroundRunInvalid",
                        "message": "background run event requires cursor",
                        "path": format!("$.events[{index}].cursor"),
                    }));
                } else if let Some(cursor) =
                    event.get("cursor").and_then(Value::as_str).map(str::trim)
                {
                    if initial_cursor_for_events == Some(cursor) {
                        event_valid = false;
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run event cursor must not equal initialCursor",
                            "path": format!("$.events[{index}].cursor"),
                        }));
                    } else if !event_cursors.insert(cursor.to_owned()) {
                        event_valid = false;
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run cursor must be unique",
                            "path": format!("$.events[{index}].cursor"),
                        }));
                    }
                }
                if event
                    .get("type")
                    .and_then(Value::as_str)
                    .is_none_or(|event_type| event_type.trim().is_empty())
                {
                    event_valid = false;
                    diagnostics.push(json!({
                        "code": "DurableBackgroundRunInvalid",
                        "message": "background run event requires type",
                        "path": format!("$.events[{index}].type"),
                    }));
                }
                let occurred_at_path =
                    if event.contains_key("occurredAt") || !event.contains_key("occurred_at") {
                        "occurredAt"
                    } else {
                        "occurred_at"
                    };
                let occurred_at_is_iso = event
                    .get("occurredAt")
                    .or_else(|| event.get("occurred_at"))
                    .and_then(Value::as_str)
                    .is_some_and(|occurred_at| {
                        let occurred_at = occurred_at.trim();
                        let bytes = occurred_at.as_bytes();
                        let digit_positions = [0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18];
                        bytes.len() >= 20
                            && digit_positions
                                .into_iter()
                                .all(|position| bytes.get(position).is_some_and(u8::is_ascii_digit))
                            && bytes.get(4) == Some(&b'-')
                            && bytes.get(7) == Some(&b'-')
                            && bytes.get(10) == Some(&b'T')
                            && bytes.get(13) == Some(&b':')
                            && bytes.get(16) == Some(&b':')
                            && (occurred_at.ends_with('Z')
                                || occurred_at.get(19..).is_some_and(|suffix| {
                                    suffix.contains('+') || suffix.contains('-')
                                }))
                    });
                if !occurred_at_is_iso {
                    event_valid = false;
                    diagnostics.push(json!({
                        "code": "DurableBackgroundRunInvalid",
                        "message": "background run event requires ISO occurredAt",
                        "path": format!("$.events[{index}].{occurred_at_path}"),
                    }));
                }
                let sequence = event.get("sequence").and_then(Value::as_u64);
                match sequence {
                    Some(sequence) => {
                        if sequence == 0 {
                            event_valid = false;
                            diagnostics.push(json!({
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event requires positive integer sequence",
                                "path": format!("$.events[{index}].sequence"),
                            }));
                        } else if previous_event_sequence
                            .is_some_and(|previous| sequence <= previous)
                        {
                            event_valid = false;
                            diagnostics.push(json!({
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event sequence must be strictly increasing",
                                "path": format!("$.events[{index}].sequence"),
                            }));
                        }
                    }
                    None => {
                        event_valid = false;
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run event requires integer sequence",
                            "path": format!("$.events[{index}].sequence"),
                        }));
                    }
                }
                if event_valid {
                    previous_event_sequence = sequence;
                    event_records.push(event);
                }
            }
            let raw_last_cursor = raw_attach
                .get("lastCursor")
                .or_else(|| raw_attach.get("last_cursor"));
            let last_cursor_path = if raw_attach.contains_key("lastCursor")
                || !raw_attach.contains_key("last_cursor")
            {
                "lastCursor"
            } else {
                "last_cursor"
            };
            let last_cursor = match raw_last_cursor {
                Some(value) => match value.as_str().filter(|cursor| !cursor.trim().is_empty()) {
                    Some(cursor) => Some(cursor),
                    None => {
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run attach requires string lastCursor",
                            "path": format!("$.attach.{last_cursor_path}"),
                        }));
                        None
                    }
                },
                None => None,
            };
            let initial_cursor = case
                .get("initialResponse")
                .or_else(|| case.get("initial_response"))
                .and_then(Value::as_object)
                .and_then(|response| {
                    response
                        .get("initialCursor")
                        .or_else(|| response.get("initial_cursor"))
                })
                .and_then(Value::as_str)
                .filter(|cursor| !cursor.trim().is_empty());
            let mut cursor_positions = BTreeMap::new();
            if let Some(cursor) = initial_cursor {
                cursor_positions.insert(cursor.to_owned(), -1);
            }
            for (index, event) in event_records.iter().enumerate() {
                if let Some(cursor) = event.get("cursor").and_then(Value::as_str) {
                    cursor_positions
                        .entry(cursor.to_owned())
                        .or_insert(index as isize);
                }
            }
            let last_cursor_index =
                last_cursor.and_then(|cursor| cursor_positions.get(cursor).copied());
            let replay_event_ids = event_records
                .iter()
                .enumerate()
                .filter(|(index, _event)| match last_cursor {
                    None => true,
                    Some(_) => last_cursor_index
                        .is_some_and(|cursor_index| (*index as isize) > cursor_index),
                })
                .map(|(_index, event)| event)
                .filter_map(|event| {
                    event
                        .get("eventId")
                        .or_else(|| event.get("event_id"))
                        .and_then(Value::as_str)
                })
                .collect::<Vec<_>>();
            let raw_expired_cursor = raw_attach
                .get("expiredCursor")
                .or_else(|| raw_attach.get("expired_cursor"));
            let expired_cursor_path = if raw_attach.contains_key("expiredCursor")
                || !raw_attach.contains_key("expired_cursor")
            {
                "expiredCursor"
            } else {
                "expired_cursor"
            };
            let expired_cursor = match raw_expired_cursor {
                Some(value) => match value.as_str().filter(|cursor| !cursor.trim().is_empty()) {
                    Some(cursor) => cursor,
                    None => {
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run attach requires string expiredCursor",
                            "path": format!("$.attach.{expired_cursor_path}"),
                        }));
                        ""
                    }
                },
                None => "",
            };
            let raw_retained_from = raw_retention
                .get("retainedFromCursor")
                .or_else(|| raw_retention.get("retained_from_cursor"));
            let retained_from_path = if raw_retention.contains_key("retainedFromCursor")
                || !raw_retention.contains_key("retained_from_cursor")
            {
                "retainedFromCursor"
            } else {
                "retained_from_cursor"
            };
            let retained_from = match raw_retained_from {
                Some(value) => match value.as_str().filter(|cursor| !cursor.trim().is_empty()) {
                    Some(cursor) => cursor,
                    None => {
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run retention requires string retainedFromCursor",
                            "path": format!("$.retention.{retained_from_path}"),
                        }));
                        ""
                    }
                },
                None => "",
            };
            let expired_cursor_index = cursor_positions.get(expired_cursor).copied();
            let retained_from_index = cursor_positions.get(retained_from).copied();
            let lifetime = case.get("lifetime").and_then(Value::as_str);
            let lifetime_allows_detach = match lifetime {
                Some("background" | "job") => true,
                _ => {
                    diagnostics.push(json!({
                        "code": "DurableBackgroundRunInvalid",
                        "message": "background run lifetime must be background or job",
                        "path": "$.lifetime",
                    }));
                    false
                }
            };
            let response_mode = case
                .get("responseMode")
                .or_else(|| case.get("response_mode"))
                .and_then(Value::as_str);
            let response_mode_path =
                if case.get("responseMode").is_some() || case.get("response_mode").is_none() {
                    "responseMode"
                } else {
                    "response_mode"
                };
            let valid_response_mode = match response_mode {
                Some("accepted" | "background") => response_mode,
                _ => {
                    diagnostics.push(json!({
                        "code": "DurableBackgroundRunInvalid",
                        "message": "background run responseMode must be accepted or background",
                        "path": format!("$.{response_mode_path}"),
                    }));
                    None
                }
            };
            let raw_initial_response = case
                .get("initialResponse")
                .or_else(|| case.get("initial_response"));
            let initial_response = raw_initial_response.and_then(Value::as_object);
            if let Some(mode) = valid_response_mode {
                if let Some(response) = initial_response {
                    let response_run_id = response
                        .get("runId")
                        .or_else(|| response.get("run_id"))
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .filter(|run_id| !run_id.is_empty());
                    if response_run_id.is_none() {
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": format!("background run {mode} response requires runId"),
                            "path": "$.initialResponse.runId",
                        }));
                    }
                    let event_stream_path = if response.contains_key("eventStream")
                        || !response.contains_key("event_stream")
                    {
                        "eventStream"
                    } else {
                        "event_stream"
                    };
                    let response_event_stream = response
                        .get("eventStream")
                        .or_else(|| response.get("event_stream"))
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .filter(|event_stream| !event_stream.is_empty());
                    if response_event_stream.is_none() {
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": format!("background run {mode} response requires eventStream"),
                            "path": format!("$.initialResponse.{event_stream_path}"),
                        }));
                    } else if let (Some(run_id), Some(event_stream)) =
                        (response_run_id, response_event_stream)
                    {
                        let run_id_path_segment = format!("/runs/{run_id}/");
                        if !event_stream.contains(&run_id_path_segment) {
                            diagnostics.push(json!({
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run eventStream must include runId",
                                "path": format!("$.initialResponse.{event_stream_path}"),
                            }));
                        }
                    }
                    let websocket_path = if response.contains_key("websocket")
                        || !response.contains_key("web_socket")
                    {
                        "websocket"
                    } else {
                        "web_socket"
                    };
                    if response
                        .get("websocket")
                        .or_else(|| response.get("web_socket"))
                        .and_then(Value::as_str)
                        .is_none_or(|websocket| websocket.trim().is_empty())
                    {
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": format!("background run {mode} response requires websocket"),
                            "path": format!("$.initialResponse.{websocket_path}"),
                        }));
                    }
                    let cancel_path = if response.contains_key("cancel")
                        || !response.contains_key("cancel_route")
                    {
                        "cancel"
                    } else {
                        "cancel_route"
                    };
                    if response
                        .get("cancel")
                        .or_else(|| response.get("cancel_route"))
                        .and_then(Value::as_str)
                        .is_none_or(|cancel| cancel.trim().is_empty())
                    {
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": format!("background run {mode} response requires cancel"),
                            "path": format!("$.initialResponse.{cancel_path}"),
                        }));
                    }
                    if response
                        .get("initialCursor")
                        .or_else(|| response.get("initial_cursor"))
                        .and_then(Value::as_str)
                        .is_none_or(|cursor| cursor.trim().is_empty())
                    {
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": format!("background run {mode} response requires initialCursor"),
                            "path": "$.initialResponse.initialCursor",
                        }));
                    }
                } else {
                    diagnostics.push(json!({
                        "code": "DurableBackgroundRunInvalid",
                        "message": format!("background run {mode} response requires object initialResponse"),
                        "path": "$.initialResponse",
                    }));
                }
            }
            let raw_cancel_run = raw_detach
                .get("cancelRun")
                .or_else(|| raw_detach.get("cancel_run"));
            let cancel_run_path =
                if raw_detach.contains_key("cancelRun") || !raw_detach.contains_key("cancel_run") {
                    "cancelRun"
                } else {
                    "cancel_run"
                };
            let cancel_run = match raw_cancel_run {
                Some(value) => match value.as_bool() {
                    Some(flag) => flag,
                    None => {
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run detach requires boolean cancelRun",
                            "path": format!("$.detach.{cancel_run_path}"),
                        }));
                        false
                    }
                },
                None => false,
            };
            let raw_summary_on_expired_cursor = raw_attach
                .get("summaryOnExpiredCursor")
                .or_else(|| raw_attach.get("summary_on_expired_cursor"));
            let summary_on_expired_cursor_path = if raw_attach
                .contains_key("summaryOnExpiredCursor")
                || !raw_attach.contains_key("summary_on_expired_cursor")
            {
                "summaryOnExpiredCursor"
            } else {
                "summary_on_expired_cursor"
            };
            let summary_on_expired_cursor = match raw_summary_on_expired_cursor {
                Some(value) => match value.as_bool() {
                    Some(flag) => flag,
                    None => {
                        diagnostics.push(json!({
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run attach requires boolean summaryOnExpiredCursor",
                            "path": format!("$.attach.{summary_on_expired_cursor_path}"),
                        }));
                        false
                    }
                },
                None => false,
            };
            let source_of_truth_path =
                if case.get("sourceOfTruth").is_some() || case.get("source_of_truth").is_none() {
                    "sourceOfTruth"
                } else {
                    "source_of_truth"
                };
            let authoritative_stream = case
                .get("sourceOfTruth")
                .or_else(|| case.get("source_of_truth"))
                .and_then(Value::as_str)
                .is_some_and(|source| source == "ApplicationEventStream");
            if !authoritative_stream {
                diagnostics.push(json!({
                    "code": "DurableBackgroundRunInvalid",
                    "message": "background run sourceOfTruth must be ApplicationEventStream",
                    "path": format!("$.{source_of_truth_path}"),
                }));
            }
            json!({
                "runContinuesAfterDetach": lifetime_allows_detach && !cancel_run,
                "acceptedResponseReturnsRunId": matches!(valid_response_mode, Some("accepted"))
                    && initial_response
                        .is_some_and(|response| response
                            .get("runId")
                            .or_else(|| response.get("run_id"))
                            .and_then(Value::as_str)
                            .is_some_and(|run_id| !run_id.trim().is_empty())),
                "replayEventIds": replay_event_ids,
                "cursorExpired": expired_cursor_index
                    .zip(retained_from_index)
                    .is_some_and(|(expired_index, retained_index)| expired_index < retained_index),
                "summaryIncluded": summary_on_expired_cursor,
                "authoritativeStream": authoritative_stream,
            })
        }
        "callback_delivery_projection" => {
            let deliveries = required_array(case, "deliveries", name)?;
            if deliveries.is_empty() {
                diagnostics.push(json!({
                    "code": "DurableCallbackDeliveryInvalid",
                    "message": "callback delivery requires at least one delivery",
                    "path": "$.deliveries",
                }));
            }
            if case
                .get("redrive")
                .is_some_and(|redrive| !redrive.is_object())
            {
                diagnostics.push(json!({
                    "code": "DurableCallbackRedriveInvalid",
                    "message": "callback redrive must be object",
                    "path": "$.redrive",
                }));
            }
            let empty_redrive = Map::new();
            let raw_redrive = case
                .get("redrive")
                .and_then(Value::as_object)
                .unwrap_or(&empty_redrive);
            let has_redrive = case
                .get("redrive")
                .and_then(Value::as_object)
                .is_some_and(|redrive| !redrive.is_empty());
            if has_redrive {
                for (key, alias, message) in [
                    (
                        "operatorPrincipal",
                        "operator_principal",
                        "callback redrive requires operatorPrincipal",
                    ),
                    (
                        "reason",
                        "redrive_reason",
                        "callback redrive requires reason",
                    ),
                ] {
                    if raw_redrive
                        .get(key)
                        .or_else(|| raw_redrive.get(alias))
                        .and_then(Value::as_str)
                        .map_or(true, |value| value.trim().is_empty())
                    {
                        diagnostics.push(json!({
                            "code": "DurableCallbackRedriveInvalid",
                            "message": message,
                            "path": format!("$.redrive.{key}"),
                        }));
                    }
                }
                for (key, alias, message) in [
                    (
                        "deliveryId",
                        "delivery_id",
                        "callback redrive requires deliveryId",
                    ),
                    ("eventId", "event_id", "callback redrive requires eventId"),
                    (
                        "originalEventId",
                        "original_event_id",
                        "callback redrive requires originalEventId",
                    ),
                ] {
                    if raw_redrive
                        .get(key)
                        .or_else(|| raw_redrive.get(alias))
                        .and_then(Value::as_str)
                        .map_or(true, |value| value.trim().is_empty())
                    {
                        diagnostics.push(json!({
                            "code": "DurableCallbackRedriveInvalid",
                            "message": message,
                            "path": format!("$.redrive.{key}"),
                        }));
                    }
                }
            }
            let empty_redrive_assertions = Map::new();
            let raw_redrive_assertions_value = case
                .get("redriveAssertions")
                .or_else(|| case.get("redrive_assertions"));
            if raw_redrive_assertions_value.is_some_and(|assertions| !assertions.is_object()) {
                diagnostics.push(json!({
                    "code": "DurableCallbackRedriveInvalid",
                    "message": "callback redrive assertions must be object",
                    "path": "$.redriveAssertions",
                }));
            }
            let raw_redrive_assertions = raw_redrive_assertions_value
                .and_then(Value::as_object)
                .unwrap_or(&empty_redrive_assertions);
            for (key, alias) in [
                (
                    "deadLetterPreservesEventId",
                    "dead_letter_preserves_event_id",
                ),
                (
                    "redriveCreatesApplicationEvent",
                    "redrive_creates_application_event",
                ),
            ] {
                let path = if raw_redrive_assertions.contains_key(key)
                    || !raw_redrive_assertions.contains_key(alias)
                {
                    key
                } else {
                    alias
                };
                if raw_redrive_assertions
                    .get(key)
                    .or_else(|| raw_redrive_assertions.get(alias))
                    .is_some_and(|assertion| !assertion.is_boolean())
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackRedriveInvalid",
                        "message": format!("callback redrive assertion requires boolean {key}"),
                        "path": format!("$.redriveAssertions.{path}"),
                    }));
                }
            }
            let redrive_event_id = raw_redrive
                .get("eventId")
                .or_else(|| raw_redrive.get("event_id"))
                .and_then(Value::as_str)
                .filter(|value| !value.trim().is_empty());
            let original_redrive_event_id = raw_redrive
                .get("originalEventId")
                .or_else(|| raw_redrive.get("original_event_id"))
                .and_then(Value::as_str)
                .filter(|value| !value.trim().is_empty());
            if let Some((event_id, original_event_id)) =
                redrive_event_id.zip(original_redrive_event_id)
            {
                if event_id != original_event_id {
                    diagnostics.push(json!({
                        "code": "DurableCallbackRedriveInvalid",
                        "message": "callback redrive must preserve originalEventId",
                        "path": "$.redrive.eventId",
                    }));
                }
            }
            let dead_letter_preserves_event_id = redrive_event_id
                .zip(original_redrive_event_id)
                .is_some_and(|(event_id, original_event_id)| event_id == original_event_id);
            let raw_redrive_creates_application_event = raw_redrive
                .get("createsApplicationEvent")
                .or_else(|| raw_redrive.get("creates_application_event"));
            let redrive_creates_application_event = if has_redrive {
                match raw_redrive_creates_application_event {
                    Some(value) => match value.as_bool() {
                        Some(flag) => flag,
                        None => {
                            diagnostics.push(json!({
                                "code": "DurableCallbackRedriveInvalid",
                                "message": "callback redrive requires boolean createsApplicationEvent",
                                "path": "$.redrive.createsApplicationEvent",
                            }));
                            false
                        }
                    },
                    None => {
                        diagnostics.push(json!({
                            "code": "DurableCallbackRedriveInvalid",
                            "message": "callback redrive requires boolean createsApplicationEvent",
                            "path": "$.redrive.createsApplicationEvent",
                        }));
                        false
                    }
                }
            } else {
                false
            };
            if !has_redrive
                && raw_redrive_assertions
                    .get("deadLetterPreservesEventId")
                    .or_else(|| raw_redrive_assertions.get("dead_letter_preserves_event_id"))
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
            {
                diagnostics.push(json!({
                    "code": "DurableCallbackRedriveInvalid",
                    "message": "callback redrive evidence required for deadLetterPreservesEventId",
                    "path": "$.redrive",
                }));
            }
            if !has_redrive
                && raw_redrive_assertions
                    .get("redriveCreatesApplicationEvent")
                    .or_else(|| raw_redrive_assertions.get("redrive_creates_application_event"))
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
            {
                diagnostics.push(json!({
                    "code": "DurableCallbackRedriveInvalid",
                    "message": "callback redrive evidence required for redriveCreatesApplicationEvent",
                    "path": "$.redrive",
                }));
            }
            let raw_subscription = case
                .get("subscription")
                .or_else(|| case.get("callback_subscription"));
            if raw_subscription.is_some_and(|subscription| !subscription.is_object()) {
                diagnostics.push(json!({
                    "code": "DurableCallbackProjectionInvalid",
                    "message": "callback projection subscription must be object",
                    "path": "$.subscription",
                }));
            }
            if let Some(subscription) = raw_subscription.and_then(Value::as_object) {
                if subscription
                    .get("subscriptionId")
                    .or_else(|| subscription.get("subscription_id"))
                    .and_then(Value::as_str)
                    .is_none_or(|subscription_id| subscription_id.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackProjectionInvalid",
                        "message": "callback subscription requires subscriptionId",
                        "path": "$.subscription.subscriptionId",
                    }));
                }
                let raw_mandatory = subscription.get("mandatory");
                let mandatory = raw_mandatory.and_then(Value::as_bool).unwrap_or(false);
                let failure_policy = subscription
                    .get("failurePolicy")
                    .or_else(|| subscription.get("failure_policy"))
                    .and_then(Value::as_str);
                if let Some(failure_policy) = failure_policy {
                    if !matches!(
                        failure_policy,
                        "best_effort"
                            | "retry_then_dead_letter"
                            | "pause_run_on_failure"
                            | "fail_run_on_failure"
                    ) {
                        diagnostics.push(json!({
                            "code": "DurableCallbackProjectionInvalid",
                            "message": "callback subscription has invalid failurePolicy",
                            "path": "$.subscription.failurePolicy",
                        }));
                    }
                    if failure_policy == "best_effort" && mandatory {
                        diagnostics.push(json!({
                            "code": "DurableCallbackProjectionInvalid",
                            "message": "mandatory callback subscription requires retry, dead-letter, or fallback failurePolicy",
                            "path": "$.subscription.failurePolicy",
                        }));
                    }
                } else if mandatory {
                    diagnostics.push(json!({
                        "code": "DurableCallbackProjectionInvalid",
                        "message": "mandatory callback subscription requires retry, dead-letter, or fallback failurePolicy",
                        "path": "$.subscription.failurePolicy",
                    }));
                }
                if raw_mandatory.and_then(Value::as_bool).is_none() {
                    diagnostics.push(json!({
                        "code": "DurableCallbackProjectionInvalid",
                        "message": "callback subscription requires boolean mandatory",
                        "path": "$.subscription.mandatory",
                    }));
                }
            }
            let subscription_id = raw_subscription
                .and_then(Value::as_object)
                .and_then(|subscription| {
                    subscription
                        .get("subscriptionId")
                        .or_else(|| subscription.get("subscription_id"))
                })
                .and_then(Value::as_str)
                .filter(|subscription_id| !subscription_id.trim().is_empty())
                .map(str::trim);
            let subscription_failure_policy = case
                .get("subscription")
                .and_then(Value::as_object)
                .and_then(|subscription| {
                    subscription
                        .get("failurePolicy")
                        .or_else(|| subscription.get("failure_policy"))
                })
                .and_then(Value::as_str);
            let mut delivery_ids = BTreeSet::new();
            let mut idempotency_key_logical_deliveries = BTreeMap::new();
            let mut idempotency_keys_unique_per_subscription_event = true;
            let mut retry_scheduled_after_5xx = false;
            let mut retry_scheduled_after_retryable_status = false;
            let mut delivered_after_2xx = false;
            let mut duplicate_409_acknowledged = false;
            let mut subscription_gone_after_410 = false;
            let mut non_retryable_4xx_terminal = false;
            for (index, delivery) in deliveries.iter().enumerate() {
                if !delivery.is_object() {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery must be object",
                        "path": format!("$.deliveries[{index}]"),
                    }));
                }
            }
            for (index, raw_delivery) in deliveries.iter().enumerate() {
                let Some(delivery) = raw_delivery.as_object() else {
                    continue;
                };
                if delivery
                    .get("deliveryId")
                    .or_else(|| delivery.get("delivery_id"))
                    .and_then(Value::as_str)
                    .map_or(true, |delivery_id| delivery_id.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery requires deliveryId",
                        "path": format!("$.deliveries[{index}].deliveryId"),
                    }));
                }
                if let Some(delivery_id) = delivery
                    .get("deliveryId")
                    .or_else(|| delivery.get("delivery_id"))
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|delivery_id| !delivery_id.is_empty())
                {
                    if !delivery_ids.insert(delivery_id.to_owned()) {
                        diagnostics.push(json!({
                            "code": "DurableCallbackDeliveryInvalid",
                            "message": "callback delivery deliveryId must be unique",
                            "path": format!("$.deliveries[{index}].deliveryId"),
                        }));
                    }
                }
                if delivery
                    .get("eventId")
                    .or_else(|| delivery.get("event_id"))
                    .and_then(Value::as_str)
                    .map_or(true, |event_id| event_id.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery requires eventId",
                        "path": format!("$.deliveries[{index}].eventId"),
                    }));
                }
                if delivery
                    .get("runId")
                    .or_else(|| delivery.get("run_id"))
                    .and_then(Value::as_str)
                    .map_or(true, |run_id| run_id.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery requires runId",
                        "path": format!("$.deliveries[{index}].runId"),
                    }));
                }
                if delivery
                    .get("sequence")
                    .is_none_or(|sequence| sequence.as_bool().is_some() || !sequence.is_u64())
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery requires integer sequence",
                        "path": format!("$.deliveries[{index}].sequence"),
                    }));
                } else if delivery
                    .get("sequence")
                    .and_then(Value::as_u64)
                    .is_some_and(|sequence| sequence == 0)
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery requires positive integer sequence",
                        "path": format!("$.deliveries[{index}].sequence"),
                    }));
                }
                if delivery
                    .get("subscriptionId")
                    .or_else(|| delivery.get("subscription_id"))
                    .and_then(Value::as_str)
                    .map_or(true, |delivery_subscription_id| {
                        delivery_subscription_id.trim().is_empty()
                    })
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery requires subscriptionId",
                        "path": format!("$.deliveries[{index}].subscriptionId"),
                    }));
                }
                if delivery
                    .get("cursor")
                    .and_then(Value::as_str)
                    .map_or(true, |cursor| cursor.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery requires cursor",
                        "path": format!("$.deliveries[{index}].cursor"),
                    }));
                }
                if delivery
                    .get("attempt")
                    .is_none_or(|attempt| attempt.as_bool().is_some() || !attempt.is_u64())
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery requires integer attempt",
                        "path": format!("$.deliveries[{index}].attempt"),
                    }));
                } else if delivery
                    .get("attempt")
                    .and_then(Value::as_u64)
                    .is_some_and(|attempt| attempt == 0)
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery requires positive integer attempt",
                        "path": format!("$.deliveries[{index}].attempt"),
                    }));
                }
                if let Some(subscription_id) = subscription_id {
                    if delivery
                        .get("subscriptionId")
                        .or_else(|| delivery.get("subscription_id"))
                        .and_then(Value::as_str)
                        .is_some_and(|delivery_subscription_id| {
                            delivery_subscription_id.trim() != subscription_id
                        })
                    {
                        diagnostics.push(json!({
                            "code": "DurableCallbackDeliveryInvalid",
                            "message": "callback delivery subscriptionId must match subscription",
                            "path": format!("$.deliveries[{index}].subscriptionId"),
                        }));
                    }
                }
                let raw_receiver_status = delivery
                    .get("receiverStatus")
                    .or_else(|| delivery.get("receiver_status"));
                if raw_receiver_status
                    .is_some_and(|status| status.as_bool().is_some() || !status.is_u64())
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery requires integer receiverStatus",
                        "path": format!("$.deliveries[{index}].receiverStatus"),
                    }));
                }
                let receiver_status = raw_receiver_status.and_then(Value::as_u64).unwrap_or(0);
                if raw_receiver_status
                    .and_then(Value::as_u64)
                    .is_some_and(|status| !(100..=599).contains(&status))
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery receiverStatus must be an HTTP status code",
                        "path": format!("$.deliveries[{index}].receiverStatus"),
                    }));
                }
                let status_valid =
                    delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| {
                            matches!(
                                status,
                                "pending"
                                    | "delivering"
                                    | "delivered"
                                    | "acknowledged"
                                    | "failed"
                                    | "dead_lettered"
                                    | "cancelled"
                                    | "expired"
                            )
                        });
                if !status_valid {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery has invalid status",
                        "path": format!("$.deliveries[{index}].status"),
                    }));
                }
                if let Some(next_retry_at) = delivery
                    .get("nextRetryAt")
                    .or_else(|| delivery.get("next_retry_at"))
                {
                    let next_retry_at_valid = next_retry_at.as_str().is_some_and(|value| {
                        let trimmed = value.trim();
                        if trimmed.is_empty() {
                            return false;
                        }
                        let without_z = trimmed.strip_suffix('Z').unwrap_or(trimmed);
                        let Some((date, time_with_offset)) = without_z.split_once('T') else {
                            return false;
                        };
                        let mut date_parts = date.split('-');
                        let year = date_parts.next();
                        let month = date_parts.next();
                        let day = date_parts.next();
                        if date_parts.next().is_some() {
                            return false;
                        }
                        let Some(year) = year.and_then(|part| part.parse::<u16>().ok()) else {
                            return false;
                        };
                        let Some(month) = month.and_then(|part| part.parse::<u8>().ok()) else {
                            return false;
                        };
                        let Some(day) = day.and_then(|part| part.parse::<u8>().ok()) else {
                            return false;
                        };
                        if year == 0 || !(1..=12).contains(&month) || !(1..=31).contains(&day) {
                            return false;
                        }
                        let offset_start =
                            time_with_offset.find(|character| character == '+' || character == '-');
                        let time = offset_start
                            .map_or(time_with_offset, |index| &time_with_offset[..index]);
                        let mut time_parts = time.split(':');
                        let hour = time_parts.next();
                        let minute = time_parts.next();
                        let second = time_parts.next();
                        if time_parts.next().is_some() {
                            return false;
                        }
                        let Some(hour) = hour.and_then(|part| part.parse::<u8>().ok()) else {
                            return false;
                        };
                        let Some(minute) = minute.and_then(|part| part.parse::<u8>().ok()) else {
                            return false;
                        };
                        let Some(second_text) = second else {
                            return false;
                        };
                        let second_integer = second_text.split('.').next();
                        let Some(second) = second_integer.and_then(|part| part.parse::<u8>().ok())
                        else {
                            return false;
                        };
                        hour <= 23 && minute <= 59 && second <= 59
                    });
                    if !next_retry_at_valid {
                        diagnostics.push(json!({
                            "code": "DurableCallbackDeliveryInvalid",
                            "message": "callback delivery requires nextRetryAt timestamp",
                            "path": format!("$.deliveries[{index}].nextRetryAt"),
                        }));
                    }
                }
                if receiver_status >= 500
                    && delivery
                        .get("nextRetryAt")
                        .or_else(|| delivery.get("next_retry_at"))
                        .is_some()
                {
                    retry_scheduled_after_5xx = true;
                }
                if (receiver_status == 429 || receiver_status >= 500)
                    && delivery
                        .get("nextRetryAt")
                        .or_else(|| delivery.get("next_retry_at"))
                        .is_some()
                {
                    retry_scheduled_after_retryable_status = true;
                }
                if (receiver_status == 429 || receiver_status >= 500)
                    && delivery
                        .get("nextRetryAt")
                        .or_else(|| delivery.get("next_retry_at"))
                        .is_some()
                    && delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| status != "failed")
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery retry requires failed status",
                        "path": format!("$.deliveries[{index}].status"),
                    }));
                }
                if subscription_failure_policy == Some("retry_then_dead_letter")
                    && (receiver_status == 429 || receiver_status >= 500)
                    && delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| status == "failed")
                    && delivery
                        .get("nextRetryAt")
                        .or_else(|| delivery.get("next_retry_at"))
                        .is_none()
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "retry_then_dead_letter callback delivery requires nextRetryAt",
                        "path": format!("$.deliveries[{index}].nextRetryAt"),
                    }));
                }
                if !(receiver_status == 429 || receiver_status >= 500)
                    && delivery
                        .get("nextRetryAt")
                        .or_else(|| delivery.get("next_retry_at"))
                        .is_some()
                    && delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| {
                            matches!(
                                status,
                                "delivered"
                                    | "acknowledged"
                                    | "dead_lettered"
                                    | "cancelled"
                                    | "expired"
                            )
                        })
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "terminal callback delivery must not have nextRetryAt",
                        "path": format!("$.deliveries[{index}].nextRetryAt"),
                    }));
                }
                if (200..=299).contains(&receiver_status)
                    && delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| status == "delivered")
                {
                    delivered_after_2xx = true;
                }
                if let Some(status) = delivery.get("status").and_then(Value::as_str) {
                    if matches!(status, "delivered" | "acknowledged") {
                        if let Some(delivered_at) = delivery
                            .get("deliveredAt")
                            .or_else(|| delivery.get("delivered_at"))
                        {
                            let delivered_at_valid = delivered_at.as_str().is_some_and(|value| {
                                let trimmed = value.trim();
                                if trimmed.is_empty() {
                                    return false;
                                }
                                let without_z = trimmed.strip_suffix('Z').unwrap_or(trimmed);
                                let Some((date, time_with_offset)) = without_z.split_once('T')
                                else {
                                    return false;
                                };
                                let mut date_parts = date.split('-');
                                let year = date_parts.next();
                                let month = date_parts.next();
                                let day = date_parts.next();
                                if date_parts.next().is_some() {
                                    return false;
                                }
                                let Some(year) = year.and_then(|part| part.parse::<u16>().ok())
                                else {
                                    return false;
                                };
                                let Some(month) = month.and_then(|part| part.parse::<u8>().ok())
                                else {
                                    return false;
                                };
                                let Some(day) = day.and_then(|part| part.parse::<u8>().ok()) else {
                                    return false;
                                };
                                if year == 0
                                    || !(1..=12).contains(&month)
                                    || !(1..=31).contains(&day)
                                {
                                    return false;
                                }
                                let offset_start = time_with_offset
                                    .find(|character| character == '+' || character == '-');
                                let time = offset_start
                                    .map_or(time_with_offset, |index| &time_with_offset[..index]);
                                let mut time_parts = time.split(':');
                                let hour = time_parts.next();
                                let minute = time_parts.next();
                                let second = time_parts.next();
                                if time_parts.next().is_some() {
                                    return false;
                                }
                                let Some(hour) = hour.and_then(|part| part.parse::<u8>().ok())
                                else {
                                    return false;
                                };
                                let Some(minute) = minute.and_then(|part| part.parse::<u8>().ok())
                                else {
                                    return false;
                                };
                                let Some(second_text) = second else {
                                    return false;
                                };
                                let second_integer = second_text.split('.').next();
                                let Some(second) =
                                    second_integer.and_then(|part| part.parse::<u8>().ok())
                                else {
                                    return false;
                                };
                                hour <= 23 && minute <= 59 && second <= 59
                            });
                            if !delivered_at_valid {
                                diagnostics.push(json!({
                                    "code": "DurableCallbackDeliveryInvalid",
                                    "message": format!("{status} callback delivery requires deliveredAt"),
                                    "path": format!("$.deliveries[{index}].deliveredAt"),
                                }));
                            }
                        }
                    }
                }
                if delivery
                    .get("status")
                    .and_then(Value::as_str)
                    .is_some_and(|status| status == "delivered")
                    && delivery
                        .get("deliveredAt")
                        .or_else(|| delivery.get("delivered_at"))
                        .and_then(Value::as_str)
                        .map_or(true, |delivered_at| delivered_at.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "delivered callback delivery requires deliveredAt",
                        "path": format!("$.deliveries[{index}].deliveredAt"),
                    }));
                }
                if (200..=299).contains(&receiver_status)
                    && delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| status != "delivered" && status != "acknowledged")
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "2xx callback delivery requires delivered or acknowledged status",
                        "path": format!("$.deliveries[{index}].status"),
                    }));
                }
                if receiver_status == 409
                    && delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| status == "acknowledged")
                {
                    duplicate_409_acknowledged = true;
                }
                if delivery
                    .get("status")
                    .and_then(Value::as_str)
                    .is_some_and(|status| status == "acknowledged")
                    && delivery
                        .get("acknowledgedAt")
                        .or_else(|| delivery.get("acknowledged_at"))
                        .and_then(Value::as_str)
                        .map_or(true, |acknowledged_at| acknowledged_at.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "acknowledged callback delivery requires acknowledgedAt",
                        "path": format!("$.deliveries[{index}].acknowledgedAt"),
                    }));
                }
                if delivery
                    .get("status")
                    .and_then(Value::as_str)
                    .is_some_and(|status| status == "acknowledged")
                {
                    if let Some(acknowledged_at) = delivery
                        .get("acknowledgedAt")
                        .or_else(|| delivery.get("acknowledged_at"))
                    {
                        let acknowledged_at_valid = acknowledged_at.as_str().is_some_and(|value| {
                            let trimmed = value.trim();
                            if trimmed.is_empty() {
                                return false;
                            }
                            let without_z = trimmed.strip_suffix('Z').unwrap_or(trimmed);
                            let Some((date, time_with_offset)) = without_z.split_once('T') else {
                                return false;
                            };
                            let mut date_parts = date.split('-');
                            let year = date_parts.next();
                            let month = date_parts.next();
                            let day = date_parts.next();
                            if date_parts.next().is_some() {
                                return false;
                            }
                            let Some(year) = year.and_then(|part| part.parse::<u16>().ok()) else {
                                return false;
                            };
                            let Some(month) = month.and_then(|part| part.parse::<u8>().ok()) else {
                                return false;
                            };
                            let Some(day) = day.and_then(|part| part.parse::<u8>().ok()) else {
                                return false;
                            };
                            if year == 0 || !(1..=12).contains(&month) || !(1..=31).contains(&day) {
                                return false;
                            }
                            let offset_start = time_with_offset
                                .find(|character| character == '+' || character == '-');
                            let time = offset_start
                                .map_or(time_with_offset, |index| &time_with_offset[..index]);
                            let mut time_parts = time.split(':');
                            let hour = time_parts.next();
                            let minute = time_parts.next();
                            let second = time_parts.next();
                            if time_parts.next().is_some() {
                                return false;
                            }
                            let Some(hour) = hour.and_then(|part| part.parse::<u8>().ok()) else {
                                return false;
                            };
                            let Some(minute) = minute.and_then(|part| part.parse::<u8>().ok())
                            else {
                                return false;
                            };
                            let Some(second_text) = second else {
                                return false;
                            };
                            let second_integer = second_text.split('.').next();
                            let Some(second) =
                                second_integer.and_then(|part| part.parse::<u8>().ok())
                            else {
                                return false;
                            };
                            hour <= 23 && minute <= 59 && second <= 59
                        });
                        if !acknowledged_at_valid {
                            diagnostics.push(json!({
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "acknowledged callback delivery requires acknowledgedAt",
                                "path": format!("$.deliveries[{index}].acknowledgedAt"),
                            }));
                        }
                    }
                }
                if delivery
                    .get("status")
                    .and_then(Value::as_str)
                    .is_some_and(|status| status == "acknowledged")
                {
                    let delivered_at = delivery
                        .get("deliveredAt")
                        .or_else(|| delivery.get("delivered_at"))
                        .and_then(Value::as_str)
                        .filter(|value| !value.trim().is_empty());
                    let acknowledged_at = delivery
                        .get("acknowledgedAt")
                        .or_else(|| delivery.get("acknowledged_at"))
                        .and_then(Value::as_str)
                        .filter(|value| !value.trim().is_empty());
                    if let (Some(delivered_at), Some(acknowledged_at)) =
                        (delivered_at, acknowledged_at)
                    {
                        if acknowledged_at < delivered_at {
                            diagnostics.push(json!({
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "acknowledgedAt must not be before deliveredAt",
                                "path": format!("$.deliveries[{index}].acknowledgedAt"),
                            }));
                        }
                    }
                }
                if receiver_status == 409
                    && delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| status != "acknowledged")
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery duplicate 409 requires acknowledged status",
                        "path": format!("$.deliveries[{index}].status"),
                    }));
                }
                if receiver_status == 410
                    && delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| status != "cancelled")
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "410 callback delivery requires cancelled status",
                        "path": format!("$.deliveries[{index}].status"),
                    }));
                }
                if receiver_status == 410
                    && delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| status == "cancelled")
                    && delivery
                        .get("lastError")
                        .or_else(|| delivery.get("last_error"))
                        .and_then(Value::as_str)
                        .is_some_and(|last_error| {
                            !last_error.trim().is_empty() && last_error != "subscription_gone"
                        })
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "410 callback delivery requires subscription_gone error",
                        "path": format!("$.deliveries[{index}].lastError"),
                    }));
                }
                if receiver_status == 410
                    && delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| status == "cancelled")
                    && delivery
                        .get("lastError")
                        .or_else(|| delivery.get("last_error"))
                        .and_then(Value::as_str)
                        .is_some_and(|last_error| last_error == "subscription_gone")
                {
                    subscription_gone_after_410 = true;
                }
                if (400..=499).contains(&receiver_status)
                    && !matches!(receiver_status, 409 | 410 | 429)
                    && delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| status != "failed")
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "non-retryable 4xx callback delivery requires failed status",
                        "path": format!("$.deliveries[{index}].status"),
                    }));
                }
                if (400..=499).contains(&receiver_status)
                    && !matches!(receiver_status, 409 | 410 | 429)
                    && delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| status == "failed")
                    && delivery
                        .get("lastError")
                        .or_else(|| delivery.get("last_error"))
                        .and_then(Value::as_str)
                        .is_some_and(|last_error| {
                            !last_error.trim().is_empty() && last_error != "non_retryable"
                        })
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "non-retryable 4xx callback delivery requires non_retryable error",
                        "path": format!("$.deliveries[{index}].lastError"),
                    }));
                }
                if (400..=499).contains(&receiver_status)
                    && !matches!(receiver_status, 409 | 410 | 429)
                    && delivery
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| status == "failed")
                    && delivery
                        .get("lastError")
                        .or_else(|| delivery.get("last_error"))
                        .and_then(Value::as_str)
                        .is_some_and(|last_error| last_error == "non_retryable")
                {
                    non_retryable_4xx_terminal = true;
                }
                if let Some(status) = delivery.get("status").and_then(Value::as_str) {
                    if matches!(status, "failed" | "dead_lettered" | "cancelled" | "expired")
                        && delivery
                            .get("lastError")
                            .or_else(|| delivery.get("last_error"))
                            .and_then(Value::as_str)
                            .map_or(true, |last_error| last_error.trim().is_empty())
                    {
                        diagnostics.push(json!({
                            "code": "DurableCallbackDeliveryInvalid",
                            "message": format!("{status} callback delivery requires lastError"),
                            "path": format!("$.deliveries[{index}].lastError"),
                        }));
                    }
                }
                if delivery
                    .get("idempotencyKey")
                    .or_else(|| delivery.get("idempotency_key"))
                    .and_then(Value::as_str)
                    .map_or(true, |key| key.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery requires idempotencyKey",
                        "path": format!("$.deliveries[{index}].idempotencyKey"),
                    }));
                }
                if let Some(key) = delivery
                    .get("idempotencyKey")
                    .or_else(|| delivery.get("idempotency_key"))
                    .and_then(Value::as_str)
                {
                    let normalized_key = key.trim().to_owned();
                    let subscription_id = delivery
                        .get("subscriptionId")
                        .or_else(|| delivery.get("subscription_id"))
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .trim()
                        .to_owned();
                    let event_id = delivery
                        .get("eventId")
                        .or_else(|| delivery.get("event_id"))
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .trim()
                        .to_owned();
                    let logical_delivery = (subscription_id, event_id);
                    if let Some(previous_delivery) =
                        idempotency_key_logical_deliveries.get(&normalized_key)
                    {
                        if previous_delivery != &logical_delivery {
                            idempotency_keys_unique_per_subscription_event = false;
                            diagnostics.push(json!({
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "callback delivery idempotencyKey must be unique",
                                "path": format!("$.deliveries[{index}].idempotencyKey"),
                            }));
                        }
                    } else {
                        idempotency_key_logical_deliveries.insert(normalized_key, logical_delivery);
                    }
                }
            }
            let raw_non_mandatory_outage_blocks_run = case
                .get("nonMandatoryOutageBlocksRun")
                .or_else(|| case.get("non_mandatory_outage_blocks_run"));
            let non_mandatory_outage_blocks_run = match raw_non_mandatory_outage_blocks_run {
                Some(value) => match value.as_bool() {
                    Some(flag) => flag,
                    None => {
                        diagnostics.push(json!({
                            "code": "DurableCallbackProjectionInvalid",
                            "message": "callback projection requires boolean nonMandatoryOutageBlocksRun",
                            "path": "$.nonMandatoryOutageBlocksRun",
                        }));
                        true
                    }
                },
                None => {
                    diagnostics.push(json!({
                        "code": "DurableCallbackProjectionInvalid",
                        "message": "callback projection requires boolean nonMandatoryOutageBlocksRun",
                        "path": "$.nonMandatoryOutageBlocksRun",
                    }));
                    true
                }
            };
            json!({
                "retryScheduledAfter5xx": retry_scheduled_after_5xx,
                "retryScheduledAfterRetryableStatus": retry_scheduled_after_retryable_status,
                "deliveredAfter2xx": delivered_after_2xx,
                "duplicate409Acknowledged": duplicate_409_acknowledged,
                "subscriptionGoneAfter410": subscription_gone_after_410,
                "nonRetryable4xxTerminal": non_retryable_4xx_terminal,
                "idempotencyKeysUniquePerSubscriptionEvent": idempotency_keys_unique_per_subscription_event,
                "deadLetterPreservesEventId": dead_letter_preserves_event_id,
                "redriveCreatesApplicationEvent": redrive_creates_application_event,
                "nonMandatoryOutageBlocksRun": non_mandatory_outage_blocks_run,
            })
        }
        "async_callback_resume_guards" => {
            let raw_checks = required_object(case, "checks", name)?;
            let raw_callback = required_object(case, "callback", name)?;
            let raw_resume = required_object(case, "resume", name)?;
            if let Some(operation) = case.get("operation").and_then(Value::as_object) {
                for (key, alias) in [
                    ("operationId", "operation_id"),
                    ("runId", "run_id"),
                    ("nodeId", "node_id"),
                    ("attemptId", "attempt_id"),
                    ("releaseId", "release_id"),
                    ("tenantId", "tenant_id"),
                    ("policySnapshotId", "policy_snapshot_id"),
                ] {
                    let path_key = if operation.contains_key(key) || !operation.contains_key(alias)
                    {
                        key
                    } else {
                        alias
                    };
                    if operation
                        .get(key)
                        .or_else(|| operation.get(alias))
                        .and_then(Value::as_str)
                        .map_or(true, |value| value.trim().is_empty())
                    {
                        diagnostics.push(json!({
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": format!("async callback resume operation requires nonblank {key}"),
                            "path": format!("$.operation.{path_key}"),
                        }));
                    }
                }
                if operation.contains_key("providerOperationId")
                    || operation.contains_key("provider_operation_id")
                {
                    let provider_operation_id_path = if operation
                        .contains_key("providerOperationId")
                        || !operation.contains_key("provider_operation_id")
                    {
                        "providerOperationId"
                    } else {
                        "provider_operation_id"
                    };
                    if operation
                        .get("providerOperationId")
                        .or_else(|| operation.get("provider_operation_id"))
                        .and_then(Value::as_str)
                        .map_or(true, |provider_operation_id| {
                            provider_operation_id.trim().is_empty()
                        })
                    {
                        diagnostics.push(json!({
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume operation requires nonblank providerOperationId",
                            "path": format!("$.operation.{provider_operation_id_path}"),
                        }));
                    }
                }
                let resume_token_hash_path = if operation.contains_key("resumeTokenHash")
                    || !operation.contains_key("resume_token_hash")
                {
                    "resumeTokenHash"
                } else {
                    "resume_token_hash"
                };
                if !operation
                    .get("resumeTokenHash")
                    .or_else(|| operation.get("resume_token_hash"))
                    .and_then(Value::as_str)
                    .is_some_and(|resume_token_hash| {
                        let Some(hex) = resume_token_hash.strip_prefix("sha256:") else {
                            return false;
                        };
                        hex.len() == 64
                            && hex
                                .bytes()
                                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
                    })
                {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume operation requires resumeTokenHash sha256 digest",
                        "path": format!("$.operation.{resume_token_hash_path}"),
                    }));
                }
                let expected_schema_path = if operation.contains_key("expectedSchema")
                    || !operation.contains_key("expected_schema")
                {
                    "expectedSchema"
                } else {
                    "expected_schema"
                };
                if operation
                    .get("expectedSchema")
                    .or_else(|| operation.get("expected_schema"))
                    .and_then(Value::as_str)
                    .map_or(true, |expected_schema| expected_schema.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume operation requires nonblank expectedSchema",
                        "path": format!("$.operation.{expected_schema_path}"),
                    }));
                }
                let deadline_is_iso = operation
                    .get("deadline")
                    .and_then(Value::as_str)
                    .is_some_and(|deadline| {
                        let deadline = deadline.trim();
                        let bytes = deadline.as_bytes();
                        let digit_positions = [0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18];
                        bytes.len() >= 20
                            && digit_positions
                                .into_iter()
                                .all(|position| bytes.get(position).is_some_and(u8::is_ascii_digit))
                            && bytes.get(4) == Some(&b'-')
                            && bytes.get(7) == Some(&b'-')
                            && bytes.get(10) == Some(&b'T')
                            && bytes.get(13) == Some(&b':')
                            && bytes.get(16) == Some(&b':')
                            && (deadline.ends_with('Z')
                                || deadline.get(19..).is_some_and(|suffix| {
                                    suffix.contains('+') || suffix.contains('-')
                                }))
                    });
                if !deadline_is_iso {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume operation requires ISO deadline",
                        "path": "$.operation.deadline",
                    }));
                }
                let budget_state_path = if operation.contains_key("budgetState")
                    || !operation.contains_key("budget_state")
                {
                    "budgetState"
                } else {
                    "budget_state"
                };
                if operation
                    .get("budgetState")
                    .or_else(|| operation.get("budget_state"))
                    .and_then(Value::as_str)
                    .map_or(true, |budget_state| budget_state.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume operation requires nonblank budgetState",
                        "path": format!("$.operation.{budget_state_path}"),
                    }));
                }
            }
            let operation_provider_operation_id = case
                .get("operation")
                .and_then(Value::as_object)
                .and_then(|operation| {
                    operation
                        .get("providerOperationId")
                        .or_else(|| operation.get("provider_operation_id"))
                })
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|provider_operation_id| !provider_operation_id.is_empty());
            let callback_receipt_supplied = [
                "callbackId",
                "callback_id",
                "payloadDigest",
                "payload_digest",
                "verifiedBy",
                "verified_by",
                "idempotencyKey",
                "idempotency_key",
                "receivedAt",
                "received_at",
                "releaseId",
                "release_id",
                "tenantId",
                "tenant_id",
                "providerOperationId",
                "provider_operation_id",
            ]
            .into_iter()
            .any(|key| raw_callback.contains_key(key));
            if callback_receipt_supplied {
                let callback_id_path = if raw_callback.contains_key("callbackId")
                    || !raw_callback.contains_key("callback_id")
                {
                    "callbackId"
                } else {
                    "callback_id"
                };
                if raw_callback
                    .get("callbackId")
                    .or_else(|| raw_callback.get("callback_id"))
                    .and_then(Value::as_str)
                    .map_or(true, |callback_id| callback_id.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume callback requires nonblank callbackId",
                        "path": format!("$.callback.{callback_id_path}"),
                    }));
                }
                let payload_digest_path = if raw_callback.contains_key("payloadDigest")
                    || !raw_callback.contains_key("payload_digest")
                {
                    "payloadDigest"
                } else {
                    "payload_digest"
                };
                if !raw_callback
                    .get("payloadDigest")
                    .or_else(|| raw_callback.get("payload_digest"))
                    .and_then(Value::as_str)
                    .is_some_and(|payload_digest| {
                        let Some(hex) = payload_digest.strip_prefix("sha256:") else {
                            return false;
                        };
                        hex.len() == 64
                            && hex
                                .bytes()
                                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
                    })
                {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume callback requires payloadDigest sha256 digest",
                        "path": format!("$.callback.{payload_digest_path}"),
                    }));
                }
                let verified_by_path = if raw_callback.contains_key("verifiedBy")
                    || !raw_callback.contains_key("verified_by")
                {
                    "verifiedBy"
                } else {
                    "verified_by"
                };
                if raw_callback
                    .get("verifiedBy")
                    .or_else(|| raw_callback.get("verified_by"))
                    .and_then(Value::as_str)
                    .map_or(true, |verified_by| verified_by.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume callback requires nonblank verifiedBy",
                        "path": format!("$.callback.{verified_by_path}"),
                    }));
                } else if raw_callback
                    .get("verifiedBy")
                    .or_else(|| raw_callback.get("verified_by"))
                    .and_then(Value::as_str)
                    .is_some_and(|verified_by| {
                        verified_by.trim().eq_ignore_ascii_case("unauthenticated")
                    })
                {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume callback requires authenticated verifiedBy",
                        "path": format!("$.callback.{verified_by_path}"),
                    }));
                }
                let idempotency_key_path = if raw_callback.contains_key("idempotencyKey")
                    || !raw_callback.contains_key("idempotency_key")
                {
                    "idempotencyKey"
                } else {
                    "idempotency_key"
                };
                if raw_callback
                    .get("idempotencyKey")
                    .or_else(|| raw_callback.get("idempotency_key"))
                    .and_then(Value::as_str)
                    .map_or(true, |idempotency_key| idempotency_key.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume callback requires nonblank idempotencyKey",
                        "path": format!("$.callback.{idempotency_key_path}"),
                    }));
                }
                let received_at_path = if raw_callback.contains_key("receivedAt")
                    || !raw_callback.contains_key("received_at")
                {
                    "receivedAt"
                } else {
                    "received_at"
                };
                let received_at_is_iso = raw_callback
                    .get("receivedAt")
                    .or_else(|| raw_callback.get("received_at"))
                    .and_then(Value::as_str)
                    .is_some_and(|received_at| {
                        let received_at = received_at.trim();
                        let bytes = received_at.as_bytes();
                        let digit_positions = [0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18];
                        bytes.len() >= 20
                            && digit_positions
                                .into_iter()
                                .all(|position| bytes.get(position).is_some_and(u8::is_ascii_digit))
                            && bytes.get(4) == Some(&b'-')
                            && bytes.get(7) == Some(&b'-')
                            && bytes.get(10) == Some(&b'T')
                            && bytes.get(13) == Some(&b':')
                            && bytes.get(16) == Some(&b':')
                            && (received_at.ends_with('Z')
                                || received_at.get(19..).is_some_and(|suffix| {
                                    suffix.contains('+') || suffix.contains('-')
                                }))
                    });
                if !received_at_is_iso {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume callback requires ISO receivedAt",
                        "path": format!("$.callback.{received_at_path}"),
                    }));
                }
                let release_id_path = if raw_callback.contains_key("releaseId")
                    || !raw_callback.contains_key("release_id")
                {
                    "releaseId"
                } else {
                    "release_id"
                };
                if raw_callback
                    .get("releaseId")
                    .or_else(|| raw_callback.get("release_id"))
                    .and_then(Value::as_str)
                    .map_or(true, |release_id| release_id.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume callback requires nonblank releaseId",
                        "path": format!("$.callback.{release_id_path}"),
                    }));
                }
                let tenant_id_path = if raw_callback.contains_key("tenantId")
                    || !raw_callback.contains_key("tenant_id")
                {
                    "tenantId"
                } else {
                    "tenant_id"
                };
                if raw_callback
                    .get("tenantId")
                    .or_else(|| raw_callback.get("tenant_id"))
                    .and_then(Value::as_str)
                    .map_or(true, |tenant_id| tenant_id.trim().is_empty())
                {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume callback requires nonblank tenantId",
                        "path": format!("$.callback.{tenant_id_path}"),
                    }));
                }
                for (key, alias) in [
                    ("operationId", "operation_id"),
                    ("runId", "run_id"),
                    ("nodeId", "node_id"),
                    ("attemptId", "attempt_id"),
                    ("policySnapshotId", "policy_snapshot_id"),
                ] {
                    let path_key =
                        if raw_callback.contains_key(key) || !raw_callback.contains_key(alias) {
                            key
                        } else {
                            alias
                        };
                    if raw_callback
                        .get(key)
                        .or_else(|| raw_callback.get(alias))
                        .and_then(Value::as_str)
                        .map_or(true, |value| value.trim().is_empty())
                    {
                        diagnostics.push(json!({
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": format!("async callback resume callback requires nonblank {key}"),
                            "path": format!("$.callback.{path_key}"),
                        }));
                    }
                }
                if let Some(operation_provider_operation_id) = operation_provider_operation_id {
                    let provider_operation_id_path = if raw_callback
                        .contains_key("providerOperationId")
                        || !raw_callback.contains_key("provider_operation_id")
                    {
                        "providerOperationId"
                    } else {
                        "provider_operation_id"
                    };
                    match raw_callback
                        .get("providerOperationId")
                        .or_else(|| raw_callback.get("provider_operation_id"))
                        .and_then(Value::as_str)
                        .map(str::trim)
                    {
                        Some(callback_provider_operation_id)
                            if !callback_provider_operation_id.is_empty() =>
                        {
                            if callback_provider_operation_id != operation_provider_operation_id {
                                diagnostics.push(json!({
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume callback providerOperationId must match operation providerOperationId",
                                    "path": format!("$.callback.{provider_operation_id_path}"),
                                }));
                            }
                        }
                        _ => {
                            diagnostics.push(json!({
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback requires providerOperationId",
                                "path": format!("$.callback.{provider_operation_id_path}"),
                            }));
                        }
                    }
                }
                if let Some(operation) = case.get("operation").and_then(Value::as_object) {
                    for (key, alias) in [
                        ("operationId", "operation_id"),
                        ("runId", "run_id"),
                        ("nodeId", "node_id"),
                        ("attemptId", "attempt_id"),
                        ("releaseId", "release_id"),
                        ("tenantId", "tenant_id"),
                        ("policySnapshotId", "policy_snapshot_id"),
                    ] {
                        let callback_value = raw_callback
                            .get(key)
                            .or_else(|| raw_callback.get(alias))
                            .and_then(Value::as_str);
                        let operation_value = operation
                            .get(key)
                            .or_else(|| operation.get(alias))
                            .and_then(Value::as_str);
                        if let (Some(callback_value), Some(operation_value)) =
                            (callback_value, operation_value)
                        {
                            let callback_value = callback_value.trim();
                            let operation_value = operation_value.trim();
                            if !callback_value.is_empty()
                                && !operation_value.is_empty()
                                && callback_value != operation_value
                            {
                                let path_key = if raw_callback.contains_key(key)
                                    || !raw_callback.contains_key(alias)
                                {
                                    key
                                } else {
                                    alias
                                };
                                diagnostics.push(json!({
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": format!("async callback resume callback {key} must match operation {key}"),
                                    "path": format!("$.callback.{path_key}"),
                                }));
                            }
                        }
                    }
                }
            }
            let mut guard_values = BTreeMap::new();
            for (key, alias) in [
                (
                    "signatureFailureRevealsOperation",
                    "signature_failure_reveals_operation",
                ),
                ("schemaFailureResumesRun", "schema_failure_resumes_run"),
                (
                    "timeoutCallbackResumesExpiredOperation",
                    "timeout_callback_resumes_expired_operation",
                ),
                (
                    "cancelledCallbackCommitsResult",
                    "cancelled_callback_commits_result",
                ),
                ("staleAttemptCanResume", "stale_attempt_can_resume"),
                (
                    "unauthenticatedCallbackCanResume",
                    "unauthenticated_callback_can_resume",
                ),
                (
                    "nonExternalCallbackEventCanBecomeReceipt",
                    "non_external_callback_event_can_become_receipt",
                ),
                (
                    "providerOperationMismatchCanResume",
                    "provider_operation_mismatch_can_resume",
                ),
            ] {
                let raw_value = raw_checks.get(key).or_else(|| raw_checks.get(alias));
                guard_values.insert(key, raw_value.and_then(Value::as_bool).unwrap_or(true));
                if raw_value.is_none_or(|value| !value.is_boolean()) {
                    let path_key =
                        if raw_checks.contains_key(key) || !raw_checks.contains_key(alias) {
                            key
                        } else {
                            alias
                        };
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": format!("async callback resume guard requires boolean {key}"),
                        "path": format!("$.checks.{path_key}"),
                    }));
                }
            }
            let mut reevaluates = BTreeSet::new();
            match raw_resume.get("reevaluates") {
                Some(Value::Array(entries)) => {
                    for (index, entry) in entries.iter().enumerate() {
                        if let Some(reevaluate) = entry
                            .as_str()
                            .map(str::trim)
                            .filter(|value| !value.is_empty())
                        {
                            reevaluates.insert(reevaluate.to_owned());
                        } else {
                            diagnostics.push(json!({
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume requires string reevaluates entry",
                                "path": format!("$.resume.reevaluates[{index}]"),
                            }));
                        }
                    }
                }
                Some(_) => {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume requires reevaluates sequence",
                        "path": "$.resume.reevaluates",
                    }));
                }
                None => {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume requires reevaluates sequence",
                        "path": "$.resume.reevaluates",
                    }));
                }
            }
            let raw_callback_journal_sequence = raw_callback
                .get("journalSequence")
                .or_else(|| raw_callback.get("journal_sequence"));
            let callback_journal_sequence = match raw_callback_journal_sequence {
                Some(value) => match value.as_u64() {
                    Some(sequence) => {
                        if sequence == 0 {
                            diagnostics.push(json!({
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume requires positive integer callback journalSequence",
                                "path": "$.callback.journalSequence",
                            }));
                        }
                        sequence
                    }
                    None => {
                        diagnostics.push(json!({
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume requires integer callback journalSequence",
                            "path": "$.callback.journalSequence",
                        }));
                        0
                    }
                },
                None => {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume requires integer callback journalSequence",
                        "path": "$.callback.journalSequence",
                    }));
                    0
                }
            };
            let raw_resume_sequence = raw_resume
                .get("resumeSequence")
                .or_else(|| raw_resume.get("resume_sequence"));
            let resume_sequence = match raw_resume_sequence {
                Some(value) => match value.as_u64() {
                    Some(sequence) => {
                        if sequence == 0 {
                            diagnostics.push(json!({
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume requires positive integer resumeSequence",
                                "path": "$.resume.resumeSequence",
                            }));
                        }
                        sequence
                    }
                    None => {
                        diagnostics.push(json!({
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume requires integer resumeSequence",
                            "path": "$.resume.resumeSequence",
                        }));
                        0
                    }
                },
                None => {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume requires integer resumeSequence",
                        "path": "$.resume.resumeSequence",
                    }));
                    0
                }
            };
            if callback_journal_sequence > 0
                && resume_sequence > 0
                && callback_journal_sequence >= resume_sequence
            {
                diagnostics.push(json!({
                    "code": "DurableAsyncCallbackResumeInvalid",
                    "message": "async callback resume requires callback journalSequence before resumeSequence",
                    "path": "$.resume.resumeSequence",
                }));
            }
            let raw_successful_resume_count = raw_resume
                .get("successfulResumeCount")
                .or_else(|| raw_resume.get("successful_resume_count"));
            let successful_resume_count = match raw_successful_resume_count {
                Some(value) => match value.as_u64() {
                    Some(count) => count,
                    None => {
                        diagnostics.push(json!({
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume requires integer successfulResumeCount",
                            "path": "$.resume.successfulResumeCount",
                        }));
                        0
                    }
                },
                None => {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume requires integer successfulResumeCount",
                        "path": "$.resume.successfulResumeCount",
                    }));
                    0
                }
            };
            json!({
                "signatureFailureRevealsOperation": guard_values
                    .get("signatureFailureRevealsOperation")
                    .copied()
                    .unwrap_or(true),
                "schemaFailureResumesRun": guard_values
                    .get("schemaFailureResumesRun")
                    .copied()
                    .unwrap_or(true),
                "timeoutCallbackResumesExpiredOperation": guard_values
                    .get("timeoutCallbackResumesExpiredOperation")
                    .copied()
                    .unwrap_or(true),
                "cancelledCallbackCommitsResult": guard_values
                    .get("cancelledCallbackCommitsResult")
                    .copied()
                    .unwrap_or(true),
                "staleAttemptCanResume": guard_values
                    .get("staleAttemptCanResume")
                    .copied()
                    .unwrap_or(true),
                "unauthenticatedCallbackCanResume": guard_values
                    .get("unauthenticatedCallbackCanResume")
                    .copied()
                    .unwrap_or(true),
                "nonExternalCallbackEventCanBecomeReceipt": guard_values
                    .get("nonExternalCallbackEventCanBecomeReceipt")
                    .copied()
                    .unwrap_or(true),
                "providerOperationMismatchCanResume": guard_values
                    .get("providerOperationMismatchCanResume")
                    .copied()
                    .unwrap_or(true),
                "receiptJournaledBeforeResume": callback_journal_sequence < resume_sequence,
                "resumeReevaluatesPolicyBudgetRelease": reevaluates.contains("policy")
                    && reevaluates.contains("budget")
                    && reevaluates.contains("release"),
                "budgetExhaustionPausesResume": raw_resume
                    .get("budgetExhaustionState")
                    .or_else(|| raw_resume.get("budget_exhaustion_state"))
                    .and_then(Value::as_str)
                    .is_some_and(|state| state == "paused_budget"),
                "coordinatorFailoverResumesOnce": successful_resume_count == 1,
            })
        }
        "async_callback_cancel_race" => {
            let raw_journal = required_array(case, "journal", name)?;
            let raw_race = required_object(case, "race", name)?;
            let mut cancel_sequence = None;
            let mut callback_sequence = None;
            let mut fences = BTreeSet::new();
            for (entry_index, raw_entry) in raw_journal.iter().enumerate() {
                let Some(entry) = raw_entry.as_object() else {
                    diagnostics.push(json!({
                        "code": "DurableAsyncCancelRaceInvalid",
                        "message": "async cancel race journal entry must be object",
                        "path": format!("$.journal[{entry_index}]"),
                    }));
                    continue;
                };
                let ownership_fence_path = if entry.contains_key("ownershipFence")
                    || !entry.contains_key("ownership_fence")
                {
                    "ownershipFence"
                } else {
                    "ownership_fence"
                };
                match entry
                    .get("ownershipFence")
                    .or_else(|| entry.get("ownership_fence"))
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|fence| !fence.is_empty())
                {
                    Some(fence) => {
                        fences.insert(fence.to_owned());
                    }
                    None => {
                        diagnostics.push(json!({
                            "code": "DurableAsyncCancelRaceInvalid",
                            "message": "async cancel race journal entry requires ownershipFence",
                            "path": format!("$.journal[{entry_index}].{ownership_fence_path}"),
                        }));
                    }
                }
                let sequence = match entry.get("sequence").and_then(Value::as_u64) {
                    Some(sequence) => {
                        if sequence == 0 {
                            diagnostics.push(json!({
                                "code": "DurableAsyncCancelRaceInvalid",
                                "message": "async cancel race journal entry requires positive integer sequence",
                                "path": format!("$.journal[{entry_index}].sequence"),
                            }));
                        }
                        sequence
                    }
                    None => {
                        diagnostics.push(json!({
                            "code": "DurableAsyncCancelRaceInvalid",
                            "message": "async cancel race journal entry requires integer sequence",
                            "path": format!("$.journal[{entry_index}].sequence"),
                        }));
                        0
                    }
                };
                match entry
                    .get("kind")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_ascii_lowercase()
                    .as_str()
                {
                    "cancelrun" | "run_cancelled" | "cancelled" => {
                        cancel_sequence = Some(
                            cancel_sequence
                                .map_or(sequence, |current| std::cmp::min(current, sequence)),
                        );
                    }
                    "externalcallbackreceived" | "external_callback_received" => {
                        callback_sequence = Some(
                            callback_sequence
                                .map_or(sequence, |current| std::cmp::min(current, sequence)),
                        );
                    }
                    _ => {}
                }
            }
            if fences.len() > 1 {
                diagnostics.push(json!({
                    "code": "DurableAsyncCancelRaceInvalid",
                    "message": "async cancel race journal entries require stable ownershipFence",
                    "path": "$.journal",
                }));
            }
            let mut race_boolean_values = BTreeMap::new();
            for (key, alias, default) in [
                (
                    "callbackReceiptRecorded",
                    "callback_receipt_recorded",
                    false,
                ),
                ("resumeAttempted", "resume_attempted", true),
                ("resultCommitted", "result_committed", true),
                ("usageReconciled", "usage_reconciled", false),
            ] {
                let raw_value = raw_race.get(key).or_else(|| raw_race.get(alias));
                race_boolean_values
                    .insert(key, raw_value.and_then(Value::as_bool).unwrap_or(default));
                if raw_value.is_none_or(|value| !value.is_boolean()) {
                    let path_key = if raw_race.contains_key(key) || !raw_race.contains_key(alias) {
                        key
                    } else {
                        alias
                    };
                    diagnostics.push(json!({
                        "code": "DurableAsyncCancelRaceInvalid",
                        "message": format!("async cancel race requires boolean {key}"),
                        "path": format!("$.race.{path_key}"),
                    }));
                }
            }
            json!({
                "journalOrderingDecidesRace": raw_race
                    .get("winner")
                    .and_then(Value::as_str)
                    .is_some_and(|winner| winner == "cancel")
                    && cancel_sequence.is_some_and(|cancel| {
                        callback_sequence.is_some_and(|callback| callback > cancel)
                    }),
                "callbackReceiptRecorded": race_boolean_values
                    .get("callbackReceiptRecorded")
                    .copied()
                    .unwrap_or(false)
                    && callback_sequence.is_some(),
                "cancelWinsBlocksResume": raw_race
                    .get("winner")
                    .and_then(Value::as_str)
                    .is_some_and(|winner| winner == "cancel")
                    && !race_boolean_values
                        .get("resumeAttempted")
                        .copied()
                        .unwrap_or(true),
                "lateCallbackCommitsResult": race_boolean_values
                    .get("resultCommitted")
                    .copied()
                    .unwrap_or(true),
                "lateUsageReconciled": race_boolean_values
                    .get("usageReconciled")
                    .copied()
                    .unwrap_or(false),
                "ownershipFenceStable": fences.len() == 1 && !fences.contains(""),
            })
        }
        "external_operation_reconciliation" => {
            let raw_operation = required_object(case, "operation", name)?;
            let raw_late_callback = case
                .get("lateCallback")
                .or_else(|| case.get("late_callback"))
                .and_then(Value::as_object)
                .ok_or_else(|| format!("{name} requires late callback"))?;
            let raw_usage = required_object(case, "usage", name)?;
            let operation_id_path = if raw_operation.contains_key("operationId")
                || !raw_operation.contains_key("operation_id")
            {
                "operationId"
            } else {
                "operation_id"
            };
            if raw_operation
                .get("operationId")
                .or_else(|| raw_operation.get("operation_id"))
                .and_then(Value::as_str)
                .map_or(true, |operation_id| operation_id.trim().is_empty())
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires nonblank operationId",
                    "path": format!("$.operation.{operation_id_path}"),
                }));
            }
            let provider_operation_id_path = if raw_operation.contains_key("providerOperationId")
                || !raw_operation.contains_key("provider_operation_id")
            {
                "providerOperationId"
            } else {
                "provider_operation_id"
            };
            if raw_operation
                .get("providerOperationId")
                .or_else(|| raw_operation.get("provider_operation_id"))
                .and_then(Value::as_str)
                .map_or(true, |provider_operation_id| {
                    provider_operation_id.trim().is_empty()
                })
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires nonblank providerOperationId",
                    "path": format!("$.operation.{provider_operation_id_path}"),
                }));
            }
            let run_id_path =
                if raw_operation.contains_key("runId") || !raw_operation.contains_key("run_id") {
                    "runId"
                } else {
                    "run_id"
                };
            if raw_operation
                .get("runId")
                .or_else(|| raw_operation.get("run_id"))
                .and_then(Value::as_str)
                .map_or(true, |run_id| run_id.trim().is_empty())
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires nonblank runId",
                    "path": format!("$.operation.{run_id_path}"),
                }));
            }
            let node_id_path =
                if raw_operation.contains_key("nodeId") || !raw_operation.contains_key("node_id") {
                    "nodeId"
                } else {
                    "node_id"
                };
            if raw_operation
                .get("nodeId")
                .or_else(|| raw_operation.get("node_id"))
                .and_then(Value::as_str)
                .map_or(true, |node_id| node_id.trim().is_empty())
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires nonblank nodeId",
                    "path": format!("$.operation.{node_id_path}"),
                }));
            }
            let attempt_id_path = if raw_operation.contains_key("attemptId")
                || !raw_operation.contains_key("attempt_id")
            {
                "attemptId"
            } else {
                "attempt_id"
            };
            if raw_operation
                .get("attemptId")
                .or_else(|| raw_operation.get("attempt_id"))
                .and_then(Value::as_str)
                .map_or(true, |attempt_id| attempt_id.trim().is_empty())
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires nonblank attemptId",
                    "path": format!("$.operation.{attempt_id_path}"),
                }));
            }
            let release_id_path = if raw_operation.contains_key("releaseId")
                || !raw_operation.contains_key("release_id")
            {
                "releaseId"
            } else {
                "release_id"
            };
            if raw_operation
                .get("releaseId")
                .or_else(|| raw_operation.get("release_id"))
                .and_then(Value::as_str)
                .map_or(true, |release_id| release_id.trim().is_empty())
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires nonblank releaseId",
                    "path": format!("$.operation.{release_id_path}"),
                }));
            }
            let tenant_id_path = if raw_operation.contains_key("tenantId")
                || !raw_operation.contains_key("tenant_id")
            {
                "tenantId"
            } else {
                "tenant_id"
            };
            if raw_operation
                .get("tenantId")
                .or_else(|| raw_operation.get("tenant_id"))
                .and_then(Value::as_str)
                .map_or(true, |tenant_id| tenant_id.trim().is_empty())
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires nonblank tenantId",
                    "path": format!("$.operation.{tenant_id_path}"),
                }));
            }
            let operation_policy_snapshot_path = if raw_operation.contains_key("policySnapshotId")
                || !raw_operation.contains_key("policy_snapshot_id")
            {
                "policySnapshotId"
            } else {
                "policy_snapshot_id"
            };
            if raw_operation
                .get("policySnapshotId")
                .or_else(|| raw_operation.get("policy_snapshot_id"))
                .and_then(Value::as_str)
                .map_or(true, |policy_snapshot_id| {
                    policy_snapshot_id.trim().is_empty()
                })
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires nonblank operation policySnapshotId",
                    "path": format!("$.operation.{operation_policy_snapshot_path}"),
                }));
            }
            let callback_id_path = if raw_late_callback.contains_key("callbackId")
                || !raw_late_callback.contains_key("callback_id")
            {
                "callbackId"
            } else {
                "callback_id"
            };
            if raw_late_callback
                .get("callbackId")
                .or_else(|| raw_late_callback.get("callback_id"))
                .and_then(Value::as_str)
                .map_or(true, |callback_id| callback_id.trim().is_empty())
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires nonblank callbackId",
                    "path": format!("$.lateCallback.{callback_id_path}"),
                }));
            }
            let callback_operation_id_path = if raw_late_callback.contains_key("operationId")
                || !raw_late_callback.contains_key("operation_id")
            {
                "operationId"
            } else {
                "operation_id"
            };
            match raw_late_callback
                .get("operationId")
                .or_else(|| raw_late_callback.get("operation_id"))
                .and_then(Value::as_str)
                .map(str::trim)
            {
                Some(callback_operation_id) if !callback_operation_id.is_empty() => {
                    let operation_id = raw_operation
                        .get("operationId")
                        .or_else(|| raw_operation.get("operation_id"))
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .unwrap_or("");
                    if !operation_id.is_empty() && callback_operation_id != operation_id {
                        diagnostics.push(json!({
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback operationId must match operation",
                            "path": format!("$.lateCallback.{callback_operation_id_path}"),
                        }));
                    }
                }
                _ => {
                    diagnostics.push(json!({
                        "code": "DurableExternalOperationInvalid",
                        "message": "external operation reconciliation requires callback operationId",
                        "path": format!("$.lateCallback.{callback_operation_id_path}"),
                    }));
                }
            }
            let callback_provider_operation_id_path = if raw_late_callback
                .contains_key("providerOperationId")
                || !raw_late_callback.contains_key("provider_operation_id")
            {
                "providerOperationId"
            } else {
                "provider_operation_id"
            };
            match raw_late_callback
                .get("providerOperationId")
                .or_else(|| raw_late_callback.get("provider_operation_id"))
                .and_then(Value::as_str)
                .map(str::trim)
            {
                Some(callback_provider_operation_id)
                    if !callback_provider_operation_id.is_empty() =>
                {
                    let provider_operation_id = raw_operation
                        .get("providerOperationId")
                        .or_else(|| raw_operation.get("provider_operation_id"))
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .unwrap_or("");
                    if !provider_operation_id.is_empty()
                        && callback_provider_operation_id != provider_operation_id
                    {
                        diagnostics.push(json!({
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback providerOperationId must match operation",
                            "path": format!("$.lateCallback.{callback_provider_operation_id_path}"),
                        }));
                    }
                }
                _ => {
                    diagnostics.push(json!({
                        "code": "DurableExternalOperationInvalid",
                        "message": "external operation reconciliation requires callback providerOperationId",
                        "path": format!("$.lateCallback.{callback_provider_operation_id_path}"),
                    }));
                }
            }
            let callback_run_id_path = if raw_late_callback.contains_key("runId")
                || !raw_late_callback.contains_key("run_id")
            {
                "runId"
            } else {
                "run_id"
            };
            match raw_late_callback
                .get("runId")
                .or_else(|| raw_late_callback.get("run_id"))
                .and_then(Value::as_str)
                .map(str::trim)
            {
                Some(callback_run_id) if !callback_run_id.is_empty() => {
                    let run_id = raw_operation
                        .get("runId")
                        .or_else(|| raw_operation.get("run_id"))
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .unwrap_or("");
                    if !run_id.is_empty() && callback_run_id != run_id {
                        diagnostics.push(json!({
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback runId must match operation",
                            "path": format!("$.lateCallback.{callback_run_id_path}"),
                        }));
                    }
                }
                _ => {
                    diagnostics.push(json!({
                        "code": "DurableExternalOperationInvalid",
                        "message": "external operation reconciliation requires callback runId",
                        "path": format!("$.lateCallback.{callback_run_id_path}"),
                    }));
                }
            }
            let callback_node_id_path = if raw_late_callback.contains_key("nodeId")
                || !raw_late_callback.contains_key("node_id")
            {
                "nodeId"
            } else {
                "node_id"
            };
            match raw_late_callback
                .get("nodeId")
                .or_else(|| raw_late_callback.get("node_id"))
                .and_then(Value::as_str)
                .map(str::trim)
            {
                Some(callback_node_id) if !callback_node_id.is_empty() => {
                    let node_id = raw_operation
                        .get("nodeId")
                        .or_else(|| raw_operation.get("node_id"))
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .unwrap_or("");
                    if !node_id.is_empty() && callback_node_id != node_id {
                        diagnostics.push(json!({
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback nodeId must match operation",
                            "path": format!("$.lateCallback.{callback_node_id_path}"),
                        }));
                    }
                }
                _ => {
                    diagnostics.push(json!({
                        "code": "DurableExternalOperationInvalid",
                        "message": "external operation reconciliation requires callback nodeId",
                        "path": format!("$.lateCallback.{callback_node_id_path}"),
                    }));
                }
            }
            let callback_attempt_id_path = if raw_late_callback.contains_key("attemptId")
                || !raw_late_callback.contains_key("attempt_id")
            {
                "attemptId"
            } else {
                "attempt_id"
            };
            match raw_late_callback
                .get("attemptId")
                .or_else(|| raw_late_callback.get("attempt_id"))
                .and_then(Value::as_str)
                .map(str::trim)
            {
                Some(callback_attempt_id) if !callback_attempt_id.is_empty() => {
                    let attempt_id = raw_operation
                        .get("attemptId")
                        .or_else(|| raw_operation.get("attempt_id"))
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .unwrap_or("");
                    if !attempt_id.is_empty() && callback_attempt_id != attempt_id {
                        diagnostics.push(json!({
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback attemptId must match operation",
                            "path": format!("$.lateCallback.{callback_attempt_id_path}"),
                        }));
                    }
                }
                _ => {
                    diagnostics.push(json!({
                        "code": "DurableExternalOperationInvalid",
                        "message": "external operation reconciliation requires callback attemptId",
                        "path": format!("$.lateCallback.{callback_attempt_id_path}"),
                    }));
                }
            }
            let callback_release_id_path = if raw_late_callback.contains_key("releaseId")
                || !raw_late_callback.contains_key("release_id")
            {
                "releaseId"
            } else {
                "release_id"
            };
            match raw_late_callback
                .get("releaseId")
                .or_else(|| raw_late_callback.get("release_id"))
                .and_then(Value::as_str)
                .map(str::trim)
            {
                Some(callback_release_id) if !callback_release_id.is_empty() => {
                    let release_id = raw_operation
                        .get("releaseId")
                        .or_else(|| raw_operation.get("release_id"))
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .unwrap_or("");
                    if !release_id.is_empty() && callback_release_id != release_id {
                        diagnostics.push(json!({
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback releaseId must match operation",
                            "path": format!("$.lateCallback.{callback_release_id_path}"),
                        }));
                    }
                }
                _ => {
                    diagnostics.push(json!({
                        "code": "DurableExternalOperationInvalid",
                        "message": "external operation reconciliation requires callback releaseId",
                        "path": format!("$.lateCallback.{callback_release_id_path}"),
                    }));
                }
            }
            let callback_tenant_id_path = if raw_late_callback.contains_key("tenantId")
                || !raw_late_callback.contains_key("tenant_id")
            {
                "tenantId"
            } else {
                "tenant_id"
            };
            match raw_late_callback
                .get("tenantId")
                .or_else(|| raw_late_callback.get("tenant_id"))
                .and_then(Value::as_str)
                .map(str::trim)
            {
                Some(callback_tenant_id) if !callback_tenant_id.is_empty() => {
                    let tenant_id = raw_operation
                        .get("tenantId")
                        .or_else(|| raw_operation.get("tenant_id"))
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .unwrap_or("");
                    if !tenant_id.is_empty() && callback_tenant_id != tenant_id {
                        diagnostics.push(json!({
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback tenantId must match operation",
                            "path": format!("$.lateCallback.{callback_tenant_id_path}"),
                        }));
                    }
                }
                _ => {
                    diagnostics.push(json!({
                        "code": "DurableExternalOperationInvalid",
                        "message": "external operation reconciliation requires callback tenantId",
                        "path": format!("$.lateCallback.{callback_tenant_id_path}"),
                    }));
                }
            }
            let payload_digest_path = if raw_late_callback.contains_key("payloadDigest")
                || !raw_late_callback.contains_key("payload_digest")
            {
                "payloadDigest"
            } else {
                "payload_digest"
            };
            if !raw_late_callback
                .get("payloadDigest")
                .or_else(|| raw_late_callback.get("payload_digest"))
                .and_then(Value::as_str)
                .is_some_and(|payload_digest| {
                    let Some(hex) = payload_digest.strip_prefix("sha256:") else {
                        return false;
                    };
                    hex.len() == 64
                        && hex
                            .bytes()
                            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
                })
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires payloadDigest sha256 digest",
                    "path": format!("$.lateCallback.{payload_digest_path}"),
                }));
            }
            if !raw_late_callback
                .get("status")
                .and_then(Value::as_str)
                .is_some_and(|status| {
                    matches!(
                        status,
                        "completed" | "failed" | "cancelled" | "expired" | "incomplete"
                    )
                })
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires terminal callback status",
                    "path": "$.lateCallback.status",
                }));
            }
            let verified_by_path = if raw_late_callback.contains_key("verifiedBy")
                || !raw_late_callback.contains_key("verified_by")
            {
                "verifiedBy"
            } else {
                "verified_by"
            };
            if raw_late_callback
                .get("verifiedBy")
                .or_else(|| raw_late_callback.get("verified_by"))
                .and_then(Value::as_str)
                .map_or(true, |verified_by| verified_by.trim().is_empty())
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires nonblank verifiedBy",
                    "path": format!("$.lateCallback.{verified_by_path}"),
                }));
            } else if raw_late_callback
                .get("verifiedBy")
                .or_else(|| raw_late_callback.get("verified_by"))
                .and_then(Value::as_str)
                .is_some_and(|verified_by| {
                    verified_by.trim().eq_ignore_ascii_case("unauthenticated")
                })
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires authenticated verifiedBy",
                    "path": format!("$.lateCallback.{verified_by_path}"),
                }));
            }
            let idempotency_key_path = if raw_late_callback.contains_key("idempotencyKey")
                || !raw_late_callback.contains_key("idempotency_key")
            {
                "idempotencyKey"
            } else {
                "idempotency_key"
            };
            if raw_late_callback
                .get("idempotencyKey")
                .or_else(|| raw_late_callback.get("idempotency_key"))
                .and_then(Value::as_str)
                .map_or(true, |idempotency_key| idempotency_key.trim().is_empty())
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires nonblank idempotencyKey",
                    "path": format!("$.lateCallback.{idempotency_key_path}"),
                }));
            }
            let policy_snapshot_path = if raw_late_callback.contains_key("policySnapshotId")
                || !raw_late_callback.contains_key("policy_snapshot_id")
            {
                "policySnapshotId"
            } else {
                "policy_snapshot_id"
            };
            match raw_late_callback
                .get("policySnapshotId")
                .or_else(|| raw_late_callback.get("policy_snapshot_id"))
                .and_then(Value::as_str)
                .map(str::trim)
            {
                Some(policy_snapshot_id) if !policy_snapshot_id.is_empty() => {
                    let operation_policy_snapshot_id = raw_operation
                        .get("policySnapshotId")
                        .or_else(|| raw_operation.get("policy_snapshot_id"))
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .unwrap_or("");
                    if !operation_policy_snapshot_id.is_empty()
                        && policy_snapshot_id != operation_policy_snapshot_id
                    {
                        diagnostics.push(json!({
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback policySnapshotId must match operation",
                            "path": format!("$.lateCallback.{policy_snapshot_path}"),
                        }));
                    }
                }
                _ => {
                    diagnostics.push(json!({
                        "code": "DurableExternalOperationInvalid",
                        "message": "external operation reconciliation requires nonblank policySnapshotId",
                        "path": format!("$.lateCallback.{policy_snapshot_path}"),
                    }));
                }
            }
            let received_at_path = if raw_late_callback.contains_key("receivedAt")
                || !raw_late_callback.contains_key("received_at")
            {
                "receivedAt"
            } else {
                "received_at"
            };
            let received_at_is_iso = raw_late_callback
                .get("receivedAt")
                .or_else(|| raw_late_callback.get("received_at"))
                .and_then(Value::as_str)
                .is_some_and(|received_at| {
                    let received_at = received_at.trim();
                    let bytes = received_at.as_bytes();
                    let digit_positions = [0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18];
                    bytes.len() >= 20
                        && digit_positions
                            .into_iter()
                            .all(|position| bytes.get(position).is_some_and(u8::is_ascii_digit))
                        && bytes.get(4) == Some(&b'-')
                        && bytes.get(7) == Some(&b'-')
                        && bytes.get(10) == Some(&b'T')
                        && bytes.get(13) == Some(&b':')
                        && bytes.get(16) == Some(&b':')
                        && (received_at.ends_with('Z')
                            || received_at
                                .get(19..)
                                .is_some_and(|suffix| suffix.contains('+') || suffix.contains('-')))
                });
            if !received_at_is_iso {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires ISO receivedAt",
                    "path": format!("$.lateCallback.{received_at_path}"),
                }));
            }
            let effect_state_path = if raw_operation.contains_key("effectState")
                || !raw_operation.contains_key("effect_state")
            {
                "effectState"
            } else {
                "effect_state"
            };
            if !raw_operation
                .get("effectState")
                .or_else(|| raw_operation.get("effect_state"))
                .and_then(Value::as_str)
                .is_some_and(|state| state == "committed")
            {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires committed effectState",
                    "path": format!("$.operation.{effect_state_path}"),
                }));
            }
            let effect_journaled_path = if raw_operation.contains_key("effectJournaled")
                || !raw_operation.contains_key("effect_journaled")
            {
                "effectJournaled"
            } else {
                "effect_journaled"
            };
            let raw_effect_journaled = raw_operation
                .get("effectJournaled")
                .or_else(|| raw_operation.get("effect_journaled"));
            let effect_journaled = raw_effect_journaled
                .and_then(Value::as_bool)
                .unwrap_or(false);
            if raw_effect_journaled.is_none_or(|value| !value.is_boolean()) {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires boolean effectJournaled",
                    "path": format!("$.operation.{effect_journaled_path}"),
                }));
            } else if !effect_journaled {
                diagnostics.push(json!({
                    "code": "DurableExternalOperationInvalid",
                    "message": "external operation reconciliation requires committed effect journal record",
                    "path": format!("$.operation.{effect_journaled_path}"),
                }));
            }
            let mut reconciliation_values = BTreeMap::new();
            for (source_name, source, key, alias, default) in [
                (
                    "lateCallback",
                    raw_late_callback,
                    "commitsResult",
                    "commits_result",
                    true,
                ),
                (
                    "lateCallback",
                    raw_late_callback,
                    "diagnosticRecorded",
                    "diagnostic_recorded",
                    false,
                ),
                (
                    "lateCallback",
                    raw_late_callback,
                    "payloadConvertedToArtifactRef",
                    "payload_converted_to_artifact_ref",
                    false,
                ),
                ("usage", raw_usage, "reconciled", "reconciled", false),
            ] {
                let raw_value = source.get(key).or_else(|| source.get(alias));
                reconciliation_values.insert(
                    (source_name, key),
                    raw_value.and_then(Value::as_bool).unwrap_or(default),
                );
                if raw_value.is_none_or(|value| !value.is_boolean()) {
                    let path_key = if source.contains_key(key) || !source.contains_key(alias) {
                        key
                    } else {
                        alias
                    };
                    diagnostics.push(json!({
                        "code": "DurableExternalOperationInvalid",
                        "message": format!("external operation reconciliation requires boolean {key}"),
                        "path": format!("$.{source_name}.{path_key}"),
                    }));
                }
            }
            if reconciliation_values
                .get(&("usage", "reconciled"))
                .copied()
                .unwrap_or(false)
            {
                match raw_usage
                    .get("providerUsageRecords")
                    .or_else(|| raw_usage.get("provider_usage_records"))
                {
                    Some(Value::Array(records)) if !records.is_empty() => {
                        for (index, record) in records.iter().enumerate() {
                            if let Some(record) = record.as_object() {
                                if record
                                    .get("metric")
                                    .and_then(Value::as_str)
                                    .map_or(true, |metric| metric.trim().is_empty())
                                {
                                    diagnostics.push(json!({
                                        "code": "DurableExternalOperationInvalid",
                                        "message": "external operation reconciliation usage record requires string metric",
                                        "path": format!("$.usage.providerUsageRecords[{index}].metric"),
                                    }));
                                }
                                match record.get("amount") {
                                    Some(value) if value.as_f64().is_none() => {
                                        diagnostics.push(json!({
                                            "code": "DurableExternalOperationInvalid",
                                            "message": "external operation reconciliation usage record requires numeric amount",
                                            "path": format!("$.usage.providerUsageRecords[{index}].amount"),
                                        }));
                                    }
                                    Some(value) => match value.as_i64() {
                                        Some(amount) if amount >= 0 => {}
                                        Some(_) => {
                                            diagnostics.push(json!({
                                                "code": "DurableExternalOperationInvalid",
                                                "message": "external operation reconciliation usage record amount must be non-negative",
                                                "path": format!("$.usage.providerUsageRecords[{index}].amount"),
                                            }));
                                        }
                                        None => {
                                            diagnostics.push(json!({
                                                "code": "DurableExternalOperationInvalid",
                                                "message": "external operation reconciliation usage record requires integer amount",
                                                "path": format!("$.usage.providerUsageRecords[{index}].amount"),
                                            }));
                                        }
                                    },
                                    None => {
                                        diagnostics.push(json!({
                                            "code": "DurableExternalOperationInvalid",
                                            "message": "external operation reconciliation usage record requires numeric amount",
                                            "path": format!("$.usage.providerUsageRecords[{index}].amount"),
                                        }));
                                    }
                                }
                            } else {
                                diagnostics.push(json!({
                                    "code": "DurableExternalOperationInvalid",
                                    "message": "external operation reconciliation usage record must be object",
                                    "path": format!("$.usage.providerUsageRecords[{index}]"),
                                }));
                            }
                        }
                    }
                    _ => {
                        diagnostics.push(json!({
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires providerUsageRecords when reconciled",
                            "path": "$.usage.providerUsageRecords",
                        }));
                    }
                }
            }
            json!({
                "sideEffectCommitPreserved": raw_operation
                    .get("effectState")
                    .or_else(|| raw_operation.get("effect_state"))
                    .and_then(Value::as_str)
                    .is_some_and(|state| state == "committed")
                    && effect_journaled,
                "lateCallbackCommitsResult": reconciliation_values
                    .get(&("lateCallback", "commitsResult"))
                    .copied()
                    .unwrap_or(true),
                "lateCallbackRecordedDiagnostic": reconciliation_values
                    .get(&("lateCallback", "diagnosticRecorded"))
                    .copied()
                    .unwrap_or(false),
                "lateUsageReconciled": reconciliation_values
                    .get(&("usage", "reconciled"))
                    .copied()
                    .unwrap_or(false),
                "largePayloadUsesArtifactRef": reconciliation_values
                    .get(&("lateCallback", "payloadConvertedToArtifactRef"))
                    .copied()
                    .unwrap_or(false),
            })
        }
        other => return Err(format!("durable TCK case {name} has unknown kind {other}")),
    };

    if let Some(expected_diagnostics) = expected_diagnostics {
        let diagnostics_match = diagnostics.as_slice() == expected_diagnostics.as_slice();
        observed
            .as_object_mut()
            .ok_or_else(|| format!("{name} observed durable TCK value must be object"))?
            .insert(
                "expectedDiagnosticsMatched".to_owned(),
                Value::Bool(diagnostics_match),
            );
    }

    for (key, expected_value) in expected {
        assert_eq!(
            observed.get(key).unwrap_or(&Value::Null),
            expected_value,
            "{name} expected field {key}"
        );
    }

    Ok(())
}

fn required_object<'a>(
    value: &'a Value,
    key: &str,
    owner: &str,
) -> Result<&'a Map<String, Value>, String> {
    value
        .get(key)
        .and_then(Value::as_object)
        .ok_or_else(|| format!("{owner} is missing object field {key}"))
}

fn required_object_map<'a>(
    mapping: &'a Map<String, Value>,
    key: &str,
    owner: &str,
) -> Result<&'a Map<String, Value>, String> {
    mapping
        .get(key)
        .and_then(Value::as_object)
        .ok_or_else(|| format!("{owner} is missing object field {key}"))
}

fn required_array<'a>(value: &'a Value, key: &str, owner: &str) -> Result<&'a Vec<Value>, String> {
    value
        .get(key)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("{owner} is missing array field {key}"))
}

fn required_str<'a>(value: &'a Value, key: &str, owner: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{owner} is missing string field {key}"))
}

fn required_str_map<'a>(
    mapping: &'a Map<String, Value>,
    key: &str,
    owner: &str,
) -> Result<&'a str, String> {
    mapping
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{owner} is missing string field {key}"))
}

fn required_u64_map(mapping: &Map<String, Value>, key: &str, owner: &str) -> Result<u64, String> {
    mapping
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("{owner} is missing integer field {key}"))
}

fn guarantee_from(value: &str) -> Result<DeliveryGuarantee, String> {
    match value {
        "best_effort" => Ok(DeliveryGuarantee::BestEffort),
        "at_most_once" => Ok(DeliveryGuarantee::AtMostOnce),
        "at_least_once" => Ok(DeliveryGuarantee::AtLeastOnce),
        other => Err(format!("unsupported delivery guarantee {other:?}")),
    }
}

fn accumulation_from(value: &str) -> Result<AccumulationMode, String> {
    match value {
        "discarding" => Ok(AccumulationMode::Discarding),
        "accumulating" => Ok(AccumulationMode::Accumulating),
        other => Err(format!("unsupported accumulation mode {other:?}")),
    }
}

fn terminal_state_from(value: &str) -> Result<DurableToolTerminalState, String> {
    match value {
        "completed" => Ok(DurableToolTerminalState::Completed),
        "failed" => Ok(DurableToolTerminalState::Failed),
        "denied" => Ok(DurableToolTerminalState::Denied),
        "cancelled" => Ok(DurableToolTerminalState::Cancelled),
        "policy_stopped" => Ok(DurableToolTerminalState::PolicyStopped),
        "incomplete" => Ok(DurableToolTerminalState::Incomplete),
        "expired" => Ok(DurableToolTerminalState::Expired),
        other => Err(format!("unsupported terminal state {other:?}")),
    }
}

fn terminal_state_name(value: &DurableToolTerminalState) -> &'static str {
    match value {
        DurableToolTerminalState::Completed => "completed",
        DurableToolTerminalState::Failed => "failed",
        DurableToolTerminalState::Denied => "denied",
        DurableToolTerminalState::Cancelled => "cancelled",
        DurableToolTerminalState::PolicyStopped => "policy_stopped",
        DurableToolTerminalState::Incomplete => "incomplete",
        DurableToolTerminalState::Expired => "expired",
    }
}

fn cursor_from(mapping: &Map<String, Value>) -> Result<SourceCursor, String> {
    Ok(SourceCursor::new(
        required_str_map(mapping, "stream", "cursor")?,
        required_u64_map(mapping, "partition", "cursor")? as u32,
        required_u64_map(mapping, "offset", "cursor")?,
    ))
}

fn event_from(mapping: &Map<String, Value>) -> Result<SourceEvent, String> {
    Ok(SourceEvent::new(
        cursor_from(mapping)?,
        mapping.get("payload").cloned().unwrap_or(Value::Null),
        mapping.get("eventTimeUnixMs").and_then(Value::as_u64),
    ))
}

fn event_list(value: &Value, key: &str, owner: &str) -> Result<Vec<SourceEvent>, String> {
    required_array(value, key, owner)?
        .iter()
        .map(|item| {
            let mapping = item
                .as_object()
                .ok_or_else(|| format!("{owner} event must be an object"))?;
            event_from(mapping)
        })
        .collect()
}

fn offsets(events: &[SourceEvent]) -> Vec<u64> {
    events.iter().map(|event| event.cursor.offset).collect()
}

fn cursor_contract(cursor: &SourceCursor) -> Value {
    json!({
        "stream": cursor.stream,
        "partition": cursor.partition,
        "offset": cursor.offset,
    })
}

fn sink_request_from(mapping: &Map<String, Value>) -> Result<SinkCommitRequest, String> {
    let mut request = SinkCommitRequest::new(
        required_str_map(mapping, "runId", "sink request")?,
        required_str_map(mapping, "nodeId", "sink request")?,
        required_str_map(mapping, "nodeAttemptId", "sink request")?,
        required_str_map(mapping, "idempotencyKey", "sink request")?,
        mapping.get("payload").cloned().unwrap_or(Value::Null),
    );
    if let Some(precondition_digest) = mapping.get("preconditionDigest").and_then(Value::as_str) {
        request = request.with_precondition_digest(precondition_digest);
    }
    Ok(request)
}

fn barrier_from(mapping: &Map<String, Value>) -> Result<CheckpointBarrier, String> {
    let raw_schema = required_object_map(mapping, "checkpointSchema", "checkpoint barrier")?;
    let source_cursors = mapping
        .get("sourceCursors")
        .and_then(Value::as_object)
        .map(|cursors| {
            cursors
                .iter()
                .map(|(source_id, raw_cursor)| {
                    Ok((
                        source_id.clone(),
                        cursor_from(raw_cursor.as_object().ok_or_else(|| {
                            "checkpoint source cursor must be an object".to_owned()
                        })?)?,
                    ))
                })
                .collect::<Result<BTreeMap<_, _>, String>>()
        })
        .transpose()?
        .unwrap_or_default();
    let completed_nodes = mapping
        .get("completedNodes")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .map(|item| {
                    item.as_str()
                        .map(str::to_owned)
                        .ok_or_else(|| "completedNodes item must be a string".to_owned())
                })
                .collect::<Result<Vec<_>, String>>()
        })
        .transpose()?
        .unwrap_or_default();
    let pending_nodes = mapping
        .get("pendingNodes")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .map(|item| {
                    item.as_str()
                        .map(str::to_owned)
                        .ok_or_else(|| "pendingNodes item must be a string".to_owned())
                })
                .collect::<Result<Vec<_>, String>>()
        })
        .transpose()?
        .unwrap_or_default();
    let schema_versions = mapping
        .get("schemaVersions")
        .and_then(Value::as_object)
        .map(|versions| {
            versions
                .iter()
                .map(|(key, value)| {
                    value
                        .as_u64()
                        .map(|version| (key.clone(), version as u32))
                        .ok_or_else(|| "schemaVersions value must be an integer".to_owned())
                })
                .collect::<Result<BTreeMap<_, _>, String>>()
        })
        .transpose()?
        .unwrap_or_default();
    let operator_state = mapping
        .get("operatorState")
        .and_then(Value::as_object)
        .map(|state| {
            state
                .iter()
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect::<BTreeMap<_, _>>()
        })
        .unwrap_or_default();
    let sink_commit_metadata = mapping
        .get("sinkCommitMetadata")
        .and_then(Value::as_object)
        .map(|metadata| {
            metadata
                .iter()
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect::<BTreeMap<_, _>>()
        })
        .unwrap_or_default();

    Ok(CheckpointBarrier {
        checkpoint_id: required_str_map(mapping, "checkpointId", "checkpoint barrier")?.to_owned(),
        run_id: required_str_map(mapping, "runId", "checkpoint barrier")?.to_owned(),
        release_id: required_str_map(mapping, "releaseId", "checkpoint barrier")?.to_owned(),
        deployment_revision_id: required_str_map(
            mapping,
            "deploymentRevisionId",
            "checkpoint barrier",
        )?
        .to_owned(),
        plan_hash: required_str_map(mapping, "planHash", "checkpoint barrier")?.to_owned(),
        checkpoint_schema: SchemaRef::new(
            required_str_map(raw_schema, "schemaId", "checkpoint schema")?,
            required_u64_map(raw_schema, "schemaVersion", "checkpoint schema")? as u32,
        ),
        state_revision: required_u64_map(mapping, "stateRevision", "checkpoint barrier")?,
        completed_nodes,
        pending_nodes,
        source_cursors,
        operator_state,
        sink_commit_metadata,
        schema_versions,
        created_at_unix_ms: mapping
            .get("createdAtUnixMs")
            .and_then(Value::as_u64)
            .unwrap_or_default(),
    })
}

fn checkpoint_error_name(error: &CheckpointBarrierError) -> &'static str {
    match error {
        CheckpointBarrierError::MissingCheckpointId => "missing_checkpoint_id",
        CheckpointBarrierError::MissingRunId => "missing_run_id",
        CheckpointBarrierError::MissingReleaseId => "missing_release_id",
        CheckpointBarrierError::MissingDeploymentRevisionId => "missing_deployment_revision_id",
        CheckpointBarrierError::MissingPlanHash => "missing_plan_hash",
        CheckpointBarrierError::InvalidCheckpointSchema => "invalid_checkpoint_schema",
        CheckpointBarrierError::MissingSchemaVersions => "missing_schema_versions",
    }
}

fn tool_terminal_from(mapping: &Map<String, Value>) -> Result<DurableToolTerminalRecord, String> {
    let mut record = DurableToolTerminalRecord::new(
        required_str_map(mapping, "runId", "tool terminal")?,
        required_str_map(mapping, "responseId", "tool terminal")?,
        required_str_map(mapping, "toolCallId", "tool terminal")?,
        required_u64_map(mapping, "revision", "tool terminal")? as u32,
        terminal_state_from(required_str_map(mapping, "terminalState", "tool terminal")?)?,
        required_str_map(mapping, "argumentsDigest", "tool terminal")?,
        required_u64_map(mapping, "completedAtUnixMs", "tool terminal")?,
    );
    if let Some(output_digest) = mapping.get("outputDigest").and_then(Value::as_str) {
        record = record.with_output_digest(output_digest);
    }
    if let Some(idempotency_key) = mapping.get("idempotencyKey").and_then(Value::as_str) {
        record = record.with_idempotency_key(idempotency_key);
    }
    if mapping
        .get("effectCommitted")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        record = record.with_effect_committed();
    }
    if mapping
        .get("durableResultCommitted")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        record = record.with_durable_result_committed();
    }
    Ok(record)
}

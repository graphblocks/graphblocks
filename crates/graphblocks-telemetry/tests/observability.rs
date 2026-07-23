use graphblocks_telemetry::{
    GenerationObservation, GenerationTelemetryRecord, MetricCardinalityLinter, MetricLabelError,
    MetricLabelSet, MetricSample, OutputPolicyTelemetryRecord, SpanTiming, TelemetryBuffer,
    TelemetryBufferError, TelemetryCapturePolicy, TelemetryCapturePolicyLinter,
    TelemetryEnqueueOutcome, TelemetryExportResult, TelemetryOnFull, TelemetryPriority,
    TelemetryProjectionError, TelemetryQueuePolicy, TelemetryRecord, TelemetryRecordKind,
    ToolExecutionTelemetryRecord, telemetry_diagnostic_bundle,
};
use serde_json::json;

#[test]
fn telemetry_crate_reexports_generation_observation_contracts() {
    let observation = GenerationObservation::new("span-1", "generate", "openai", "gpt-test")
        .with_timing(SpanTiming::new(1_000).with_started_at(1_020))
        .record_chunk(1, 12, 1_040)
        .record_usage("model_output_tokens", 4)
        .finish("stop", 1_080);

    assert_eq!(observation.chunk_count, 1);
    assert_eq!(observation.first_chunk_sequence, Some(1));
    assert_eq!(observation.output_bytes, 12);
    assert_eq!(observation.timing.time_to_first_output_ms(), Some(20));
    assert_eq!(
        observation.usage.get("model_output_tokens").copied(),
        Some(4)
    );
    assert_eq!(observation.finish_reason.as_deref(), Some("stop"));
}

#[test]
fn telemetry_crate_reexports_metric_cardinality_and_buffer_contracts() {
    let labels = MetricLabelSet::new()
        .with_label("environment", "prod")
        .with_label("run_id", "run-1");
    assert_eq!(
        labels.validate_cardinality_budget(),
        Err(MetricLabelError::ForbiddenLabels {
            labels: vec!["run_id".to_owned()],
        })
    );

    let mut buffer = TelemetryBuffer::new(TelemetryQueuePolicy::new(
        1,
        TelemetryOnFull::DropLowPriority,
    ));
    buffer
        .enqueue(TelemetryRecord::new(
            "debug-1",
            TelemetryRecordKind::DebugSpan,
            TelemetryPriority::Low,
        ))
        .expect("initial telemetry record should fit");

    assert_eq!(
        buffer.enqueue(TelemetryRecord::new(
            "span-1",
            TelemetryRecordKind::Span,
            TelemetryPriority::High,
        )),
        Ok(TelemetryEnqueueOutcome::accepted_with_drop(["debug-1"])),
    );
    assert_eq!(
        buffer.enqueue(TelemetryRecord::new(
            "audit-1",
            TelemetryRecordKind::RequiredAudit,
            TelemetryPriority::High,
        )),
        Err(TelemetryBufferError::RequiredDurablePath {
            kind: TelemetryRecordKind::RequiredAudit,
        })
    );
}

#[test]
fn generation_telemetry_record_projects_runtime_observation_contract() {
    let observation = GenerationObservation::new("span-1", "generate", "openai", "gpt-test")
        .with_timing(
            SpanTiming::new(1_000)
                .with_admitted_at(1_050)
                .with_started_at(1_080),
        )
        .record_chunk(1, 12, 1_100)
        .record_usage("input_tokens", 20)
        .record_usage("output_tokens", 8)
        .finish("stop", 1_200);

    let record =
        GenerationTelemetryRecord::from_generation_observation("gen-1", "run-1", &observation)
            .with_release_id("release-1")
            .with_input_digest("sha256:input")
            .with_output_digest("sha256:output")
            .with_attribute("tenant", json!("tenant-1"));

    assert_eq!(
        record.observation_contract(),
        json!({
            "record_id": "gen-1",
            "run_id": "run-1",
            "span_id": "span-1",
            "node_id": "generate",
            "provider": "openai",
            "model": "gpt-test",
            "release_id": "release-1",
            "input_digest": "sha256:input",
            "output_digest": "sha256:output",
            "usage": {
                "input_tokens": 20,
                "output_tokens": 8,
            },
            "timing_ms": {
                "execution": 120,
                "flow_wait": 30,
                "queue_wait": 50,
                "streaming": 100,
                "time_to_first_output": 20,
            },
            "attributes": {
                "tenant": "tenant-1",
            },
        })
    );
}

#[test]
fn policy_and_tool_telemetry_records_apply_capture_policy() {
    let output_record = OutputPolicyTelemetryRecord::new(
        "policy-1",
        "run-1",
        "stream-1",
        "response-1",
        "before_client_delivery",
        "abort_response",
    )
    .with_release_id("release-1")
    .with_policy_snapshot_id("policy-snapshot-1")
    .with_terminal_reason("policy_denied")
    .with_draft_disposition("retract")
    .with_pending_tool_calls("deny")
    .with_durable_result("none")
    .with_accepted_through_sequence(7)
    .with_last_client_delivered_sequence(5)
    .with_attribute("tenant", json!("tenant-1"))
    .with_attribute("prompt", json!("secret prompt"))
    .with_attribute("debug", json!("drop me"));
    let tool_record = ToolExecutionTelemetryRecord::new(
        "tool-1",
        "run-1",
        "call-1",
        "ticket.create",
        "completed",
    )
    .with_release_id("release-1")
    .with_result_mode("value")
    .with_effect_outcome("committed")
    .with_effect("network")
    .with_effect("external_write")
    .with_duration_ms(128)
    .with_attribute("tenant", json!("tenant-1"))
    .with_attribute("tool_result", json!("secret result"))
    .with_attribute("debug", json!("drop me"));
    let policy = TelemetryCapturePolicy::new()
        .with_redacted_attribute_key("prompt")
        .with_redacted_attribute_key("tool_result")
        .with_dropped_attribute_key("debug");

    let redacted_output = policy.apply_output_policy(&output_record);
    let redacted_tool = policy.apply_tool_execution(&tool_record);

    assert_eq!(
        output_record.observation_contract(),
        json!({
            "record_id": "policy-1",
            "run_id": "run-1",
            "stream_id": "stream-1",
            "response_id": "response-1",
            "enforcement_point": "before_client_delivery",
            "disposition": "abort_response",
            "release_id": "release-1",
            "policy_snapshot_id": "policy-snapshot-1",
            "terminal_reason": "policy_denied",
            "draft_disposition": "retract",
            "pending_tool_calls": "deny",
            "durable_result": "none",
            "accepted_through_sequence": 7,
            "last_client_delivered_sequence": 5,
            "attributes": {
                "debug": "drop me",
                "prompt": "secret prompt",
                "tenant": "tenant-1",
            },
        })
    );
    assert_eq!(
        tool_record.observation_contract(),
        json!({
            "record_id": "tool-1",
            "run_id": "run-1",
            "tool_call_id": "call-1",
            "tool_name": "ticket.create",
            "status": "completed",
            "release_id": "release-1",
            "result_mode": "value",
            "effect_outcome": "committed",
            "effects": ["external_write", "network"],
            "duration_ms": 128,
            "attributes": {
                "debug": "drop me",
                "tenant": "tenant-1",
                "tool_result": "secret result",
            },
        })
    );
    assert_eq!(
        redacted_output.observation_contract()["attributes"],
        json!({
            "prompt": "[redacted]",
            "tenant": "tenant-1",
        })
    );
    assert_eq!(
        redacted_tool.observation_contract()["attributes"],
        json!({
            "tenant": "tenant-1",
            "tool_result": "[redacted]",
        })
    );
    assert_eq!(
        output_record.attributes.get("prompt"),
        Some(&json!("secret prompt"))
    );
}

#[test]
fn telemetry_records_validate_runtime_literal_contracts() {
    let generation_record =
        GenerationTelemetryRecord::new("gen-1", "run-1", "span-1", "agent", "openai", "gpt-test")
            .with_release_id("release-1")
            .with_input_digest("sha256:input")
            .with_output_digest("sha256:output");
    let output_record = OutputPolicyTelemetryRecord::new(
        "policy-1",
        "run-1",
        "stream-1",
        "response-1",
        "before_client_delivery",
        "abort_response",
    )
    .with_terminal_reason("policy_denied")
    .with_draft_disposition("retract")
    .with_pending_tool_calls("deny")
    .with_durable_result("none");
    let tool_record =
        ToolExecutionTelemetryRecord::new("tool-1", "run-1", "call-1", "ticket.create", "running")
            .with_result_mode("incremental")
            .with_effect_outcome("committed")
            .with_effect("external_write")
            .with_effect("network");

    assert_eq!(generation_record.validate(), Ok(()));
    assert_eq!(output_record.validate(), Ok(()));
    assert_eq!(tool_record.validate(), Ok(()));

    assert_eq!(
        GenerationTelemetryRecord::new("", "run-1", "span-1", "agent", "openai", "gpt-test")
            .validate(),
        Err(TelemetryProjectionError::EmptyField { field: "record_id" })
    );
    assert_eq!(
        OutputPolicyTelemetryRecord::new(
            "policy-1",
            "run-1",
            "stream-1",
            "response-1",
            "after_delivery",
            "abort_response",
        )
        .validate(),
        Err(TelemetryProjectionError::InvalidLiteral {
            field: "enforcement_point",
            value: "after_delivery".to_owned(),
        })
    );
    assert_eq!(
        OutputPolicyTelemetryRecord::new(
            "policy-1",
            "run-1",
            "stream-1",
            "response-1",
            "before_client_delivery",
            "block",
        )
        .validate(),
        Err(TelemetryProjectionError::InvalidLiteral {
            field: "disposition",
            value: "block".to_owned(),
        })
    );
    assert_eq!(
        OutputPolicyTelemetryRecord::new(
            "policy-1",
            "run-1",
            "stream-1",
            "response-1",
            "before_client_delivery",
            "abort_response",
        )
        .with_pending_tool_calls("drop")
        .validate(),
        Err(TelemetryProjectionError::InvalidLiteral {
            field: "pending_tool_calls",
            value: "drop".to_owned(),
        })
    );
    assert_eq!(
        ToolExecutionTelemetryRecord::new("tool-1", "run-1", "call-1", "ticket.create", "waiting",)
            .validate(),
        Err(TelemetryProjectionError::InvalidLiteral {
            field: "status",
            value: "waiting".to_owned(),
        })
    );
    assert_eq!(
        ToolExecutionTelemetryRecord::new(
            "tool-1",
            "run-1",
            "call-1",
            "ticket.create",
            "completed",
        )
        .with_result_mode("stream")
        .validate(),
        Err(TelemetryProjectionError::InvalidLiteral {
            field: "result_mode",
            value: "stream".to_owned(),
        })
    );
    assert_eq!(
        ToolExecutionTelemetryRecord::new(
            "tool-1",
            "run-1",
            "call-1",
            "ticket.create",
            "completed",
        )
        .with_effect("telepathy")
        .validate(),
        Err(TelemetryProjectionError::InvalidLiteral {
            field: "effect",
            value: "telepathy".to_owned(),
        })
    );
}

#[test]
fn telemetry_records_reject_noncanonical_keys_and_sequence_boundaries() {
    assert_eq!(
        GenerationTelemetryRecord::new(" gen-1", "run-1", "span-1", "agent", "openai", "gpt-test",)
            .validate(),
        Err(TelemetryProjectionError::NonCanonicalField { field: "record_id" })
    );
    assert_eq!(
        GenerationTelemetryRecord::new("gen-1", "run-1", "span-1", "agent", "openai", "gpt-test",)
            .with_usage(" ", 1)
            .validate(),
        Err(TelemetryProjectionError::EmptyField {
            field: "usage_unit"
        })
    );
    assert_eq!(
        GenerationTelemetryRecord::new(
            "run\0id",
            "run-1",
            "span-1",
            "agent",
            "openai",
            "gpt-test",
        )
        .validate(),
        Err(TelemetryProjectionError::ControlCharacter { field: "record_id" })
    );
    assert_eq!(
        GenerationTelemetryRecord::new("gen-1", "run-1", "span-1", "agent", "openai", "gpt-test",)
            .with_attribute("trace\u{7f}id", json!("trace-1"))
            .validate(),
        Err(TelemetryProjectionError::ControlCharacter {
            field: "attribute_key"
        })
    );
    assert_eq!(
        OutputPolicyTelemetryRecord::new(
            "policy-1",
            "run-1",
            "stream-1",
            "response-1",
            "before_client_delivery",
            "allow",
        )
        .with_accepted_through_sequence(0)
        .validate(),
        Err(TelemetryProjectionError::InvalidAcceptedThroughSequence)
    );
    assert_eq!(
        OutputPolicyTelemetryRecord::new(
            "policy-1",
            "run-1",
            "stream-1",
            "response-1",
            "before_client_delivery",
            "allow",
        )
        .with_accepted_through_sequence(3)
        .with_last_client_delivered_sequence(4)
        .validate(),
        Err(TelemetryProjectionError::DeliveredBeyondAccepted {
            accepted: 3,
            delivered: 4,
        })
    );
}

#[test]
fn telemetry_capture_policy_redacts_drops_and_removes_disabled_digests() {
    let record =
        GenerationTelemetryRecord::new("gen-1", "run-1", "span-1", "agent", "openai", "gpt-test")
            .with_input_digest("sha256:input")
            .with_output_digest("sha256:output")
            .with_attribute("tenant", json!("tenant-1"))
            .with_attribute("prompt", json!("secret prompt"))
            .with_attribute("token", json!("secret token"))
            .with_attribute("debug", json!("drop me"));
    let policy = TelemetryCapturePolicy::new()
        .with_redacted_attribute_key("prompt")
        .with_redacted_attribute_key("token")
        .with_dropped_attribute_key("debug")
        .without_input_digest();

    let redacted = policy.apply_generation(&record);

    assert_eq!(redacted.input_digest, None);
    assert_eq!(redacted.output_digest.as_deref(), Some("sha256:output"));
    assert_eq!(
        redacted.attributes,
        [
            ("prompt".to_owned(), json!("[redacted]")),
            ("tenant".to_owned(), json!("tenant-1")),
            ("token".to_owned(), json!("[redacted]")),
        ]
        .into_iter()
        .collect()
    );
    assert_eq!(
        record.attributes.get("prompt"),
        Some(&json!("secret prompt"))
    );
}

#[test]
fn telemetry_key_protection_normalizes_case_and_naming_style() {
    let record =
        GenerationTelemetryRecord::new("gen-1", "run-1", "span-1", "agent", "openai", "gpt-test")
            .with_attribute("Authorization", json!("Bearer secret"))
            .with_attribute("apiKey", json!("secret-key"))
            .with_attribute("accessToken", json!("access-secret"))
            .with_attribute(
                "http.request.header.Authorization",
                json!("Bearer nested-secret"),
            )
            .with_attribute("totalTokens", json!(42));
    let policy = TelemetryCapturePolicy::new()
        .with_redacted_attribute_key("authorization")
        .with_redacted_attribute_key("api_key")
        .with_redacted_attribute_key("token");

    let redacted = policy.apply_generation(&record);
    let lint = TelemetryCapturePolicyLinter::from_keys(
        ["authorization", "api_key"],
        std::iter::empty::<&str>(),
    )
    .lint_policy(
        &TelemetryCapturePolicy::new()
            .with_redacted_attribute_key("Authorization")
            .with_redacted_attribute_key("apiKey"),
    );

    assert_eq!(redacted.attributes["Authorization"], json!("[redacted]"));
    assert_eq!(redacted.attributes["apiKey"], json!("[redacted]"));
    assert_eq!(redacted.attributes["accessToken"], json!("[redacted]"));
    assert_eq!(
        redacted.attributes["http.request.header.Authorization"],
        json!("[redacted]")
    );
    assert_eq!(redacted.attributes["totalTokens"], json!(42));
    assert!(lint.passed(), "{:#?}", lint.issues);
}

#[test]
fn telemetry_capture_policy_linter_flags_unprotected_and_invalid_redaction_policy() {
    let linter = TelemetryCapturePolicyLinter::from_keys(
        ["api_key", "authorization"],
        ["messages", "prompt"],
    );
    let policy = TelemetryCapturePolicy::new()
        .with_redacted_attribute_key("api_key")
        .with_replacement(" ");

    let result = linter.lint_policy(&policy);

    assert!(!result.passed());
    assert_eq!(
        result.issue_contracts(),
        vec![
            json!({
                "attribute_key": "api_key",
                "reason": "redaction_replacement_empty",
                "required_action": "set_non_empty_replacement",
            }),
            json!({
                "attribute_key": "authorization",
                "reason": "sensitive_attribute_not_protected",
                "required_action": "redact_or_drop",
            }),
            json!({
                "attribute_key": "messages",
                "reason": "content_attribute_not_protected",
                "required_action": "redact_or_drop",
            }),
            json!({
                "attribute_key": "prompt",
                "reason": "content_attribute_not_protected",
                "required_action": "redact_or_drop",
            }),
        ]
    );
}

#[test]
fn telemetry_export_result_contract_is_non_fatal_to_run() {
    let failure = TelemetryExportResult::failed("otlp", ["gen-1"], "TimeoutError", true);

    assert_eq!(
        failure.result_contract(),
        json!({
            "exporter": "otlp",
            "status": "failed",
            "record_ids": ["gen-1"],
            "error_type": "TimeoutError",
            "retryable": true,
            "run_impact": "none",
        })
    );
    assert_eq!(
        TelemetryExportResult::new(
            "otlp",
            "failed",
            ["gen-1"],
            Some("TimeoutError"),
            true,
            "fail"
        ),
        Err(TelemetryProjectionError::ExportAffectsRunCorrectness)
    );
}

#[test]
fn telemetry_diagnostic_bundle_combines_observability_health()
-> Result<(), TelemetryProjectionError> {
    let capture_lint = TelemetryCapturePolicyLinter::from_keys(["api_key"], ["prompt"])
        .lint_policy(&TelemetryCapturePolicy::new());
    let samples = [
        MetricSample::new("graphblocks_tool_executions_total")
            .with_label("tool_name", "ticket.create")
            .with_label("run_id", "run-1"),
        MetricSample::new("graphblocks_tool_executions_total")
            .with_label("tool_name", "ticket.update"),
    ];
    let cardinality_lint = MetricCardinalityLinter::new()
        .with_max_distinct_values_per_label(1)
        .lint_samples(&samples)?;
    let exporter_failure =
        TelemetryExportResult::failed("otlp", ["policy-1", "tool-1"], "TimeoutError", true);
    let exporter_success = TelemetryExportResult::completed("langfuse", ["gen-1"]);

    let bundle = telemetry_diagnostic_bundle(
        "observability-health",
        Some(&capture_lint),
        Some(&cardinality_lint),
        [&exporter_success, &exporter_failure],
    );

    assert!(!bundle.ok());
    assert_eq!(
        bundle.bundle_contract(),
        json!({
            "bundle_id": "observability-health",
            "ok": false,
            "summary": {
                "error": 2,
                "warning": 3,
                "info": 0,
            },
            "sections": [
                {
                    "name": "capture_policy",
                    "ok": false,
                    "summary": {
                        "error": 2,
                        "warning": 0,
                        "info": 0,
                    },
                    "diagnostics": [
                        {
                            "code": "TelemetryCapturePolicy.content_attribute_not_protected",
                            "severity": "error",
                            "path": "$.capturePolicy.attributes.prompt",
                            "message": "Telemetry attribute 'prompt' failed capture-policy lint; required action: redact_or_drop",
                        },
                        {
                            "code": "TelemetryCapturePolicy.sensitive_attribute_not_protected",
                            "severity": "error",
                            "path": "$.capturePolicy.attributes.api_key",
                            "message": "Telemetry attribute 'api_key' failed capture-policy lint; required action: redact_or_drop",
                        },
                    ],
                },
                {
                    "name": "exporters",
                    "ok": true,
                    "summary": {
                        "error": 0,
                        "warning": 1,
                        "info": 0,
                    },
                    "diagnostics": [
                        {
                            "code": "TelemetryExport.failed",
                            "severity": "warning",
                            "path": "$.exporters.otlp",
                            "message": "Telemetry exporter 'otlp' reported status 'failed' for 2 record(s); retryable: true; error_type: TimeoutError",
                        },
                    ],
                },
                {
                    "name": "metric_cardinality",
                    "ok": true,
                    "summary": {
                        "error": 0,
                        "warning": 2,
                        "info": 0,
                    },
                    "diagnostics": [
                        {
                            "code": "TelemetryMetricCardinality.blocked_label",
                            "severity": "warning",
                            "path": "$.metrics.graphblocks_tool_executions_total.labels.run_id",
                            "message": "Telemetry metric 'graphblocks_tool_executions_total' label 'run_id' observed 1 distinct value(s); limit: 0",
                        },
                        {
                            "code": "TelemetryMetricCardinality.too_many_values",
                            "severity": "warning",
                            "path": "$.metrics.graphblocks_tool_executions_total.labels.tool_name",
                            "message": "Telemetry metric 'graphblocks_tool_executions_total' label 'tool_name' observed 2 distinct value(s); limit: 1",
                        },
                    ],
                },
            ],
        })
    );
    Ok(())
}

#[test]
fn metric_cardinality_linter_flags_unbounded_labels() -> Result<(), TelemetryProjectionError> {
    let samples = [
        MetricSample::new("graphblocks_generation_usage_tokens_total")
            .with_label("provider", "openai")
            .with_label("model", "small")
            .with_label("run_id", "run-1"),
        MetricSample::new("graphblocks_generation_usage_tokens_total")
            .with_label("provider", "openai")
            .with_label("model", "medium"),
        MetricSample::new("graphblocks_generation_usage_tokens_total")
            .with_label("provider", "openai")
            .with_label("model", "large"),
    ];
    let linter = MetricCardinalityLinter::new().with_max_distinct_values_per_label(2);

    let result = linter.lint_samples(&samples)?;

    assert!(!result.passed());
    assert_eq!(
        result.issue_contracts(),
        vec![
            json!({
                "metric_name": "graphblocks_generation_usage_tokens_total",
                "label": "model",
                "distinct_values": 3,
                "limit": 2,
                "reason": "too_many_values",
            }),
            json!({
                "metric_name": "graphblocks_generation_usage_tokens_total",
                "label": "run_id",
                "distinct_values": 1,
                "limit": 0,
                "reason": "blocked_label",
            }),
        ]
    );
    Ok(())
}

#[test]
fn metric_cardinality_linter_blocks_camel_case_identity_labels()
-> Result<(), TelemetryProjectionError> {
    let sample = MetricSample::new("graphblocks_runs_total")
        .with_label("runId", "run-1")
        .with_label("traceId", "trace-1")
        .with_label("resource.run_id", "run-2");

    let result = MetricCardinalityLinter::new().lint_samples([&sample])?;

    assert_eq!(
        result
            .issues
            .iter()
            .map(|issue| (issue.label.as_str(), issue.reason.as_str()))
            .collect::<Vec<_>>(),
        vec![
            ("resource.run_id", "blocked_label"),
            ("runId", "blocked_label"),
            ("traceId", "blocked_label")
        ]
    );
    Ok(())
}

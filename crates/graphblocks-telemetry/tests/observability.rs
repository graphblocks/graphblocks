use graphblocks_telemetry::{
    GenerationObservation, GenerationTelemetryRecord, MetricCardinalityLinter, MetricLabelError,
    MetricLabelSet, MetricSample, SpanTiming, TelemetryBuffer, TelemetryBufferError,
    TelemetryCapturePolicy, TelemetryCapturePolicyLinter, TelemetryEnqueueOutcome,
    TelemetryExportResult, TelemetryOnFull, TelemetryPriority, TelemetryProjectionError,
    TelemetryQueuePolicy, TelemetryRecord, TelemetryRecordKind,
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

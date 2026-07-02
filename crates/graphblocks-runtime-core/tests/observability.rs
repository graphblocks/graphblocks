use graphblocks_runtime_core::observability::{
    CaptureDecision, CaptureMode, DiagnosticBundle, DiagnosticBundleError,
    DiagnosticBundleRedaction, DiagnosticExcerpt, DiagnosticExcerptKind, GenerationObservation,
    MetricLabelError, MetricLabelSet, ObservabilityEventName, ObservabilityObservation,
    RedactionRule, SpanTiming, TelemetryBuffer, TelemetryBufferError, TelemetryEnqueueOutcome,
    TelemetryExporterKind, TelemetryExporterReliability, TelemetryExporterRoute,
    TelemetryExporterRouteError, TelemetryOnFull, TelemetryPriority, TelemetryQueuePolicy,
    TelemetryRecord, TelemetryRecordKind,
};
use serde_json::json;

#[test]
fn span_timing_separates_queue_wait_flow_wait_first_output_and_streaming() {
    let timing = SpanTiming::new(1_000)
        .with_admitted_at(1_050)
        .with_started_at(1_080)
        .with_first_output_at(1_170)
        .with_completed_at(1_300);

    assert_eq!(timing.queue_wait_ms(), Some(50));
    assert_eq!(timing.flow_wait_ms(), Some(30));
    assert_eq!(timing.time_to_first_output_ms(), Some(90));
    assert_eq!(timing.execution_ms(), Some(220));
    assert_eq!(timing.streaming_ms(), Some(130));
}

#[test]
fn span_timing_ignores_incomplete_or_reversed_boundaries() {
    let incomplete = SpanTiming::new(1_000).with_started_at(1_100);
    let reversed = SpanTiming::new(1_000)
        .with_started_at(1_200)
        .with_first_output_at(1_150);

    assert_eq!(incomplete.queue_wait_ms(), None);
    assert_eq!(incomplete.time_to_first_output_ms(), None);
    assert_eq!(reversed.time_to_first_output_ms(), None);
}

#[test]
fn generation_observation_aggregates_chunks_usage_and_finish_reason() {
    let observation = GenerationObservation::new("span-1", "generate", "openai", "gpt-test")
        .with_timing(SpanTiming::new(1_000).with_started_at(1_020))
        .record_chunk(1, 12, 1_040)
        .record_chunk(2, 18, 1_080)
        .record_usage("model_output_tokens", 9)
        .record_usage("model_reasoning_tokens", 3)
        .finish("stop", 1_100);

    assert_eq!(observation.chunk_count, 2);
    assert_eq!(observation.first_chunk_sequence, Some(1));
    assert_eq!(observation.last_chunk_sequence, Some(2));
    assert_eq!(observation.output_bytes, 30);
    assert_eq!(observation.finish_reason.as_deref(), Some("stop"));
    assert_eq!(
        observation.usage.get("model_output_tokens").copied(),
        Some(9)
    );
    assert_eq!(observation.timing.first_output_at, Some(1_040));
    assert_eq!(observation.timing.completed_at, Some(1_100));
}

#[test]
fn metric_label_set_rejects_high_cardinality_labels() {
    let labels = MetricLabelSet::new()
        .with_label("environment", "prod")
        .with_label("graph_id", "support-agent")
        .with_label("run_id", "run-000001")
        .with_label("provider_response_id", "resp-123");

    assert_eq!(
        labels.validate_cardinality_budget(),
        Err(MetricLabelError::ForbiddenLabels {
            labels: vec!["provider_response_id".to_owned(), "run_id".to_owned()],
        })
    );
}

#[test]
fn async_and_callback_observation_names_match_spec_required_events() {
    let events = [
        ObservabilityEventName::AsyncOperationStart,
        ObservabilityEventName::AsyncOperationWait,
        ObservabilityEventName::AsyncOperationCallbackReceived,
        ObservabilityEventName::AsyncOperationResume,
        ObservabilityEventName::CallbackDeliverySchedule,
        ObservabilityEventName::CallbackDeliveryAttempt,
        ObservabilityEventName::CallbackDeliverySuccess,
        ObservabilityEventName::CallbackDeliveryFailure,
        ObservabilityEventName::CallbackDeliveryDeadLetter,
        ObservabilityEventName::RunAttach,
        ObservabilityEventName::RunDetach,
        ObservabilityEventName::RunReplay,
    ];

    assert_eq!(
        events
            .iter()
            .map(|event| event.as_str())
            .collect::<Vec<_>>(),
        vec![
            "async.operation.start",
            "async.operation.wait",
            "async.operation.callback_received",
            "async.operation.resume",
            "callback.delivery.schedule",
            "callback.delivery.attempt",
            "callback.delivery.success",
            "callback.delivery.failure",
            "callback.delivery.dead_letter",
            "run.attach",
            "run.detach",
            "run.replay",
        ]
    );
}

#[test]
fn async_observability_observation_rejects_high_cardinality_labels() {
    let observation = ObservabilityObservation::new(ObservabilityEventName::AsyncOperationResume)
        .with_label("operation_kind", "ci_job")
        .with_label("run_id", "run-123")
        .with_label("operation_id", "op-456")
        .with_attribute("state", json!("resuming"));

    assert_eq!(
        observation.validate_metric_labels(),
        Err(MetricLabelError::ForbiddenLabels {
            labels: vec!["operation_id".to_owned(), "run_id".to_owned()],
        })
    );
}

#[test]
fn callback_delivery_observation_accepts_low_cardinality_labels_and_attributes() {
    let observation =
        ObservabilityObservation::new(ObservabilityEventName::CallbackDeliveryFailure)
            .with_label("target_kind", "webhook")
            .with_label("failure_class", "server_error")
            .with_attribute("attempt", json!(3));

    observation
        .validate_metric_labels()
        .expect("low-cardinality labels are valid");
    assert_eq!(observation.name.as_str(), "callback.delivery.failure");
    assert_eq!(
        observation
            .labels
            .labels
            .get("target_kind")
            .map(String::as_str),
        Some("webhook")
    );
    assert_eq!(observation.attributes["attempt"], 3);
}

#[test]
fn hash_only_capture_keeps_digest_without_exporting_content() {
    let captured = CaptureDecision::hash_only("billing-30d").capture_text(
        "message",
        "hello secret",
        None,
        [RedactionRule::literal("secret", "[redacted]")],
    );

    assert_eq!(captured.mode, CaptureMode::HashOnly);
    assert_eq!(captured.content_kind, "message");
    assert!(captured.content_digest.starts_with("sha256:"));
    assert_eq!(captured.preview, None);
    assert_eq!(captured.content_ref, None);
    assert_eq!(captured.redaction_count, 0);
    assert!(!format!("{captured:?}").contains("hello secret"));
}

#[test]
fn redacted_preview_applies_redaction_before_export_capture() {
    let captured = CaptureDecision::redacted_preview("debug-7d")
        .with_consent_ref("consent-1")
        .capture_text(
            "tool_result",
            "safe prefix secret suffix",
            None,
            [RedactionRule::literal("secret", "[redacted]")],
        );

    assert_eq!(captured.mode, CaptureMode::RedactedPreview);
    assert_eq!(
        captured.preview.as_deref(),
        Some("safe prefix [redacted] suffix")
    );
    assert_eq!(captured.content_ref, None);
    assert_eq!(captured.retention_policy, "debug-7d");
    assert_eq!(captured.consent_ref.as_deref(), Some("consent-1"));
    assert_eq!(captured.redaction_count, 1);
}

#[test]
fn reference_only_capture_exports_reference_without_preview() {
    let captured = CaptureDecision::reference_only("records-90d").capture_text(
        "document",
        "document body",
        Some("artifact://doc-1"),
        [],
    );

    assert_eq!(captured.mode, CaptureMode::ReferenceOnly);
    assert_eq!(captured.preview, None);
    assert_eq!(captured.content_ref.as_deref(), Some("artifact://doc-1"));
    assert!(captured.content_digest.starts_with("sha256:"));
}

#[test]
fn telemetry_buffer_drops_low_priority_records_when_full() -> Result<(), TelemetryBufferError> {
    let mut buffer = TelemetryBuffer::new(TelemetryQueuePolicy::new(
        2,
        TelemetryOnFull::DropLowPriority,
    ));

    assert_eq!(
        buffer.enqueue(TelemetryRecord::new(
            "debug-1",
            TelemetryRecordKind::DebugSpan,
            TelemetryPriority::Low,
        ))?,
        TelemetryEnqueueOutcome::accepted()
    );
    buffer.enqueue(TelemetryRecord::new(
        "metric-1",
        TelemetryRecordKind::Metric,
        TelemetryPriority::Normal,
    ))?;

    let outcome = buffer.enqueue(TelemetryRecord::new(
        "span-1",
        TelemetryRecordKind::Span,
        TelemetryPriority::High,
    ))?;

    assert_eq!(
        outcome,
        TelemetryEnqueueOutcome::accepted_with_drop(["debug-1"])
    );
    assert_eq!(buffer.dropped_count(), 1);
    assert_eq!(
        buffer
            .records()
            .iter()
            .map(|record| record.record_id.as_str())
            .collect::<Vec<_>>(),
        vec!["metric-1", "span-1"]
    );
    Ok(())
}

#[test]
fn telemetry_buffer_rejects_required_durable_records() {
    let mut buffer = TelemetryBuffer::new(TelemetryQueuePolicy::new(
        2,
        TelemetryOnFull::DropLowPriority,
    ));

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
fn lossy_exporter_routes_reject_audit_and_usage_ledger_records() {
    let route = TelemetryExporterRoute::new(
        "otlp-best-effort",
        TelemetryExporterKind::Otlp,
        TelemetryExporterReliability::Lossy,
    );

    assert_eq!(
        route.validate_record_kind(TelemetryRecordKind::RequiredAudit),
        Err(TelemetryExporterRouteError::LossyDurableRecord {
            exporter_id: "otlp-best-effort".to_owned(),
            kind: TelemetryRecordKind::RequiredAudit,
        })
    );
    assert_eq!(
        route.validate_record_kind(TelemetryRecordKind::UsageLedger),
        Err(TelemetryExporterRouteError::LossyDurableRecord {
            exporter_id: "otlp-best-effort".to_owned(),
            kind: TelemetryRecordKind::UsageLedger,
        })
    );
    route
        .validate_record_kind(TelemetryRecordKind::Span)
        .expect("lossy exporter can receive ordinary telemetry");
}

#[test]
fn durable_exporter_routes_accept_required_audit_and_usage_ledger_records() {
    let route = TelemetryExporterRoute::new(
        "audit-outbox",
        TelemetryExporterKind::AuditLog,
        TelemetryExporterReliability::Durable,
    );

    route
        .validate_record_kind(TelemetryRecordKind::RequiredAudit)
        .expect("durable route accepts audit records");
    route
        .validate_record_kind(TelemetryRecordKind::UsageLedger)
        .expect("durable route accepts usage ledger records");
}

#[test]
fn exporter_route_rejects_empty_identity() {
    let route = TelemetryExporterRoute::new(
        " ",
        TelemetryExporterKind::Langfuse,
        TelemetryExporterReliability::Lossy,
    );

    assert_eq!(
        route.validate_record_kind(TelemetryRecordKind::Span),
        Err(TelemetryExporterRouteError::EmptyExporterId)
    );
}

#[test]
fn diagnostic_bundle_digest_is_stable_without_bundle_identity_or_inventory_order() {
    let left = DiagnosticBundle::redacted("bundle-1", "run-1")
        .with_release("release-1", "rev-1")
        .with_plan_hashes("sha256:graph", "sha256:plan")
        .with_package("graphblocks-runtime-core", "0.1.0")
        .with_package("graphblocks-compiler", "0.1.0")
        .with_configuration_hash("policy", "sha256:policy")
        .with_configuration_hash("bindings", "sha256:bindings")
        .with_run_terminal_summary(json!({"outcome": "completed"}))
        .with_redaction_report("redacted 2 previews");
    let right = DiagnosticBundle::redacted("bundle-2", "run-1")
        .with_release("release-1", "rev-1")
        .with_plan_hashes("sha256:graph", "sha256:plan")
        .with_package("graphblocks-compiler", "0.1.0")
        .with_package("graphblocks-runtime-core", "0.1.0")
        .with_configuration_hash("bindings", "sha256:bindings")
        .with_configuration_hash("policy", "sha256:policy")
        .with_run_terminal_summary(json!({"outcome": "completed"}))
        .with_redaction_report("redacted 2 previews");

    assert_eq!(left.redaction, DiagnosticBundleRedaction::Redacted);
    assert_eq!(left.content_digest(), right.content_digest());
}

#[test]
fn redacted_diagnostic_bundle_rejects_unredacted_content_excerpts() {
    let bundle = DiagnosticBundle::redacted("bundle-1", "run-1").with_excerpt(
        DiagnosticExcerpt::new("trace-1", DiagnosticExcerptKind::Trace)
            .with_content_mode(CaptureMode::Full)
            .with_payload(json!({"message": "raw customer content"})),
    );

    assert_eq!(
        bundle.validate_redaction(),
        Err(DiagnosticBundleError::UnredactedContent {
            excerpt_id: "trace-1".to_owned(),
        })
    );
}

#[test]
fn content_free_diagnostic_bundle_allows_hash_only_excerpts() -> Result<(), DiagnosticBundleError> {
    let bundle = DiagnosticBundle::content_free("bundle-1", "run-1").with_excerpt(
        DiagnosticExcerpt::new("metric-1", DiagnosticExcerptKind::Metric)
            .with_content_mode(CaptureMode::HashOnly)
            .with_payload(json!({"digest": "sha256:metric"})),
    );

    bundle.validate_redaction()
}

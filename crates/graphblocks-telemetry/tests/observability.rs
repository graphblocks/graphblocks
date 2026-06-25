use graphblocks_telemetry::{
    GenerationObservation, MetricLabelError, MetricLabelSet, SpanTiming, TelemetryBuffer,
    TelemetryBufferError, TelemetryEnqueueOutcome, TelemetryOnFull, TelemetryPriority,
    TelemetryQueuePolicy, TelemetryRecord, TelemetryRecordKind,
};

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

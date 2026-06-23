use graphblocks_runtime_core::observability::{
    GenerationObservation, MetricLabelError, MetricLabelSet, SpanTiming,
};

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

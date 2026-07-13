from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml

from graphblocks.canonical import canonical_hash
from graphblocks.evaluation import (
    GateConstraint,
    MetricObservation,
    ResourceSnapshotRef,
    TrialResult,
    evaluate_gate,
)


def run_benchmark(matrix_path: Path) -> dict[str, object]:
    document = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("kind") != "VllmBenchmarkMatrix":
        raise ValueError("vLLM benchmark matrix must be a VllmBenchmarkMatrix resource")
    spec = document.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("vLLM benchmark matrix spec must be a mapping")
    configs = spec.get("configs")
    workload = spec.get("workload")
    constraints = spec.get("constraints")
    if not isinstance(configs, dict) or len(configs) < 2:
        raise ValueError("vLLM benchmark matrix requires at least two configs")
    if not isinstance(workload, dict) or not isinstance(constraints, dict):
        raise ValueError("vLLM benchmark workload and constraints must be mappings")
    baseline_id = spec.get("baseline")
    candidate_id = spec.get("candidate")
    if baseline_id not in configs or candidate_id not in configs:
        raise ValueError("vLLM benchmark baseline and candidate must name configs")
    if baseline_id == candidate_id:
        raise ValueError("vLLM benchmark baseline and candidate must differ")

    config_reports: dict[str, dict[str, object]] = {}
    for config_id, raw_config in configs.items():
        if not isinstance(config_id, str) or not config_id.strip():
            raise ValueError("vLLM benchmark config ids must be non-empty strings")
        if not isinstance(raw_config, dict):
            raise ValueError("vLLM benchmark configs must be mappings")
        serve_args = raw_config.get("serveArgs")
        fixture = raw_config.get("fixture")
        if not isinstance(serve_args, dict) or not isinstance(fixture, dict):
            raise ValueError("vLLM benchmark config requires serveArgs and fixture")
        samples = fixture.get("samples")
        if not isinstance(samples, list) or not samples:
            raise ValueError("vLLM benchmark fixture requires samples")
        if len(samples) != workload.get("promptCount"):
            raise ValueError("vLLM benchmark sample count must match promptCount")
        run_duration_ms = fixture.get("runDurationMs")
        if (
            isinstance(run_duration_ms, bool)
            or not isinstance(run_duration_ms, (int, float))
            or run_duration_ms <= 0
        ):
            raise ValueError("vLLM benchmark runDurationMs must be positive")

        ttfts: list[Decimal] = []
        decode_tps_values: list[Decimal] = []
        sample_reports: list[dict[str, object]] = []
        total_output_tokens = 0
        for raw_sample in samples:
            if not isinstance(raw_sample, dict):
                raise ValueError("vLLM benchmark samples must be mappings")
            prompt_id = raw_sample.get("promptId")
            ttft_ms = raw_sample.get("ttftMs")
            e2e_ms = raw_sample.get("e2eMs")
            output_tokens = raw_sample.get("outputTokens")
            if not isinstance(prompt_id, str) or not prompt_id.strip():
                raise ValueError("vLLM benchmark promptId must be a non-empty string")
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in (ttft_ms, e2e_ms)
            ):
                raise ValueError("vLLM benchmark timings must be numeric")
            if ttft_ms < 0 or e2e_ms <= ttft_ms:
                raise ValueError("vLLM benchmark E2E must be greater than TTFT")
            if (
                isinstance(output_tokens, bool)
                or not isinstance(output_tokens, int)
                or output_tokens < 2
            ):
                raise ValueError("vLLM benchmark outputTokens must be at least two")
            if output_tokens > workload.get("maxOutputTokens"):
                raise ValueError("vLLM benchmark outputTokens exceeds maxOutputTokens")

            ttft = Decimal(str(ttft_ms))
            decode_seconds = (Decimal(str(e2e_ms)) - ttft) / Decimal(1000)
            decode_tps = Decimal(output_tokens - 1) / decode_seconds
            ttfts.append(ttft)
            decode_tps_values.append(decode_tps)
            total_output_tokens += output_tokens
            sample_reports.append(
                {
                    "decodeTps": str(decode_tps),
                    "e2eMs": str(Decimal(str(e2e_ms))),
                    "outputTokens": output_tokens,
                    "promptId": prompt_id,
                    "ttftMs": str(ttft),
                }
            )

        sorted_ttfts = sorted(ttfts)
        percentile_values: dict[str, Decimal] = {}
        for label, percentile in (("p50", Decimal("0.50")), ("p95", Decimal("0.95"))):
            position = Decimal(len(sorted_ttfts) - 1) * percentile
            lower_index = int(position)
            upper_index = min(lower_index + 1, len(sorted_ttfts) - 1)
            fraction = position - Decimal(lower_index)
            percentile_values[label] = sorted_ttfts[lower_index] + (
                sorted_ttfts[upper_index] - sorted_ttfts[lower_index]
            ) * fraction
        mean_decode_tps = sum(decode_tps_values, Decimal(0)) / Decimal(
            len(decode_tps_values)
        )
        output_throughput_tps = Decimal(total_output_tokens) / (
            Decimal(str(run_duration_ms)) / Decimal(1000)
        )
        config_reports[config_id] = {
            "configDigest": canonical_hash(serve_args),
            "meanDecodeTps": str(mean_decode_tps),
            "outputThroughputTps": str(output_throughput_tps),
            "p50TtftMs": str(percentile_values["p50"]),
            "p95TtftMs": str(percentile_values["p95"]),
            "runDurationMs": str(Decimal(str(run_duration_ms))),
            "samples": sample_reports,
            "serveArgs": dict(serve_args),
            "totalOutputTokens": total_output_tokens,
        }

    baseline_report = config_reports[str(baseline_id)]
    candidate_report = config_reports[str(candidate_id)]
    baseline_p95_ttft = Decimal(str(baseline_report["p95TtftMs"]))
    candidate_p95_ttft = Decimal(str(candidate_report["p95TtftMs"]))
    baseline_decode_tps = Decimal(str(baseline_report["meanDecodeTps"]))
    candidate_decode_tps = Decimal(str(candidate_report["meanDecodeTps"]))
    baseline_output_tps = Decimal(str(baseline_report["outputThroughputTps"]))
    candidate_output_tps = Decimal(str(candidate_report["outputThroughputTps"]))
    ttft_improvement = (
        (baseline_p95_ttft - candidate_p95_ttft) / baseline_p95_ttft * Decimal(100)
    )
    output_tps_improvement = (
        (candidate_output_tps - baseline_output_tps)
        / baseline_output_tps
        * Decimal(100)
    )

    snapshot_metadata = {
        "hardware": spec.get("hardware"),
        "model": spec.get("model"),
        "model_revision": spec.get("modelRevision"),
        "tokenizer_revision": spec.get("tokenizerRevision"),
        "vllm_version": spec.get("vllmVersion"),
    }
    baseline = ResourceSnapshotRef(
        resource_id=str(baseline_id),
        digest=str(baseline_report["configDigest"]),
        resource_kind="vllm_config",
        metadata=snapshot_metadata,
    )
    candidate = ResourceSnapshotRef(
        resource_id=str(candidate_id),
        digest=str(candidate_report["configDigest"]),
        resource_kind="vllm_config",
        metadata=snapshot_metadata,
    )
    evaluator = {
        "kind": "vllm-serving-benchmark",
        "matrix_digest": canonical_hash(document),
        "prompt_count": workload["promptCount"],
        "workload_digest": canonical_hash(workload),
    }
    metrics = [
        MetricObservation(
            "p95_ttft_ms",
            candidate_p95_ttft,
            unit="ms",
            direction="minimize",
            baseline_value=baseline_p95_ttft,
            subject=candidate,
            evaluator=evaluator,
        ),
        MetricObservation(
            "mean_decode_tps",
            candidate_decode_tps,
            unit="token/s",
            direction="maximize",
            baseline_value=baseline_decode_tps,
            subject=candidate,
            evaluator=evaluator,
        ),
        MetricObservation(
            "output_throughput_tps",
            candidate_output_tps,
            unit="token/s",
            direction="maximize",
            baseline_value=baseline_output_tps,
            subject=candidate,
            evaluator=evaluator,
        ),
    ]
    p95_constraint = constraints.get("p95TtftMs")
    decode_constraint = constraints.get("meanDecodeTps")
    throughput_constraint = constraints.get("outputThroughputTps")
    if not all(
        isinstance(value, dict)
        for value in (p95_constraint, decode_constraint, throughput_constraint)
    ):
        raise ValueError("vLLM benchmark constraints must be mappings")
    gate = evaluate_gate(
        "vllm-config-performance",
        candidate,
        metrics=metrics,
        constraints=[
            GateConstraint(
                "p95_ttft_ms",
                "at_most",
                Decimal(str(p95_constraint["atMost"])),
            ),
            GateConstraint(
                "mean_decode_tps",
                "at_least",
                Decimal(str(decode_constraint["atLeast"])),
            ),
            GateConstraint(
                "output_throughput_tps",
                "at_least",
                Decimal(str(throughput_constraint["atLeast"])),
            ),
        ],
    )
    trial = TrialResult(
        trial_id="vllm-config-baseline-vs-larger-batch",
        base=baseline,
        candidate=candidate,
        metrics=metrics,
        gate=gate,
        outcome="accepted" if gate.decision == "pass" else "rejected",
    )
    evidence: dict[str, object] = {
        "benchmarkId": trial.trial_id,
        "baseline": str(baseline_id),
        "candidate": str(candidate_id),
        "configs": config_reports,
        "metrics": [
            {
                "baselineValue": str(metric.baseline_value),
                "direction": metric.direction,
                "name": metric.name,
                "unit": metric.unit,
                "value": str(metric.value),
            }
            for metric in metrics
        ],
        "summary": {
            "gateDecision": gate.decision,
            "outcome": trial.outcome,
            "outputThroughputImprovementPct": str(output_tps_improvement),
            "p95TtftImprovementPct": str(ttft_improvement),
        },
        "workload": dict(workload),
    }
    return {**evidence, "evidenceDigest": canonical_hash(evidence)}

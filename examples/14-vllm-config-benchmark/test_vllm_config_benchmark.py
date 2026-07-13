from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _test_support import assert_example_runner


def test_vllm_config_benchmark_example() -> None:
    payload = assert_example_runner(
        Path(__file__).with_name("run.py"),
        expected_checks={
            "mock-graph:final-output",
            "mock-graph:journal",
            "mock-graph:resolved-inputs",
        },
        expected_boundaries={
            "benchmark-aggregator",
            "mock-vllm-servers",
            "performance-gate",
        },
    )
    benchmark = payload["benchmark"]

    assert benchmark["baseline"] == "baseline"
    assert benchmark["candidate"] == "larger-batch"
    assert benchmark["summary"] == {
        "gateDecision": "pass",
        "outcome": "accepted",
        "outputThroughputImprovementPct": "60.0",
        "p95TtftImprovementPct": "50.0",
    }
    assert benchmark["configs"]["baseline"]["p95TtftMs"] == "197.00"
    assert benchmark["configs"]["larger-batch"]["p95TtftMs"] == "98.50"
    assert benchmark["configs"]["baseline"]["meanDecodeTps"] == "50"
    assert benchmark["configs"]["larger-batch"]["meanDecodeTps"] == "80"
    assert benchmark["configs"]["baseline"]["outputThroughputTps"] == "128"
    assert benchmark["configs"]["larger-batch"]["outputThroughputTps"] == "204.8"
    assert all(
        sample["outputTokens"] == 64
        for report in benchmark["configs"].values()
        for sample in report["samples"]
    )
    assert str(benchmark["evidenceDigest"]).startswith("sha256:")

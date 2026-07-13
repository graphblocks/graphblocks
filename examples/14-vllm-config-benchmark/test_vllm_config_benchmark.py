from __future__ import annotations

from pathlib import Path
import sys

from graphblocks.composition import compose_documents


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

    assert "composition:expanded" in payload["integration"]["checks"]
    assert len(payload["composition"]["instances"]) == 2


def test_vllm_benchmark_materializes_composed_subgraphs() -> None:
    result = compose_documents(Path(__file__).with_name("example.yaml"))
    graph = next(document for document in result.documents if document["kind"] == "Graph")

    assert set(graph["spec"]["nodes"]) == {
        "collect__load",
        "collect__measure",
        "collect__warmup",
        "evaluate__aggregate",
        "evaluate__compare",
    }
    assert "composition" not in graph["spec"]
    assert all("slot" not in node for node in graph["spec"]["nodes"].values())
    assert {(item.node, item.fragment) for item in result.report.instances} == {
        ("collect", "collection/collect-performance"),
        ("evaluate", "evaluation/evaluate-performance"),
    }
    nodes = graph["spec"]["nodes"]
    assert nodes["collect__measure"]["config"]["metrics"] == [
        "ttft_ms",
        "decode_tps",
        "output_throughput_tps",
    ]
    assert nodes["evaluate__aggregate"]["config"]["decodeTpsFormula"] == (
        "(output_tokens-1)/((e2e_ms-ttft_ms)/1000)"
    )
    assert nodes["evaluate__compare"]["config"]["constraints"] == {
        "p95TtftMs": {"atMost": 120},
        "meanDecodeTps": {"atLeast": 75},
        "outputThroughputTps": {"atLeast": 200},
    }

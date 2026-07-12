from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _test_support import assert_example_runner


def test_llm_interviewer_rag_benchmark_example() -> None:
    script = Path(__file__).with_name("run.py")
    payload = assert_example_runner(
        script,
        expected_checks={
            "mock-graph:final-output",
            "mock-graph:journal",
            "mock-graph:resolved-inputs",
        },
        expected_boundaries={
            "benchmark-aggregator",
            "local-retriever",
            "scripted-answer-model",
            "scripted-interviewer",
        },
    )
    benchmark = payload["benchmark"]

    assert benchmark["summary"] == {
        "caseCount": 3,
        "gateDecision": "pass",
        "noRagMeanScore": "0.2",
        "outcome": "accepted",
        "ragMeanScore": "1",
        "ragScoreDelta": "0.8",
        "ragWinRate": "1",
    }
    assert [case["winner"] for case in benchmark["cases"]] == ["rag", "rag", "rag"]
    assert [case["blindOrder"] for case in benchmark["cases"]] == [
        {"A": "rag", "B": "no_rag"},
        {"A": "no_rag", "B": "rag"},
        {"A": "rag", "B": "no_rag"},
    ]
    assert all(case["retrievedItemIds"] for case in benchmark["cases"])
    assert str(benchmark["evidenceDigest"]).startswith("sha256:")
    assert benchmark["metrics"][0]["baselineValue"] == "0.2"

    repeated = assert_example_runner(
        script,
        expected_checks={"mock-graph:final-output"},
        expected_boundaries={"scripted-interviewer"},
    )
    assert repeated["benchmark"]["evidenceDigest"] == benchmark["evidenceDigest"]

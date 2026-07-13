from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from graphblocks.composition import compose_documents

from _test_support import assert_example_runner


def test_llm_interviewer_graph_composes_three_subgraphs() -> None:
    example = Path(__file__).with_name("example.yaml")
    composition = compose_documents(example)
    graph = next(document for document in composition.documents if document["kind"] == "Graph")

    assert [document["kind"] for document in composition.documents] == ["Graph", "Binding"]
    assert "composition" not in graph["spec"]
    assert set(graph["spec"]["nodes"]) == {
        "setup__load",
        "setup__ask",
        "variants__retrieve",
        "variants__ragAnswer",
        "variants__noRagAnswer",
        "evaluation__blind",
        "evaluation__score",
        "evaluation__aggregate",
    }
    assert {instance.node for instance in composition.report.instances} == {
        "setup",
        "variants",
        "evaluation",
    }
    nodes = graph["spec"]["nodes"]
    assert nodes["variants__ragAnswer"]["bindings"] == {"model": "answer-model"}
    assert nodes["variants__noRagAnswer"]["bindings"] == {"model": "answer-model"}
    assert nodes["evaluation__score"]["bindings"] == {"model": "interviewer-model"}
    assert nodes["evaluation__score"]["config"]["blind"] is True
    binding = next(
        document for document in composition.documents if document["kind"] == "Binding"
    )
    assert set(binding["spec"]["resources"]) == {
        "interviewer-model",
        "answer-model",
        "benchmark-retriever",
    }


def test_llm_interviewer_rag_benchmark_example() -> None:
    script = Path(__file__).with_name("run.py")
    payload = assert_example_runner(
        script,
        expected_checks={
            "composition:expanded",
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

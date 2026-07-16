from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _test_support import assert_example_runner


def test_enterprise_federated_rag_example() -> None:
    payload = assert_example_runner(
        Path(__file__).with_name("run.py"),
        expected_checks={
            "acceptance:rag citation validation",
            "acceptance:abstention check",
            "mock-graph:resolved-inputs",
            "mock-graph:final-output",
        },
        expected_boundaries={"mock-retrievers", "scripted-llm"},
    )
    runtimes = payload["runtimes"]
    assert runtimes["parity"] == {
        "graphHash": True,
        "grounding": True,
        "semanticResult": True,
        "status": True,
        "succeededNodeOrder": True,
    }
    variants = runtimes["variants"]
    assert [variants[key]["runtime"] for key in variants] == [
        "yaml-cli",
        "python-api",
        "rust-api",
    ]
    assert {variants[key]["status"] for key in variants} == {"succeeded"}
    assert {tuple(variants[key]["grounding"].items()) for key in variants} == {
        (("issueCount", 0), ("ok", True))
    }
    assert variants["1-1-yaml"]["semanticResult"] == {
        "answerId": "answer-key-rotation",
        "citations": ["citation-rotation", "citation-ticket"],
        "status": "grounded",
        "text": "Use the security console and obtain two approvals.",
    }
    assert variants["1-1-yaml"]["succeededNodes"] == [
        "retrieve",
        "fuse",
        "rerank",
        "context",
        "generate",
        "validate",
    ]
    assert str(runtimes["evidenceDigest"]).startswith("sha256:")

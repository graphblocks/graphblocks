from __future__ import annotations

from pathlib import Path
import sys

import pytest

from graphblocks.documents import FrozenList


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_support import assert_example_runner
from runtime_contract import normalize_runtime_result


def _runtime_payload(*, citations: object) -> dict[str, object]:
    return {
        "status": "succeeded",
        "outputs": {
            "candidate": {
                "answerId": "answer-key-rotation",
                "citations": citations,
                "text": "Use the security console and obtain two approvals.",
            },
            "validation": {
                "issues": FrozenList(),
                "ok": True,
            },
        },
        "journal": FrozenList(
            [
                {
                    "kind": "run_started",
                    "payload": {"graphHash": "sha256:runtime-graph"},
                },
                {
                    "kind": "node_succeeded",
                    "payload": {"node": "generate"},
                },
            ]
        ),
    }


def test_runtime_contract_accepts_frozen_json_sequences() -> None:
    payload = _runtime_payload(
        citations=FrozenList([{"citationId": "citation-rotation"}])
    )

    result = normalize_runtime_result(
        payload,
        runtime="python-api",
        graph={"kind": "Graph"},
    )

    assert result["grounding"] == {"issueCount": 0, "ok": True}
    assert result["semanticResult"] == {
        "answerId": "answer-key-rotation",
        "citations": ["citation-rotation"],
        "status": "grounded",
        "text": "Use the security console and obtain two approvals.",
    }
    assert result["succeededNodes"] == ["generate"]


@pytest.mark.parametrize(
    "citations",
    [
        "citation-rotation",
        b"citation-rotation",
        {"citationId": "citation-rotation"},
    ],
)
def test_runtime_contract_rejects_non_array_sequences(citations: object) -> None:
    with pytest.raises(RuntimeError, match="candidate citations must be an array"):
        normalize_runtime_result(
            _runtime_payload(citations=citations),
            runtime="python-api",
            graph={"kind": "Graph"},
        )


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

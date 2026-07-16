from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "examples"))

from _integration import FixtureBlock, NetworkAccessBlocked, WorkerBlockAdapter, run_integration
from graphblocks.worker import (
    WorkerInvokeRequest,
    WorkerInvokeResult,
    WorkerProtocolMessage,
    WorkerStaleLeaseEpochError,
)


EXAMPLE_SLUGS = (
    "01-enterprise-federated-rag",
    "02-document-ingestion",
    "03-policy-governed-chat",
    "04-tui-workspace-assistant",
    "05-authority-backed-advisory",
    "06-bounded-research-orchestrator",
    "07-verified-rtl-workspace-trial",
    "08-kubernetes-production-deployment",
    "09-observability-profile",
    "10-realtime-voice-extension",
    "11-coding-agent-background-callbacks",
    "12-custom-python-rust-blocks",
    "13-llm-interviewer-rag-benchmark",
    "14-vllm-config-benchmark",
)


@pytest.mark.parametrize("slug", EXAMPLE_SLUGS)
def test_example_executes_mocked_integration(slug: str) -> None:
    example_path = ROOT / "examples" / slug / "example.yaml"

    report = run_integration(example_path)

    assert report["ok"] is True
    assert report["example"] == slug
    assert "references:resolved" in report["checks"]
    assert len(report["checks"]) >= 3
    assert report["mockedBoundaries"] or report["executedBlocks"]
    assert str(report["evidenceDigest"]).startswith("sha256:")


def test_example_integration_inventory_matches_root_examples() -> None:
    directories = {
        path.name
        for path in (ROOT / "examples").iterdir()
        if path.is_dir() and path.name[:2].isdigit()
    }

    assert directories == set(EXAMPLE_SLUGS)
    for slug in EXAMPLE_SLUGS:
        example_root = ROOT / "examples" / slug
        assert (example_root / "example.yaml").is_file()
        assert (example_root / "integration.yaml").is_file()
        assert (example_root / "run.py").is_file()


def test_mock_block_rejects_missing_resolved_input() -> None:
    calls: list[dict[str, object]] = []
    block = FixtureBlock(
        "retrieve.execute_plan@1",
        {
            "retrieve": {
                "service": "mock-retriever",
                "expectedInputs": {"query": "required query"},
                "outputs": {"result": {"hits": []}},
            }
        },
        calls,
    )

    with pytest.raises(AssertionError, match="query is missing"):
        block({}, {}, {"node": "retrieve", "run_id": "run-test"})

    assert calls == []


def test_example_integration_evidence_is_deterministic() -> None:
    example_path = ROOT / "examples" / "01-enterprise-federated-rag" / "example.yaml"

    first = run_integration(example_path)
    second = run_integration(example_path)

    assert first["evidenceDigest"] == second["evidenceDigest"]
    assert first["mockCalls"] == second["mockCalls"]


def test_network_blocker_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="real network access"):
        NetworkAccessBlocked()(None, ("example.com", 443))


def test_example_rust_subprocesses_are_forced_offline() -> None:
    integration_source = (ROOT / "examples" / "_integration.py").read_text(
        encoding="utf-8"
    )
    variant_source = (
        ROOT / "examples" / "01-enterprise-federated-rag" / "variants.py"
    ).read_text(encoding="utf-8")

    assert '"--offline"' in integration_source
    assert '"--offline"' in variant_source


def test_enterprise_rag_runtime_evidence_uses_actual_grounding_result() -> None:
    runtime_contract_path = (
        ROOT / "examples" / "01-enterprise-federated-rag" / "runtime_contract.py"
    )
    spec = importlib.util.spec_from_file_location(
        "graphblocks_example_runtime_contract_test",
        runtime_contract_path,
    )
    assert spec is not None and spec.loader is not None
    runtime_contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime_contract)

    result = runtime_contract.normalize_runtime_result(
        {
            "status": "succeeded",
            "outputs": {
                "candidate": {
                    "answerId": "answer-1",
                    "citations": [],
                    "text": "insufficient evidence",
                },
                "validation": {
                    "ok": False,
                    "issues": [{"code": "missing-citation"}],
                },
            },
            "journal": [],
        },
        runtime="test-runtime",
        graph={"kind": "Graph"},
    )

    assert result["grounding"] == {"issueCount": 1, "ok": False}
    assert result["semanticResult"]["status"] == "ungrounded"
    assert result["status"] == "succeeded"


def test_worker_block_adapter_rejects_stale_lease_result() -> None:
    def stale_worker(message: WorkerProtocolMessage) -> WorkerProtocolMessage:
        request = message.payload
        assert isinstance(request, WorkerInvokeRequest)
        return WorkerProtocolMessage.invoke_result(
            f"{message.message_id}-result",
            message.sequence + 1,
            WorkerInvokeResult(
                invocation_id=request.invocation_id,
                node_attempt_id=request.node_attempt_id,
                lease_epoch=request.lease_epoch + 1,
                outputs={"text": "stale"},
            ),
            causation_id=message.message_id,
        )

    calls: list[dict[str, object]] = []
    block = WorkerBlockAdapter(
        "examples.python.normalize-text@1",
        "examples.python.normalize_text",
        "stale-worker",
        stale_worker,
        calls,
    )

    with pytest.raises(WorkerStaleLeaseEpochError):
        block(
            {"text": "hello"},
            {},
            {"node": "normalize", "attempt": 1, "run_id": "run-stale"},
        )

    assert calls == []

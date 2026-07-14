from __future__ import annotations

import json
from pathlib import Path

import graphblocks
import pytest
from graphblocks.runtime import ExecutionJournal, InProcessRuntime, stdlib_registry


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tck" / "durable" / "native-callback-runtime.json"


def test_python_runtime_mirrors_native_callback_runtime_fixture() -> None:
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))
    journals: dict[str, ExecutionJournal] = {}
    runtime = InProcessRuntime(
        stdlib_registry(),
        journal_factory=lambda run_id: journals.setdefault(
            run_id, ExecutionJournal(run_id)
        ),
    )

    waiting = runtime.run(case["graph"], {}, run_id=case["runId"])

    assert waiting.status == case["expected"]["waitingStatus"]
    assert waiting.checkpoint is not None
    assert waiting.checkpoint.operation["operation_id"] == case["receipt"]["operation_id"]
    assert waiting.checkpoint.state_digest == waiting.checkpoint.content_digest()

    receipt = case["receipt"]
    admission = receipt["resume_admission"]
    admission["compatible_release_digest"] = waiting.checkpoint.graph_hash
    admission["run_id"] = waiting.checkpoint.run_id
    admission["checkpoint_id"] = waiting.checkpoint.checkpoint_id
    admission["checkpoint_state_digest"] = waiting.checkpoint.state_digest
    denied_receipt = json.loads(json.dumps(receipt))
    denied_receipt["resume_admission"]["outcome"] = "denied"
    with pytest.raises(
        ValueError,
        match="runtime callback_receipt trusted resume admission is invalid",
    ):
        runtime.run(
            case["graph"],
            {},
            run_id=case["runId"],
            checkpoint=waiting.checkpoint,
            callback_receipt=denied_receipt,
        )

    resumed = runtime.run(
        case["graph"],
        {},
        run_id=case["runId"],
        checkpoint=waiting.checkpoint,
        callback_receipt=receipt,
    )

    assert resumed.status == case["expected"]["resumedStatus"]
    assert resumed.outputs["callback"] == receipt["payload"]
    assert resumed.outputs["operation"]["state"] == "resuming"
    kinds = [record.kind for record in resumed.journal.records]
    positions = [kinds.index(kind) for kind in case["expected"]["journalOrder"]]
    assert positions == sorted(positions)
    assert receipt["payload_digest"] == graphblocks.canonical_hash(
        receipt["payload"]
    )

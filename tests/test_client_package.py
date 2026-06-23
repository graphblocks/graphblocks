from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_client_package_exposes_application_event_protocol(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    metadata = graphblocks_client.ApplicationEventMetadata(
        event_id="event-1",
        run_id="run-1",
        response_id="response-1",
        turn_id="turn-1",
        sequence=1,
        release_id="release-1",
        policy_snapshot_id="policy-1",
        occurred_at="2026-06-23T00:00:00Z",
    )
    event = graphblocks_client.ApplicationEvent.new(
        "OutputPolicyAllowed",
        metadata,
        payload={"decision_id": "decision-1"},
    )

    assert event.kind == "OutputPolicyAllowed"
    assert event.metadata.run_id == "run-1"
    assert event.payload == {"decision_id": "decision-1"}
    assert "ToolCallCompleted" in graphblocks_client.TOOL_APPLICATION_EVENT_KINDS
    assert "OutputCutoff" in graphblocks_client.STANDARD_APPLICATION_EVENT_KINDS

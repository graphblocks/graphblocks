from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_audit_package_exposes_append_only_event_and_enforcement_records(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-audit" / "src"))
    graphblocks_audit = importlib.import_module("graphblocks_audit")

    metadata = graphblocks_audit.ApplicationEventMetadata(
        event_id="event-1",
        run_id="run-1",
        response_id="response-1",
        sequence=1,
        release_id="release-1",
        policy_snapshot_id="policy-1",
        occurred_at="2026-06-23T00:00:00Z",
        turn_id="turn-1",
    )
    event = graphblocks_audit.ApplicationEvent.new(
        "OutputPolicyAllowed",
        metadata,
        payload={"decision_id": "decision-1"},
    )
    decision = graphblocks_audit.PolicyDecision(
        decision_id="decision-1",
        effect="allow",
        reason_codes=("allow-output",),
        policy_refs=("policy/output",),
        input_digest="sha256:input",
    )
    enforcement = graphblocks_audit.PolicyEnforcementRecord.from_decision(
        record_id="enforcement-1",
        decision=decision,
        enforcement_point="before_client_delivery",
        status="enforced",
    )

    assert event.metadata.event_id == "event-1"
    assert enforcement.decision_id == "decision-1"
    assert enforcement.enforcement_point == "before_client_delivery"
    assert enforcement.status == "enforced"

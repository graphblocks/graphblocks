from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_policy_package_exposes_static_evaluator_and_output_gate_contract(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy" / "src"))
    graphblocks_policy = importlib.import_module("graphblocks_policy")

    evaluator = graphblocks_policy.StaticPolicyEvaluator(
        rules=[
            graphblocks_policy.PolicyRule(
                "allow-model",
                "allow",
                actions=("model.generate",),
                resource_selectors=("model",),
            )
        ]
    )
    request = graphblocks_policy.PolicyRequest(
        request_id="request-1",
        enforcement_point="on_generation_chunk",
        action="model.generate",
        resource=graphblocks_policy.ResourceRef("model:support", resource_kind="model"),
        occurred_at="2026-06-23T00:00:00Z",
    )
    decision = evaluator.evaluate(request, evaluated_at="2026-06-23T00:00:01Z")

    gate = graphblocks_policy.OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(graphblocks_policy.GenerationChunk.text("stream-1", "response-1", 1, "blocked"))
    update = gate.apply_decision(
        graphblocks_policy.OutputPolicyDecision.abort_response("output-decision-1", input_digest="sha256:output"),
        occurred_at="2026-06-23T00:00:02Z",
    )

    assert decision.effect == "allow"
    assert decision.input_digest == request.with_input_digest().input_digest
    assert update.cutoff is not None
    assert update.cutoff.policy_decision_id == "output-decision-1"
    assert update.pending_tool_calls == "deny"

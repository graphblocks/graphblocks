from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]


def load_runtime_wrapper(fake_native=None):
    package_root = ROOT / "packages" / "graphblocks-runtime" / "src" / "graphblocks_runtime"
    module_name = "_graphblocks_runtime_wrapper_under_test"
    native_module_name = f"{module_name}._native"
    spec = importlib.util.spec_from_file_location(
        module_name,
        package_root / "__init__.py",
        submodule_search_locations=[str(package_root)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    if fake_native is not None:
        sys.modules[native_module_name] = fake_native
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop(native_module_name, None)
    return module


def test_runtime_wrapper_reports_native_binding_readiness_without_second_implementation() -> None:
    runtime = load_runtime_wrapper()

    assert runtime.native_extension_available() is False
    status = runtime.native_extension_status()
    assert status == {
        "available": False,
        "binding_crate": "graphblocks-python",
        "binding_version": None,
        "module": "graphblocks_runtime._native",
        "error": status["error"],
    }
    assert "_native" in status["error"]
    assert "native_extension_available" in runtime.__all__
    assert "native_extension_status" in runtime.__all__
    assert "require_native_extension" in runtime.__all__
    with pytest.raises(RuntimeError, match="single PyO3 binding crate graphblocks-python"):
        runtime.require_native_extension()


def test_runtime_wrapper_convenience_helpers_delegate_to_native_json() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def compile_graph_json(document_json: str, block_catalog_json: str | None = None) -> str:
        calls.append(("compile", (document_json, block_catalog_json or "")))
        return json.dumps({"ok": True, "graph": json.loads(document_json), "diagnostics": []})

    def run_stdlib_graph_json(graph_json: str, inputs_json: str) -> str:
        calls.append(("run_stdlib", (graph_json, inputs_json)))
        return json.dumps(
            {
                "runId": "run-native-1",
                "status": "succeeded",
                "outputs": {"answer": "ok"},
            }
        )

    def run_test_graph_json(graph_json: str, inputs_json: str, node_outputs_json: str) -> str:
        calls.append(("run_test", (graph_json, inputs_json, node_outputs_json)))
        return json.dumps({"runId": "run-test-1", "status": "succeeded", "outputs": {"fixture": True}})

    def finalize_tool_call_json(draft_json: str, resolved_tool_id: str, created_at_unix_ms: int) -> str:
        calls.append(("finalize_tool", (draft_json, resolved_tool_id, created_at_unix_ms)))
        return json.dumps(
            {
                "toolCallId": json.loads(draft_json)["toolCallId"],
                "resolvedToolId": resolved_tool_id,
                "createdAtUnixMs": created_at_unix_ms,
            }
        )

    def evaluate_output_gate_json(gate_json: str, operations_json: str) -> str:
        calls.append(("output_gate", (gate_json, operations_json)))
        return json.dumps({"gate": json.loads(gate_json), "updates": json.loads(operations_json)})

    def evaluate_declarative_output_policy_json(
        rules_json: str,
        chunk_json: str,
        evaluated_at_unix_ms: int,
    ) -> str:
        calls.append(("output_policy", (rules_json, chunk_json, evaluated_at_unix_ms)))
        return json.dumps(
            {
                "disposition": "allow",
                "rules": json.loads(rules_json),
                "chunk": json.loads(chunk_json),
                "evaluatedAtUnixMs": evaluated_at_unix_ms,
            }
        )

    def decide_agent_step_json(spec_json: str, request_json: str) -> str:
        calls.append(("agent_step", (spec_json, request_json)))
        return json.dumps({"decision": "continue", "spec": json.loads(spec_json), "request": json.loads(request_json)})

    def admit_exhaustion_work_json(policy_json: str, request_json: str) -> str:
        calls.append(("exhaustion", (policy_json, request_json)))
        return json.dumps({"allowed": True, "policy": json.loads(policy_json), "request": json.loads(request_json)})

    def validate_worker_advertisement_json(
        advertisement_json: str,
        expected_package_lock_hash: str | None = None,
    ) -> str:
        calls.append(("worker_advertisement", (advertisement_json, expected_package_lock_hash)))
        return json.dumps({"ok": True, "advertisement": json.loads(advertisement_json)})

    def validate_remote_payload_json(payload_json: str, max_inline_bytes: int) -> str:
        calls.append(("remote_payload", (payload_json, max_inline_bytes)))
        return json.dumps({"ok": True, "payload": json.loads(payload_json)})

    fake_native = SimpleNamespace(
        __version__="0.1.0",
        admit_exhaustion_work_json=admit_exhaustion_work_json,
        binding_version=lambda: "0.1.0",
        compile_graph_json=compile_graph_json,
        decide_agent_step_json=decide_agent_step_json,
        evaluate_declarative_output_policy_json=evaluate_declarative_output_policy_json,
        evaluate_output_gate_json=evaluate_output_gate_json,
        finalize_tool_call_json=finalize_tool_call_json,
        run_stdlib_graph_json=run_stdlib_graph_json,
        run_test_graph_json=run_test_graph_json,
        validate_remote_payload_json=validate_remote_payload_json,
        validate_worker_advertisement_json=validate_worker_advertisement_json,
    )
    runtime = load_runtime_wrapper(fake_native)

    compiled = runtime.compile_graph({"kind": "Graph"}, block_catalog=[{"typeId": "prompt.render"}])
    stdlib = runtime.run_stdlib_graph({"kind": "Graph"}, {"message": {"text": "hi"}})
    test_run = runtime.run_test_graph({"kind": "Graph"}, {"message": "hi"}, {"node": {"value": "ok"}})
    finalized = runtime.finalize_tool_call(
        {
            "toolCallId": "call-1",
            "responseId": "response-1",
            "toolName": "knowledge.search",
            "status": "arguments_complete",
            "argumentFragments": ["{}"],
            "sequence": 1,
        },
        resolved_tool_id="resolved-tool-1",
        created_at_unix_ms=1_000,
    )
    gate_result = runtime.evaluate_output_gate(
        {"streamId": "stream-1"},
        [{"op": "chunk", "chunk": {"sequence": 1}}],
    )
    policy_decision = runtime.evaluate_declarative_output_policy(
        [{"ruleId": "allow"}],
        {"streamId": "stream-1", "sequence": 1},
        evaluated_at_unix_ms=1_010,
    )
    agent_decision = runtime.decide_agent_step(
        {"maxSteps": 4},
        {"step": 2, "pendingToolCalls": 0},
    )
    exhaustion = runtime.admit_exhaustion_work(
        {"preset": "finish_current_turn"},
        {"workKind": "read_only_tool"},
    )
    worker = runtime.validate_worker_advertisement(
        {"workerId": "worker-1"},
        expected_package_lock_hash="sha256:lock",
    )
    remote_payload = runtime.validate_remote_payload(
        {"kind": "inline_json", "value": {"ok": True}},
        max_inline_bytes=128,
    )

    assert runtime.native_extension_available() is True
    assert compiled["ok"] is True
    assert stdlib["outputs"] == {"answer": "ok"}
    assert test_run["outputs"] == {"fixture": True}
    assert finalized == {
        "toolCallId": "call-1",
        "resolvedToolId": "resolved-tool-1",
        "createdAtUnixMs": 1_000,
    }
    assert gate_result == {
        "gate": {"streamId": "stream-1"},
        "updates": [{"op": "chunk", "chunk": {"sequence": 1}}],
    }
    assert policy_decision == {
        "disposition": "allow",
        "rules": [{"ruleId": "allow"}],
        "chunk": {"streamId": "stream-1", "sequence": 1},
        "evaluatedAtUnixMs": 1_010,
    }
    assert agent_decision == {
        "decision": "continue",
        "spec": {"maxSteps": 4},
        "request": {"step": 2, "pendingToolCalls": 0},
    }
    assert exhaustion == {
        "allowed": True,
        "policy": {"preset": "finish_current_turn"},
        "request": {"workKind": "read_only_tool"},
    }
    assert worker == {"ok": True, "advertisement": {"workerId": "worker-1"}}
    assert remote_payload == {"ok": True, "payload": {"kind": "inline_json", "value": {"ok": True}}}
    assert calls == [
        (
            "compile",
            (
                '{"kind":"Graph"}',
                '[{"typeId":"prompt.render"}]',
            ),
        ),
        ("run_stdlib", ('{"kind":"Graph"}', '{"message":{"text":"hi"}}')),
        ("run_test", ('{"kind":"Graph"}', '{"message":"hi"}', '{"node":{"value":"ok"}}')),
        (
            "finalize_tool",
            (
                '{"argumentFragments":["{}"],"responseId":"response-1","sequence":1,'
                '"status":"arguments_complete","toolCallId":"call-1","toolName":"knowledge.search"}',
                "resolved-tool-1",
                1_000,
            ),
        ),
        (
            "output_gate",
            ('{"streamId":"stream-1"}', '[{"chunk":{"sequence":1},"op":"chunk"}]'),
        ),
        (
            "output_policy",
            ('[{"ruleId":"allow"}]', '{"sequence":1,"streamId":"stream-1"}', 1_010),
        ),
        (
            "agent_step",
            ('{"maxSteps":4}', '{"pendingToolCalls":0,"step":2}'),
        ),
        (
            "exhaustion",
            ('{"preset":"finish_current_turn"}', '{"workKind":"read_only_tool"}'),
        ),
        (
            "worker_advertisement",
            ('{"workerId":"worker-1"}', "sha256:lock"),
        ),
        (
            "remote_payload",
            ('{"kind":"inline_json","value":{"ok":true}}', 128),
        ),
    ]
    assert "admit_exhaustion_work" in runtime.__all__
    assert "compile_graph" in runtime.__all__
    assert "decide_agent_step" in runtime.__all__
    assert "run_stdlib_graph" in runtime.__all__
    assert "run_test_graph" in runtime.__all__
    assert "finalize_tool_call" in runtime.__all__
    assert "evaluate_output_gate" in runtime.__all__
    assert "evaluate_declarative_output_policy" in runtime.__all__
    assert "validate_worker_advertisement" in runtime.__all__
    assert "validate_remote_payload" in runtime.__all__

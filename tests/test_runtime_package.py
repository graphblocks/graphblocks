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
    calls: list[tuple[str, tuple[str, ...]]] = []

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

    fake_native = SimpleNamespace(
        __version__="0.1.0",
        admit_exhaustion_work_json=lambda policy_json, request_json: "{}",
        binding_version=lambda: "0.1.0",
        compile_graph_json=compile_graph_json,
        decide_agent_step_json=lambda spec_json, request_json: "{}",
        evaluate_declarative_output_policy_json=lambda rules_json, chunk_json, evaluated_at_unix_ms: "{}",
        evaluate_output_gate_json=lambda gate_json, operations_json: "{}",
        finalize_tool_call_json=lambda draft_json, resolved_tool_id, created_at_unix_ms: "{}",
        run_stdlib_graph_json=run_stdlib_graph_json,
        run_test_graph_json=run_test_graph_json,
        validate_remote_payload_json=lambda payload_json, max_inline_bytes: "{}",
        validate_worker_advertisement_json=lambda advertisement_json, expected_package_lock_hash=None: "{}",
    )
    runtime = load_runtime_wrapper(fake_native)

    compiled = runtime.compile_graph({"kind": "Graph"}, block_catalog=[{"typeId": "prompt.render"}])
    stdlib = runtime.run_stdlib_graph({"kind": "Graph"}, {"message": {"text": "hi"}})
    test_run = runtime.run_test_graph({"kind": "Graph"}, {"message": "hi"}, {"node": {"value": "ok"}})

    assert runtime.native_extension_available() is True
    assert compiled["ok"] is True
    assert stdlib["outputs"] == {"answer": "ok"}
    assert test_run["outputs"] == {"fixture": True}
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
    ]
    assert "compile_graph" in runtime.__all__
    assert "run_stdlib_graph" in runtime.__all__
    assert "run_test_graph" in runtime.__all__

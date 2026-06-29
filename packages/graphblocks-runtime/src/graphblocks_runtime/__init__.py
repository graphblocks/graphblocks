from __future__ import annotations

import json

_NATIVE_EXTENSION_MODULE = "graphblocks_runtime._native"
_BINDING_CRATE = "graphblocks-python"

try:
    from ._native import (
        __version__,
        admit_exhaustion_work_json,
        binding_version,
        compile_graph_json,
        decide_agent_step_json,
        evaluate_declarative_output_policy_json,
        evaluate_output_gate_json,
        finalize_tool_call_json,
        run_stdlib_graph_json,
        run_test_graph_json,
        validate_remote_payload_json,
        validate_worker_advertisement_json,
    )

    _NATIVE_EXTENSION_AVAILABLE = True
    _NATIVE_EXTENSION_ERROR: ImportError | None = None
except ImportError as error:
    __version__ = "0.1.0"
    _NATIVE_EXTENSION_AVAILABLE = False
    _NATIVE_EXTENSION_ERROR = error

    def native_extension_available() -> bool:
        return _NATIVE_EXTENSION_AVAILABLE

    def native_extension_status() -> dict[str, bool | str | None]:
        return {
            "available": _NATIVE_EXTENSION_AVAILABLE,
            "binding_crate": _BINDING_CRATE,
            "binding_version": None,
            "module": _NATIVE_EXTENSION_MODULE,
            "error": str(_NATIVE_EXTENSION_ERROR),
        }

    def require_native_extension() -> None:
        raise RuntimeError(
            "graphblocks_runtime native extension is not built; build graphblocks-runtime "
            "with maturin so the single PyO3 binding crate graphblocks-python is installed "
            "as graphblocks_runtime._native"
        )

    def binding_version() -> str:
        return __version__

    def admit_exhaustion_work_json(policy_json: str, request_json: str) -> str:
        require_native_extension()

    def compile_graph_json(document_json: str, block_catalog_json: str | None = None) -> str:
        require_native_extension()

    def finalize_tool_call_json(
        draft_json: str,
        resolved_tool_id: str,
        created_at_unix_ms: int,
    ) -> str:
        require_native_extension()

    def decide_agent_step_json(spec_json: str, request_json: str) -> str:
        require_native_extension()

    def evaluate_output_gate_json(gate_json: str, operations_json: str) -> str:
        require_native_extension()

    def evaluate_declarative_output_policy_json(
        rules_json: str,
        chunk_json: str,
        evaluated_at_unix_ms: int,
    ) -> str:
        require_native_extension()

    def validate_worker_advertisement_json(
        advertisement_json: str,
        expected_package_lock_hash: str | None = None,
    ) -> str:
        require_native_extension()

    def validate_remote_payload_json(payload_json: str, max_inline_bytes: int) -> str:
        require_native_extension()

    def run_test_graph_json(graph_json: str, inputs_json: str, node_outputs_json: str) -> str:
        require_native_extension()

    def run_stdlib_graph_json(graph_json: str, inputs_json: str) -> str:
        require_native_extension()
else:

    def native_extension_available() -> bool:
        return _NATIVE_EXTENSION_AVAILABLE

    def native_extension_status() -> dict[str, bool | str | None]:
        return {
            "available": _NATIVE_EXTENSION_AVAILABLE,
            "binding_crate": _BINDING_CRATE,
            "binding_version": binding_version(),
            "module": _NATIVE_EXTENSION_MODULE,
            "error": None,
        }

    def require_native_extension() -> None:
        return None


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _json_object_result(result_json: str, label: str) -> dict[str, object]:
    payload = json.loads(result_json)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must decode to a JSON object")
    return payload


def compile_graph(document: dict[str, object], block_catalog: object | None = None) -> dict[str, object]:
    block_catalog_json = None if block_catalog is None else _canonical_json(block_catalog)
    return _json_object_result(
        compile_graph_json(_canonical_json(document), block_catalog_json),
        "native compiler result",
    )


def run_stdlib_graph(graph: dict[str, object], inputs: dict[str, object]) -> dict[str, object]:
    return _json_object_result(
        run_stdlib_graph_json(_canonical_json(graph), _canonical_json(inputs)),
        "native stdlib runtime result",
    )


def run_test_graph(
    graph: dict[str, object],
    inputs: dict[str, object],
    node_outputs: dict[str, object],
) -> dict[str, object]:
    return _json_object_result(
        run_test_graph_json(
            _canonical_json(graph),
            _canonical_json(inputs),
            _canonical_json(node_outputs),
        ),
        "native test runtime result",
    )


__all__ = [
    "__version__",
    "admit_exhaustion_work_json",
    "binding_version",
    "compile_graph",
    "compile_graph_json",
    "decide_agent_step_json",
    "evaluate_declarative_output_policy_json",
    "evaluate_output_gate_json",
    "finalize_tool_call_json",
    "native_extension_available",
    "native_extension_status",
    "require_native_extension",
    "run_stdlib_graph",
    "run_stdlib_graph_json",
    "run_test_graph",
    "run_test_graph_json",
    "validate_remote_payload_json",
    "validate_worker_advertisement_json",
]

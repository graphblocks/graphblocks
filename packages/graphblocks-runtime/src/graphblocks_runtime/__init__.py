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
        prepare_tool_result_for_model_json,
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

    def prepare_tool_result_for_model_json(
        call_json: str,
        result_json: str,
        resolved_tool_json: str,
        schema_registry_json: str,
        content_policy_json: str | None = None,
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


def finalize_tool_call(
    draft: dict[str, object],
    *,
    resolved_tool_id: str,
    created_at_unix_ms: int,
) -> dict[str, object]:
    return _json_object_result(
        finalize_tool_call_json(_canonical_json(draft), resolved_tool_id, created_at_unix_ms),
        "native finalized tool call",
    )


def prepare_tool_result_for_model(
    call: dict[str, object],
    result: dict[str, object],
    resolved_tool: dict[str, object],
    schema_registry: object,
    *,
    content_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    content_policy_json = None if content_policy is None else _canonical_json(content_policy)
    return _json_object_result(
        prepare_tool_result_for_model_json(
            _canonical_json(call),
            _canonical_json(result),
            _canonical_json(resolved_tool),
            _canonical_json(schema_registry),
            content_policy_json,
        ),
        "native prepared tool result",
    )


def decide_agent_step(spec: dict[str, object], request: dict[str, object]) -> dict[str, object]:
    return _json_object_result(
        decide_agent_step_json(_canonical_json(spec), _canonical_json(request)),
        "native agent step decision",
    )


def admit_exhaustion_work(policy: dict[str, object], request: dict[str, object]) -> dict[str, object]:
    return _json_object_result(
        admit_exhaustion_work_json(_canonical_json(policy), _canonical_json(request)),
        "native exhaustion admission result",
    )


def evaluate_output_gate(
    gate: dict[str, object],
    operations: object,
) -> dict[str, object]:
    return _json_object_result(
        evaluate_output_gate_json(_canonical_json(gate), _canonical_json(operations)),
        "native output gate result",
    )


def evaluate_declarative_output_policy(
    rules: object,
    chunk: dict[str, object],
    *,
    evaluated_at_unix_ms: int,
) -> dict[str, object]:
    return _json_object_result(
        evaluate_declarative_output_policy_json(
            _canonical_json(rules),
            _canonical_json(chunk),
            evaluated_at_unix_ms,
        ),
        "native output policy decision",
    )


def validate_worker_advertisement(
    advertisement: dict[str, object],
    *,
    expected_package_lock_hash: str | None = None,
) -> dict[str, object]:
    return _json_object_result(
        validate_worker_advertisement_json(_canonical_json(advertisement), expected_package_lock_hash),
        "native worker advertisement validation result",
    )


def validate_remote_payload(payload: dict[str, object], *, max_inline_bytes: int) -> dict[str, object]:
    return _json_object_result(
        validate_remote_payload_json(_canonical_json(payload), max_inline_bytes),
        "native remote payload validation result",
    )


__all__ = [
    "__version__",
    "admit_exhaustion_work",
    "admit_exhaustion_work_json",
    "binding_version",
    "compile_graph",
    "compile_graph_json",
    "decide_agent_step",
    "decide_agent_step_json",
    "evaluate_declarative_output_policy",
    "evaluate_declarative_output_policy_json",
    "evaluate_output_gate",
    "evaluate_output_gate_json",
    "finalize_tool_call",
    "finalize_tool_call_json",
    "native_extension_available",
    "native_extension_status",
    "prepare_tool_result_for_model",
    "prepare_tool_result_for_model_json",
    "require_native_extension",
    "run_stdlib_graph",
    "run_stdlib_graph_json",
    "run_test_graph",
    "run_test_graph_json",
    "validate_remote_payload",
    "validate_remote_payload_json",
    "validate_worker_advertisement",
    "validate_worker_advertisement_json",
]

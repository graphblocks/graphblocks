from __future__ import annotations

try:
    from ._native import (
        __version__,
        admit_exhaustion_work_json,
        binding_version,
        compile_graph_json,
        run_stdlib_graph_json,
        run_test_graph_json,
        validate_remote_payload_json,
        validate_worker_advertisement_json,
    )
except ImportError:
    __version__ = "0.1.0"

    def binding_version() -> str:
        return __version__

    def admit_exhaustion_work_json(policy_json: str, request_json: str) -> str:
        raise RuntimeError("graphblocks_runtime native extension is not built")

    def compile_graph_json(document_json: str) -> str:
        raise RuntimeError("graphblocks_runtime native extension is not built")

    def validate_worker_advertisement_json(
        advertisement_json: str,
        expected_package_lock_hash: str | None = None,
    ) -> str:
        raise RuntimeError("graphblocks_runtime native extension is not built")

    def validate_remote_payload_json(payload_json: str, max_inline_bytes: int) -> str:
        raise RuntimeError("graphblocks_runtime native extension is not built")

    def run_test_graph_json(graph_json: str, inputs_json: str, node_outputs_json: str) -> str:
        raise RuntimeError("graphblocks_runtime native extension is not built")

    def run_stdlib_graph_json(graph_json: str, inputs_json: str) -> str:
        raise RuntimeError("graphblocks_runtime native extension is not built")


__all__ = [
    "__version__",
    "admit_exhaustion_work_json",
    "binding_version",
    "compile_graph_json",
    "run_stdlib_graph_json",
    "run_test_graph_json",
    "validate_remote_payload_json",
    "validate_worker_advertisement_json",
]

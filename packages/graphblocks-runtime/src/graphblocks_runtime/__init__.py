from __future__ import annotations

try:
    from ._native import __version__, binding_version, compile_graph_json, run_test_graph_json
except ImportError:
    __version__ = "0.1.0"

    def binding_version() -> str:
        return __version__

    def compile_graph_json(document_json: str) -> str:
        raise RuntimeError("graphblocks_runtime native extension is not built")

    def run_test_graph_json(graph_json: str, inputs_json: str, node_outputs_json: str) -> str:
        raise RuntimeError("graphblocks_runtime native extension is not built")


__all__ = ["__version__", "binding_version", "compile_graph_json", "run_test_graph_json"]

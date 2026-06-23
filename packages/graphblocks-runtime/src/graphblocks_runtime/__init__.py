from __future__ import annotations

try:
    from ._native import __version__, binding_version, compile_graph_json
except ImportError:
    __version__ = "0.1.0"

    def binding_version() -> str:
        return __version__

    def compile_graph_json(document_json: str) -> str:
        raise RuntimeError("graphblocks_runtime native extension is not built")


__all__ = ["__version__", "binding_version", "compile_graph_json"]

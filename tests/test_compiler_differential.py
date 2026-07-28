from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphblocks.canonical import canonical_dumps
from graphblocks.compiler import compile_graph_reference
from graphblocks.plugins import BlockCatalog


graphblocks_runtime = pytest.importorskip(
    "graphblocks_runtime",
    reason="compiler differential tests require the native Rust binding",
)


def test_python_reference_matches_native_compiler_tck_contract() -> None:
    cases = json.loads(
        (Path(__file__).parents[1] / "tck" / "compiler" / "cases.json").read_text(
            encoding="utf-8"
        )
    )

    for case in cases:
        allow_unknown_blocks = "block_catalog" not in case
        raw_catalog = case.get("block_catalog", [])
        python_catalog = (
            BlockCatalog({}, allow_unknown_blocks=True)
            if allow_unknown_blocks
            else BlockCatalog.from_blocks(raw_catalog)
        )
        python_result = compile_graph_reference(
            case["document"],
            block_catalog=python_catalog,
            allow_unknown_blocks=allow_unknown_blocks,
        ).to_dict()
        rust_result = graphblocks_runtime.compile_graph(
            case["document"],
            block_catalog=raw_catalog,
            allow_unknown_blocks=allow_unknown_blocks,
        )

        assert python_result["ok"] == rust_result["ok"], case["name"]
        assert python_result["hash"] == rust_result["hash"], case["name"]
        assert canonical_dumps(python_result["graph"]) == canonical_dumps(
            rust_result["graph"]
        ), case["name"]
        assert [
            {
                "code": diagnostic["code"],
                "severity": diagnostic["severity"],
                "path": diagnostic["path"],
            }
            for diagnostic in python_result["diagnostics"]
        ] == [
            {
                "code": diagnostic["code"],
                "severity": diagnostic["severity"],
                "path": diagnostic["path"],
            }
            for diagnostic in rust_result["diagnostics"]
        ], case["name"]

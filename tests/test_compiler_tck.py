from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from graphblocks import compile_graph
from graphblocks.plugins import BlockCatalog


def test_python_compiler_matches_shared_tck_cases() -> None:
    cases = json.loads((Path(__file__).parents[1] / "tck" / "compiler" / "cases.json").read_text())

    for case in cases:
        block_catalog = None
        if "block_catalog" in case:
            block_catalog = BlockCatalog.from_blocks(case["block_catalog"])
        plan = compile_graph(case["document"], block_catalog=block_catalog)
        error_codes = [
            diagnostic.code for diagnostic in plan.diagnostics.diagnostics if diagnostic.severity == "error"
        ]
        warning_codes = [
            diagnostic.code for diagnostic in plan.diagnostics.diagnostics if diagnostic.severity == "warning"
        ]

        expected: dict[str, Any] = case["expected"]
        assert plan.graph_hash == expected["graph_hash"], case["name"]
        assert error_codes == expected["error_codes"], case["name"]
        assert warning_codes == expected.get("warning_codes", []), case["name"]

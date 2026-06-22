from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from graphblocks import compile_graph


def test_python_compiler_matches_shared_tck_cases() -> None:
    cases = json.loads((Path(__file__).parents[1] / "tck" / "compiler" / "cases.json").read_text())

    for case in cases:
        plan = compile_graph(case["document"])
        error_codes = [
            diagnostic.code for diagnostic in plan.diagnostics.diagnostics if diagnostic.severity == "error"
        ]

        expected: dict[str, Any] = case["expected"]
        assert plan.graph_hash == expected["graph_hash"], case["name"]
        assert error_codes == expected["error_codes"], case["name"]

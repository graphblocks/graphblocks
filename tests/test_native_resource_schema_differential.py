from __future__ import annotations

import json
from pathlib import Path

import pytest

import graphblocks_runtime
from graphblocks.schema import (
    resource_schema_errors_reference,
)


ROOT = Path(__file__).parents[1]
RESOURCE_CASES = json.loads(
    (ROOT / "tck/schema/resources.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", RESOURCE_CASES, ids=lambda case: case["name"])
def test_native_resource_schema_matches_shared_tck_and_python_reference(
    case: dict[str, object],
) -> None:
    document = case["document"]
    expected = case["expected"]
    assert isinstance(expected, dict)

    native_errors = graphblocks_runtime.resource_schema_errors(document)
    reference_errors = resource_schema_errors_reference(
        document,
        schema_root=ROOT / "schemas",
    )
    native_contracts = [
        {
            "code": error["code"],
            "path": error["path"],
            "keyword": error["keyword"],
        }
        for error in native_errors
    ]
    reference_contracts = [
        {
            "code": error.code,
            "path": error.path,
            "keyword": error.keyword,
        }
        for error in reference_errors
    ]

    assert (not native_errors) is expected["valid"]
    assert native_contracts == expected.get("errors", [])
    assert native_contracts == reference_contracts
    assert all(
        set(error) == {"code", "keyword", "message", "path", "schemaPath"}
        and all(type(value) is str for value in error.values())
        for error in native_errors
    )

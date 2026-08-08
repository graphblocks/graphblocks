from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

import graphblocks_runtime
from graphblocks.migration import (
    MigrationError,
    migrate_document_reference as migrate_document,
)


ROOT = Path(__file__).parents[1]
MIGRATION_CASES = json.loads(
    (ROOT / "tck/migration/cases.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", MIGRATION_CASES, ids=lambda case: case["name"])
def test_native_resource_migration_matches_shared_tck_and_python_reference(
    case: dict[str, object],
) -> None:
    document = case["document"]
    expected = case["expected"]
    assert isinstance(document, dict)
    assert isinstance(expected, dict)
    source = deepcopy(document)

    native = graphblocks_runtime.migrate_resource(document)
    if "error" in expected:
        expected_error = expected["error"]
        assert isinstance(expected_error, dict)
        assert native["ok"] is False
        native_error = native["error"]
        assert isinstance(native_error, dict)
        assert set(native_error) == {"code", "message", "path"}
        assert native_error["code"] == expected_error["code"]
        assert native_error["path"] == expected_error["path"]
        assert type(native_error["message"]) is str
        with pytest.raises(MigrationError) as captured:
            migrate_document(document)
        assert captured.value.code == expected_error["code"]
        assert captured.value.path == expected_error["path"]
    else:
        assert native == {"document": expected["document"], "ok": True}
        assert migrate_document(document) == expected["document"]

    assert document == source


def test_native_resource_migration_rejects_non_object_json() -> None:
    with pytest.raises(TypeError, match="must be a JSON object"):
        graphblocks_runtime.migrate_resource([])

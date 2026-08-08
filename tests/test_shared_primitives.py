from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Callable

import pytest

from graphblocks.approval import ApprovalRequest
from graphblocks.evaluation import ResourceSnapshotRef
from graphblocks.policy import PrincipalRef
from graphblocks.review import ReviewRequest


ROOT = Path(__file__).parents[1]


def _approval(metadata: object) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id="approval-1",
        run_id="run-1",
        subject=ResourceSnapshotRef("resource-1", "sha256:resource"),
        action="resource.update",
        arguments_digest="sha256:arguments",
        risk="external_effect",
        summary="Update resource",
        metadata=metadata,  # type: ignore[arg-type]
    )


def _review(metadata: object) -> ReviewRequest:
    return ReviewRequest(
        request_id="review-1",
        subject=ResourceSnapshotRef("resource-1", "sha256:resource"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality",),
        created_at="2026-08-08T00:00:00Z",
        metadata=metadata,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("build", (_approval, _review))
def test_metadata_entry_points_share_strict_recursive_snapshot_corpus(
    build: Callable[[object], object],
) -> None:
    source = {"scope": {"labels": ["reviewed"]}}
    model = build(source)
    source["scope"]["labels"].append("mutated")  # type: ignore[index, union-attr]

    assert model.metadata == {"scope": {"labels": ("reviewed",)}}  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        model.metadata["scope"]["labels"] = ("mutated",)  # type: ignore[attr-defined, index]

    with pytest.raises(ValueError, match="strict canonical JSON"):
        build({"invalid": object()})

    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    with pytest.raises(ValueError, match="must not contain cyclic values"):
        build(recursive)


def test_adapter_tool_factories_share_one_provider_neutral_constructor() -> None:
    graphblocks_client = importlib.import_module("graphblocks.client")
    graphblocks_mcp = importlib.import_module("graphblocks.integrations.mcp")
    graphblocks_openapi = importlib.import_module("graphblocks.integrations.openapi")
    kwargs = {
        "name": "knowledge.search",
        "description": "Search knowledge.",
        "input_schema": "schemas/SearchRequest@1",
        "output_schema": "schemas/SearchResult@1",
        "tags": ("search", "knowledge"),
        "version": "1.0.0",
    }

    definitions = (
        graphblocks_client.define_remote_tool(**kwargs),
        graphblocks_mcp.define_mcp_tool(**kwargs),
        graphblocks_openapi.define_openapi_tool(**kwargs),
    )

    assert definitions[0] == definitions[1] == definitions[2]
    assert definitions[0].tags == frozenset({"knowledge", "search"})


@pytest.mark.parametrize(
    ("module_name", "error_name"),
    (
        ("graphblocks.integrations.mcp", "McpToolAdapterError"),
        ("graphblocks.integrations.openapi", "OpenApiToolAdapterError"),
    ),
)
def test_integration_identity_validation_shares_wire_primitive(
    module_name: str,
    error_name: str,
) -> None:
    module = importlib.import_module(module_name)
    error_type = getattr(module, error_name)

    assert (
        module._stable_string(  # noqa: SLF001
            " schemas/Tool@1 ",
            owner="adapter",
            field_name="schema",
        )
        == "schemas/Tool@1"
    )
    with pytest.raises(error_type, match="control characters"):
        module._stable_string(  # noqa: SLF001
            "schemas/Tool\u0000@1",
            owner="adapter",
            field_name="schema",
        )


def test_reported_duplicate_helpers_cannot_return_to_adapter_modules() -> None:
    targets = {
        "src/graphblocks/approval.py": {"_freeze_metadata_value"},
        "src/graphblocks/review.py": {
            "_freeze_metadata_value",
            "_thaw_metadata_value",
        },
        "src/graphblocks/integrations/mcp.py": {"_stable_string"},
        "src/graphblocks/integrations/openapi.py": {"_stable_string"},
    }
    for relative, forbidden in targets.items():
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        definitions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert definitions.isdisjoint(forbidden), relative

    wrappers = {
        "src/graphblocks/client.py": "define_remote_tool",
        "src/graphblocks/integrations/mcp.py": "define_mcp_tool",
        "src/graphblocks/integrations/openapi.py": "define_openapi_tool",
    }
    for relative, wrapper_name in wrappers.items():
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        wrapper = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == wrapper_name
        )
        calls = {
            node.func.id
            for node in ast.walk(wrapper)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert calls == {"create_tool_definition"}

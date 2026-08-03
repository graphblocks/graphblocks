from __future__ import annotations

from pathlib import Path

import pytest

from graphblocks import load_documents
from graphblocks.loader import InputBudget


def test_graph_document_loader_rejects_duplicate_yaml_mapping_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "graph.yaml"
    path.write_text(
        "apiVersion: graphblocks.ai/v1\n"
        "kind: Graph\n"
        "metadata:\n"
        "  name: trusted\n"
        "  name: replaced\n"
        "spec: {nodes: {}, edges: []}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate YAML mapping key 'name'"):
        load_documents(path)


def test_load_documents_rejects_recursive_aliases(tmp_path: Path) -> None:
    path = tmp_path / "recursive.yaml"
    path.write_text("root: &root\n  child: *root\n", encoding="utf-8")

    with pytest.raises(ValueError, match="recursive"):
        load_documents(path)


def test_load_documents_rejects_overdeep_documents(tmp_path: Path) -> None:
    path = tmp_path / "deep.yaml"
    path.write_text(
        "root:\n"
        + "".join(f"{'  ' * depth}level_{depth}:\n" for depth in range(1, 66))
        + ("  " * 66)
        + "value: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="YAML document exceeds maximum depth 64"):
        load_documents(path)


def test_load_documents_wraps_parser_recursion_for_extreme_depth(
    tmp_path: Path,
) -> None:
    path = tmp_path / "parser-deep.yaml"
    path.write_text(
        "".join(f"{'  ' * depth}level_{depth}:\n" for depth in range(1_200))
        + ("  " * 1_200)
        + "value: true\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="invalid YAML: document nesting exceeds parser limit",
    ):
        load_documents(path)


@pytest.mark.parametrize(
    ("content", "message"),
    (
        ("1: value\n", "YAML mapping keys must be strings"),
        ("value: .nan\n", "YAML document numbers must be finite"),
        ("value: 2026-07-23\n", "YAML document values must be JSON-compatible"),
    ),
)
def test_load_documents_rejects_non_json_yaml_values(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    path = tmp_path / "non-json.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_documents(path)


@pytest.mark.parametrize(
    "content",
    (
        'value: "\\uD800"\n',
        '"\\uDFFF": value\n',
    ),
)
def test_load_documents_rejects_unicode_surrogates(
    tmp_path: Path,
    content: str,
) -> None:
    path = tmp_path / "surrogate.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="Unicode scalar values"):
        load_documents(path)


def test_load_documents_normalizes_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "invalid-utf8.yaml"
    path.write_bytes(b"value: \xff\n")

    with pytest.raises(
        ValueError,
        match=r"invalid-utf8\.yaml: invalid YAML: document is not UTF-8",
    ):
        load_documents(path)


def test_load_documents_stops_at_stream_document_budget(
    tmp_path: Path,
) -> None:
    path = tmp_path / "many-empty-documents.yaml"
    path.write_text("---\n" * 10_000, encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="YAML stream exceeds maximum document count 3",
    ):
        load_documents(path, budget=InputBudget(max_documents=3))


def test_load_documents_enforces_cumulative_node_budget(
    tmp_path: Path,
) -> None:
    path = tmp_path / "many-small-documents.yaml"
    path.write_text("value: 1\n---\nvalue: 2\n", encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="YAML stream exceeds maximum cumulative node count 3",
    ):
        load_documents(path, budget=InputBudget(max_cumulative_nodes=3))


def test_load_documents_rejects_input_before_unbounded_decode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "oversized.yaml"
    path.write_bytes(b"value: " + b"x" * 64)

    with pytest.raises(
        ValueError,
        match="YAML input exceeds maximum byte count 16",
    ):
        load_documents(path, budget=InputBudget(max_input_bytes=16))


@pytest.mark.parametrize("value", (0, -1, True, 1.5, "10"))
def test_input_budget_requires_exact_positive_integers(value: object) -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        InputBudget(max_documents=value)  # type: ignore[arg-type]

from __future__ import annotations

from pathlib import Path

import pytest

from graphblocks import load_documents


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

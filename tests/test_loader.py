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

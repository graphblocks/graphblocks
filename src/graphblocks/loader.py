from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_documents(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    with source.open("r", encoding="utf-8") as stream:
        documents = [document for document in yaml.safe_load_all(stream) if document is not None]
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            raise ValueError(f"{source}:{index + 1}: expected a YAML mapping document")
    return documents


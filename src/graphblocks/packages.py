from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml


def load_package_catalog(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        with resources.files("graphblocks").joinpath("data/package-catalog.yaml").open("r", encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    with Path(path).open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def package_rows(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for package in catalog.get("packages", []):
        if isinstance(package, dict):
            rows.append(
                {
                    "distribution": package.get("distribution"),
                    "import": package.get("import"),
                    "default": package.get("default", False),
                    "layer": package.get("layer"),
                    "kind": package.get("kind"),
                    "implementationPhase": package.get("implementationPhase"),
                    "stability": package.get("stability"),
                }
            )
    return sorted(rows, key=lambda item: str(item.get("distribution")))


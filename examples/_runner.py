from __future__ import annotations

import json
from pathlib import Path

import yaml

from graphblocks.cli import main as graphblocks_main

from _integration import run_integration


def run_example(path: Path) -> int:
    root = Path(__file__).resolve().parents[1]
    example_path = path.resolve()
    validate_args = ["validate", str(example_path)]
    plugin_manifest = example_path.with_name("graphblocks-plugin.yaml")
    if plugin_manifest.is_file():
        validate_args.extend(("--plugin-path", str(plugin_manifest)))
    result = graphblocks_main(validate_args)
    if result != 0:
        return result

    documents = list(yaml.safe_load_all(example_path.read_text(encoding="utf-8")))
    kinds = [str(document["kind"]) for document in documents]
    relative_path = example_path.relative_to(root)
    integration = run_integration(example_path)
    print(
        json.dumps(
            {
                "validated": relative_path.as_posix(),
                "kinds": kinds,
                "integration": integration,
            },
            sort_keys=True,
        )
    )
    return 0

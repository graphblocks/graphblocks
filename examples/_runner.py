from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path

import yaml

from graphblocks.cli import main as graphblocks_main

from _integration import run_integration


def run_example(
    path: Path,
    *,
    additional_evidence: Callable[[], Mapping[str, object]] | None = None,
) -> int:
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
    payload: dict[str, object] = {
        "validated": relative_path.as_posix(),
        "kinds": kinds,
        "integration": integration,
    }
    if additional_evidence is not None:
        evidence = dict(additional_evidence())
        reserved_keys = payload.keys() & evidence.keys()
        if reserved_keys:
            raise ValueError(
                f"additional example evidence uses reserved keys {sorted(reserved_keys)}"
            )
        payload.update(evidence)
    print(json.dumps(payload, sort_keys=True))
    return 0

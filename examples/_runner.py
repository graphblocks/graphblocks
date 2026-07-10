from __future__ import annotations

from pathlib import Path

import yaml

from graphblocks.cli import main as graphblocks_main


def run_example(path: Path) -> int:
    root = Path(__file__).resolve().parents[1]
    example_path = path.resolve()
    result = graphblocks_main(["validate", str(example_path)])
    if result != 0:
        return result

    documents = list(yaml.safe_load_all(example_path.read_text(encoding="utf-8")))
    kinds = [str(document["kind"]) for document in documents]
    relative_path = example_path.relative_to(root)
    print(f"validated {relative_path.as_posix()} ({', '.join(kinds)})")
    return 0

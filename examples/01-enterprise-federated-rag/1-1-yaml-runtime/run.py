from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import yaml

from graphblocks.canonical import canonical_dumps


VARIANT_ROOT = Path(__file__).parent
EXAMPLE_ROOT = VARIANT_ROOT.parent
sys.path.insert(0, str(EXAMPLE_ROOT))

from runtime_contract import normalize_runtime_result


def execute() -> dict[str, object]:
    graph_path = VARIANT_ROOT / "graph.yaml"
    inputs_json = (VARIANT_ROOT / "inputs.json").read_text(encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphblocks",
            "run",
            str(graph_path),
            "--input-json",
            inputs_json,
            "--run-id",
            "example-01-1-yaml",
        ],
        cwd=EXAMPLE_ROOT.parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    payload = json.loads(completed.stdout)
    graph = yaml.safe_load(graph_path.read_text(encoding="utf-8"))
    return normalize_runtime_result(payload, runtime="yaml-cli", graph=graph)


def main() -> int:
    print(canonical_dumps(execute()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

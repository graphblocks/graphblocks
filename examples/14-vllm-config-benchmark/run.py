from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _runner import run_example
from benchmark import run_benchmark


def main() -> int:
    example_root = Path(__file__).parent
    return run_example(
        example_root / "example.yaml",
        additional_evidence=lambda: {
            "benchmark": run_benchmark(example_root / "configs.yaml")
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())

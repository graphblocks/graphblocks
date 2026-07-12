from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _runner import run_example
from benchmark import run_benchmark


def main() -> int:
    return run_example(
        Path(__file__).with_name("example.yaml"),
        additional_evidence=lambda: {"benchmark": run_benchmark()},
    )


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _runner import run_example
from harness import run_harness


def main() -> int:
    example_root = Path(__file__).parent
    return run_example(
        example_root / "example.yaml",
        additional_evidence=lambda: {"harness": run_harness(example_root / "fixtures")},
    )


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _runner import run_example
from variants import run_variants


def main() -> int:
    example_root = Path(__file__).parent
    return run_example(
        example_root / "example.yaml",
        additional_evidence=lambda: {"runtimes": run_variants(example_root)},
    )


if __name__ == "__main__":
    raise SystemExit(main())

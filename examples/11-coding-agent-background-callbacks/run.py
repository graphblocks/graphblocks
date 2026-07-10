from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _runner import run_example


def main() -> int:
    return run_example(Path(__file__).with_name("example.yaml"))


if __name__ == "__main__":
    raise SystemExit(main())

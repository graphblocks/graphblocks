from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _runner import run_example
from model_selector import MODEL_CHOICES, model_selection_evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the TUI workspace assistant example")
    parser.add_argument(
        "--model",
        choices=MODEL_CHOICES,
        default="gpt",
        help="binding profile to select (default: gpt)",
    )
    args = parser.parse_args(argv)
    example_root = Path(__file__).parent
    return run_example(
        example_root / "example.yaml",
        additional_evidence=lambda: model_selection_evidence(example_root, args.model),
    )


if __name__ == "__main__":
    raise SystemExit(main())

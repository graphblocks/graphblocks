from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _test_support import assert_example_runner


def test_tui_workspace_assistant_example() -> None:
    assert_example_runner(Path(__file__).with_name("run.py"))

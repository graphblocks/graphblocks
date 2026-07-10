from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _test_support import assert_example_runner


def test_verified_rtl_workspace_trial_example() -> None:
    assert_example_runner(
        Path(__file__).with_name("run.py"),
        expected_checks={
            "acceptance:budget lease reservation check",
            "acceptance:review invalidation check",
            "acceptance:governed trial commit gate",
        },
        expected_boundaries={"EDA lease pool", "reviewer", "workspace store"},
    )

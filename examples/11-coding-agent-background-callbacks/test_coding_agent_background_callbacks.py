from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _test_support import assert_example_runner


def test_coding_agent_background_callbacks_example() -> None:
    assert_example_runner(
        Path(__file__).with_name("run.py"),
        expected_checks={
            "acceptance:accepted invocation handle check",
            "acceptance:cursor replay after detach",
            "acceptance:callback journal-before-resume check",
            "acceptance:signed webhook delivery check",
        },
        expected_boundaries={"CI callback", "secret resolver", "webhook transport"},
    )

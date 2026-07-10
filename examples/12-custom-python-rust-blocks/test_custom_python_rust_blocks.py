from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples"))

from _test_support import assert_example_runner


def test_custom_python_rust_blocks_example() -> None:
    payload = assert_example_runner(
        Path(__file__).with_name("run.py"),
        expected_checks={
            "plugin:validated",
            "schemas:manifest",
            "worker:python",
            "worker:rust",
            "worker:result-fences",
            "worker-graph:final-output",
        },
        expected_boundaries=set(),
    )

    integration = payload["integration"]
    assert integration["mockCalls"] == []
    assert integration["mockedBoundaries"] == []
    assert integration["executedBlocks"] == [
        "examples.python.normalize-text@1",
        "examples.rust.text-stats@1",
    ]
    assert [call["service"] for call in integration["workerCalls"]] == [
        "python-worker",
        "rust-worker",
    ]
    assert all(
        str(call["requestDigest"]).startswith("sha256:")
        and str(call["resultDigest"]).startswith("sha256:")
        for call in integration["workerCalls"]
    )

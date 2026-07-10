from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def assert_example_runner(script: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    expected = script.parent.relative_to(root) / "example.yaml"
    assert f"validated {expected.as_posix()}" in result.stdout

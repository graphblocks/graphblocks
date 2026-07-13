from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "typing"


def _run_mypy(fixture: str, cache_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(ROOT / "pyproject.toml"),
            "--cache-dir",
            str(cache_dir),
            str(FIXTURES / fixture),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_valid_typed_graph_passes_mypy(tmp_path: Path) -> None:
    completed = _run_mypy("valid_typed_graph.py", tmp_path / "valid-cache")

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_incompatible_typed_graph_fails_mypy(tmp_path: Path) -> None:
    completed = _run_mypy("incompatible_typed_graph.py", tmp_path / "invalid-cache")

    assert completed.returncode == 1
    assert "incompatible type" in completed.stdout
    assert completed.stdout.count(": error:") == 3
    assert 'Argument "query"' in completed.stdout
    assert 'Argument "sources"' in completed.stdout
    assert 'type parameter "T" of "publish"' in completed.stdout

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


def assert_example_runner(
    script: Path,
    *,
    expected_checks: set[str],
    expected_boundaries: set[str],
) -> dict[str, object]:
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
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload["validated"] == expected.as_posix()
    integration = payload["integration"]
    assert integration["ok"] is True
    assert set(integration["checks"]) >= expected_checks
    assert set(integration["mockedBoundaries"]) >= expected_boundaries
    assert str(integration["evidenceDigest"]).startswith("sha256:")
    for call in integration["mockCalls"]:
        assert str(call["inputDigest"]).startswith("sha256:")
    return payload

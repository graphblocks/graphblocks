"""Reconstructed GB-POL-001 command wrapper for the captured malformed inputs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = ROOT / "reproductions" / "original"


def main() -> int:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphblocks",
            "policy",
            "test",
            str(ORIGINAL / "policy-malformed.yaml"),
            "--cases",
            str(ORIGINAL / "policy-case.yaml"),
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = completed.stdout + completed.stderr
    if (
        completed.returncode != 1
        or "PolicyBundle.spec.rules[0].actions expected array of strings" not in output
        or '"passed": true' in output
    ):
        raise SystemExit("malformed policy did not fail closed")
    print("GB-POL-001 fixed: malformed action/resource objects are rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

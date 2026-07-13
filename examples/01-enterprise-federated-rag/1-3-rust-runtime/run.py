from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from graphblocks.canonical import canonical_dumps


VARIANT_ROOT = Path(__file__).parent


def execute() -> dict[str, object]:
    target_dir = Path(
        os.environ.get(
            "GRAPHBLOCKS_EXAMPLE_RUST_TARGET_DIR",
            str(Path(tempfile.gettempdir()) / "graphblocks-example-rust-target"),
        )
    )
    completed = subprocess.run(
        [
            "cargo",
            "run",
            "--quiet",
            "--locked",
            "--manifest-path",
            str(VARIANT_ROOT / "Cargo.toml"),
            "--target-dir",
            str(target_dir),
        ],
        cwd=VARIANT_ROOT.parents[2],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("Rust runtime response must be an object")
    return payload


def main() -> int:
    print(canonical_dumps(execute()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

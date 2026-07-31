#!/usr/bin/env python3
"""Generate the packaged Rust mirror of the canonical durable TCK fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PATH = Path("tck/durable/cases.json")
MANIFEST_PATH = Path("tck/durable/fixture-manifest.json")
MIRROR_PATHS = (
    Path("crates/graphblocks-runtime-durable/tests/fixtures/durable-cases.json"),
)


def _expected_outputs(root: Path) -> dict[Path, bytes]:
    source_path = root / CANONICAL_PATH
    if not source_path.is_file():
        raise FileNotFoundError(
            f"canonical durable TCK fixture is missing: {CANONICAL_PATH.as_posix()}"
        )
    source = source_path.read_bytes()
    digest = "sha256:" + hashlib.sha256(source).hexdigest()
    manifest = {
        "canonical": {
            "bytes": len(source),
            "path": CANONICAL_PATH.as_posix(),
            "sha256": digest,
        },
        "generatedBy": "tools/generate_durable_tck_fixture.py",
        "manifestVersion": 1,
        "mirrors": [
            {
                "bytes": len(source),
                "path": path.as_posix(),
                "sha256": digest,
            }
            for path in MIRROR_PATHS
        ],
    }
    rendered_manifest = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return {
        **{path: source for path in MIRROR_PATHS},
        MANIFEST_PATH: rendered_manifest,
    }


def check_outputs(root: Path = ROOT) -> tuple[Path, ...]:
    return tuple(
        path
        for path, expected in _expected_outputs(root).items()
        if not (root / path).is_file() or (root / path).read_bytes() != expected
    )


def write_outputs(root: Path = ROOT) -> None:
    for path, content in _expected_outputs(root).items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the Rust mirror or digest manifest is stale",
    )
    args = parser.parse_args()

    if args.check:
        stale = check_outputs()
        if stale:
            print(
                "durable TCK generated outputs are stale: "
                + ", ".join(path.as_posix() for path in stale)
                + "; run python tools/generate_durable_tck_fixture.py",
                file=sys.stderr,
            )
            return 1
        return 0

    write_outputs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

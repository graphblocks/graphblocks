from __future__ import annotations

from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).parents[1]
UPSTREAM = ROOT / "docs" / "upstream" / "GraphBlocks_v1.0_Final"


def test_upstream_bundle_sha256sums_match_current_files() -> None:
    expected: dict[str, str] = {}
    for line in (UPSTREAM / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, relative_path = line.split(maxsplit=1)
        expected[relative_path] = digest

    actual = {
        str(path.relative_to(UPSTREAM)): sha256(path.read_bytes()).hexdigest()
        for path in sorted(UPSTREAM.rglob("*"))
        if path.is_file() and path.name != "SHA256SUMS"
    }

    assert actual == expected

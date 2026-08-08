from __future__ import annotations

from graphblocks.canonical import canonical_hash


def test_canonical_hash_uses_sha256() -> None:
    assert canonical_hash({"mutation": "canonical"}) == (
        "sha256:fb491e7ae0cfb43965f35bfd4ab7135bdff85485072bbe023619282ad3f05c11"
    )

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from graphblocks.canonical import canonical_hash

from runtime_contract import EXPECTED_SEMANTIC_RESULT


def run_variants(example_root: Path) -> dict[str, object]:
    rust_target_dir = Path(
        os.environ.get(
            "GRAPHBLOCKS_EXAMPLE_RUST_TARGET_DIR",
            str(Path(tempfile.gettempdir()) / "graphblocks-example-rust-target"),
        )
    )
    variant_commands = {
        "1-1-yaml": [
            sys.executable,
            str(example_root / "1-1-yaml-runtime" / "run.py"),
        ],
        "1-2-python": [
            sys.executable,
            str(example_root / "1-2-python-runtime" / "run.py"),
        ],
        "1-3-rust": [
            "cargo",
            "run",
            "--quiet",
            "--locked",
            "--offline",
            "--manifest-path",
            str(example_root / "1-3-rust-runtime" / "Cargo.toml"),
            "--target-dir",
            str(rust_target_dir),
        ],
    }
    variants: dict[str, dict[str, object]] = {}
    for variant, command in variant_commands.items():
        completed = subprocess.run(
            command,
            cwd=example_root.parents[1],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"{variant} failed: {completed.stderr.strip() or completed.stdout.strip()}"
            )
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError(f"{variant} output must be an object")
        variants[variant] = payload

    semantic_results = {
        canonical_hash(payload.get("semanticResult")) for payload in variants.values()
    }
    graph_hashes = {payload.get("graphHash") for payload in variants.values()}
    grounding_results = {
        canonical_hash(payload.get("grounding")) for payload in variants.values()
    }
    node_orders = {
        canonical_hash(payload.get("succeededNodes")) for payload in variants.values()
    }
    statuses = {payload.get("status") for payload in variants.values()}
    parity = {
        "graphHash": len(graph_hashes) == 1,
        "grounding": len(grounding_results) == 1,
        "semanticResult": len(semantic_results) == 1,
        "status": len(statuses) == 1,
        "succeededNodeOrder": len(node_orders) == 1,
    }
    if not all(parity.values()):
        raise RuntimeError(f"runtime variants diverged: {parity}")
    if any(
        payload.get("semanticResult") != EXPECTED_SEMANTIC_RESULT
        for payload in variants.values()
    ):
        raise RuntimeError("runtime semantic result does not match the example contract")
    if any(
        payload.get("grounding") != {"issueCount": 0, "ok": True}
        for payload in variants.values()
    ):
        raise RuntimeError("runtime grounding result does not match the example contract")
    if statuses != {"succeeded"}:
        raise RuntimeError("runtime status does not match the example contract")
    evidence: dict[str, object] = {"parity": parity, "variants": variants}
    return {**evidence, "evidenceDigest": canonical_hash(evidence)}

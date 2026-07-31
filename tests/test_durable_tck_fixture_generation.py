from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[1]
GENERATOR_PATH = ROOT / "tools" / "generate_durable_tck_fixture.py"
CANONICAL_PATH = ROOT / "tck" / "durable" / "cases.json"
MANIFEST_PATH = ROOT / "tck" / "durable" / "fixture-manifest.json"
MIRROR_PATH = (
    ROOT
    / "crates"
    / "graphblocks-runtime-durable"
    / "tests"
    / "fixtures"
    / "durable-cases.json"
)


def test_durable_tck_generated_outputs_are_current(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(GENERATOR_PATH), "--check"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_durable_tck_manifest_binds_canonical_source_and_mirror() -> None:
    canonical = CANONICAL_PATH.read_bytes()
    mirror = MIRROR_PATH.read_bytes()
    digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert set(manifest) == {
        "canonical",
        "generatedBy",
        "manifestVersion",
        "mirrors",
    }
    assert manifest == {
        "canonical": {
            "bytes": len(canonical),
            "path": "tck/durable/cases.json",
            "sha256": digest,
        },
        "generatedBy": "tools/generate_durable_tck_fixture.py",
        "manifestVersion": 1,
        "mirrors": [
            {
                "bytes": len(mirror),
                "path": (
                    "crates/graphblocks-runtime-durable/tests/fixtures/"
                    "durable-cases.json"
                ),
                "sha256": "sha256:" + hashlib.sha256(mirror).hexdigest(),
            }
        ],
    }
    assert mirror == canonical


def test_durable_tck_generator_repairs_and_detects_mirror_drift(
    tmp_path: Path,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "generate_durable_tck_fixture",
        GENERATOR_PATH,
    )
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    canonical = b'[{"kind":"source_replay","name":"case"}]\n'
    canonical_path = tmp_path / generator.CANONICAL_PATH
    canonical_path.parent.mkdir(parents=True)
    canonical_path.write_bytes(canonical)

    generator.write_outputs(tmp_path)

    assert generator.check_outputs(tmp_path) == ()
    mirror_path = tmp_path / generator.MIRROR_PATHS[0]
    assert mirror_path.read_bytes() == canonical
    mirror_path.write_bytes(b"stale\n")
    assert generator.check_outputs(tmp_path) == (generator.MIRROR_PATHS[0],)

    generator.write_outputs(tmp_path)
    manifest_path = tmp_path / generator.MANIFEST_PATH
    manifest_path.write_text("{}\n", encoding="utf-8")
    assert generator.check_outputs(tmp_path) == (generator.MANIFEST_PATH,)

    generator.write_outputs(tmp_path)

    assert generator.check_outputs(tmp_path) == ()

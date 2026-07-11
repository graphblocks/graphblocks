from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from graphblocks.schema import SchemaManifest


@pytest.mark.parametrize("installed_output_kind", ("incomplete", "malformed"))
def test_wheelhouse_gate_rejects_invalid_installed_schema_manifest(
    monkeypatch,
    tmp_path,
    installed_output_kind: str,
) -> None:
    module_path = Path(__file__).parents[1] / "tools" / "verify_wheelhouse.py"
    spec = importlib.util.spec_from_file_location("verify_wheelhouse_schema", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    root = tmp_path / "repo"
    for manifest_path, distribution in (
        (root / "pyproject.toml", "graphblocks"),
        (
            root / "packages" / "graphblocks-runtime" / "pyproject.toml",
            "graphblocks-runtime",
        ),
        (
            root / "packages" / "graphblocks-testing" / "pyproject.toml",
            "graphblocks-testing",
        ),
    ):
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            f'[project]\nname = "{distribution}"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
    schema_root = root / "schemas"
    schema_root.mkdir()
    for name in ("first", "second"):
        (schema_root / f"{name}.schema.json").write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": f"example.com/{name}.schema.json",
                    "title": name.title(),
                    "type": "object",
                }
            ),
            encoding="utf-8",
        )
    subset_root = tmp_path / "installed-schemas"
    subset_root.mkdir()
    (subset_root / "first.schema.json").write_text(
        (schema_root / "first.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    installed_payload = SchemaManifest.from_directory(subset_root).manifest_payload()
    installed_output = (
        json.dumps(installed_payload)
        if installed_output_kind == "incomplete"
        else "{not-json"
    )

    class FakeEnvBuilder:
        def __init__(self, *, with_pip: bool) -> None:
            assert with_pip

        def create(self, path: str) -> None:
            (Path(path) / "bin").mkdir(parents=True)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "build" in command and "--outdir" in command:
            output_root = Path(command[command.index("--outdir") + 1])
            manifest_root = Path(command[-1])
            distribution = "graphblocks" if manifest_root == root else manifest_root.name
            (output_root / f"{distribution}-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
        if command[-4:] == ["-m", "graphblocks", "schemas", "manifest"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=installed_output,
            )
        if command[-3:] == ["pip", "list", "--format=json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    [
                        {"name": "graphblocks", "version": "0.1.0"},
                        {"name": "graphblocks-runtime", "version": "0.1.0"},
                        {"name": "graphblocks-testing", "version": "0.1.0"},
                    ]
                ),
            )
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module.venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="installed schema manifest"):
        module.main(["--wheelhouse", str(tmp_path / "wheelhouse")])

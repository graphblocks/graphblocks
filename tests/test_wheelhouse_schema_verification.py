from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
from types import ModuleType, SimpleNamespace

import pytest

from graphblocks.schema import SchemaManifest


def _load_wheelhouse_module() -> ModuleType:
    module_path = Path(__file__).parents[1] / "tools" / "verify_wheelhouse.py"
    spec = importlib.util.spec_from_file_location("verify_wheelhouse_schema", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("installed_output_kind", ("incomplete", "malformed"))
def test_wheelhouse_gate_rejects_invalid_installed_schema_manifest(
    monkeypatch,
    tmp_path,
    installed_output_kind: str,
) -> None:
    module = _load_wheelhouse_module()

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
    monkeypatch.setattr(
        module,
        "build_wheel_matrix",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            targets=(
                SimpleNamespace(manifest="pyproject.toml"),
                SimpleNamespace(manifest="packages/graphblocks-runtime/pyproject.toml"),
                SimpleNamespace(manifest="packages/graphblocks-testing/pyproject.toml"),
            ),
            diagnostics=(),
        ),
    )
    monkeypatch.setattr(module.venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="installed schema manifest"):
        module.main(["--wheelhouse", str(tmp_path / "wheelhouse")])


def test_wheelhouse_gate_uses_pep503_distribution_identity(monkeypatch, tmp_path) -> None:
    module = _load_wheelhouse_module()
    expected_schema = SchemaManifest.from_directory(module.ROOT / "schemas").manifest_payload()

    class FakeEnvBuilder:
        def __init__(self, *, with_pip: bool) -> None:
            assert with_pip

        def create(self, path: str) -> None:
            (Path(path) / "bin").mkdir(parents=True)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "build" in command and "--outdir" in command:
            output_root = Path(command[command.index("--outdir") + 1])
            manifest_root = Path(command[-1])
            project = module.tomllib.loads(
                (manifest_root / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]
            wheel_name = str(project["name"]).replace("-", "_")
            (output_root / f"{wheel_name}-{project['version']}-py3-none-any.whl").write_bytes(
                b"wheel"
            )
        if command[-4:] == ["-m", "graphblocks", "schemas", "manifest"]:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(expected_schema))
        if command[-3:] == ["pip", "list", "--format=json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    [
                        {"name": "GraphBlocks", "version": "0.1.0"},
                        {"name": "GraphBlocks_Runtime", "version": "0.1.0"},
                        {"name": "GraphBlocks.Testing", "version": "0.1.0"},
                    ]
                ),
            )
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(module.venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main(["--wheelhouse", str(tmp_path / "wheelhouse")]) == 0


def test_wheelhouse_gate_derives_build_targets_from_package_catalog(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_wheelhouse_module()
    root = tmp_path / "repo"
    manifest = root / "custom" / "pyproject.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('[project]\nname = "custom-wheel"\nversion = "0.1.0"\n', encoding="utf-8")
    catalog = {"catalogVersion": 1}
    matrix = SimpleNamespace(
        ok=True,
        targets=(SimpleNamespace(manifest="custom/pyproject.toml"),),
        diagnostics=(),
    )
    matrix_calls: list[tuple[Path, object]] = []

    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "load_package_catalog", lambda: catalog, raising=False)

    def fake_build_wheel_matrix(path: Path, *, catalog: object) -> object:
        matrix_calls.append((path, catalog))
        return matrix

    monkeypatch.setattr(module, "build_wheel_matrix", fake_build_wheel_matrix, raising=False)

    class ExpectedStop(Exception):
        pass

    def stop_after_first_build(command: list[str], **kwargs: object) -> None:
        assert Path(command[-1]) == manifest.parent
        raise ExpectedStop

    monkeypatch.setattr(module.subprocess, "run", stop_after_first_build)

    with pytest.raises(ExpectedStop):
        module.main(["--wheelhouse", str(tmp_path / "wheelhouse")])
    assert matrix_calls == [(root, catalog)]

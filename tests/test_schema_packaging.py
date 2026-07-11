from __future__ import annotations

import json
from pathlib import Path
import tomllib

import graphblocks.cli as cli_module
from graphblocks.cli import main


ROOT = Path(__file__).parents[1]


def test_wheel_places_schemas_inside_graphblocks_package() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"] == {
        "schemas": "graphblocks/schemas"
    }


def test_schema_manifest_defaults_to_packaged_schemas(tmp_path, monkeypatch, capsys) -> None:
    package_root = tmp_path / "installed-graphblocks"
    schema_root = package_root / "schemas" / "graphblocks.ai" / "v1alpha1"
    schema_root.mkdir(parents=True)
    (schema_root / "example.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "graphblocks.ai/v1alpha1/example.schema.json",
                "type": "object",
            }
        ),
        encoding="utf-8",
    )
    working_directory = tmp_path / "outside-repository"
    working_directory.mkdir()
    monkeypatch.chdir(working_directory)
    monkeypatch.setattr(cli_module.resources, "files", lambda package: package_root)

    assert main(["schemas", "manifest"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [entry["schemaId"] for entry in payload["schemas"]] == [
        "graphblocks.ai/v1alpha1/example.schema.json"
    ]

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
import venv

from packaging.utils import canonicalize_name

from graphblocks.packages import build_wheel_matrix, load_package_catalog
from graphblocks.schema import SchemaManifest


ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and verify the catalog's first-party Python distributions offline."
    )
    parser.add_argument("--wheelhouse", type=Path, required=True)
    args = parser.parse_args(argv)

    wheelhouse = args.wheelhouse.resolve()
    wheelhouse.mkdir(parents=True, exist_ok=True)
    if any(wheelhouse.glob("*.whl")):
        raise ValueError("wheelhouse must not contain existing wheel artifacts")
    build_environment = dict(os.environ)
    build_environment["PATH"] = (
        f"{Path(sys.executable).absolute().parent}{os.pathsep}"
        f"{build_environment.get('PATH', '')}"
    )
    catalog = load_package_catalog()
    matrix = build_wheel_matrix(ROOT, catalog=catalog)
    if not matrix.ok:
        raise RuntimeError(f"first-party wheel matrix is invalid: {matrix.diagnostics!r}")
    manifests = tuple(ROOT / target.manifest for target in matrix.targets)
    expected_distributions: dict[str, str] = {}
    for manifest in manifests:
        project = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]
        distribution = str(project["name"])
        version = str(project["version"])
        expected_distributions[canonicalize_name(distribution)] = version
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(wheelhouse),
                str(manifest.parent),
            ],
            check=True,
            cwd=ROOT,
            env=build_environment,
        )

    built_wheels = tuple(sorted(wheelhouse.glob("*.whl")))
    if len(built_wheels) != len(manifests):
        raise RuntimeError(
            f"expected {len(manifests)} first-party wheel artifacts, found {len(built_wheels)}"
        )

    with TemporaryDirectory(prefix="graphblocks-wheel-download-") as download_root:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--dest",
                download_root,
                *(str(wheel) for wheel in built_wheels),
            ],
            check=True,
            cwd=ROOT,
        )
        for downloaded_wheel in sorted(Path(download_root).glob("*.whl")):
            destination = wheelhouse / downloaded_wheel.name
            if not destination.exists():
                shutil.copy2(downloaded_wheel, destination)

    expected_schema_manifest = SchemaManifest.from_directory(ROOT / "schemas").manifest_payload()
    with TemporaryDirectory(prefix="graphblocks-wheelhouse-") as install_root:
        venv.EnvBuilder(with_pip=True).create(install_root)
        isolated_python = Path(install_root) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        install_environment = dict(os.environ)
        install_environment.pop("PYTHONHOME", None)
        install_environment.pop("PYTHONPATH", None)
        subprocess.run(
            [
                str(isolated_python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                *(str(wheel) for wheel in built_wheels),
            ],
            check=True,
            cwd=ROOT,
            env=install_environment,
        )
        subprocess.run(
            [str(isolated_python), "-m", "pip", "check"],
            check=True,
            cwd=ROOT,
            env=install_environment,
        )
        subprocess.run(
            [
                str(isolated_python),
                "-c",
                (
                    "import importlib; "
                    "from graphblocks.packages import load_package_catalog; "
                    "import graphblocks, graphblocks_runtime, graphblocks_testing; "
                    "importlib.import_module('graphblocks_runtime._native'); "
                    "catalog = load_package_catalog(); "
                    "[importlib.import_module(item['import']) for item in catalog['components'] if item.get('import')]"
                ),
            ],
            check=True,
            cwd=ROOT,
            env=install_environment,
        )
        installed_schema_manifest = subprocess.run(
            [str(isolated_python), "-m", "graphblocks", "schemas", "manifest"],
            check=True,
            cwd=install_root,
            env=install_environment,
            capture_output=True,
            text=True,
        )
        try:
            installed_schema_payload = json.loads(installed_schema_manifest.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("installed schema manifest is not valid JSON") from error
        if installed_schema_payload != expected_schema_manifest:
            raise RuntimeError(
                "installed schema manifest does not match the checked-in source manifest"
            )
        installed = subprocess.run(
            [str(isolated_python), "-m", "pip", "list", "--format=json"],
            check=True,
            cwd=ROOT,
            capture_output=True,
            env=install_environment,
            text=True,
        )
        installed_distributions = {
            canonicalize_name(str(distribution["name"])): str(distribution["version"])
            for distribution in json.loads(installed.stdout)
        }
        installed_first_party = {
            distribution: installed_distributions.get(distribution)
            for distribution in expected_distributions
        }
        if installed_first_party != expected_distributions:
            raise RuntimeError(
                "offline wheelhouse installation did not install all first-party distributions: "
                f"expected {expected_distributions!r}, observed {installed_first_party!r}"
            )

    print(f"verified {len(manifests)} first-party wheels in {wheelhouse}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

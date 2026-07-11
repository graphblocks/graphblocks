from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
import venv

from graphblocks.schema import SchemaManifest


ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and verify every first-party Python distribution offline."
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
    manifests = [ROOT / "pyproject.toml", *sorted((ROOT / "packages").glob("*/pyproject.toml"))]
    expected_distributions: dict[str, str] = {}
    for manifest in manifests:
        project = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]
        distribution = str(project["name"])
        version = str(project["version"])
        expected_distributions[distribution.lower().replace("_", "-")] = version
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

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            str(wheelhouse),
            "PyYAML>=6.0",
            "packaging>=24.0",
        ],
        check=True,
        cwd=ROOT,
    )
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
                *(
                    f"{distribution}=={version}"
                    for distribution, version in sorted(expected_distributions.items())
                ),
            ],
            check=True,
            cwd=ROOT,
        )
        subprocess.run(
            [str(isolated_python), "-m", "pip", "check"],
            check=True,
            cwd=ROOT,
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
            text=True,
        )
        installed_distributions = {
            str(distribution["name"]).lower().replace("_", "-"): str(distribution["version"])
            for distribution in json.loads(installed.stdout)
        }
        installed_first_party = {
            distribution: installed_distributions.get(distribution)
            for distribution in expected_distributions
        }
        if installed_first_party != expected_distributions:
            raise RuntimeError(
                "offline wheelhouse installation did not install every first-party distribution: "
                f"expected {expected_distributions!r}, observed {installed_first_party!r}"
            )

    print(f"verified {len(manifests)} first-party wheels in {wheelhouse}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

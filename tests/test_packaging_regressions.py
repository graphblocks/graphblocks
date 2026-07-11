from __future__ import annotations

import pytest

from graphblocks.packages import build_wheel_matrix


@pytest.mark.parametrize(
    ("requires_python", "python_versions", "expected_versions"),
    [
        (">=3.11,!=3.12.*", ("3.11", "3.12"), ("3.11",)),
        ("~=3.11.0", ("3.11", "3.12", "3.13"), ("3.11",)),
        ("===3.11", ("3.11", "3.12"), ("3.11",)),
    ],
)
def test_wheel_matrix_honors_pep440_python_constraints(
    tmp_path,
    requires_python: str,
    python_versions: tuple[str, ...],
    expected_versions: tuple[str, ...],
) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        f"""
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "selected"
version = "0.1.0"
requires-python = "{requires_python}"

[tool.hatch.build.targets.wheel]
packages = ["src/selected"]
""".strip(),
        encoding="utf-8",
    )
    matrix = build_wheel_matrix(
        tmp_path,
        python_versions=python_versions,
        catalog={
            "artifacts": [
                {
                    "distribution": "selected",
                    "kind": "pure_python",
                    "manifest": "pyproject.toml",
                }
            ]
        },
    )

    assert not matrix.ok
    assert [item.code for item in matrix.diagnostics] == ["WheelPythonVersionUnsupported"]
    assert matrix.targets[0].python_versions == expected_versions

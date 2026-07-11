from __future__ import annotations

import graphblocks.packages as packages


def test_wheel_matrix_reports_invalid_requires_python_constraint(
    monkeypatch,
    tmp_path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "selected"
version = "0.1.0"
requires-python = ">=3.11,definitely-not-a-version"

[tool.hatch.build.targets.wheel]
packages = ["src/selected"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        packages,
        "load_package_catalog",
        lambda: {
            "artifacts": [
                {
                    "distribution": "selected",
                    "kind": "pure_python",
                    "manifest": "pyproject.toml",
                }
            ]
        },
    )

    matrix = packages.build_wheel_matrix(
        tmp_path,
        python_versions=("3.11", "3.12"),
    )

    assert [(item.code, item.path) for item in matrix.diagnostics] == [
        ("WheelPythonRequiresInvalid", "$.pyproject.toml.project.requires-python")
    ]
    assert matrix.targets == ()

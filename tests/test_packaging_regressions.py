from __future__ import annotations

import pytest

from graphblocks.packages import (
    PackageManifestAuditPolicy,
    audit_package_manifests,
    build_wheel_matrix,
)


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


@pytest.mark.parametrize("dependency", ("Vulnerable.SDK>=1", "vulnerable--sdk>=1"))
def test_package_manifest_audit_canonicalizes_blocked_dependency_names(
    tmp_path,
    dependency: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"""
[project]
name = "unsafe-package"
version = "0.1.0"
license = "Apache-2.0"
dependencies = ["{dependency}"]
""".strip(),
        encoding="utf-8",
    )

    diagnostics = audit_package_manifests(
        tmp_path,
        policy=PackageManifestAuditPolicy(blocked_dependencies=("vulnerable_sdk",)),
    )

    assert [(item.code, item.path) for item in diagnostics.diagnostics] == [
        ("PackageBlockedDependency", "$.pyproject.toml.project.dependencies[0]")
    ]


def test_package_manifest_audit_policy_deduplicates_canonical_dependency_names() -> None:
    policy = PackageManifestAuditPolicy(
        blocked_dependencies=(
            "Vulnerable.SDK",
            "vulnerable--sdk",
            "vulnerable_sdk",
        )
    )

    assert policy.blocked_dependencies == ("vulnerable-sdk",)

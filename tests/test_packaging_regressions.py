from __future__ import annotations

import pytest

from graphblocks.packages import (
    PackageManifestAuditPolicy,
    _supports_python_version,
    audit_package_manifests,
    build_wheel_matrix,
    doctor_package_catalog,
)


def _write_valid_wheel_manifest(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "escaped-wheel"
version = "0.1.0"
license = "Apache-2.0"
requires-python = ">=3.11"

[tool.hatch.build.targets.wheel]
packages = ["src/escaped_wheel"]
""".strip(),
        encoding="utf-8",
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
    requires_python: str,
    python_versions: tuple[str, ...],
    expected_versions: tuple[str, ...],
) -> None:
    assert tuple(
        version
        for version in python_versions
        if _supports_python_version(requires_python, version)
    ) == expected_versions


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


def test_wheel_matrix_rejects_manifest_outside_root(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside_manifest = tmp_path / "outside" / "pyproject.toml"
    _write_valid_wheel_manifest(outside_manifest)
    (root / "linked-outside").symlink_to(outside_manifest.parent, target_is_directory=True)

    for manifest_ref in (
        "../outside/pyproject.toml",
        str(outside_manifest),
        "linked-outside/pyproject.toml",
    ):
        catalog = {
            "artifacts": [
                {
                    "distribution": "escaped-wheel",
                    "kind": "pure_python",
                    "manifest": manifest_ref,
                }
            ]
        }

        matrix = build_wheel_matrix(root, catalog=catalog)

        assert matrix.targets == ()
        assert [(item.code, item.path) for item in matrix.diagnostics] == [
            ("WheelManifestOutsideRoot", "$.artifacts[0].manifest")
        ]


def test_wheel_matrix_reports_malformed_manifest_path(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    matrix = build_wheel_matrix(
        root,
        catalog={
            "artifacts": [
                {
                    "distribution": "escaped-wheel",
                    "kind": "pure_python",
                    "manifest": "invalid\0manifest",
                }
            ]
        },
    )

    assert matrix.targets == ()
    assert [(item.code, item.path) for item in matrix.diagnostics] == [
        ("WheelManifestInvalid", "$.artifacts[0].manifest")
    ]


def test_wheel_matrix_canonicalizes_manifest_aliases(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_valid_wheel_manifest(root / "pyproject.toml")

    direct = build_wheel_matrix(
        root,
        catalog={
            "artifacts": [
                {
                    "distribution": "escaped-wheel",
                    "kind": "pure_python",
                    "manifest": "pyproject.toml",
                }
            ]
        },
    )
    aliased = build_wheel_matrix(
        root,
        catalog={
            "artifacts": [
                {
                    "distribution": "escaped-wheel",
                    "kind": "pure_python",
                    "manifest": "missing/../pyproject.toml",
                }
            ]
        },
    )

    assert aliased.targets[0].manifest == "pyproject.toml"
    assert aliased.content_digest() == direct.content_digest()


def test_wheel_matrix_rejects_duplicate_canonical_manifest_aliases(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_valid_wheel_manifest(root / "pyproject.toml")

    matrix = build_wheel_matrix(
        root,
        catalog={
            "artifacts": [
                {
                    "distribution": "escaped-wheel",
                    "kind": "pure_python",
                    "manifest": "pyproject.toml",
                },
                {
                    "distribution": "escaped_wheel",
                    "kind": "pure_python",
                    "manifest": "./pyproject.toml",
                },
            ]
        },
    )

    assert len(matrix.targets) == 1
    assert [(item.code, item.path) for item in matrix.diagnostics] == [
        ("WheelManifestDuplicate", "$.artifacts[1].manifest")
    ]


def test_wheel_matrix_does_not_follow_manifest_swapped_after_resolution(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    manifest_path = root / "pyproject.toml"
    _write_valid_wheel_manifest(manifest_path)
    outside_manifest = tmp_path / "outside.toml"
    outside_manifest.write_text("invalid = [", encoding="utf-8")
    original_read_text = type(manifest_path).read_text

    def swap_before_read(path, *args, **kwargs):
        if path == manifest_path:
            path.unlink()
            path.symlink_to(outside_manifest)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(manifest_path), "read_text", swap_before_read)

    matrix = build_wheel_matrix(
        root,
        catalog={
            "artifacts": [
                {
                    "distribution": "escaped-wheel",
                    "kind": "pure_python",
                    "manifest": "pyproject.toml",
                }
            ]
        },
    )

    assert len(matrix.targets) == 1
    assert matrix.diagnostics == ()


def test_package_catalog_doctor_rejects_manifest_outside_root(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside_manifest = tmp_path / "outside" / "pyproject.toml"
    _write_valid_wheel_manifest(outside_manifest)
    (root / "linked-outside").symlink_to(outside_manifest.parent, target_is_directory=True)

    for manifest_ref in (
        "../outside/pyproject.toml",
        str(outside_manifest),
        "linked-outside/pyproject.toml",
    ):
        catalog = {
            "catalogVersion": 1,
            "specVersion": "1.0",
            "artifacts": [
                {
                    "distribution": "escaped-wheel",
                    "kind": "pure_python",
                    "manifest": manifest_ref,
                    "dependsOn": [],
                }
            ],
            "components": [],
            "defaultSelection": {
                "artifacts": [],
                "components": [],
                "excludedCategories": [],
            },
            "extensionComponents": {},
        }

        diagnostics = doctor_package_catalog(catalog, root=root)

        assert (
            "PackageArtifactManifestOutsideRoot",
            "$.artifacts.escaped-wheel.manifest",
        ) in [(item.code, item.path) for item in diagnostics.diagnostics]


def test_package_catalog_doctor_reports_malformed_manifest_path(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    catalog = {
        "catalogVersion": 1,
        "specVersion": "1.0",
        "artifacts": [
            {
                "distribution": "escaped-wheel",
                "kind": "pure_python",
                "manifest": "invalid\0manifest",
                "dependsOn": [],
            }
        ],
        "components": [],
        "defaultSelection": {
            "artifacts": [],
            "components": [],
            "excludedCategories": [],
        },
        "extensionComponents": {},
    }

    diagnostics = doctor_package_catalog(catalog, root=root)

    assert (
        "PackageArtifactManifestInvalid",
        "$.artifacts.escaped-wheel.manifest",
    ) in [(item.code, item.path) for item in diagnostics.diagnostics]

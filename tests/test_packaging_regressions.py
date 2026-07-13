from __future__ import annotations

import os

import pytest

import graphblocks.packages as packages_module
from graphblocks.packages import (
    PackageManifestAuditPolicy,
    _supports_python_version,
    audit_package_manifests,
    build_wheel_matrix,
    doctor_package_catalog,
)


DESCRIPTOR_RELATIVE_OPEN_CASES = (
    pytest.param(
        True,
        id="descriptor-relative",
        marks=pytest.mark.skipif(
            not (
                bool(getattr(os, "O_NOFOLLOW", 0))
                and os.open in getattr(os, "supports_dir_fd", ())
            ),
            reason="descriptor-relative O_NOFOLLOW opens are unavailable",
        ),
    ),
    pytest.param(False, id="fallback"),
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


def test_wheel_matrix_reads_manifest_without_descriptor_relative_open(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_valid_wheel_manifest(root / "pyproject.toml")
    original_open = os.open

    def open_without_dir_fd(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise NotImplementedError("dir_fd is unavailable")
        return original_open(path, flags, mode)

    monkeypatch.setattr(packages_module.os, "open", open_without_dir_fd)
    monkeypatch.setattr(packages_module.os, "supports_dir_fd", {open_without_dir_fd})

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


def test_wheel_matrix_reads_manifest_without_no_follow_flag(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_valid_wheel_manifest(root / "pyproject.toml")
    original_open = os.open

    def open_without_no_follow(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise AssertionError("descriptor-relative traversal requires O_NOFOLLOW")
        return original_open(path, flags, mode)

    monkeypatch.setattr(packages_module.os, "open", open_without_no_follow)
    monkeypatch.setattr(packages_module.os, "supports_dir_fd", {open_without_no_follow})
    monkeypatch.delattr(packages_module.os, "O_NOFOLLOW", raising=False)

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


def test_wheel_matrix_fallback_rejects_parent_swapped_to_outside_symlink(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "repo"
    manifest_path = root / "package" / "pyproject.toml"
    _write_valid_wheel_manifest(manifest_path)
    outside_directory = tmp_path / "outside"
    outside_manifest = outside_directory / "pyproject.toml"
    _write_valid_wheel_manifest(outside_manifest)
    original_open = os.open
    original_fdopen = os.fdopen
    fallback_opened = False
    outside_manifest_read = False

    def swap_before_fallback_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal fallback_opened
        if dir_fd is not None:
            raise NotImplementedError("dir_fd is unavailable")
        if os.fspath(path) == os.fspath(manifest_path) and not fallback_opened:
            fallback_opened = True
            manifest_path.unlink()
            manifest_path.parent.rmdir()
            manifest_path.parent.symlink_to(outside_directory, target_is_directory=True)
        return original_open(path, flags, mode)

    def track_outside_manifest_read(fd, *args, **kwargs):
        nonlocal outside_manifest_read
        if os.path.samestat(os.fstat(fd), outside_manifest.stat()):
            outside_manifest_read = True
        return original_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(packages_module.os, "open", swap_before_fallback_open)
    monkeypatch.setattr(packages_module.os, "fdopen", track_outside_manifest_read)
    monkeypatch.delattr(packages_module.os, "O_NOFOLLOW", raising=False)

    matrix = build_wheel_matrix(
        root,
        catalog={
            "artifacts": [
                {
                    "distribution": "escaped-wheel",
                    "kind": "pure_python",
                    "manifest": "package/pyproject.toml",
                }
            ]
        },
    )

    assert matrix.targets == ()
    assert not outside_manifest_read
    assert [(item.code, item.path) for item in matrix.diagnostics] == [
        ("WheelManifestOutsideRoot", "$.artifacts[0].manifest")
    ]


@pytest.mark.parametrize("descriptor_relative", DESCRIPTOR_RELATIVE_OPEN_CASES)
def test_wheel_matrix_rejects_manifest_swapped_to_fifo_without_blocking(
    tmp_path,
    monkeypatch,
    descriptor_relative: bool,
) -> None:
    make_fifo = getattr(os, "mkfifo", None)
    non_blocking = getattr(os, "O_NONBLOCK", 0)
    if make_fifo is None or not non_blocking:
        pytest.skip("FIFO non-blocking opens are unavailable")

    root = tmp_path / "repo"
    root.mkdir()
    manifest_path = root / "pyproject.toml"
    _write_valid_wheel_manifest(manifest_path)
    original_open = os.open
    swapped = False

    def swap_to_fifo(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if descriptor_relative:
            should_swap = dir_fd is not None and os.fspath(path) == manifest_path.name
        else:
            should_swap = dir_fd is None and os.fspath(path) == os.fspath(manifest_path)
        if should_swap and not swapped:
            swapped = True
            manifest_path.unlink()
            make_fifo(manifest_path)
            assert flags & non_blocking, "manifest opens must not block on FIFOs"
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(packages_module.os, "open", swap_to_fifo)
    if descriptor_relative:
        monkeypatch.setattr(packages_module.os, "supports_dir_fd", {swap_to_fifo})
    else:
        monkeypatch.delattr(packages_module.os, "O_NOFOLLOW", raising=False)

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

    assert swapped
    assert matrix.targets == ()
    assert [(item.code, item.path) for item in matrix.diagnostics] == [
        ("WheelManifestInvalid", "$.artifacts[0].manifest")
    ]


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


def test_package_catalog_doctor_compares_manifest_dependencies_canonically(
    tmp_path,
) -> None:
    (tmp_path / "base.toml").write_text(
        """
[project]
name = "base-wheel"
version = "1.0.0"
dependencies = []
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "feature.toml").write_text(
        """
[project]
name = "feature-wheel"
version = "1.0.0"
dependencies = ["BASE_WHEEL~=1.0"]
""".strip(),
        encoding="utf-8",
    )
    diagnostics = doctor_package_catalog(
        {
            "artifacts": [
                {
                    "distribution": "Base.Wheel",
                    "kind": "pure_python",
                    "manifest": "base.toml",
                    "dependsOn": [],
                },
                {
                    "distribution": "Feature_Wheel",
                    "kind": "pure_python",
                    "manifest": "feature.toml",
                    "dependsOn": ["BASE---WHEEL"],
                },
            ],
            "components": [],
        },
        root=tmp_path,
    )

    assert diagnostics.diagnostics == ()

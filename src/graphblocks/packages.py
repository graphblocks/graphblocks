from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
import tomllib
from typing import Any

import yaml

from .diagnostics import Diagnostic, DiagnosticSet


@dataclass(frozen=True, slots=True)
class PackageLockEntry:
    distribution: str
    version_constraint: str | None
    import_package: str | None
    default: bool
    layer: str | None
    kind: str | None
    stability: str | None
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    forbidden_dependencies: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PackageLock:
    catalog_version: int
    spec_version: str
    requested: tuple[str, ...]
    entries: tuple[PackageLockEntry, ...]
    excluded_categories: tuple[str, ...] = field(default_factory=tuple)

    def entry(self, distribution: str) -> PackageLockEntry | None:
        for entry in self.entries:
            if entry.distribution == distribution:
                return entry
        return None


@dataclass(frozen=True, slots=True)
class PackageManifestAuditPolicy:
    allowed_licenses: tuple[str, ...] = ("Apache-2.0",)
    blocked_dependencies: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_licenses",
            tuple(sorted({license.strip() for license in self.allowed_licenses if license.strip()})),
        )
        object.__setattr__(
            self,
            "blocked_dependencies",
            tuple(
                sorted(
                    {
                        dependency.strip().lower().replace("_", "-")
                        for dependency in self.blocked_dependencies
                        if dependency.strip()
                    }
                )
            ),
        )


def load_package_catalog(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        with resources.files("graphblocks").joinpath("data/package-catalog.yaml").open("r", encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    with Path(path).open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def package_rows(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for package in catalog.get("packages", []):
        if isinstance(package, dict):
            rows.append(
                {
                    "distribution": package.get("distribution"),
                    "import": package.get("import"),
                    "default": package.get("default", False),
                    "layer": package.get("layer"),
                    "kind": package.get("kind"),
                    "implementationPhase": package.get("implementationPhase"),
                    "stability": package.get("stability"),
                }
            )
    return sorted(rows, key=lambda item: str(item.get("distribution")))


def build_package_lock(
    catalog: dict[str, Any],
    *,
    requested: tuple[str, ...] = (),
    include_default: bool = True,
) -> PackageLock:
    packages_by_distribution = {
        package["distribution"]: package
        for package in catalog.get("packages", [])
        if isinstance(package, dict) and isinstance(package.get("distribution"), str)
    }
    default_metapackage = catalog.get("defaultMetaPackage")
    default_metapackage = default_metapackage if isinstance(default_metapackage, dict) else {}
    default_distribution = default_metapackage.get("distribution")

    default_constraints: dict[str, str] = {}
    for dependency in default_metapackage.get("dependencies", []):
        if not isinstance(dependency, str) or not dependency.strip():
            continue
        distribution = dependency
        constraint = None
        for marker in ("~=", "==", ">=", "<=", "!=", ">", "<"):
            marker_index = dependency.find(marker)
            if marker_index > 0:
                distribution = dependency[:marker_index]
                constraint = dependency[marker_index:]
                break
        if constraint is not None:
            default_constraints[distribution] = constraint

    roots: list[str] = []
    if include_default and isinstance(default_distribution, str) and default_distribution:
        roots.append(default_distribution)
    for distribution in requested:
        if distribution not in roots:
            roots.append(distribution)

    selected: set[str] = set()
    visiting: set[str] = set()

    for distribution in roots:
        stack: list[tuple[str, bool]] = [(distribution, False)]
        while stack:
            current, expanded = stack.pop()
            if current in selected:
                continue
            package = packages_by_distribution.get(current)
            if package is None:
                raise ValueError(f"unknown package distribution {current}")
            if expanded:
                visiting.discard(current)
                selected.add(current)
                continue
            if current in visiting:
                raise ValueError(f"package dependency cycle includes {current}")
            visiting.add(current)
            stack.append((current, True))
            dependencies = [
                dependency
                for dependency in package.get("dependsOn", [])
                if isinstance(dependency, str) and dependency.strip() and dependency not in selected
            ]
            for dependency in reversed(dependencies):
                stack.append((dependency, False))

    entries: list[PackageLockEntry] = []
    for distribution in sorted(selected):
        package = packages_by_distribution[distribution]
        dependencies = tuple(
            dependency for dependency in package.get("dependsOn", []) if isinstance(dependency, str) and dependency
        )
        forbidden_dependencies = tuple(
            dependency
            for dependency in package.get("forbiddenDependencies", [])
            if isinstance(dependency, str) and dependency
        )
        entries.append(
            PackageLockEntry(
                distribution=distribution,
                version_constraint=default_constraints.get(distribution),
                import_package=package.get("import") if isinstance(package.get("import"), str) else None,
                default=bool(package.get("default", False)),
                layer=package.get("layer") if isinstance(package.get("layer"), str) else None,
                kind=package.get("kind") if isinstance(package.get("kind"), str) else None,
                stability=package.get("stability") if isinstance(package.get("stability"), str) else None,
                dependencies=dependencies,
                forbidden_dependencies=forbidden_dependencies,
            )
        )

    excluded_categories = tuple(
        category
        for category in default_metapackage.get("excludedCategories", [])
        if isinstance(category, str) and category
    )
    return PackageLock(
        catalog_version=int(catalog.get("catalogVersion", 0)),
        spec_version=str(catalog.get("specVersion", "")),
        requested=tuple(requested),
        entries=tuple(entries),
        excluded_categories=excluded_categories,
    )


def audit_package_manifests(
    root: str | Path,
    *,
    policy: PackageManifestAuditPolicy = PackageManifestAuditPolicy(),
) -> DiagnosticSet:
    diagnostics: list[Diagnostic] = []
    root_path = Path(root)
    allowed_licenses = set(policy.allowed_licenses)
    blocked_dependencies = set(policy.blocked_dependencies)
    if not root_path.is_dir():
        return DiagnosticSet(
            (
                Diagnostic(
                    "PackageAuditRootMissing",
                    f"package audit root is not a directory: {root_path}",
                    "$",
                ),
            )
        )

    pyproject_paths = [root_path / "pyproject.toml", *sorted(root_path.glob("packages/*/pyproject.toml"))]
    for manifest_path in pyproject_paths:
        if not manifest_path.exists():
            continue
        relative_path = manifest_path.relative_to(root_path).as_posix()
        try:
            manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            diagnostics.append(
                Diagnostic(
                    "PackageManifestInvalid",
                    f"invalid Python package manifest: {error}",
                    f"$.{relative_path}",
                )
            )
            continue
        project = manifest.get("project")
        if not isinstance(project, dict):
            diagnostics.append(
                Diagnostic(
                    "PackageManifestInvalid",
                    "Python package manifests require a project table",
                    f"$.{relative_path}.project",
                )
            )
            continue
        license_value = project.get("license")
        if isinstance(license_value, dict):
            raw_license = license_value.get("text")
            license_text = raw_license if isinstance(raw_license, str) else None
        else:
            license_text = license_value if isinstance(license_value, str) else None
        if not license_text or not license_text.strip():
            diagnostics.append(
                Diagnostic(
                    "PackageLicenseMissing",
                    "Python package manifest must declare a license",
                    f"$.{relative_path}.project.license",
                )
            )
        elif allowed_licenses and license_text.strip() not in allowed_licenses:
            diagnostics.append(
                Diagnostic(
                    "PackageLicenseDenied",
                    f"license {license_text.strip()!r} is not in the allowed license policy",
                    f"$.{relative_path}.project.license",
                )
            )
        dependencies = project.get("dependencies", [])
        if isinstance(dependencies, list):
            for index, dependency in enumerate(dependencies):
                if not isinstance(dependency, str):
                    continue
                dependency_name = dependency.strip().split(";", 1)[0].strip()
                for marker in ("[", "~=", "==", ">=", "<=", "!=", ">", "<"):
                    marker_index = dependency_name.find(marker)
                    if marker_index > 0:
                        dependency_name = dependency_name[:marker_index]
                        break
                dependency_name = dependency_name.strip().lower().replace("_", "-")
                if dependency_name in blocked_dependencies:
                    diagnostics.append(
                        Diagnostic(
                            "PackageBlockedDependency",
                            f"dependency {dependency_name!r} is blocked by vulnerability policy",
                            f"$.{relative_path}.project.dependencies[{index}]",
                        )
                    )
        optional_dependencies = project.get("optional-dependencies", {})
        if isinstance(optional_dependencies, dict):
            for extra in sorted(optional_dependencies):
                dependencies = optional_dependencies[extra]
                if not isinstance(dependencies, list):
                    continue
                for index, dependency in enumerate(dependencies):
                    if not isinstance(dependency, str):
                        continue
                    dependency_name = dependency.strip().split(";", 1)[0].strip()
                    for marker in ("[", "~=", "==", ">=", "<=", "!=", ">", "<"):
                        marker_index = dependency_name.find(marker)
                        if marker_index > 0:
                            dependency_name = dependency_name[:marker_index]
                            break
                    dependency_name = dependency_name.strip().lower().replace("_", "-")
                    if dependency_name in blocked_dependencies:
                        diagnostics.append(
                            Diagnostic(
                                "PackageBlockedDependency",
                                f"dependency {dependency_name!r} is blocked by vulnerability policy",
                                f"$.{relative_path}.project.optional-dependencies.{extra}[{index}]",
                            )
                        )

    workspace_license: str | None = None
    workspace_manifest_path = root_path / "Cargo.toml"
    if workspace_manifest_path.exists():
        try:
            workspace_manifest = tomllib.loads(workspace_manifest_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            diagnostics.append(
                Diagnostic(
                    "PackageManifestInvalid",
                    f"invalid Rust workspace manifest: {error}",
                    "$.Cargo.toml",
                )
            )
            workspace_manifest = {}
        workspace_package = workspace_manifest.get("workspace", {}).get("package")
        if isinstance(workspace_package, dict):
            raw_workspace_license = workspace_package.get("license")
            workspace_license = raw_workspace_license if isinstance(raw_workspace_license, str) else None

    for manifest_path in sorted(root_path.glob("crates/*/Cargo.toml")):
        relative_path = manifest_path.relative_to(root_path).as_posix()
        try:
            manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            diagnostics.append(
                Diagnostic(
                    "PackageManifestInvalid",
                    f"invalid Rust package manifest: {error}",
                    f"$.{relative_path}",
                )
            )
            continue
        package = manifest.get("package")
        if not isinstance(package, dict):
            diagnostics.append(
                Diagnostic(
                    "PackageManifestInvalid",
                    "Rust package manifests require a package table",
                    f"$.{relative_path}.package",
                )
            )
            continue
        raw_license = package.get("license")
        if isinstance(raw_license, str):
            license_text = raw_license
        elif isinstance(raw_license, dict) and raw_license.get("workspace") is True:
            license_text = workspace_license
        else:
            license_text = None
        if not license_text or not license_text.strip():
            diagnostics.append(
                Diagnostic(
                    "PackageLicenseMissing",
                    "Rust package manifest must declare a license",
                    f"$.{relative_path}.package.license",
                )
            )
        elif allowed_licenses and license_text.strip() not in allowed_licenses:
            diagnostics.append(
                Diagnostic(
                    "PackageLicenseDenied",
                    f"license {license_text.strip()!r} is not in the allowed license policy",
                    f"$.{relative_path}.package.license",
                )
            )
        for table_name in ("dependencies", "dev-dependencies", "build-dependencies"):
            dependencies = manifest.get(table_name, {})
            if not isinstance(dependencies, dict):
                continue
            for dependency, dependency_spec in sorted(dependencies.items()):
                dependency_name = str(dependency).strip().lower().replace("_", "-")
                if isinstance(dependency_spec, dict):
                    package_name = dependency_spec.get("package")
                    if isinstance(package_name, str) and package_name.strip():
                        dependency_name = package_name.strip().lower().replace("_", "-")
                if dependency_name in blocked_dependencies:
                    diagnostics.append(
                        Diagnostic(
                            "PackageBlockedDependency",
                            f"dependency {dependency_name!r} is blocked by vulnerability policy",
                            f"$.{relative_path}.{table_name}.{dependency}",
                        )
                    )

    return DiagnosticSet(tuple(diagnostics))


def doctor_package_catalog(catalog: dict[str, Any], *, root: str | Path | None = None) -> DiagnosticSet:
    diagnostics: list[Diagnostic] = []
    raw_packages = catalog.get("packages", [])
    packages = raw_packages if isinstance(raw_packages, list) else []
    packages_by_distribution: dict[str, dict[str, Any]] = {}

    for index, package in enumerate(packages):
        if not isinstance(package, dict):
            diagnostics.append(
                Diagnostic(
                    "PackageEntryInvalid",
                    "package catalog entries must be mappings",
                    f"$.packages[{index}]",
                )
            )
            continue
        distribution = package.get("distribution")
        if not isinstance(distribution, str) or not distribution.strip():
            diagnostics.append(
                Diagnostic(
                    "PackageDistributionMissing",
                    "package catalog entries require distribution",
                    f"$.packages[{index}].distribution",
                )
            )
            continue
        if distribution in packages_by_distribution:
            diagnostics.append(
                Diagnostic(
                    "PackageDuplicateDistribution",
                    f"duplicate package distribution {distribution!r}",
                    f"$.packages[{index}].distribution",
                )
            )
            continue
        packages_by_distribution[distribution] = package

    default_metapackage = catalog.get("defaultMetaPackage")
    default_metapackage = default_metapackage if isinstance(default_metapackage, dict) else {}
    default_distribution = default_metapackage.get("distribution")
    if isinstance(default_distribution, str) and default_distribution and default_distribution not in packages_by_distribution:
        diagnostics.append(
            Diagnostic(
                "PackageDefaultMissing",
                f"default metapackage {default_distribution!r} is not listed in packages",
                "$.defaultMetaPackage.distribution",
            )
        )

    for index, dependency in enumerate(default_metapackage.get("dependencies", [])):
        if not isinstance(dependency, str) or not dependency.strip():
            diagnostics.append(
                Diagnostic(
                    "PackageDefaultDependencyInvalid",
                    "default metapackage dependencies must be non-empty strings",
                    f"$.defaultMetaPackage.dependencies[{index}]",
                )
            )
            continue
        distribution = dependency
        for marker in ("~=", "==", ">=", "<=", "!=", ">", "<"):
            marker_index = dependency.find(marker)
            if marker_index > 0:
                distribution = dependency[:marker_index]
                break
        if distribution not in packages_by_distribution:
            diagnostics.append(
                Diagnostic(
                    "PackageDefaultDependencyMissing",
                    f"default dependency {distribution!r} is not listed in packages",
                    f"$.defaultMetaPackage.dependencies[{index}]",
                )
            )

    for distribution in sorted(packages_by_distribution):
        package = packages_by_distribution[distribution]
        raw_dependencies = package.get("dependsOn", [])
        dependencies = raw_dependencies if isinstance(raw_dependencies, list) else []
        if not isinstance(raw_dependencies, list):
            diagnostics.append(
                Diagnostic(
                    "PackageDependenciesInvalid",
                    "package dependsOn must be a list",
                    f"$.packages.{distribution}.dependsOn",
                )
            )
            continue
        for index, dependency in enumerate(dependencies):
            if not isinstance(dependency, str) or not dependency.strip():
                diagnostics.append(
                    Diagnostic(
                        "PackageDependencyInvalid",
                        "package dependencies must be non-empty strings",
                        f"$.packages.{distribution}.dependsOn[{index}]",
                    )
                )
            elif dependency not in packages_by_distribution:
                diagnostics.append(
                    Diagnostic(
                        "PackageDependencyMissing",
                        f"package {distribution!r} depends on unknown package {dependency!r}",
                        f"$.packages.{distribution}.dependsOn[{index}]",
                    )
                )

        raw_forbidden_dependencies = package.get("forbiddenDependencies", [])
        forbidden_dependencies = raw_forbidden_dependencies if isinstance(raw_forbidden_dependencies, list) else []
        if not isinstance(raw_forbidden_dependencies, list):
            diagnostics.append(
                Diagnostic(
                    "PackageForbiddenDependenciesInvalid",
                    "package forbiddenDependencies must be a list",
                    f"$.packages.{distribution}.forbiddenDependencies",
                )
            )
            continue
        valid_forbidden_dependencies: set[str] = set()
        for index, dependency in enumerate(forbidden_dependencies):
            if not isinstance(dependency, str) or not dependency.strip():
                diagnostics.append(
                    Diagnostic(
                        "PackageForbiddenDependencyInvalid",
                        "package forbidden dependencies must be non-empty strings",
                        f"$.packages.{distribution}.forbiddenDependencies[{index}]",
                    )
                )
            else:
                valid_forbidden_dependencies.add(dependency)
        for index, dependency in enumerate(dependencies):
            if isinstance(dependency, str) and dependency in valid_forbidden_dependencies:
                diagnostics.append(
                    Diagnostic(
                        "PackageForbiddenDependencySelected",
                        f"package {distribution!r} depends on forbidden dependency {dependency!r}",
                        f"$.packages.{distribution}.dependsOn[{index}]",
                    )
                )

    states: dict[str, str] = {}
    reported_cycles: set[frozenset[str]] = set()
    for root_distribution in sorted(packages_by_distribution):
        if states.get(root_distribution) == "done":
            continue
        stack: list[tuple[str, int]] = [(root_distribution, 0)]
        path: list[str] = []
        while stack:
            current, next_index = stack[-1]
            if current not in states:
                states[current] = "visiting"
                path.append(current)

            package = packages_by_distribution[current]
            dependencies = [
                dependency
                for dependency in package.get("dependsOn", [])
                if isinstance(dependency, str) and dependency in packages_by_distribution
            ]
            if next_index >= len(dependencies):
                states[current] = "done"
                stack.pop()
                if path and path[-1] == current:
                    path.pop()
                elif current in path:
                    path.remove(current)
                continue

            dependency = dependencies[next_index]
            stack[-1] = (current, next_index + 1)
            dependency_state = states.get(dependency)
            if dependency_state == "visiting":
                cycle_start = path.index(dependency) if dependency in path else 0
                cycle = [*path[cycle_start:], dependency]
                cycle_key = frozenset(cycle)
                if cycle_key not in reported_cycles:
                    reported_cycles.add(cycle_key)
                    diagnostics.append(
                        Diagnostic(
                            "PackageDependencyCycle",
                            f"package dependency cycle detected: {' -> '.join(cycle)}",
                            f"$.packages.{current}.dependsOn",
                        )
                    )
            elif dependency_state != "done":
                stack.append((dependency, 0))

    if root is not None:
        root_path = Path(root)
        if not root_path.is_dir():
            diagnostics.append(
                Diagnostic(
                    "PackageDoctorRootMissing",
                    f"package doctor root is not a directory: {root_path}",
                    "$",
                )
            )
        else:
            manifest_paths = [
                root_path / "pyproject.toml",
                *sorted(root_path.glob("packages/*/pyproject.toml")),
            ]
            known_distributions = set(packages_by_distribution)
            for manifest_path in manifest_paths:
                if not manifest_path.exists():
                    continue
                relative_path = manifest_path.relative_to(root_path).as_posix()
                try:
                    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
                except tomllib.TOMLDecodeError as error:
                    diagnostics.append(
                        Diagnostic(
                            "PackageManifestInvalid",
                            f"invalid Python package manifest: {error}",
                            f"$.{relative_path}",
                        )
                    )
                    continue
                project = manifest.get("project")
                if not isinstance(project, dict):
                    diagnostics.append(
                        Diagnostic(
                            "PackageManifestInvalid",
                            "Python package manifests require a project table",
                            f"$.{relative_path}.project",
                        )
                    )
                    continue
                distribution = project.get("name")
                if not isinstance(distribution, str) or not distribution.strip():
                    diagnostics.append(
                        Diagnostic(
                            "PackageManifestDistributionMissing",
                            "Python package manifest must declare project.name",
                            f"$.{relative_path}.project.name",
                        )
                    )
                    continue
                distribution = distribution.strip()
                package = packages_by_distribution.get(distribution)
                if package is None:
                    diagnostics.append(
                        Diagnostic(
                            "PackageManifestDistributionUnknown",
                            f"package manifest distribution {distribution!r} is not listed in catalog",
                            f"$.{relative_path}.project.name",
                        )
                    )
                    continue
                expected_dependencies = tuple(
                    dependency for dependency in package.get("dependsOn", []) if isinstance(dependency, str) and dependency
                )
                dependencies = project.get("dependencies", [])
                actual_dependencies: list[str] = []
                if isinstance(dependencies, list):
                    for dependency in dependencies:
                        if not isinstance(dependency, str):
                            continue
                        dependency_name = dependency.strip().split(";", 1)[0].strip()
                        for marker in ("[", "~=", "==", ">=", "<=", "!=", ">", "<"):
                            marker_index = dependency_name.find(marker)
                            if marker_index > 0:
                                dependency_name = dependency_name[:marker_index]
                                break
                        dependency_name = dependency_name.strip().lower().replace("_", "-")
                        if dependency_name in known_distributions and dependency_name not in actual_dependencies:
                            actual_dependencies.append(dependency_name)
                dependency_path = f"$.{relative_path}.project.dependencies"
                for dependency in expected_dependencies:
                    if dependency not in actual_dependencies:
                        diagnostics.append(
                            Diagnostic(
                                "PackageManifestDependencyMissing",
                                f"package manifest for {distribution!r} is missing catalog dependency {dependency!r}",
                                dependency_path,
                            )
                        )
                for dependency in actual_dependencies:
                    if dependency not in expected_dependencies:
                        diagnostics.append(
                            Diagnostic(
                                "PackageManifestDependencyUnexpected",
                                f"package manifest for {distribution!r} declares uncataloged first-party dependency {dependency!r}",
                                dependency_path,
                            )
                        )

    excluded_categories = {
        category
        for category in default_metapackage.get("excludedCategories", [])
        if isinstance(category, str) and category
    }
    if excluded_categories and isinstance(default_distribution, str) and default_distribution in packages_by_distribution:
        try:
            default_lock = build_package_lock(catalog, requested=(), include_default=True)
        except ValueError:
            default_lock = None
        if default_lock is not None:
            for entry in default_lock.entries:
                package = packages_by_distribution.get(entry.distribution, {})
                categories: set[str] = set()
                raw_category = package.get("category")
                if isinstance(raw_category, str):
                    categories.add(raw_category)
                raw_categories = package.get("categories", [])
                if isinstance(raw_categories, list):
                    categories.update(category for category in raw_categories if isinstance(category, str))
                blocked = sorted(categories & excluded_categories)
                if blocked:
                    diagnostics.append(
                        Diagnostic(
                            "PackageDefaultIncludesExcludedCategory",
                            f"default package closure includes excluded category {blocked[0]!r}",
                            f"$.packages.{entry.distribution}.categories",
                        )
                    )

    return DiagnosticSet(tuple(diagnostics))

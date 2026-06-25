from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
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


def doctor_package_catalog(catalog: dict[str, Any]) -> DiagnosticSet:
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
    for root in sorted(packages_by_distribution):
        if states.get(root) == "done":
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
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

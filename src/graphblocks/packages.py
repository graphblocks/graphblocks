from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
import tomllib
from typing import Any, Literal

import yaml

from .canonical import canonical_hash
from .diagnostics import Diagnostic, DiagnosticSet

WheelBuildKind = Literal["pure_python", "native_extension"]


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    return value


def _validate_optional_non_empty_string(owner: str, field_name: str, value: object | None) -> str | None:
    if value is None:
        return None
    return _validate_non_empty_string(owner, field_name, value)


def _validate_string_tuple(owner: str, field_name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a collection of strings")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{owner} {field_name} must be a collection of strings") from error
    for item in items:
        _validate_non_empty_string(owner, f"{field_name} item", item)
    return items


def _python_dependency_name(dependency: str) -> str:
    dependency_name = dependency.strip().split(";", 1)[0].strip()
    if "@" in dependency_name:
        dependency_name = dependency_name.split("@", 1)[0].strip()
    for marker in ("[", "(", "~=", "==", ">=", "<=", "!=", ">", "<"):
        marker_index = dependency_name.find(marker)
        if marker_index > 0:
            dependency_name = dependency_name[:marker_index]
            break
    return dependency_name.strip().lower().replace("_", "-")


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

    def __post_init__(self) -> None:
        _validate_non_empty_string("package lock entry", "distribution", self.distribution)
        _validate_optional_non_empty_string("package lock entry", "version_constraint", self.version_constraint)
        _validate_optional_non_empty_string("package lock entry", "import_package", self.import_package)
        if not isinstance(self.default, bool):
            raise ValueError("package lock entry default must be a boolean")
        for field_name in ("layer", "kind", "stability"):
            _validate_optional_non_empty_string("package lock entry", field_name, getattr(self, field_name))
        object.__setattr__(
            self,
            "dependencies",
            _validate_string_tuple("package lock entry", "dependencies", self.dependencies),
        )
        object.__setattr__(
            self,
            "forbidden_dependencies",
            _validate_string_tuple("package lock entry", "forbidden_dependencies", self.forbidden_dependencies),
        )

    def lock_payload(self) -> dict[str, object]:
        return {
            "default": self.default,
            "dependencies": list(self.dependencies),
            "distribution": self.distribution,
            "forbiddenDependencies": list(self.forbidden_dependencies),
            "import": self.import_package,
            "kind": self.kind,
            "layer": self.layer,
            "stability": self.stability,
            "versionConstraint": self.version_constraint,
        }


@dataclass(frozen=True, slots=True)
class PackageLock:
    catalog_version: int
    spec_version: str
    requested: tuple[str, ...]
    entries: tuple[PackageLockEntry, ...]
    excluded_categories: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.catalog_version, int) or isinstance(self.catalog_version, bool):
            raise ValueError("package lock catalog_version must be an integer")
        if self.catalog_version <= 0:
            raise ValueError("package lock catalog_version must be positive")
        _validate_non_empty_string("package lock", "spec_version", self.spec_version)
        object.__setattr__(self, "requested", _validate_string_tuple("package lock", "requested", self.requested))
        entries = tuple(self.entries)
        if any(not isinstance(entry, PackageLockEntry) for entry in entries):
            raise ValueError("package lock entries must be PackageLockEntry")
        distributions = [entry.distribution for entry in entries]
        if len(set(distributions)) != len(distributions):
            raise ValueError("package lock entries must have unique distributions")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self,
            "excluded_categories",
            _validate_string_tuple("package lock", "excluded_categories", self.excluded_categories),
        )

    def entry(self, distribution: str) -> PackageLockEntry | None:
        for entry in self.entries:
            if entry.distribution == distribution:
                return entry
        return None

    def lock_payload(self) -> dict[str, object]:
        return {
            "catalogVersion": self.catalog_version,
            "excludedCategories": list(self.excluded_categories),
            "packages": [entry.lock_payload() for entry in self.entries],
            "requested": sorted(set(self.requested)),
            "specVersion": self.spec_version,
        }

    def content_digest(self) -> str:
        return canonical_hash(self.lock_payload())


@dataclass(frozen=True, slots=True)
class WheelBuildTarget:
    distribution: str
    manifest: str
    backend: str
    kind: WheelBuildKind
    source_layout: str
    python_versions: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in ("distribution", "manifest", "backend", "source_layout"):
            _validate_non_empty_string("wheel build target", field_name, getattr(self, field_name))
        if self.kind not in {"pure_python", "native_extension"}:
            raise ValueError(f"invalid wheel build target kind {self.kind}")
        object.__setattr__(
            self,
            "python_versions",
            _validate_string_tuple("wheel build target", "python_versions", self.python_versions),
        )

    def target_contract(self) -> dict[str, object]:
        return {
            "distribution": self.distribution,
            "manifest": self.manifest,
            "backend": self.backend,
            "kind": self.kind,
            "source_layout": self.source_layout,
            "python_versions": list(self.python_versions),
        }


@dataclass(frozen=True, slots=True)
class WheelMatrix:
    targets: tuple[WheelBuildTarget, ...]
    diagnostics: tuple[Diagnostic, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        targets = tuple(self.targets)
        if any(not isinstance(target, WheelBuildTarget) for target in targets):
            raise ValueError("wheel matrix targets must be WheelBuildTarget")
        distributions = [target.distribution for target in targets]
        if len(set(distributions)) != len(distributions):
            raise ValueError("wheel matrix targets must have unique distributions")
        object.__setattr__(self, "targets", tuple(sorted(targets, key=lambda item: item.distribution)))
        diagnostics = tuple(self.diagnostics)
        if any(not isinstance(diagnostic, Diagnostic) for diagnostic in diagnostics):
            raise ValueError("wheel matrix diagnostics must be Diagnostic")
        object.__setattr__(
            self,
            "diagnostics",
            tuple(sorted(diagnostics, key=lambda item: (item.path, item.code, item.message))),
        )

    @property
    def ok(self) -> bool:
        return not any(diagnostic.severity == "error" for diagnostic in self.diagnostics)

    def matrix_contract(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "target_count": len(self.targets),
            "targets": [target.target_contract() for target in self.targets],
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.matrix_contract())


@dataclass(frozen=True, slots=True)
class PackageManifestAuditPolicy:
    allowed_licenses: tuple[str, ...] = ("Apache-2.0",)
    blocked_dependencies: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        allowed_licenses = _validate_string_tuple(
            "package manifest audit policy",
            "allowed_licenses",
            self.allowed_licenses,
        )
        blocked_dependencies = _validate_string_tuple(
            "package manifest audit policy",
            "blocked_dependencies",
            self.blocked_dependencies,
        )
        object.__setattr__(
            self,
            "allowed_licenses",
            tuple(sorted({license.strip() for license in allowed_licenses})),
        )
        object.__setattr__(
            self,
            "blocked_dependencies",
            tuple(
                sorted(
                    {
                        dependency.strip().lower().replace("_", "-")
                        for dependency in blocked_dependencies
                    }
                )
            ),
        )


def load_package_catalog(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        with resources.files("graphblocks").joinpath("data/package-catalog.yaml").open("r", encoding="utf-8") as stream:
            catalog = yaml.safe_load(stream)
    else:
        with Path(path).open("r", encoding="utf-8") as stream:
            catalog = yaml.safe_load(stream)
    if not isinstance(catalog, dict):
        raise ValueError("package catalog must be a mapping")
    catalog_version = catalog.get("catalogVersion")
    if isinstance(catalog_version, bool) or not isinstance(catalog_version, int) or catalog_version <= 0:
        raise ValueError("package catalog catalogVersion must be a positive integer")
    spec_version = catalog.get("specVersion")
    if not isinstance(spec_version, str) or not spec_version.strip():
        raise ValueError("package catalog specVersion must be a non-empty string")
    packages = catalog.get("packages")
    if not isinstance(packages, list):
        raise ValueError("package catalog packages must be a list")
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError("package catalog packages entries must be mappings")
        distribution = package.get("distribution")
        if not isinstance(distribution, str) or not distribution.strip():
            raise ValueError("package catalog package distribution must be a non-empty string")
    return catalog


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


def _package_categories(package: dict[str, Any]) -> set[str]:
    categories: set[str] = set()
    raw_category = package.get("category")
    if isinstance(raw_category, str):
        categories.add(raw_category)
    raw_categories = package.get("categories", [])
    if isinstance(raw_categories, list):
        categories.update(category for category in raw_categories if isinstance(category, str))
    return categories


def _package_dependency_closure(
    distribution: str, packages_by_distribution: dict[str, dict[str, Any]]
) -> set[str]:
    package = packages_by_distribution.get(distribution)
    if package is None:
        return set()
    raw_direct_dependencies = package.get("dependsOn", [])
    raw_direct_dependencies = raw_direct_dependencies if isinstance(raw_direct_dependencies, list) else []
    direct_dependencies = [
        dependency
        for dependency in raw_direct_dependencies
        if isinstance(dependency, str) and dependency in packages_by_distribution
    ]
    closure: set[str] = set()
    stack = list(reversed(direct_dependencies))
    while stack:
        dependency = stack.pop()
        if dependency in closure:
            continue
        closure.add(dependency)
        dependency_package = packages_by_distribution[dependency]
        raw_nested_dependencies = dependency_package.get("dependsOn", [])
        raw_nested_dependencies = raw_nested_dependencies if isinstance(raw_nested_dependencies, list) else []
        stack.extend(
            nested
            for nested in reversed(raw_nested_dependencies)
            if isinstance(nested, str) and nested in packages_by_distribution and nested not in closure
        )
    return closure


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
    default_selected: set[str] = set()
    visiting: set[str] = set()
    excluded_categories = tuple(
        category
        for category in default_metapackage.get("excludedCategories", [])
        if isinstance(category, str) and category
    )

    for distribution in roots:
        collecting_default = include_default and distribution == default_distribution
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
                if collecting_default:
                    default_selected.add(current)
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

    for distribution in sorted(selected):
        package = packages_by_distribution[distribution]
        forbidden_dependencies = {
            dependency
            for dependency in package.get("forbiddenDependencies", [])
            if isinstance(dependency, str) and dependency
        }
        selected_forbidden = sorted(forbidden_dependencies & selected)
        if selected_forbidden:
            raise ValueError(
                "forbidden package dependency "
                f"{selected_forbidden[0]!r} selected for package {distribution!r}"
            )

    excluded_category_set = set(excluded_categories)
    if excluded_category_set:
        for distribution in sorted(default_selected):
            package = packages_by_distribution[distribution]
            blocked = sorted(_package_categories(package) & excluded_category_set)
            if blocked:
                raise ValueError(
                    f"default package closure includes excluded category {blocked[0]!r} "
                    f"from package {distribution!r}"
                )

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

    return PackageLock(
        catalog_version=int(catalog.get("catalogVersion", 0)),
        spec_version=str(catalog.get("specVersion", "")),
        requested=tuple(requested),
        entries=tuple(entries),
        excluded_categories=excluded_categories,
    )


def _pyproject_paths(root_path: Path) -> list[Path]:
    return [root_path / "pyproject.toml", *sorted(root_path.glob("packages/*/pyproject.toml"))]


def _supports_python_version(requires_python: str, version: str) -> bool:
    text = requires_python.strip()
    if not text:
        return False
    major_minor = tuple(int(part) for part in version.split(".")[:2])
    for clause in (part.strip() for part in text.split(",")):
        if not clause:
            continue
        for operator in (">=", "==", ">", "<=", "<"):
            if clause.startswith(operator):
                raw_bound = clause[len(operator) :].strip()
                bound_parts = raw_bound.split(".")[:2]
                if not all(part.isdigit() for part in bound_parts):
                    continue
                bound = tuple(int(part) for part in bound_parts)
                if operator == ">=" and major_minor < bound:
                    return False
                if operator == ">" and major_minor <= bound:
                    return False
                if operator == "<=" and major_minor > bound:
                    return False
                if operator == "<" and major_minor >= bound:
                    return False
                if operator == "==" and major_minor != bound:
                    return False
                break
    return True


def build_wheel_matrix(
    root: str | Path,
    *,
    python_versions: tuple[str, ...] = ("3.11", "3.12"),
) -> WheelMatrix:
    root_path = Path(root)
    diagnostics: list[Diagnostic] = []
    targets: list[WheelBuildTarget] = []
    if not root_path.is_dir():
        return WheelMatrix(
            targets=(),
            diagnostics=(
                Diagnostic(
                    "WheelMatrixRootMissing",
                    f"wheel matrix root is not a directory: {root_path}",
                    "$",
                ),
            ),
        )

    for manifest_path in _pyproject_paths(root_path):
        if not manifest_path.exists():
            continue
        relative_path = manifest_path.relative_to(root_path).as_posix()
        path_prefix = f"$.{relative_path}"
        try:
            manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            diagnostics.append(
                Diagnostic(
                    "WheelManifestInvalid",
                    f"invalid Python package manifest: {error}",
                    path_prefix,
                )
            )
            continue
        project = manifest.get("project")
        if not isinstance(project, dict):
            diagnostics.append(
                Diagnostic(
                    "WheelProjectMissing",
                    "wheel pyproject must declare a project table",
                    f"{path_prefix}.project",
                )
            )
            continue
        distribution = project.get("name")
        if not isinstance(distribution, str) or not distribution.strip():
            diagnostics.append(
                Diagnostic(
                    "WheelDistributionMissing",
                    "wheel pyproject must declare project.name",
                    f"{path_prefix}.project.name",
                )
            )
            continue
        distribution = distribution.strip()
        build_system = manifest.get("build-system")
        if not isinstance(build_system, dict):
            diagnostics.append(
                Diagnostic(
                    "WheelBuildSystemMissing",
                    f"wheel target {distribution!r} must declare build-system",
                    f"{path_prefix}.build-system",
                )
            )
            continue
        backend = build_system.get("build-backend")
        if not isinstance(backend, str) or not backend.strip():
            diagnostics.append(
                Diagnostic(
                    "WheelBuildBackendMissing",
                    f"wheel target {distribution!r} must declare build-system.build-backend",
                    f"{path_prefix}.build-system.build-backend",
                )
            )
            continue
        backend = backend.strip()
        requires_python = project.get("requires-python")
        if not isinstance(requires_python, str) or not requires_python.strip():
            diagnostics.append(
                Diagnostic(
                    "WheelPythonRequiresMissing",
                    f"wheel target {distribution!r} must declare project.requires-python",
                    f"{path_prefix}.project.requires-python",
                )
            )
            continue
        supported_versions = tuple(
            version for version in python_versions if _supports_python_version(requires_python, version)
        )
        if supported_versions != tuple(python_versions):
            diagnostics.append(
                Diagnostic(
                    "WheelPythonVersionUnsupported",
                    f"wheel target {distribution!r} does not support the required Python matrix",
                    f"{path_prefix}.project.requires-python",
                )
            )

        tool = manifest.get("tool")
        tool = tool if isinstance(tool, dict) else {}
        if backend == "hatchling.build":
            hatch = tool.get("hatch")
            hatch = hatch if isinstance(hatch, dict) else {}
            build = hatch.get("build")
            build = build if isinstance(build, dict) else {}
            targets_config = build.get("targets")
            targets_config = targets_config if isinstance(targets_config, dict) else {}
            wheel = targets_config.get("wheel")
            wheel = wheel if isinstance(wheel, dict) else {}
            source_layout: str | None = None
            packages = wheel.get("packages")
            only_include = wheel.get("only-include")
            if isinstance(packages, list) and packages and all(isinstance(item, str) and item.strip() for item in packages):
                source_layout = ",".join(packages)
            elif (
                isinstance(only_include, list)
                and only_include
                and all(isinstance(item, str) and item.strip() for item in only_include)
            ):
                source_layout = ",".join(only_include)
            if source_layout is None:
                diagnostics.append(
                    Diagnostic(
                        "WheelBuildTargetMissing",
                        f"hatchling wheel target {distribution!r} must declare packages or only-include",
                        f"{path_prefix}.tool",
                    )
                )
                continue
            targets.append(
                WheelBuildTarget(
                    distribution=distribution,
                    manifest=relative_path,
                    backend=backend,
                    kind="pure_python",
                    source_layout=source_layout,
                    python_versions=supported_versions,
                )
            )
            continue

        if backend == "maturin":
            maturin = tool.get("maturin")
            maturin = maturin if isinstance(maturin, dict) else {}
            python_source = maturin.get("python-source")
            manifest_ref = maturin.get("manifest-path")
            module_name = maturin.get("module-name")
            if not all(isinstance(value, str) and value.strip() for value in (python_source, manifest_ref, module_name)):
                diagnostics.append(
                    Diagnostic(
                        "WheelBuildTargetMissing",
                        f"maturin wheel target {distribution!r} must declare python-source, module-name, and manifest-path",
                        f"{path_prefix}.tool.maturin",
                    )
                )
                continue
            targets.append(
                WheelBuildTarget(
                    distribution=distribution,
                    manifest=relative_path,
                    backend=backend,
                    kind="native_extension",
                    source_layout=str(python_source),
                    python_versions=supported_versions,
                )
            )
            continue

        diagnostics.append(
            Diagnostic(
                "WheelBuildBackendUnsupported",
                f"wheel target {distribution!r} uses unsupported build backend {backend!r}",
                f"{path_prefix}.build-system.build-backend",
            )
        )

    return WheelMatrix(targets=tuple(targets), diagnostics=tuple(diagnostics))


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
                dependency_name = _python_dependency_name(dependency)
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
                    dependency_name = _python_dependency_name(dependency)
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
        direct_dependencies = {dependency for dependency in dependencies if isinstance(dependency, str)}
        transitive_forbidden_dependencies = sorted(
            (valid_forbidden_dependencies & _package_dependency_closure(distribution, packages_by_distribution))
            - direct_dependencies
        )
        for dependency in transitive_forbidden_dependencies:
            diagnostics.append(
                Diagnostic(
                    "PackageForbiddenDependencySelected",
                    f"package {distribution!r} transitively selects forbidden dependency {dependency!r}",
                    f"$.packages.{distribution}.forbiddenDependencies",
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
        except ValueError as error:
            default_lock = None
            if "default package closure includes excluded category" in str(error):
                diagnostics.append(
                    Diagnostic(
                        "PackageDefaultIncludesExcludedCategory",
                        str(error),
                        "$.defaultMetaPackage.excludedCategories",
                    )
                )
        if default_lock is not None:
            for entry in default_lock.entries:
                package = packages_by_distribution.get(entry.distribution, {})
                blocked = sorted(_package_categories(package) & excluded_categories)
                if blocked:
                    diagnostics.append(
                        Diagnostic(
                            "PackageDefaultIncludesExcludedCategory",
                            f"default package closure includes excluded category {blocked[0]!r}",
                            f"$.packages.{entry.distribution}.categories",
                        )
                    )

    return DiagnosticSet(tuple(diagnostics))

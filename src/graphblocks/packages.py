from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
import os
from pathlib import Path
import stat
import tomllib
from typing import Any, Literal

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
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
    return canonicalize_name(dependency_name.strip())


@dataclass(frozen=True, slots=True)
class PackageLockEntry:
    distribution: str
    artifact: str
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
        _validate_non_empty_string("package lock entry", "artifact", self.artifact)
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

    @property
    def component(self) -> str:
        return self.distribution

    def lock_payload(self) -> dict[str, object]:
        return {
            "artifact": self.artifact,
            "component": self.component,
            "default": self.default,
            "dependencies": list(self.dependencies),
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
    artifacts: tuple[str, ...] = field(default_factory=tuple)
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
        artifacts = _validate_string_tuple("package lock", "artifacts", self.artifacts)
        if len(set(artifacts)) != len(artifacts):
            raise ValueError("package lock artifacts must be unique")
        object.__setattr__(self, "artifacts", tuple(sorted(artifacts)))
        object.__setattr__(
            self,
            "excluded_categories",
            _validate_string_tuple("package lock", "excluded_categories", self.excluded_categories),
        )

    def entry(self, component: str) -> PackageLockEntry | None:
        for entry in self.entries:
            if entry.component == component:
                return entry
        return None

    def lock_payload(self) -> dict[str, object]:
        return {
            "artifacts": list(self.artifacts),
            "catalogVersion": self.catalog_version,
            "components": [entry.lock_payload() for entry in self.entries],
            "excludedCategories": list(self.excluded_categories),
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
                        canonicalize_name(dependency.strip())
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
    artifacts = catalog.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("package catalog artifacts must be a list")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("package catalog artifact entries must be mappings")
        distribution = artifact.get("distribution")
        if not isinstance(distribution, str) or not distribution.strip():
            raise ValueError("package catalog artifact distribution must be a non-empty string")
    components = catalog.get("components")
    if not isinstance(components, list):
        raise ValueError("package catalog components must be a list")
    for component in components:
        if not isinstance(component, dict):
            raise ValueError("package catalog component entries must be mappings")
        name = component.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("package catalog component name must be a non-empty string")
    return catalog


def package_rows(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for component in catalog.get("components", []):
        if isinstance(component, dict):
            rows.append(
                {
                    "component": component.get("name"),
                    "artifact": component.get("artifact"),
                    "distribution": component.get("name"),
                    "import": component.get("import"),
                    "default": component.get("default", False),
                    "layer": component.get("layer"),
                    "kind": component.get("kind"),
                    "implementationPhase": component.get("implementationPhase"),
                    "stability": component.get("stability"),
                }
            )
    return sorted(rows, key=lambda item: str(item.get("component")))


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
    artifacts_by_distribution = {
        artifact["distribution"]: artifact
        for artifact in catalog.get("artifacts", [])
        if isinstance(artifact, dict) and isinstance(artifact.get("distribution"), str)
    }
    components_by_name = {
        component["name"]: component
        for component in catalog.get("components", [])
        if isinstance(component, dict) and isinstance(component.get("name"), str)
    }
    default_selection = catalog.get("defaultSelection")
    default_selection = default_selection if isinstance(default_selection, dict) else {}

    artifact_roots: list[str] = []
    component_roots: list[str] = []
    if include_default:
        artifact_roots.extend(
            artifact
            for artifact in default_selection.get("artifacts", [])
            if isinstance(artifact, str) and artifact
        )
        component_roots.extend(
            component
            for component in default_selection.get("components", [])
            if isinstance(component, str) and component
        )
    for selection in requested:
        matched = False
        if selection in artifacts_by_distribution:
            matched = True
            if selection not in artifact_roots:
                artifact_roots.append(selection)
        if selection in components_by_name:
            matched = True
            if selection not in component_roots:
                component_roots.append(selection)
        if not matched:
            raise ValueError(f"unknown package selection {selection}")

    selected: set[str] = set()
    default_selected: set[str] = set()
    visiting: set[str] = set()
    excluded_categories = tuple(
        category
        for category in default_selection.get("excludedCategories", [])
        if isinstance(category, str) and category
    )

    default_component_roots = (
        {
            component
            for component in default_selection.get("components", [])
            if isinstance(component, str) and component
        }
        if include_default
        else set()
    )
    for component_name in component_roots:
        collecting_default = component_name in default_component_roots
        stack: list[tuple[str, bool]] = [(component_name, False)]
        while stack:
            current, expanded = stack.pop()
            if current in selected:
                continue
            component = components_by_name.get(current)
            if component is None:
                raise ValueError(f"unknown package component {current}")
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
                for dependency in component.get("dependsOn", [])
                if isinstance(dependency, str) and dependency.strip() and dependency not in selected
            ]
            for dependency in reversed(dependencies):
                stack.append((dependency, False))

    for component_name in sorted(selected):
        component = components_by_name[component_name]
        forbidden_dependencies = {
            dependency
            for dependency in component.get("forbiddenDependencies", [])
            if isinstance(dependency, str) and dependency
        }
        selected_forbidden = sorted(forbidden_dependencies & selected)
        if selected_forbidden:
            raise ValueError(
                "forbidden package dependency "
                f"{selected_forbidden[0]!r} selected for component {component_name!r}"
            )

    excluded_category_set = set(excluded_categories)
    if excluded_category_set:
        for component_name in sorted(default_selected):
            component = components_by_name[component_name]
            blocked = sorted(_package_categories(component) & excluded_category_set)
            if blocked:
                raise ValueError(
                    f"default component closure includes excluded category {blocked[0]!r} "
                    f"from component {component_name!r}"
                )

    selected_artifacts = set(artifact_roots)
    for component_name in selected:
        artifact = components_by_name[component_name].get("artifact")
        if not isinstance(artifact, str) or artifact not in artifacts_by_distribution:
            raise ValueError(f"component {component_name!r} maps to unknown artifact {artifact!r}")
        selected_artifacts.add(artifact)

    artifact_roots_to_resolve = set(selected_artifacts)
    selected_artifacts.clear()
    artifact_visiting: set[str] = set()
    for artifact_root in sorted(artifact_roots_to_resolve):
        artifact_stack = [(artifact_root, False)]
        while artifact_stack:
            artifact_name, expanded = artifact_stack.pop()
            if artifact_name in selected_artifacts:
                continue
            artifact = artifacts_by_distribution.get(artifact_name)
            if artifact is None:
                raise ValueError(f"unknown package artifact {artifact_name}")
            if expanded:
                artifact_visiting.discard(artifact_name)
                selected_artifacts.add(artifact_name)
                continue
            if artifact_name in artifact_visiting:
                raise ValueError(f"artifact dependency cycle includes {artifact_name}")
            artifact_visiting.add(artifact_name)
            artifact_stack.append((artifact_name, True))
            for dependency in reversed(artifact.get("dependsOn", [])):
                if isinstance(dependency, str) and dependency not in selected_artifacts:
                    artifact_stack.append((dependency, False))

    entries: list[PackageLockEntry] = []
    for component_name in sorted(selected):
        component = components_by_name[component_name]
        artifact_name = str(component["artifact"])
        artifact = artifacts_by_distribution[artifact_name]
        dependencies = tuple(
            dependency
            for dependency in component.get("dependsOn", [])
            if isinstance(dependency, str) and dependency
        )
        forbidden_dependencies = tuple(
            dependency
            for dependency in component.get("forbiddenDependencies", [])
            if isinstance(dependency, str) and dependency
        )
        entries.append(
            PackageLockEntry(
                distribution=component_name,
                artifact=artifact_name,
                version_constraint=(
                    artifact.get("versionConstraint")
                    if isinstance(artifact.get("versionConstraint"), str)
                    else None
                ),
                import_package=(
                    component.get("import") if isinstance(component.get("import"), str) else None
                ),
                default=bool(component.get("default", False)),
                layer=component.get("layer") if isinstance(component.get("layer"), str) else None,
                kind=component.get("kind") if isinstance(component.get("kind"), str) else None,
                stability=(
                    component.get("stability")
                    if isinstance(component.get("stability"), str)
                    else None
                ),
                dependencies=dependencies,
                forbidden_dependencies=forbidden_dependencies,
            )
        )

    return PackageLock(
        catalog_version=int(catalog.get("catalogVersion", 0)),
        spec_version=str(catalog.get("specVersion", "")),
        requested=tuple(requested),
        entries=tuple(entries),
        artifacts=tuple(selected_artifacts),
        excluded_categories=excluded_categories,
    )


class _ManifestOutsideRootError(ValueError):
    pass


def _read_manifest_beneath_root(root_path: Path, manifest_ref: str) -> tuple[Path, str]:
    reference = Path(manifest_ref)
    if reference.is_absolute():
        raise _ManifestOutsideRootError(manifest_ref)
    resolved_root = root_path.resolve()
    candidate = (resolved_root / reference).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise _ManifestOutsideRootError(manifest_ref)

    relative_path = candidate.relative_to(resolved_root)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(resolved_root, directory_flags | no_follow)
    opened_directories = [directory_fd]
    file_fd: int | None = None
    try:
        for part in relative_path.parts[:-1]:
            directory_fd = os.open(
                part,
                directory_flags | no_follow,
                dir_fd=directory_fd,
            )
            opened_directories.append(directory_fd)
        if not relative_path.parts:
            raise IsADirectoryError(str(candidate))
        file_fd = os.open(
            relative_path.parts[-1],
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow,
            dir_fd=directory_fd,
        )
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise OSError(f"manifest is not a regular file: {candidate}")
        with os.fdopen(file_fd, encoding="utf-8") as manifest_file:
            file_fd = None
            manifest_text = manifest_file.read()
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for opened_fd in reversed(opened_directories):
            os.close(opened_fd)
    return candidate, manifest_text


def _supports_python_version(requires_python: str, version: str) -> bool:
    if not requires_python.strip():
        return False
    try:
        return SpecifierSet(requires_python).contains(Version(version), prereleases=True)
    except (InvalidSpecifier, InvalidVersion):
        return False


def build_wheel_matrix(
    root: str | Path,
    *,
    python_versions: tuple[str, ...] = ("3.11", "3.12"),
    catalog: dict[str, Any] | None = None,
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

    for index, version in enumerate(python_versions):
        try:
            if not isinstance(version, str) or not version.strip():
                raise InvalidVersion(str(version))
            Version(version)
        except InvalidVersion:
            return WheelMatrix(
                targets=(),
                diagnostics=(
                    Diagnostic(
                        "WheelPythonVersionInvalid",
                        f"wheel matrix Python version is invalid: {version!r}",
                        f"$.python_versions[{index}]",
                    ),
                ),
            )

    if catalog is None:
        catalog = load_package_catalog()
    raw_artifacts = catalog.get("artifacts", [])
    artifacts = raw_artifacts if isinstance(raw_artifacts, list) else []
    manifest_owners: dict[Path, str] = {}
    for artifact_index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            continue
        artifact_kind = artifact.get("kind")
        if artifact_kind not in {"pure_python", "native_wheel"}:
            continue
        distribution = artifact.get("distribution")
        if not isinstance(distribution, str) or not distribution.strip():
            diagnostics.append(
                Diagnostic(
                    "WheelDistributionMissing",
                    "Python artifact must declare distribution",
                    f"$.artifacts[{artifact_index}].distribution",
                )
            )
            continue
        distribution = distribution.strip()
        manifest_ref = artifact.get("manifest")
        if not isinstance(manifest_ref, str) or not manifest_ref.strip():
            diagnostics.append(
                Diagnostic(
                    "WheelManifestMissing",
                    f"wheel artifact {distribution!r} must declare manifest",
                    f"$.artifacts[{artifact_index}].manifest",
                )
            )
            continue
        try:
            manifest_path, manifest_text = _read_manifest_beneath_root(root_path, manifest_ref)
        except _ManifestOutsideRootError:
            diagnostics.append(
                Diagnostic(
                    "WheelManifestOutsideRoot",
                    f"wheel artifact {distribution!r} manifest must remain beneath root",
                    f"$.artifacts[{artifact_index}].manifest",
                )
            )
            continue
        except (RuntimeError, ValueError):
            diagnostics.append(
                Diagnostic(
                    "WheelManifestInvalid",
                    f"wheel artifact {distribution!r} manifest path is invalid",
                    f"$.artifacts[{artifact_index}].manifest",
                )
            )
            continue
        except (FileNotFoundError, NotADirectoryError):
            diagnostics.append(
                Diagnostic(
                    "WheelManifestMissing",
                    f"wheel artifact {distribution!r} manifest does not exist",
                    f"$.artifacts[{artifact_index}].manifest",
                )
            )
            continue
        except OSError as error:
            diagnostics.append(
                Diagnostic(
                    "WheelManifestInvalid",
                    f"wheel artifact {distribution!r} manifest could not be read safely: {error}",
                    f"$.artifacts[{artifact_index}].manifest",
                )
            )
            continue
        relative_path = manifest_path.relative_to(root_path.resolve()).as_posix()
        if manifest_path in manifest_owners:
            diagnostics.append(
                Diagnostic(
                    "WheelManifestDuplicate",
                    (
                        f"wheel artifacts {manifest_owners[manifest_path]!r} and "
                        f"{distribution!r} use the same manifest"
                    ),
                    f"$.artifacts[{artifact_index}].manifest",
                )
            )
            continue
        manifest_owners[manifest_path] = distribution
        path_prefix = f"$.{relative_path}"
        try:
            manifest = tomllib.loads(manifest_text)
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
        manifest_distribution = project.get("name")
        if not isinstance(manifest_distribution, str) or not manifest_distribution.strip():
            diagnostics.append(
                Diagnostic(
                    "WheelDistributionMissing",
                    "wheel pyproject must declare project.name",
                    f"{path_prefix}.project.name",
                )
            )
            continue
        manifest_distribution = manifest_distribution.strip()
        if canonicalize_name(manifest_distribution) != canonicalize_name(distribution):
            diagnostics.append(
                Diagnostic(
                    "WheelDistributionMismatch",
                    (
                        f"wheel artifact {distribution!r} manifest declares "
                        f"project.name {manifest_distribution!r}"
                    ),
                    f"{path_prefix}.project.name",
                )
            )
            continue
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
        try:
            SpecifierSet(requires_python)
        except InvalidSpecifier as error:
            diagnostics.append(
                Diagnostic(
                    "WheelPythonRequiresInvalid",
                    f"wheel target {distribution!r} declares invalid project.requires-python: {error}",
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
        build_system = manifest.get("build-system", {})
        if isinstance(build_system, dict):
            requirements = build_system.get("requires", [])
            if isinstance(requirements, list):
                for index, dependency in enumerate(requirements):
                    if not isinstance(dependency, str):
                        continue
                    dependency_name = _python_dependency_name(dependency)
                    if dependency_name in blocked_dependencies:
                        diagnostics.append(
                            Diagnostic(
                                "PackageBlockedDependency",
                                f"dependency {dependency_name!r} is blocked by vulnerability policy",
                                f"$.{relative_path}.build-system.requires[{index}]",
                            )
                        )
        dependency_groups = manifest.get("dependency-groups", {})
        if isinstance(dependency_groups, dict):
            for group in sorted(dependency_groups):
                dependencies = dependency_groups[group]
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
                                f"$.{relative_path}.dependency-groups.{group}[{index}]",
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

    raw_artifacts = catalog.get("artifacts", [])
    artifacts = raw_artifacts if isinstance(raw_artifacts, list) else []
    artifacts_by_distribution: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_artifacts, list):
        diagnostics.append(
            Diagnostic(
                "PackageArtifactsInvalid",
                "package catalog artifacts must be a list",
                "$.artifacts",
            )
        )
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            diagnostics.append(
                Diagnostic(
                    "PackageArtifactEntryInvalid",
                    "package catalog artifact entries must be mappings",
                    f"$.artifacts[{index}]",
                )
            )
            continue
        distribution = artifact.get("distribution")
        if not isinstance(distribution, str) or not distribution.strip():
            diagnostics.append(
                Diagnostic(
                    "PackageArtifactDistributionMissing",
                    "package catalog artifacts require distribution",
                    f"$.artifacts[{index}].distribution",
                )
            )
            continue
        distribution = distribution.strip()
        if distribution in artifacts_by_distribution:
            diagnostics.append(
                Diagnostic(
                    "PackageArtifactDuplicateDistribution",
                    f"duplicate artifact distribution {distribution!r}",
                    f"$.artifacts[{index}].distribution",
                )
            )
            continue
        artifacts_by_distribution[distribution] = artifact

    raw_components = catalog.get("components", [])
    components = raw_components if isinstance(raw_components, list) else []
    components_by_name: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_components, list):
        diagnostics.append(
            Diagnostic(
                "PackageComponentsInvalid",
                "package catalog components must be a list",
                "$.components",
            )
        )
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            diagnostics.append(
                Diagnostic(
                    "PackageComponentEntryInvalid",
                    "package catalog component entries must be mappings",
                    f"$.components[{index}]",
                )
            )
            continue
        name = component.get("name")
        if not isinstance(name, str) or not name.strip():
            diagnostics.append(
                Diagnostic(
                    "PackageComponentNameMissing",
                    "package catalog components require name",
                    f"$.components[{index}].name",
                )
            )
            continue
        name = name.strip()
        if name in components_by_name:
            diagnostics.append(
                Diagnostic(
                    "PackageComponentDuplicateName",
                    f"duplicate component name {name!r}",
                    f"$.components[{index}].name",
                )
            )
            continue
        components_by_name[name] = component

    for distribution in sorted(artifacts_by_distribution):
        artifact = artifacts_by_distribution[distribution]
        raw_dependencies = artifact.get("dependsOn", [])
        dependencies = raw_dependencies if isinstance(raw_dependencies, list) else []
        if not isinstance(raw_dependencies, list):
            diagnostics.append(
                Diagnostic(
                    "PackageArtifactDependenciesInvalid",
                    "artifact dependsOn must be a list",
                    f"$.artifacts.{distribution}.dependsOn",
                )
            )
        for index, dependency in enumerate(dependencies):
            if not isinstance(dependency, str) or not dependency.strip():
                diagnostics.append(
                    Diagnostic(
                        "PackageArtifactDependencyInvalid",
                        "artifact dependencies must be non-empty strings",
                        f"$.artifacts.{distribution}.dependsOn[{index}]",
                    )
                )
            elif dependency not in artifacts_by_distribution:
                diagnostics.append(
                    Diagnostic(
                        "PackageArtifactDependencyMissing",
                        f"artifact {distribution!r} depends on unknown artifact {dependency!r}",
                        f"$.artifacts.{distribution}.dependsOn[{index}]",
                    )
                )

    artifact_states: dict[str, str] = {}
    artifact_reported_cycles: set[frozenset[str]] = set()
    for root_distribution in sorted(artifacts_by_distribution):
        if artifact_states.get(root_distribution) == "done":
            continue
        stack: list[tuple[str, int]] = [(root_distribution, 0)]
        path: list[str] = []
        while stack:
            current, next_index = stack[-1]
            if current not in artifact_states:
                artifact_states[current] = "visiting"
                path.append(current)
            artifact = artifacts_by_distribution[current]
            dependencies = [
                dependency
                for dependency in artifact.get("dependsOn", [])
                if isinstance(dependency, str) and dependency in artifacts_by_distribution
            ]
            if next_index >= len(dependencies):
                artifact_states[current] = "done"
                stack.pop()
                if path and path[-1] == current:
                    path.pop()
                elif current in path:
                    path.remove(current)
                continue
            dependency = dependencies[next_index]
            stack[-1] = (current, next_index + 1)
            dependency_state = artifact_states.get(dependency)
            if dependency_state == "visiting":
                cycle_start = path.index(dependency) if dependency in path else 0
                cycle = [*path[cycle_start:], dependency]
                cycle_key = frozenset(cycle)
                if cycle_key not in artifact_reported_cycles:
                    artifact_reported_cycles.add(cycle_key)
                    diagnostics.append(
                        Diagnostic(
                            "PackageArtifactDependencyCycle",
                            f"artifact dependency cycle detected: {' -> '.join(cycle)}",
                            f"$.artifacts.{current}.dependsOn",
                        )
                    )
            elif dependency_state != "done":
                stack.append((dependency, 0))

    for name in sorted(components_by_name):
        component = components_by_name[name]
        artifact = component.get("artifact")
        if not isinstance(artifact, str) or not artifact.strip():
            diagnostics.append(
                Diagnostic(
                    "PackageComponentArtifactMissing",
                    f"component {name!r} must map to an artifact",
                    f"$.components.{name}.artifact",
                )
            )
        elif artifact not in artifacts_by_distribution:
            diagnostics.append(
                Diagnostic(
                    "PackageComponentArtifactUnknown",
                    f"component {name!r} maps to unknown artifact {artifact!r}",
                    f"$.components.{name}.artifact",
                )
            )

        import_package = component.get("import")
        if import_package is not None and (
            not isinstance(import_package, str) or not import_package.strip()
        ):
            diagnostics.append(
                Diagnostic(
                    "PackageComponentImportInvalid",
                    f"component {name!r} import must be null or a non-empty string",
                    f"$.components.{name}.import",
                )
            )

        raw_dependencies = component.get("dependsOn", [])
        dependencies = raw_dependencies if isinstance(raw_dependencies, list) else []
        if not isinstance(raw_dependencies, list):
            diagnostics.append(
                Diagnostic(
                    "PackageComponentDependenciesInvalid",
                    "component dependsOn must be a list",
                    f"$.components.{name}.dependsOn",
                )
            )
        for index, dependency in enumerate(dependencies):
            if not isinstance(dependency, str) or not dependency.strip():
                diagnostics.append(
                    Diagnostic(
                        "PackageComponentDependencyInvalid",
                        "component dependencies must be non-empty strings",
                        f"$.components.{name}.dependsOn[{index}]",
                    )
                )
            elif dependency not in components_by_name:
                diagnostics.append(
                    Diagnostic(
                        "PackageComponentDependencyMissing",
                        f"component {name!r} depends on unknown component {dependency!r}",
                        f"$.components.{name}.dependsOn[{index}]",
                    )
                )

        raw_forbidden_dependencies = component.get("forbiddenDependencies", [])
        forbidden_dependencies = (
            raw_forbidden_dependencies
            if isinstance(raw_forbidden_dependencies, list)
            else []
        )
        if not isinstance(raw_forbidden_dependencies, list):
            diagnostics.append(
                Diagnostic(
                    "PackageForbiddenDependenciesInvalid",
                    "component forbiddenDependencies must be a list",
                    f"$.components.{name}.forbiddenDependencies",
                )
            )
        valid_forbidden_dependencies: set[str] = set()
        for index, dependency in enumerate(forbidden_dependencies):
            if not isinstance(dependency, str) or not dependency.strip():
                diagnostics.append(
                    Diagnostic(
                        "PackageForbiddenDependencyInvalid",
                        "component forbidden dependencies must be non-empty strings",
                        f"$.components.{name}.forbiddenDependencies[{index}]",
                    )
                )
            else:
                valid_forbidden_dependencies.add(dependency)
        for index, dependency in enumerate(dependencies):
            if isinstance(dependency, str) and dependency in valid_forbidden_dependencies:
                diagnostics.append(
                    Diagnostic(
                        "PackageForbiddenDependencySelected",
                        f"component {name!r} depends on forbidden dependency {dependency!r}",
                        f"$.components.{name}.dependsOn[{index}]",
                    )
                )
        direct_dependencies = {
            dependency for dependency in dependencies if isinstance(dependency, str)
        }
        transitive_forbidden_dependencies = sorted(
            (
                valid_forbidden_dependencies
                & _package_dependency_closure(name, components_by_name)
            )
            - direct_dependencies
        )
        for dependency in transitive_forbidden_dependencies:
            diagnostics.append(
                Diagnostic(
                    "PackageForbiddenDependencySelected",
                    f"component {name!r} transitively selects forbidden dependency {dependency!r}",
                    f"$.components.{name}.forbiddenDependencies",
                )
            )

    component_states: dict[str, str] = {}
    component_reported_cycles: set[frozenset[str]] = set()
    for root_component in sorted(components_by_name):
        if component_states.get(root_component) == "done":
            continue
        stack = [(root_component, 0)]
        path = []
        while stack:
            current, next_index = stack[-1]
            if current not in component_states:
                component_states[current] = "visiting"
                path.append(current)
            component = components_by_name[current]
            dependencies = [
                dependency
                for dependency in component.get("dependsOn", [])
                if isinstance(dependency, str) and dependency in components_by_name
            ]
            if next_index >= len(dependencies):
                component_states[current] = "done"
                stack.pop()
                if path and path[-1] == current:
                    path.pop()
                elif current in path:
                    path.remove(current)
                continue
            dependency = dependencies[next_index]
            stack[-1] = (current, next_index + 1)
            dependency_state = component_states.get(dependency)
            if dependency_state == "visiting":
                cycle_start = path.index(dependency) if dependency in path else 0
                cycle = [*path[cycle_start:], dependency]
                cycle_key = frozenset(cycle)
                if cycle_key not in component_reported_cycles:
                    component_reported_cycles.add(cycle_key)
                    diagnostics.append(
                        Diagnostic(
                            "PackageComponentDependencyCycle",
                            f"component dependency cycle detected: {' -> '.join(cycle)}",
                            f"$.components.{current}.dependsOn",
                        )
                    )
            elif dependency_state != "done":
                stack.append((dependency, 0))

    default_selection = catalog.get("defaultSelection")
    default_selection = default_selection if isinstance(default_selection, dict) else {}
    raw_default_artifacts = default_selection.get("artifacts", [])
    default_artifacts = (
        raw_default_artifacts if isinstance(raw_default_artifacts, list) else []
    )
    if not isinstance(raw_default_artifacts, list):
        diagnostics.append(
            Diagnostic(
                "PackageDefaultArtifactsInvalid",
                "default artifact selection must be a list",
                "$.defaultSelection.artifacts",
            )
        )
    for index, artifact in enumerate(default_artifacts):
        if not isinstance(artifact, str) or not artifact.strip():
            diagnostics.append(
                Diagnostic(
                    "PackageDefaultArtifactInvalid",
                    "default artifacts must be non-empty strings",
                    f"$.defaultSelection.artifacts[{index}]",
                )
            )
        elif artifact not in artifacts_by_distribution:
            diagnostics.append(
                Diagnostic(
                    "PackageDefaultArtifactMissing",
                    f"default artifact {artifact!r} is not listed in artifacts",
                    f"$.defaultSelection.artifacts[{index}]",
                )
            )

    raw_default_components = default_selection.get("components", [])
    default_components = (
        raw_default_components if isinstance(raw_default_components, list) else []
    )
    if not isinstance(raw_default_components, list):
        diagnostics.append(
            Diagnostic(
                "PackageDefaultComponentsInvalid",
                "default component selection must be a list",
                "$.defaultSelection.components",
            )
        )
    valid_default_components: set[str] = set()
    for index, component in enumerate(default_components):
        if not isinstance(component, str) or not component.strip():
            diagnostics.append(
                Diagnostic(
                    "PackageDefaultComponentInvalid",
                    "default components must be non-empty strings",
                    f"$.defaultSelection.components[{index}]",
                )
            )
        elif component not in components_by_name:
            diagnostics.append(
                Diagnostic(
                    "PackageDefaultComponentMissing",
                    f"default component {component!r} is not listed in components",
                    f"$.defaultSelection.components[{index}]",
                )
            )
        else:
            valid_default_components.add(component)
    flagged_default_components = {
        name
        for name, component in components_by_name.items()
        if component.get("default") is True
    }
    for component in sorted(flagged_default_components ^ valid_default_components):
        diagnostics.append(
            Diagnostic(
                "PackageDefaultComponentMismatch",
                (
                    f"component {component!r} default flag does not match "
                    "defaultSelection.components"
                ),
                f"$.components.{component}.default",
            )
        )

    release_trains = catalog.get("releaseTrains", {})
    if isinstance(release_trains, dict):
        for train_name, train in release_trains.items():
            if not isinstance(train, dict):
                diagnostics.append(
                    Diagnostic(
                        "PackageReleaseTrainInvalid",
                        "release train entries must be mappings",
                        f"$.releaseTrains.{train_name}",
                    )
                )
                continue
            if "components" in train:
                train_components = train.get("components")
                if not isinstance(train_components, list):
                    diagnostics.append(
                        Diagnostic(
                            "PackageReleaseTrainComponentsInvalid",
                            "release train components must be a list",
                            f"$.releaseTrains.{train_name}.components",
                        )
                    )
                else:
                    for index, component in enumerate(train_components):
                        if not isinstance(component, str) or component not in components_by_name:
                            diagnostics.append(
                                Diagnostic(
                                    "PackageReleaseTrainComponentMissing",
                                    f"release train references unknown component {component!r}",
                                    f"$.releaseTrains.{train_name}.components[{index}]",
                                )
                            )
            compatibility_by = train.get("compatibilityBy")
            if compatibility_by is not None and (
                not isinstance(compatibility_by, list)
                or any(
                    not isinstance(check, str) or not check.strip()
                    for check in compatibility_by
                )
            ):
                diagnostics.append(
                    Diagnostic(
                        "PackageReleaseTrainConformanceInvalid",
                        "release train compatibilityBy must contain non-empty strings",
                        f"$.releaseTrains.{train_name}.compatibilityBy",
                    )
                )

    extension_components = catalog.get("extensionComponents", {})
    if isinstance(extension_components, dict):
        for group_name, group_components in extension_components.items():
            if not isinstance(group_components, list):
                diagnostics.append(
                    Diagnostic(
                        "PackageExtensionComponentsInvalid",
                        "extension component groups must be lists",
                        f"$.extensionComponents.{group_name}",
                    )
                )
                continue
            for index, component in enumerate(group_components):
                if not isinstance(component, str) or component not in components_by_name:
                    diagnostics.append(
                        Diagnostic(
                            "PackageExtensionComponentMissing",
                            f"extension group references unknown component {component!r}",
                            f"$.extensionComponents.{group_name}[{index}]",
                        )
                    )

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
            root_path = root_path.resolve()
            python_artifacts = [
                (distribution, artifact)
                for distribution, artifact in artifacts_by_distribution.items()
                if artifact.get("kind") in {"pure_python", "native_wheel"}
            ]
            manifest_paths: dict[str, Path] = {}
            manifest_texts: dict[str, str] = {}
            manifest_owners: dict[Path, str] = {}
            for distribution, artifact in python_artifacts:
                manifest_ref = artifact.get("manifest")
                if not isinstance(manifest_ref, str) or not manifest_ref.strip():
                    diagnostics.append(
                        Diagnostic(
                            "PackageArtifactManifestMissing",
                            f"Python artifact {distribution!r} must declare manifest",
                            f"$.artifacts.{distribution}.manifest",
                        )
                    )
                    continue
                try:
                    manifest_path, manifest_text = _read_manifest_beneath_root(
                        root_path,
                        manifest_ref,
                    )
                except _ManifestOutsideRootError:
                    diagnostics.append(
                        Diagnostic(
                            "PackageArtifactManifestOutsideRoot",
                            f"artifact {distribution!r} manifest must remain beneath root",
                            f"$.artifacts.{distribution}.manifest",
                        )
                    )
                    continue
                except (RuntimeError, ValueError):
                    diagnostics.append(
                        Diagnostic(
                            "PackageArtifactManifestInvalid",
                            f"artifact {distribution!r} manifest path is invalid",
                            f"$.artifacts.{distribution}.manifest",
                        )
                    )
                    continue
                except (FileNotFoundError, NotADirectoryError):
                    diagnostics.append(
                        Diagnostic(
                            "PackageArtifactManifestMissing",
                            f"artifact {distribution!r} manifest does not exist",
                            f"$.artifacts.{distribution}.manifest",
                        )
                    )
                    continue
                except OSError as error:
                    diagnostics.append(
                        Diagnostic(
                            "PackageArtifactManifestInvalid",
                            f"artifact {distribution!r} manifest could not be read safely: {error}",
                            f"$.artifacts.{distribution}.manifest",
                        )
                    )
                    continue
                if manifest_path in manifest_owners:
                    diagnostics.append(
                        Diagnostic(
                            "PackageArtifactManifestDuplicate",
                            (
                                f"artifacts {manifest_owners[manifest_path]!r} and "
                                f"{distribution!r} use the same manifest"
                            ),
                            f"$.artifacts.{distribution}.manifest",
                        )
                    )
                    continue
                manifest_owners[manifest_path] = distribution
                manifest_paths[distribution] = manifest_path
                manifest_texts[distribution] = manifest_text

            local_versions: dict[str, str] = {}
            parsed_manifests: dict[str, dict[str, Any]] = {}
            for distribution, manifest_path in manifest_paths.items():
                try:
                    candidate_manifest = tomllib.loads(manifest_texts[distribution])
                except tomllib.TOMLDecodeError as error:
                    relative_path = manifest_path.relative_to(root_path).as_posix()
                    diagnostics.append(
                        Diagnostic(
                            "PackageManifestInvalid",
                            f"invalid Python artifact manifest: {error}",
                            f"$.{relative_path}",
                        )
                    )
                    continue
                parsed_manifests[distribution] = candidate_manifest
                candidate_project = candidate_manifest.get("project")
                if not isinstance(candidate_project, dict):
                    continue
                candidate_name = candidate_project.get("name")
                candidate_version = candidate_project.get("version")
                if (
                    isinstance(candidate_name, str)
                    and candidate_name.strip()
                    and isinstance(candidate_version, str)
                    and candidate_version.strip()
                ):
                    local_versions[canonicalize_name(candidate_name)] = (
                        candidate_version.strip()
                    )

            known_artifacts_by_canonical = {
                canonicalize_name(distribution): distribution
                for distribution in artifacts_by_distribution
            }
            for distribution, manifest_path in manifest_paths.items():
                manifest = parsed_manifests.get(distribution)
                if manifest is None:
                    continue
                artifact = artifacts_by_distribution[distribution]
                relative_path = manifest_path.relative_to(root_path).as_posix()
                project = manifest.get("project")
                if not isinstance(project, dict):
                    diagnostics.append(
                        Diagnostic(
                            "PackageManifestInvalid",
                            "Python artifact manifests require a project table",
                            f"$.{relative_path}.project",
                        )
                    )
                    continue
                manifest_distribution = project.get("name")
                if not isinstance(manifest_distribution, str) or not manifest_distribution.strip():
                    diagnostics.append(
                        Diagnostic(
                            "PackageManifestDistributionMissing",
                            "Python artifact manifest must declare project.name",
                            f"$.{relative_path}.project.name",
                        )
                    )
                    continue
                manifest_distribution = manifest_distribution.strip()
                if canonicalize_name(manifest_distribution) != canonicalize_name(distribution):
                    diagnostics.append(
                        Diagnostic(
                            "PackageManifestDistributionMismatch",
                            (
                                f"artifact {distribution!r} manifest declares "
                                f"project.name {manifest_distribution!r}"
                            ),
                            f"$.{relative_path}.project.name",
                        )
                    )
                    continue

                expected_dependencies = tuple(
                    dependency
                    for dependency in artifact.get("dependsOn", [])
                    if isinstance(dependency, str) and dependency
                )
                dependencies = project.get("dependencies", [])
                actual_dependencies: list[str] = []
                if isinstance(dependencies, list):
                    for dependency_index, dependency in enumerate(dependencies):
                        if not isinstance(dependency, str):
                            continue
                        dependency_requirement = dependency.strip()
                        try:
                            parsed_requirement = Requirement(dependency_requirement)
                        except InvalidRequirement as error:
                            diagnostics.append(
                                Diagnostic(
                                    "PackageManifestDependencyRequirementInvalid",
                                    (
                                        f"artifact manifest for {distribution!r} has invalid "
                                        f"dependency requirement {dependency_requirement!r}: {error}"
                                    ),
                                    (
                                        f"$.{relative_path}.project.dependencies"
                                        f"[{dependency_index}]"
                                    ),
                                )
                            )
                            normalized_requirement = dependency_requirement.lower().replace(
                                "_", "-"
                            )
                            for canonical_name, artifact_name in known_artifacts_by_canonical.items():
                                if normalized_requirement.startswith(canonical_name):
                                    suffix = normalized_requirement[
                                        len(canonical_name) : len(canonical_name) + 1
                                    ]
                                    if not suffix or suffix in "[<>=!~ @;":
                                        if artifact_name not in actual_dependencies:
                                            actual_dependencies.append(artifact_name)
                                        break
                            continue
                        canonical_name = canonicalize_name(parsed_requirement.name)
                        dependency_name = known_artifacts_by_canonical.get(canonical_name)
                        if (
                            dependency_name is not None
                            and dependency_name not in actual_dependencies
                        ):
                            actual_dependencies.append(dependency_name)
                        local_version = local_versions.get(canonical_name)
                        if dependency_name is not None and local_version is not None:
                            try:
                                parsed_local_version = Version(local_version)
                            except InvalidVersion as error:
                                diagnostics.append(
                                    Diagnostic(
                                        "PackageManifestVersionInvalid",
                                        (
                                            f"local artifact {dependency_name!r} has invalid "
                                            f"version {local_version!r}: {error}"
                                        ),
                                        (
                                            f"$.{relative_path}.project.dependencies"
                                            f"[{dependency_index}]"
                                        ),
                                    )
                                )
                            else:
                                if (
                                    parsed_requirement.specifier
                                    and not parsed_requirement.specifier.contains(
                                        parsed_local_version,
                                        prereleases=True,
                                    )
                                ):
                                    diagnostics.append(
                                        Diagnostic(
                                            "PackageManifestDependencyVersionUnsatisfied",
                                            (
                                                f"artifact manifest for {distribution!r} requires "
                                                f"{dependency_requirement!r}, but the local version "
                                                f"is {local_version!r}"
                                            ),
                                            (
                                                f"$.{relative_path}.project.dependencies"
                                                f"[{dependency_index}]"
                                            ),
                                        )
                                    )
                dependency_path = f"$.{relative_path}.project.dependencies"
                for dependency in expected_dependencies:
                    if dependency not in actual_dependencies:
                        diagnostics.append(
                            Diagnostic(
                                "PackageManifestDependencyMissing",
                                (
                                    f"artifact manifest for {distribution!r} is missing "
                                    f"catalog dependency {dependency!r}"
                                ),
                                dependency_path,
                            )
                        )
                for dependency in actual_dependencies:
                    if dependency not in expected_dependencies:
                        diagnostics.append(
                            Diagnostic(
                                "PackageManifestDependencyUnexpected",
                                (
                                    f"artifact manifest for {distribution!r} declares "
                                    f"uncataloged artifact dependency {dependency!r}"
                                ),
                                dependency_path,
                            )
                        )

    excluded_categories = {
        category
        for category in default_selection.get("excludedCategories", [])
        if isinstance(category, str) and category
    }
    if excluded_categories and valid_default_components:
        try:
            default_lock = build_package_lock(catalog, requested=(), include_default=True)
        except ValueError as error:
            default_lock = None
            if "default component closure includes excluded category" in str(error):
                diagnostics.append(
                    Diagnostic(
                        "PackageDefaultIncludesExcludedCategory",
                        str(error),
                        "$.defaultSelection.excludedCategories",
                    )
                )
        if default_lock is not None:
            for entry in default_lock.entries:
                component = components_by_name.get(entry.distribution, {})
                blocked = sorted(_package_categories(component) & excluded_categories)
                if blocked:
                    diagnostics.append(
                        Diagnostic(
                            "PackageDefaultIncludesExcludedCategory",
                            f"default component closure includes excluded category {blocked[0]!r}",
                            f"$.components.{entry.distribution}.categories",
                        )
                    )

    return DiagnosticSet(tuple(diagnostics))

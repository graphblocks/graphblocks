from __future__ import annotations

import ast
from collections.abc import Collection
from functools import cache
from importlib import import_module
from pathlib import Path


_ProductionModule = tuple[Path, ast.Module, bool]


@cache
def _production_modules() -> tuple[Path, dict[str, _ProductionModule]]:
    repository_root = Path(__file__).resolve().parents[1]
    source_roots = [
        repository_root / "src",
        *sorted((repository_root / "packages").glob("*/src")),
    ]
    modules: dict[str, _ProductionModule] = {}

    for source_root in source_roots:
        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            module_parts = path.relative_to(source_root).with_suffix("").parts
            is_package = module_parts[-1] == "__init__"
            if is_package:
                module_parts = module_parts[:-1]
            module_name = ".".join(module_parts)
            modules[module_name] = (path, tree, is_package)

    return repository_root, modules


def _syntactic_import_dependencies(
    *,
    module_name: str,
    tree: ast.Module,
    is_package: bool,
    tracked_modules: Collection[str],
) -> set[str]:
    package_parts = module_name.split(".")
    if not is_package:
        package_parts = package_parts[:-1]
    type_only_imports: set[int] = set()
    for candidate in ast.walk(tree):
        if not isinstance(candidate, ast.If):
            continue
        is_type_checking = (
            isinstance(candidate.test, ast.Name)
            and candidate.test.id in {"TYPE_CHECKING", "_TYPE_CHECKING"}
        ) or (
            isinstance(candidate.test, ast.Attribute)
            and isinstance(candidate.test.value, ast.Name)
            and candidate.test.value.id == "typing"
            and candidate.test.attr == "TYPE_CHECKING"
        )
        if not is_type_checking:
            continue
        for statement in candidate.body:
            for child in ast.walk(statement):
                if isinstance(child, ast.Import | ast.ImportFrom):
                    type_only_imports.add(id(child))

    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in type_only_imports:
            continue
        targets: list[tuple[str, bool]] = []
        if isinstance(node, ast.Import):
            targets.extend(
                (alias.name, alias.name == module_name)
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                imported_module = node.module or ""
            else:
                parent_count = node.level - 1
                imported_parts = package_parts[
                    : len(package_parts) - parent_count
                ]
                if node.module is not None:
                    imported_parts = (
                        *imported_parts,
                        *node.module.split("."),
                    )
                imported_module = ".".join(imported_parts)
            is_explicit_self_import = (
                imported_module == module_name and node.module is not None
            )
            for alias in node.names:
                imported_name = (
                    imported_module
                    if alias.name == "*"
                    else f"{imported_module}.{alias.name}".strip(".")
                )
                if imported_name in tracked_modules:
                    targets.append(
                        (
                            imported_name,
                            is_explicit_self_import
                            or imported_name == module_name,
                        )
                    )
                else:
                    targets.append(
                        (imported_module, is_explicit_self_import)
                    )

        for target, is_explicit_self_import in targets:
            target_parts = target.split(".")
            while target_parts:
                candidate = ".".join(target_parts)
                if candidate in tracked_modules:
                    if (
                        candidate == module_name
                        and not is_explicit_self_import
                    ):
                        break
                    dependencies.add(candidate)
                    break
                target_parts.pop()

    return dependencies


def test_internal_modules_do_not_import_the_package_root() -> None:
    repository_root, modules = _production_modules()
    violations: list[str] = []

    for module_name, (path, tree, is_package) in modules.items():
        package_parts = module_name.split(".")
        if not is_package:
            package_parts = package_parts[:-1]
        for node in ast.walk(tree):
            imports_package_root = isinstance(
                node, ast.Import
            ) and any(alias.name == "graphblocks" for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    imported_module = node.module
                else:
                    parent_count = node.level - 1
                    imported_parts = package_parts[
                        : len(package_parts) - parent_count
                    ]
                    if node.module is not None:
                        imported_parts = (
                            *imported_parts,
                            *node.module.split("."),
                        )
                    imported_module = ".".join(imported_parts)
                imports_package_root = imported_module == "graphblocks"
            if imports_package_root:
                relative_path = path.relative_to(repository_root)
                violations.append(f"{relative_path}:{node.lineno}")

    assert not violations, (
        "internal modules must import the defining leaf module, not the graphblocks "
        f"package root: {', '.join(violations)}"
    )


def test_production_python_syntactic_import_graph_is_acyclic() -> None:
    _repository_root, modules = _production_modules()
    edges = {
        module_name: _syntactic_import_dependencies(
            module_name=module_name,
            tree=tree,
            is_package=is_package,
            tracked_modules=modules,
        )
        for module_name, (_path, tree, is_package) in modules.items()
    }

    next_index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(module_name: str) -> None:
        nonlocal next_index
        indices[module_name] = next_index
        lowlinks[module_name] = next_index
        next_index += 1
        stack.append(module_name)
        on_stack.add(module_name)

        for dependency in sorted(edges[module_name]):
            if dependency not in indices:
                visit(dependency)
                lowlinks[module_name] = min(
                    lowlinks[module_name],
                    lowlinks[dependency],
                )
            elif dependency in on_stack:
                lowlinks[module_name] = min(
                    lowlinks[module_name],
                    indices[dependency],
                )

        if lowlinks[module_name] != indices[module_name]:
            return
        component: list[str] = []
        while stack:
            dependency = stack.pop()
            on_stack.remove(dependency)
            component.append(dependency)
            if dependency == module_name:
                break
        components.append(tuple(sorted(component)))

    for module_name in sorted(modules):
        if module_name not in indices:
            visit(module_name)

    cycles = sorted(
        component
        for component in components
        if len(component) > 1 or component[0] in edges[component[0]]
    )
    assert not cycles, (
        "production Python syntactic imports must be acyclic: "
        + "; ".join(", ".join(component) for component in cycles)
    )


def test_syntactic_import_graph_distinguishes_self_from_native_children() -> None:
    tracked_modules = {"pkg", "pkg.foo"}
    self_imports = ast.parse(
        "from . import foo\n"
        "from pkg import foo\n"
    )
    native_child_import = ast.parse("from ._native import value\n")

    assert _syntactic_import_dependencies(
        module_name="pkg.foo",
        tree=self_imports,
        is_package=False,
        tracked_modules=tracked_modules,
    ) == {"pkg.foo"}
    assert not _syntactic_import_dependencies(
        module_name="pkg",
        tree=native_child_import,
        is_package=True,
        tracked_modules=tracked_modules,
    )


def test_lazy_facades_preserve_exports_and_directory_contract() -> None:
    for facade_name in (
        "graphblocks.budget",
        "graphblocks.canonical",
        "graphblocks.documents",
    ):
        facade = import_module(facade_name)
        export_modules = facade._LAZY_EXPORT_MODULES

        names_before_resolution = dir(facade)
        assert len(names_before_resolution) == len(set(names_before_resolution))

        for export_name, target_name in export_modules.items():
            target = import_module(target_name, facade.__package__)
            assert getattr(facade, export_name) is getattr(target, export_name)

        names_after_resolution = dir(facade)
        assert len(names_after_resolution) == len(set(names_after_resolution))

        missing_name = "_definitely_not_a_graphblocks_export"
        try:
            getattr(facade, missing_name)
        except AttributeError as error:
            assert missing_name in str(error)
        else:
            raise AssertionError(f"{facade_name} accepted an unknown export")


def test_canonical_lazy_export_preserves_wildcard_and_rebinding_contracts() -> None:
    canonical = import_module("graphblocks.canonical")
    migration = import_module("graphblocks.migration")
    wildcard_namespace: dict[str, object] = {}
    missing = object()
    previously_cached = canonical.__dict__.pop(
        "migrate_document",
        missing,
    )

    try:
        exec(
            "from graphblocks.canonical import *",
            wildcard_namespace,
        )

        assert (
            wildcard_namespace["migrate_document"]
            is migration.migrate_document
        )

        calls: list[dict[str, object]] = []

        def fake_migrate_document(
            document: dict[str, object],
        ) -> dict[str, object]:
            calls.append(document)
            return {"migrated": document}

        original = canonical.migrate_document
        canonical.migrate_document = fake_migrate_document
        try:
            normalized = canonical.normalize_graph({"raw": True})
        finally:
            canonical.migrate_document = original

        assert normalized == {"migrated": {"raw": True}}
        assert calls == [{"raw": True}]
    finally:
        canonical.__dict__.pop("migrate_document", None)
        if previously_cached is not missing:
            canonical.migrate_document = previously_cached

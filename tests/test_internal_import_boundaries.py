from __future__ import annotations

import ast
from pathlib import Path


def test_internal_modules_do_not_import_the_package_root() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    source_roots = [
        repository_root / "src",
        *sorted((repository_root / "packages").glob("*/src")),
    ]
    violations: list[str] = []

    for source_root in source_roots:
        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative_path = path.relative_to(repository_root)
            module_parts = path.relative_to(source_root).with_suffix("").parts
            package_parts = module_parts[:-1]
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
                    violations.append(f"{relative_path}:{node.lineno}")

    assert not violations, (
        "internal modules must import the defining leaf module, not the graphblocks "
        f"package root: {', '.join(violations)}"
    )

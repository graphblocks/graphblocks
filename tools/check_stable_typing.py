#!/usr/bin/env python3
"""Verify strict-mypy ownership or time-bounded debt for every stable symbol."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path, PurePosixPath
import re
import sys
import tomllib

import yaml


ROOT = Path(__file__).resolve().parents[1]
SURFACE_PATH = Path("compatibility/stable-python-surface.yaml")
DEBT_PATH = Path("compatibility/stable-python-typing-debt.yaml")
PYPROJECT_PATH = Path("pyproject.toml")
PACKAGE_INIT_PATH = Path("src/graphblocks/__init__.py")
_ISSUE_PATTERN = re.compile(r"TYPE-[0-9]{3,}")


def _load_yaml(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _exact_nonempty_string(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return None
    return value


def _stable_symbols(surface: object) -> tuple[str, ...]:
    if not isinstance(surface, Mapping):
        raise ValueError("stable Python surface must contain a mapping")
    raw_symbols = surface.get("symbols")
    if not isinstance(raw_symbols, list) or not raw_symbols:
        raise ValueError("stable Python surface must enumerate symbols")
    symbols: list[str] = []
    for index, entry in enumerate(raw_symbols):
        if not isinstance(entry, Mapping):
            raise ValueError(f"stable Python surface symbol {index} must be a mapping")
        path = _exact_nonempty_string(entry.get("path"))
        if path is None or not path.startswith("graphblocks."):
            raise ValueError(
                f"stable Python surface symbol {index} has an invalid path"
            )
        symbols.append(path)
    duplicates = sorted(
        symbol for symbol, count in Counter(symbols).items() if count > 1
    )
    if duplicates:
        raise ValueError(
            "stable Python surface contains duplicate symbols: " + ", ".join(duplicates)
        )
    return tuple(symbols)


def _root_export_owners(package_init: Path) -> dict[str, str]:
    tree = ast.parse(
        package_init.read_text(encoding="utf-8"),
        filename=str(package_init),
    )
    owners: dict[str, str] = {}
    owner_imports = [
        node for node in tree.body if isinstance(node, ast.ImportFrom)
    ]
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        is_type_checking = (
            isinstance(node.test, ast.Name)
            and node.test.id in {"TYPE_CHECKING", "_TYPE_CHECKING"}
        ) or (
            isinstance(node.test, ast.Attribute)
            and isinstance(node.test.value, ast.Name)
            and node.test.value.id == "typing"
            and node.test.attr == "TYPE_CHECKING"
        )
        if is_type_checking:
            owner_imports.extend(
                statement
                for statement in node.body
                if isinstance(statement, ast.ImportFrom)
            )
    for node in owner_imports:
        if node.level != 1 or not node.module:
            continue
        module = f"graphblocks.{node.module}"
        for imported in node.names:
            if imported.name == "*":
                raise ValueError(
                    "graphblocks package root must not use wildcard exports"
                )
            public_name = imported.asname or imported.name
            previous = owners.get(public_name)
            if previous is not None and previous != module:
                raise ValueError(
                    f"graphblocks root export {public_name!r} has multiple owners"
                )
            owners[public_name] = module
    return owners


def _root_public_exports(package_init: Path) -> tuple[str, ...]:
    tree = ast.parse(
        package_init.read_text(encoding="utf-8"),
        filename=str(package_init),
    )
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        )
    ]
    if len(assignments) != 1:
        raise ValueError("graphblocks package root must assign __all__ exactly once")
    raw_exports = ast.literal_eval(assignments[0].value)
    if not isinstance(raw_exports, (list, tuple)):
        raise ValueError("graphblocks package root __all__ must be a list or tuple")
    exports: list[str] = []
    for index, raw_export in enumerate(raw_exports):
        export = _exact_nonempty_string(raw_export)
        if export is None:
            raise ValueError(
                f"graphblocks package root __all__ entry {index} must be an exact nonempty string"
            )
        exports.append(export)
    duplicates = sorted(
        export for export, count in Counter(exports).items() if count > 1
    )
    if duplicates:
        raise ValueError(
            "graphblocks package root __all__ contains duplicates: "
            + ", ".join(duplicates)
        )
    return tuple(exports)


def validate_root_exports(
    *,
    stable_symbols: Sequence[str],
    public_exports: Sequence[str],
) -> list[str]:
    stable_root_exports = tuple(
        dict.fromkeys(symbol.split(".", 2)[1] for symbol in stable_symbols)
    )
    stable_root_set = set(stable_root_exports)
    public_export_set = set(public_exports)
    errors = [
        f"stable root export is missing from __all__: {export}"
        for export in sorted(stable_root_set - public_export_set)
    ]
    errors.extend(
        f"preview or internal root export is present in __all__: {export}"
        for export in sorted(public_export_set - stable_root_set)
    )
    if not errors and tuple(public_exports) != stable_root_exports:
        errors.append("graphblocks package root __all__ must follow stable surface order")
    return errors


def _symbol_owner_map(
    symbols: Sequence[str],
    export_owners: Mapping[str, str],
) -> dict[str, str]:
    owners: dict[str, str] = {}
    for symbol in symbols:
        parts = symbol.split(".")
        if len(parts) < 2 or parts[0] != "graphblocks":
            raise ValueError(f"stable Python symbol has an invalid path: {symbol}")
        root_name = parts[1]
        owner = export_owners.get(root_name)
        if owner is None:
            raise ValueError(
                f"stable Python symbol is not an explicit root export: {symbol}"
            )
        owners[symbol] = owner
    return owners


def _module_from_source_path(raw_path: str) -> str | None:
    path = PurePosixPath(raw_path.replace("\\", "/"))
    if len(path.parts) < 3 or path.parts[0] != "src":
        return None
    if path.suffix != ".py":
        return None
    module_parts = list(path.parts[1:])
    if module_parts[-1] == "__init__.py":
        module_parts.pop()
    else:
        module_parts[-1] = path.stem
    if not module_parts:
        return None
    return ".".join(module_parts)


def _strict_modules(pyproject: object) -> set[str]:
    if not isinstance(pyproject, Mapping):
        raise ValueError("pyproject.toml must contain a mapping")
    tool = pyproject.get("tool")
    if not isinstance(tool, Mapping):
        raise ValueError("pyproject.toml must contain [tool.mypy]")
    mypy = tool.get("mypy")
    if not isinstance(mypy, Mapping):
        raise ValueError("pyproject.toml must contain [tool.mypy]")
    if mypy.get("strict") is not True:
        raise ValueError("[tool.mypy] must enable strict = true")
    if mypy.get("follow_imports") != "silent":
        raise ValueError("[tool.mypy] must set follow_imports = \"silent\"")
    raw_files = mypy.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("[tool.mypy].files must enumerate checked paths")
    modules: set[str] = set()
    for index, raw_path in enumerate(raw_files):
        path = _exact_nonempty_string(raw_path)
        if path is None:
            raise ValueError(
                f"[tool.mypy].files entry {index} must be an exact nonempty string"
            )
        module = _module_from_source_path(path)
        if module is not None:
            modules.add(module)
    return modules


def _review_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or value != value.strip():
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if value == parsed.isoformat() else None


def validate_typing_coverage(
    *,
    stable_symbols: Sequence[str],
    symbol_owners: Mapping[str, str],
    strict_modules: set[str],
    debt: object,
    today: date,
) -> list[str]:
    """Return deterministic contract violations for stable typing coverage."""

    errors: list[str] = []
    stable_counts = Counter(stable_symbols)
    stable_set = set(stable_symbols)
    for symbol in sorted(
        symbol for symbol, count in stable_counts.items() if count > 1
    ):
        errors.append(f"stable symbol is duplicated: {symbol}")
    for symbol in sorted(stable_set - set(symbol_owners)):
        errors.append(f"stable symbol has no owner: {symbol}")
    for symbol in sorted(set(symbol_owners) - stable_set):
        errors.append(f"unknown symbol has an owner mapping: {symbol}")

    actual_modules = {
        symbol_owners[symbol] for symbol in stable_set if symbol in symbol_owners
    }
    if not isinstance(debt, Mapping):
        return [*errors, "typing debt registry must contain a mapping"]
    if debt.get("version") != 1:
        errors.append("typing debt registry version must be 1")
    raw_modules = debt.get("modules")
    if not isinstance(raw_modules, list):
        return [*errors, "typing debt registry must enumerate modules"]

    seen_modules: set[str] = set()
    seen_issues: set[str] = set()
    debt_symbol_counts: Counter[str] = Counter()
    for index, entry in enumerate(raw_modules):
        label = f"typing debt module {index}"
        if not isinstance(entry, Mapping):
            errors.append(f"{label} must be a mapping")
            continue
        module = _exact_nonempty_string(entry.get("module"))
        if module is None:
            errors.append(f"{label} module must be an exact nonempty string")
            continue
        label = f"typing debt module {module}"
        if module in seen_modules:
            errors.append(f"debt module is registered more than once: {module}")
        seen_modules.add(module)
        if module not in actual_modules:
            errors.append(f"unknown stable owner module in debt: {module}")
        if module in strict_modules:
            errors.append(f"strict module has stale debt: {module}")

        issue = _exact_nonempty_string(entry.get("issue"))
        if issue is None or _ISSUE_PATTERN.fullmatch(issue) is None:
            errors.append(f"{label} issue must match TYPE-###")
        elif issue in seen_issues:
            errors.append(f"typing debt issue is registered more than once: {issue}")
        else:
            seen_issues.add(issue)
        for field_name in ("owner", "reason"):
            if _exact_nonempty_string(entry.get(field_name)) is None:
                errors.append(f"{label} {field_name} must be an exact nonempty string")
        review_by = _review_date(entry.get("review_by"))
        if review_by is None:
            errors.append(f"{label} review_by must be an ISO date")
        elif review_by < today:
            errors.append(f"{label} debt review expired on {review_by.isoformat()}")

        raw_symbols = entry.get("symbols")
        if not isinstance(raw_symbols, list) or not raw_symbols:
            errors.append(f"{label} must enumerate symbols")
            continue
        local_symbols: set[str] = set()
        for symbol_index, raw_symbol in enumerate(raw_symbols):
            symbol = _exact_nonempty_string(raw_symbol)
            if symbol is None:
                errors.append(
                    f"{label} symbol {symbol_index} must be an exact nonempty string"
                )
                continue
            if symbol in local_symbols or debt_symbol_counts[symbol] > 0:
                errors.append(f"debt symbol is registered more than once: {symbol}")
            local_symbols.add(symbol)
            debt_symbol_counts[symbol] += 1
            if symbol not in stable_set:
                errors.append(f"unknown stable symbol in debt: {symbol}")
                continue
            actual_owner = symbol_owners.get(symbol)
            if actual_owner != module:
                errors.append(
                    f"debt symbol owner mismatch for {symbol}: "
                    f"expected {actual_owner}, found {module}"
                )

    for module in sorted(actual_modules):
        if module not in strict_modules and module not in seen_modules:
            errors.append(
                f"stable owner has neither strict coverage nor debt: {module}"
            )
    for symbol in sorted(stable_set):
        owner = symbol_owners.get(symbol)
        debt_count = debt_symbol_counts[symbol]
        if owner in strict_modules:
            if debt_count:
                errors.append(f"strict stable symbol has stale debt: {symbol}")
        elif debt_count == 0:
            errors.append(f"stable symbol is not covered: {symbol}")
        elif debt_count > 1:
            errors.append(f"stable symbol has duplicate debt coverage: {symbol}")
    return sorted(set(errors))


def check_repository(
    root: Path = ROOT,
    *,
    today: date | None = None,
) -> list[str]:
    surface = _load_yaml(root / SURFACE_PATH)
    stable_symbols = _stable_symbols(surface)
    export_owners = _root_export_owners(root / PACKAGE_INIT_PATH)
    public_exports = _root_public_exports(root / PACKAGE_INIT_PATH)
    symbol_owners = _symbol_owner_map(stable_symbols, export_owners)
    with (root / PYPROJECT_PATH).open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)
    strict_modules = _strict_modules(pyproject)
    debt = _load_yaml(root / DEBT_PATH)
    return sorted(
        {
            *validate_root_exports(
                stable_symbols=stable_symbols,
                public_exports=public_exports,
            ),
            *validate_typing_coverage(
                stable_symbols=stable_symbols,
                symbol_owners=symbol_owners,
                strict_modules=strict_modules,
                debt=debt,
                today=today or date.today(),
            ),
        }
    )


def main() -> int:
    try:
        errors = check_repository()
        stable_symbols = _stable_symbols(_load_yaml(ROOT / SURFACE_PATH))
    except (
        OSError,
        SyntaxError,
        ValueError,
        tomllib.TOMLDecodeError,
        yaml.YAMLError,
    ) as error:
        print(f"stable typing configuration error: {error}", file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(f"stable typing error: {error}", file=sys.stderr)
        return 1
    print(
        "OK stable Python typing ownership: "
        f"{len(stable_symbols)} symbols are strict or have current debt"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

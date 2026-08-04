#!/usr/bin/env python3
"""Verify strict-mypy ownership or time-bounded debt for every stable symbol."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from importlib.metadata import version as distribution_version
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tomllib
import tokenize

import yaml


ROOT = Path(__file__).resolve().parents[1]
SURFACE_PATH = Path("compatibility/stable-python-surface.yaml")
DEBT_PATH = Path("compatibility/stable-python-typing-debt.yaml")
SCOPE_BUDGET_PATH = Path("compatibility/python-typing-scope.yaml")
PREVIEW_BUDGET_PATH = Path("compatibility/python-preview-typing-budget.yaml")
PYPROJECT_PATH = Path("pyproject.toml")
PACKAGE_INIT_PATH = Path("src/graphblocks/__init__.py")
PRODUCTION_SOURCE_ROOT = Path("src/graphblocks")
_PREVIEW_MYPY_MODE = "strict-no-incremental-follow-imports-silent"
_ISSUE_PATTERN = re.compile(r"TYPE-[0-9]{3,}")
_CODED_TYPE_IGNORE_PATTERN = re.compile(
    r"# type: ignore\[[a-z][a-z0-9-]*(?:,\s*[a-z][a-z0-9-]*)*\]"
    r"(?:\s+#.*)?\Z"
)


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


def _type_ignore_comment_counts(source_path: Path) -> tuple[int, int]:
    count = 0
    uncoded_count = 0
    with source_path.open("rb") as source_file:
        for token in tokenize.tokenize(source_file.readline):
            if token.type != tokenize.COMMENT or not token.string.startswith(
                "# type: ignore"
            ):
                continue
            count += 1
            if _CODED_TYPE_IGNORE_PATTERN.fullmatch(token.string) is None:
                uncoded_count += 1
    return count, uncoded_count


def _production_type_ignore_comment_counts(
    source_root: Path,
) -> tuple[int, int]:
    count = 0
    uncoded_count = 0
    for source_path in sorted(source_root.rglob("*.py")):
        source_count, source_uncoded_count = _type_ignore_comment_counts(source_path)
        count += source_count
        uncoded_count += source_uncoded_count
    return count, uncoded_count


def validate_production_typing_budget(
    *,
    budget: object,
    strict_module_count: int,
    type_ignore_comment_count: int,
    uncoded_type_ignore_comment_count: int,
) -> list[str]:
    """Return production strict-scope and no-new-ignore budget violations."""

    if not isinstance(budget, Mapping):
        return ["production typing scope budget must contain a mapping"]
    expected_fields = {
        "version",
        "productionSourceRoot",
        "minimumStrictModuleCount",
        "maximumTypeIgnoreCommentCount",
        "maximumUncodedTypeIgnoreCommentCount",
    }
    errors: list[str] = []
    unknown_fields = sorted(set(budget) - expected_fields)
    missing_fields = sorted(expected_fields - set(budget))
    if unknown_fields:
        errors.append(
            "production typing scope budget has unknown fields: "
            + ", ".join(unknown_fields)
        )
    if missing_fields:
        errors.append(
            "production typing scope budget is missing fields: "
            + ", ".join(missing_fields)
        )
    if budget.get("version") != 1:
        errors.append("production typing scope budget version must be 1")
    if budget.get("productionSourceRoot") != PRODUCTION_SOURCE_ROOT.as_posix():
        errors.append(
            "production typing scope budget source root must be "
            f"{PRODUCTION_SOURCE_ROOT.as_posix()}"
        )
    minimum_strict_modules = budget.get("minimumStrictModuleCount")
    if (
        isinstance(minimum_strict_modules, bool)
        or not isinstance(minimum_strict_modules, int)
        or minimum_strict_modules < 1
    ):
        errors.append(
            "production typing scope minimumStrictModuleCount must be a "
            "positive integer"
        )
    elif strict_module_count < minimum_strict_modules:
        errors.append(
            "production strict mypy scope regressed: "
            f"expected at least {minimum_strict_modules} modules, "
            f"found {strict_module_count}"
        )
    maximum_type_ignores = budget.get("maximumTypeIgnoreCommentCount")
    if (
        isinstance(maximum_type_ignores, bool)
        or not isinstance(maximum_type_ignores, int)
        or maximum_type_ignores < 0
    ):
        errors.append(
            "production typing scope maximumTypeIgnoreCommentCount must be "
            "a non-negative integer"
        )
    elif type_ignore_comment_count > maximum_type_ignores:
        errors.append(
            "production type-ignore debt increased: "
            f"maximum {maximum_type_ignores}, found {type_ignore_comment_count}"
        )
    maximum_uncoded_type_ignores = budget.get(
        "maximumUncodedTypeIgnoreCommentCount"
    )
    if (
        isinstance(maximum_uncoded_type_ignores, bool)
        or not isinstance(maximum_uncoded_type_ignores, int)
        or maximum_uncoded_type_ignores < 0
    ):
        errors.append(
            "production typing scope maximumUncodedTypeIgnoreCommentCount "
            "must be a non-negative integer"
        )
    elif uncoded_type_ignore_comment_count > maximum_uncoded_type_ignores:
        errors.append(
            "production uncoded type-ignore debt increased: "
            f"maximum {maximum_uncoded_type_ignores}, "
            f"found {uncoded_type_ignore_comment_count}"
        )
    return errors


def _preview_package_configs(budget: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(budget, Mapping):
        raise ValueError("preview typing budget must contain a mapping")
    expected_fields = {"version", "mypyVersion", "mypyMode", "packages"}
    unknown_fields = sorted(set(budget) - expected_fields)
    missing_fields = sorted(expected_fields - set(budget))
    if unknown_fields:
        raise ValueError(
            "preview typing budget has unknown fields: " + ", ".join(unknown_fields)
        )
    if missing_fields:
        raise ValueError(
            "preview typing budget is missing fields: " + ", ".join(missing_fields)
        )
    if budget.get("version") != 1:
        raise ValueError("preview typing budget version must be 1")
    if _exact_nonempty_string(budget.get("mypyVersion")) is None:
        raise ValueError("preview typing budget mypyVersion must be exact")
    if budget.get("mypyMode") != _PREVIEW_MYPY_MODE:
        raise ValueError(f"preview typing budget mypyMode must be {_PREVIEW_MYPY_MODE}")
    raw_packages = budget.get("packages")
    if not isinstance(raw_packages, list) or not raw_packages:
        raise ValueError("preview typing budget must enumerate packages")

    required_package_fields = {
        "distribution",
        "sourceRoot",
        "minimumStrictModuleCount",
        "maximumDebtModuleCount",
        "maximumDiagnosticCount",
        "maximumTypeIgnoreCommentCount",
        "maximumUncodedTypeIgnoreCommentCount",
    }
    optional_package_fields = {
        "rootCompatibilityMap",
        "maximumPreviewCompatibilityAliasCount",
    }
    packages: list[Mapping[str, object]] = []
    seen_distributions: set[str] = set()
    seen_source_roots: set[str] = set()
    for index, entry in enumerate(raw_packages):
        label = f"preview typing package {index}"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{label} must be a mapping")
        unknown = sorted(set(entry) - required_package_fields - optional_package_fields)
        missing = sorted(required_package_fields - set(entry))
        if unknown:
            raise ValueError(f"{label} has unknown fields: " + ", ".join(unknown))
        if missing:
            raise ValueError(f"{label} is missing fields: " + ", ".join(missing))
        distribution = _exact_nonempty_string(entry.get("distribution"))
        source_root = _exact_nonempty_string(entry.get("sourceRoot"))
        if distribution is None:
            raise ValueError(f"{label} distribution must be exact")
        if source_root is None:
            raise ValueError(f"{label} sourceRoot must be exact")
        if distribution in seen_distributions:
            raise ValueError(f"preview typing package is duplicated: {distribution}")
        if source_root in seen_source_roots:
            raise ValueError(f"preview typing source root is duplicated: {source_root}")
        seen_distributions.add(distribution)
        seen_source_roots.add(source_root)
        for field_name in (
            "minimumStrictModuleCount",
            "maximumDebtModuleCount",
            "maximumDiagnosticCount",
            "maximumTypeIgnoreCommentCount",
            "maximumUncodedTypeIgnoreCommentCount",
        ):
            value = entry.get(field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} {field_name} must be a non-negative integer")
        has_compatibility_map = "rootCompatibilityMap" in entry
        has_alias_budget = "maximumPreviewCompatibilityAliasCount" in entry
        if has_compatibility_map != has_alias_budget:
            raise ValueError(
                f"{label} must declare rootCompatibilityMap and "
                "maximumPreviewCompatibilityAliasCount together"
            )
        if has_compatibility_map:
            if _exact_nonempty_string(entry.get("rootCompatibilityMap")) is None:
                raise ValueError(f"{label} rootCompatibilityMap must be exact")
            alias_budget = entry.get("maximumPreviewCompatibilityAliasCount")
            if (
                isinstance(alias_budget, bool)
                or not isinstance(alias_budget, int)
                or alias_budget < 0
            ):
                raise ValueError(
                    f"{label} maximumPreviewCompatibilityAliasCount must be "
                    "a non-negative integer"
                )
        packages.append(entry)
    return tuple(packages)


def collect_preview_typing_report(
    *,
    root: Path,
    budget: object,
    strict_modules: set[str],
) -> dict[str, object]:
    """Run strict mypy across every shipped Python package and classify debt."""

    package_configs = _preview_package_configs(budget)
    source_roots = [str(entry["sourceRoot"]) for entry in package_configs]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--follow-imports=silent",
            "--no-incremental",
            "--output",
            "json",
            *source_roots,
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in {0, 1}:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(
            "preview typing mypy execution failed" + (f": {detail}" if detail else "")
        )

    diagnostic_counts: Counter[str] = Counter()
    normalized_roots = {
        str(entry["distribution"]): str(entry["sourceRoot"]).replace("\\", "/")
        for entry in package_configs
    }
    for index, line in enumerate(completed.stdout.splitlines()):
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"preview typing mypy output line {index + 1} is not JSON"
            ) from error
        if not isinstance(record, Mapping):
            raise ValueError(
                f"preview typing mypy output line {index + 1} must be an object"
            )
        if record.get("severity") != "error":
            continue
        raw_path = _exact_nonempty_string(record.get("file"))
        if raw_path is None:
            raise ValueError(
                f"preview typing mypy output line {index + 1} has no exact file"
            )
        path = raw_path.replace("\\", "/")
        if not any(
            path == source_root or path.startswith(f"{source_root}/")
            for source_root in normalized_roots.values()
        ):
            raise ValueError(
                f"preview typing diagnostic is outside package roots: {path}"
            )
        diagnostic_counts[path] += 1

    package_reports: list[dict[str, object]] = []
    for entry in package_configs:
        distribution = str(entry["distribution"])
        source_root_text = normalized_roots[distribution]
        source_root = root / source_root_text
        if not source_root.is_dir():
            raise ValueError(
                f"preview typing source root does not exist: {source_root_text}"
            )
        module_reports: list[dict[str, object]] = []
        for source_path in sorted(source_root.rglob("*.py")):
            relative = source_path.relative_to(source_root)
            module_parts = [source_root.name, *relative.parts]
            if module_parts[-1] == "__init__.py":
                module_parts.pop()
            else:
                module_parts[-1] = source_path.stem
            module = ".".join(module_parts)
            relative_path = source_path.relative_to(root).as_posix()
            ignore_count, uncoded_ignore_count = _type_ignore_comment_counts(
                source_path
            )
            module_reports.append(
                {
                    "module": module,
                    "path": relative_path,
                    "classification": (
                        "strict" if module in strict_modules else "debt"
                    ),
                    "diagnosticCount": diagnostic_counts[relative_path],
                    "typeIgnoreCommentCount": ignore_count,
                    "uncodedTypeIgnoreCommentCount": uncoded_ignore_count,
                }
            )

        compatibility_alias_count = 0
        compatibility_map = entry.get("rootCompatibilityMap")
        if compatibility_map is not None:
            compatibility_path = root / str(compatibility_map)
            tree = ast.parse(
                compatibility_path.read_text(encoding="utf-8"),
                filename=str(compatibility_path),
            )
            assignments = [
                node
                for node in tree.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name)
                    and target.id == "_ROOT_COMPAT_EXPORTS_BY_MODULE"
                    for target in node.targets
                )
            ]
            if len(assignments) != 1:
                raise ValueError(
                    "preview root compatibility map must be assigned exactly once"
                )
            raw_mapping = ast.literal_eval(assignments[0].value)
            if not isinstance(raw_mapping, dict):
                raise ValueError("preview root compatibility map must be a mapping")
            for module, aliases in raw_mapping.items():
                if (
                    _exact_nonempty_string(module) is None
                    or not isinstance(aliases, (list, tuple))
                    or any(_exact_nonempty_string(alias) is None for alias in aliases)
                ):
                    raise ValueError(
                        "preview root compatibility map entries must contain exact aliases"
                    )
                compatibility_alias_count += len(aliases)

        strict_count = sum(
            module["classification"] == "strict" for module in module_reports
        )
        package_reports.append(
            {
                "distribution": distribution,
                "sourceRoot": source_root_text,
                "moduleCount": len(module_reports),
                "strictModuleCount": strict_count,
                "debtModuleCount": len(module_reports) - strict_count,
                "diagnosticCount": sum(
                    int(module["diagnosticCount"]) for module in module_reports
                ),
                "typeIgnoreCommentCount": sum(
                    int(module["typeIgnoreCommentCount"]) for module in module_reports
                ),
                "uncodedTypeIgnoreCommentCount": sum(
                    int(module["uncodedTypeIgnoreCommentCount"])
                    for module in module_reports
                ),
                "previewCompatibilityAliasCount": compatibility_alias_count,
                "modules": module_reports,
            }
        )

    return {
        "reportVersion": 1,
        "mypyVersion": distribution_version("mypy"),
        "mypyMode": _PREVIEW_MYPY_MODE,
        "packages": package_reports,
    }


def validate_preview_typing_budget(
    *,
    budget: object,
    report: object,
) -> list[str]:
    """Return package-level preview typing debt and report-integrity violations."""

    package_configs = _preview_package_configs(budget)
    if not isinstance(report, Mapping):
        return ["preview typing report must contain a mapping"]
    errors: list[str] = []
    if report.get("reportVersion") != 1:
        errors.append("preview typing report version must be 1")
    if report.get("mypyVersion") != budget.get("mypyVersion"):
        errors.append(
            "preview typing mypy version drifted: "
            f"expected {budget.get('mypyVersion')}, "
            f"found {report.get('mypyVersion')}"
        )
    if report.get("mypyMode") != _PREVIEW_MYPY_MODE:
        errors.append(f"preview typing report mode must be {_PREVIEW_MYPY_MODE}")
    raw_reports = report.get("packages")
    if not isinstance(raw_reports, list):
        return [*errors, "preview typing report must enumerate packages"]
    report_by_distribution: dict[str, Mapping[str, object]] = {}
    for index, package_report in enumerate(raw_reports):
        if not isinstance(package_report, Mapping):
            errors.append(f"preview typing report package {index} must be a mapping")
            continue
        distribution = _exact_nonempty_string(package_report.get("distribution"))
        if distribution is None:
            errors.append(
                f"preview typing report package {index} distribution must be exact"
            )
            continue
        if distribution in report_by_distribution:
            errors.append(
                f"preview typing report package is duplicated: {distribution}"
            )
        report_by_distribution[distribution] = package_report

    configured_distributions = {str(entry["distribution"]) for entry in package_configs}
    for distribution in sorted(configured_distributions - set(report_by_distribution)):
        errors.append(f"preview typing report is missing package: {distribution}")
    for distribution in sorted(set(report_by_distribution) - configured_distributions):
        errors.append(f"preview typing report has unknown package: {distribution}")

    for config in package_configs:
        distribution = str(config["distribution"])
        package_report = report_by_distribution.get(distribution)
        if package_report is None:
            continue
        if package_report.get("sourceRoot") != config.get("sourceRoot"):
            errors.append(f"preview typing source root drifted for {distribution}")
        integer_fields = (
            "moduleCount",
            "strictModuleCount",
            "debtModuleCount",
            "diagnosticCount",
            "typeIgnoreCommentCount",
            "uncodedTypeIgnoreCommentCount",
            "previewCompatibilityAliasCount",
        )
        valid_metrics = True
        for field_name in integer_fields:
            value = package_report.get(field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errors.append(
                    f"preview typing {distribution} {field_name} must be a "
                    "non-negative integer"
                )
                valid_metrics = False
        raw_modules = package_report.get("modules")
        if not isinstance(raw_modules, list):
            errors.append(f"preview typing {distribution} must enumerate modules")
            continue
        seen_modules: set[str] = set()
        module_sums = {
            "strictModuleCount": 0,
            "debtModuleCount": 0,
            "diagnosticCount": 0,
            "typeIgnoreCommentCount": 0,
            "uncodedTypeIgnoreCommentCount": 0,
        }
        for module_index, module_report in enumerate(raw_modules):
            label = f"preview typing {distribution} module {module_index}"
            if not isinstance(module_report, Mapping):
                errors.append(f"{label} must be a mapping")
                continue
            module = _exact_nonempty_string(module_report.get("module"))
            path = _exact_nonempty_string(module_report.get("path"))
            classification = module_report.get("classification")
            if module is None or path is None:
                errors.append(f"{label} identity must be exact")
                continue
            if module in seen_modules:
                errors.append(f"preview typing module is duplicated: {module}")
            seen_modules.add(module)
            if classification not in {"strict", "debt"}:
                errors.append(f"{label} classification must be strict or debt")
            else:
                module_sums[f"{classification}ModuleCount"] += 1
            for field_name in (
                "diagnosticCount",
                "typeIgnoreCommentCount",
                "uncodedTypeIgnoreCommentCount",
            ):
                value = module_report.get(field_name)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    errors.append(
                        f"{label} {field_name} must be a non-negative integer"
                    )
                else:
                    module_sums[field_name] += value
        if valid_metrics:
            if package_report["moduleCount"] != len(raw_modules):
                errors.append(
                    f"preview typing {distribution} moduleCount is inconsistent"
                )
            for field_name, observed in module_sums.items():
                if package_report[field_name] != observed:
                    errors.append(
                        f"preview typing {distribution} {field_name} is inconsistent"
                    )

            comparisons = (
                ("strictModuleCount", "minimumStrictModuleCount", "regressed below"),
                ("debtModuleCount", "maximumDebtModuleCount", "increased above"),
                ("diagnosticCount", "maximumDiagnosticCount", "increased above"),
                (
                    "typeIgnoreCommentCount",
                    "maximumTypeIgnoreCommentCount",
                    "increased above",
                ),
                (
                    "uncodedTypeIgnoreCommentCount",
                    "maximumUncodedTypeIgnoreCommentCount",
                    "increased above",
                ),
                (
                    "previewCompatibilityAliasCount",
                    "maximumPreviewCompatibilityAliasCount",
                    "increased above",
                ),
            )
            for report_field, budget_field, direction in comparisons:
                if budget_field not in config:
                    continue
                observed = int(package_report[report_field])
                limit = int(config[budget_field])
                violates = (
                    observed < limit
                    if direction == "regressed below"
                    else observed > limit
                )
                if violates:
                    errors.append(
                        f"preview typing {distribution} {report_field} {direction} "
                        f"budget {limit}: found {observed}"
                    )
    return sorted(set(errors))


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


def analyze_repository(
    root: Path = ROOT,
    *,
    today: date | None = None,
) -> tuple[list[str], dict[str, object]]:
    surface = _load_yaml(root / SURFACE_PATH)
    stable_symbols = _stable_symbols(surface)
    export_owners = _root_export_owners(root / PACKAGE_INIT_PATH)
    public_exports = _root_public_exports(root / PACKAGE_INIT_PATH)
    symbol_owners = _symbol_owner_map(stable_symbols, export_owners)
    with (root / PYPROJECT_PATH).open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)
    strict_modules = _strict_modules(pyproject)
    debt = _load_yaml(root / DEBT_PATH)
    scope_budget = _load_yaml(root / SCOPE_BUDGET_PATH)
    preview_budget = _load_yaml(root / PREVIEW_BUDGET_PATH)
    preview_report = collect_preview_typing_report(
        root=root,
        budget=preview_budget,
        strict_modules=strict_modules,
    )
    (
        type_ignore_comment_count,
        uncoded_type_ignore_comment_count,
    ) = _production_type_ignore_comment_counts(
        root / PRODUCTION_SOURCE_ROOT
    )
    errors = sorted(
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
            *validate_production_typing_budget(
                budget=scope_budget,
                strict_module_count=len(strict_modules),
                type_ignore_comment_count=type_ignore_comment_count,
                uncoded_type_ignore_comment_count=(
                    uncoded_type_ignore_comment_count
                ),
            ),
            *validate_preview_typing_budget(
                budget=preview_budget,
                report=preview_report,
            ),
        }
    )
    return errors, preview_report


def check_repository(
    root: Path = ROOT,
    *,
    today: date | None = None,
) -> list[str]:
    errors, _ = analyze_repository(root, today=today)
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify stable ownership and bounded preview typing debt."
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="write the deterministic package/module debt report as JSON",
    )
    args = parser.parse_args(argv)
    try:
        errors, preview_report = analyze_repository()
        stable_symbols = _stable_symbols(_load_yaml(ROOT / SURFACE_PATH))
    except (
        OSError,
        SyntaxError,
        ValueError,
        tomllib.TOMLDecodeError,
        tokenize.TokenError,
        yaml.YAMLError,
    ) as error:
        print(f"stable typing configuration error: {error}", file=sys.stderr)
        return 2
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(preview_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if errors:
        for error in errors:
            print(f"stable typing error: {error}", file=sys.stderr)
        return 1
    print(
        "OK stable Python typing ownership: "
        f"{len(stable_symbols)} symbols are strict or have current debt; "
        "preview debt "
        + ", ".join(
            f"{package['distribution']}="
            f"{package['debtModuleCount']} modules/"
            f"{package['diagnosticCount']} diagnostics"
            for package in preview_report["packages"]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

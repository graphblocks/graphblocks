#!/usr/bin/env python3
"""Check or intentionally refresh the first-stable compatibility snapshots."""

from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stderr, redirect_stdout
import dataclasses
import difflib
import importlib
import inspect
from io import StringIO
import json
from pathlib import Path
import sys
from typing import Any

import yaml


ROOT = Path(__file__).parents[1]
COMPATIBILITY_ROOT = ROOT / "compatibility"
PYTHON_SURFACE_PATH = COMPATIBILITY_ROOT / "stable-python-surface.yaml"
PYTHON_SNAPSHOT_PATH = COMPATIBILITY_ROOT / "stable-python-api.json"
CLI_CASES_PATH = COMPATIBILITY_ROOT / "stable-cli-cases.yaml"
CLI_SNAPSHOT_PATH = COMPATIBILITY_ROOT / "stable-cli-contracts.json"
TESTING_SURFACE_PATH = COMPATIBILITY_ROOT / "stable-testing-surface.yaml"
TESTING_SNAPSHOT_PATH = COMPATIBILITY_ROOT / "stable-testing-api.json"
TESTING_CLI_CASES_PATH = COMPATIBILITY_ROOT / "stable-testing-cli-cases.yaml"
TESTING_CLI_SNAPSHOT_PATH = COMPATIBILITY_ROOT / "stable-testing-cli-contracts.json"
TESTING_SOURCE_ROOT = ROOT / "packages" / "graphblocks-testing" / "src"
MISSING = dataclasses.MISSING
STANDARD_ANNOTATION_NAMES = frozenset(
    {
        "Any",
        "Callable",
        "ClassVar",
        "Final",
        "Iterable",
        "Iterator",
        "Literal",
        "Mapping",
        "MutableMapping",
        "None",
        "Optional",
        "Path",
        "Protocol",
        "Self",
        "Sequence",
        "TypeAlias",
        "TypeVar",
        "Union",
        "bool",
        "bytearray",
        "bytes",
        "collections",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "object",
        "set",
        "str",
        "tuple",
        "type",
        "typing",
    }
)


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.relative_to(ROOT)} must contain a mapping")
    return value


def _resolve_import_path(path: str, *, package: str) -> object:
    parts = path.split(".")
    if not parts or parts[0] != package:
        raise ValueError(f"stable API path must start with {package!r}: {path!r}")
    value: object = importlib.import_module(parts[0])
    for part in parts[1:]:
        try:
            value = getattr(value, part)
        except AttributeError as error:
            raise ValueError(f"stable API path does not resolve: {path!r}") from error
    return value


def _callable_kind(value: object) -> str:
    if inspect.isclass(value):
        return "class"
    if inspect.ismethod(value):
        return "bound-method"
    if inspect.isfunction(value):
        return "function"
    if callable(value):
        return "callable"
    raise ValueError(f"stable API symbol is not callable: {value!r}")


def _default_value(value: object) -> dict[str, object]:
    if value is MISSING:
        return {"kind": "required"}
    if value is None or isinstance(value, bool | int | float | str):
        try:
            json.dumps(value, allow_nan=False)
        except ValueError:
            pass
        else:
            return {"kind": "value", "value": value}
    return {"kind": "value-repr", "value": repr(value)}


def _factory_name(factory: object) -> str:
    module = getattr(factory, "__module__", None)
    qualname = getattr(factory, "__qualname__", None)
    if isinstance(module, str) and isinstance(qualname, str):
        return f"{module}.{qualname}"
    return repr(factory)


def _dataclass_contract(value: object) -> dict[str, object] | None:
    if not inspect.isclass(value) or not dataclasses.is_dataclass(value):
        return None
    parameters = value.__dataclass_params__
    fields: list[dict[str, object]] = []
    for field in dataclasses.fields(value):
        if field.name.startswith("_"):
            continue
        default = _default_value(field.default)
        if field.default_factory is not MISSING:
            default = {
                "kind": "factory",
                "value": _factory_name(field.default_factory),
            }
        fields.append(
            {
                "name": field.name,
                "annotation": str(field.type),
                "default": default,
                "init": field.init,
                "keywordOnly": field.kw_only,
            }
        )
    return {
        "fields": fields,
        "frozen": parameters.frozen,
        "slots": hasattr(value, "__slots__"),
    }


def _annotation_identifiers(annotation: object) -> set[str]:
    if annotation is inspect.Signature.empty:
        return set()
    source = annotation if isinstance(annotation, str) else inspect.formatannotation(annotation)
    try:
        expression = ast.parse(source, mode="eval")
    except SyntaxError as error:
        raise ValueError(f"stable API annotation is not inspectable: {source!r}") from error
    return {
        node.id
        for node in ast.walk(expression)
        if isinstance(node, ast.Name)
    }


def _annotation_references(value: object) -> set[str]:
    signature = inspect.signature(value)
    references = _annotation_identifiers(signature.return_annotation)
    for parameter in signature.parameters.values():
        references.update(_annotation_identifiers(parameter.annotation))
    if inspect.isclass(value) and dataclasses.is_dataclass(value):
        for field in dataclasses.fields(value):
            if not field.name.startswith("_"):
                references.update(_annotation_identifiers(field.type))
    return references


def _build_python_snapshot(policy_path: Path, *, package: str) -> dict[str, object]:
    policy = _load_yaml(policy_path)
    raw_symbols = policy.get("symbols")
    if not isinstance(raw_symbols, list) or not raw_symbols:
        raise ValueError(f"{policy_path.name} must enumerate symbols")

    resolved: list[tuple[dict[str, object], str, str, object]] = []
    paths: list[str] = []
    for entry in raw_symbols:
        if not isinstance(entry, dict):
            raise ValueError("stable Python symbol entries must be mappings")
        path = entry.get("path")
        profile = entry.get("profile")
        if not isinstance(path, str) or not isinstance(profile, str):
            raise ValueError("stable Python symbols require string path and profile")
        value = _resolve_import_path(path, package=package)
        resolved.append((entry, path, profile, value))
        paths.append(path)

    if len(paths) != len(set(paths)):
        raise ValueError(f"{policy_path.name} contains duplicate paths")

    stable_type_names = {path.split(".", 2)[1] for path in paths}
    symbols: list[dict[str, object]] = []
    for entry, path, profile, value in resolved:
        declared_kind = entry.get("kind", "callable")
        if declared_kind == "type-alias":
            alias_value = str(value)
            references = _annotation_identifiers(alias_value)
            unknown_references = sorted(
                references - STANDARD_ANNOTATION_NAMES - stable_type_names
            )
            if unknown_references and package == "graphblocks":
                raise ValueError(
                    f"stable API type alias {path!r} names unlisted public type(s): "
                    + ", ".join(unknown_references)
                )
            symbols.append(
                {
                    "path": path,
                    "profile": profile,
                    "kind": "type-alias",
                    "value": alias_value,
                    "typeReferences": sorted(
                        references & stable_type_names
                    ),
                }
            )
            continue
        if declared_kind != "callable":
            raise ValueError(
                f"stable Python symbol {path!r} has unsupported kind {declared_kind!r}"
            )
        references = _annotation_references(value)
        unknown_references = sorted(
            references - STANDARD_ANNOTATION_NAMES - stable_type_names
        )
        if unknown_references and package == "graphblocks":
            raise ValueError(
                f"stable API symbol {path!r} names unlisted public type(s): "
                + ", ".join(unknown_references)
            )
        contract: dict[str, object] = {
            "path": path,
            "profile": profile,
            "kind": _callable_kind(value),
            "signature": str(inspect.signature(value)),
            "typeReferences": sorted(
                references & stable_type_names
            ),
        }
        dataclass_contract = _dataclass_contract(value)
        if dataclass_contract is not None:
            contract["dataclass"] = dataclass_contract
        symbols.append(contract)
    return {
        "snapshotVersion": policy.get("snapshotVersion"),
        "targetRelease": policy.get("targetRelease"),
        "readiness": policy.get("readiness"),
        "symbols": symbols,
    }


def build_python_snapshot() -> dict[str, object]:
    return _build_python_snapshot(PYTHON_SURFACE_PATH, package="graphblocks")


def _enable_testing_source_import() -> None:
    source_root = str(TESTING_SOURCE_ROOT)
    if source_root not in sys.path:
        sys.path.insert(0, source_root)


def build_testing_snapshot() -> dict[str, object]:
    _enable_testing_source_import()
    return _build_python_snapshot(TESTING_SURFACE_PATH, package="graphblocks_testing")


def _resolve_cli_argv(argv: list[object]) -> list[str]:
    resolved: list[str] = []
    for value in argv:
        if not isinstance(value, str):
            raise ValueError("stable CLI argv entries must be strings")
        fixture = "fixtures/"
        if value.startswith(fixture):
            resolved.append(str(COMPATIBILITY_ROOT / value))
        else:
            resolved.append(value)
    return resolved


def build_cli_snapshot() -> dict[str, object]:
    from graphblocks.cli import main

    policy = _load_yaml(CLI_CASES_PATH)
    raw_cases = policy.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("stable-cli-cases.yaml must enumerate cases")

    cases: list[dict[str, object]] = []
    case_ids: list[str] = []
    for entry in raw_cases:
        if not isinstance(entry, dict):
            raise ValueError("stable CLI case entries must be mappings")
        case_id = entry.get("id")
        command = entry.get("command")
        profile = entry.get("profile")
        argv = entry.get("argv")
        if not all(isinstance(value, str) for value in (case_id, command, profile)):
            raise ValueError("stable CLI cases require string id, command, and profile")
        if command not in {"validate", "plan", "run"}:
            raise ValueError(f"unsupported stable CLI command: {command!r}")
        if not isinstance(argv, list) or not argv or argv[0] != command:
            raise ValueError(f"stable CLI case {case_id!r} argv must start with its command")

        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(_resolve_cli_argv(argv))
        raw_stdout = stdout.getvalue()
        try:
            stdout_json = json.loads(raw_stdout)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"stable CLI case {case_id!r} did not emit JSON stdout: {raw_stdout!r}"
            ) from error
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise ValueError(f"stable CLI case {case_id!r} returned a non-integer exit code")
        cases.append(
            {
                "id": case_id,
                "profile": profile,
                "command": command,
                "argv": argv,
                "exitCode": exit_code,
                "stdoutJson": stdout_json,
                "stderr": stderr.getvalue(),
            }
        )
        case_ids.append(case_id)

    if len(case_ids) != len(set(case_ids)):
        raise ValueError("stable-cli-cases.yaml contains duplicate case ids")
    return {
        "snapshotVersion": policy.get("snapshotVersion"),
        "targetRelease": policy.get("targetRelease"),
        "readiness": policy.get("readiness"),
        "stdoutContract": policy.get("stdoutContract"),
        "cases": cases,
    }


def build_testing_cli_snapshot() -> dict[str, object]:
    _enable_testing_source_import()
    from graphblocks_testing import main

    policy = _load_yaml(TESTING_CLI_CASES_PATH)
    raw_cases = policy.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("stable-testing-cli-cases.yaml must enumerate cases")

    cases: list[dict[str, object]] = []
    case_ids: list[str] = []
    for entry in raw_cases:
        if not isinstance(entry, dict):
            raise ValueError("stable testing CLI case entries must be mappings")
        case_id = entry.get("id")
        command = entry.get("command")
        profile = entry.get("profile")
        argv = entry.get("argv")
        if not all(isinstance(value, str) for value in (case_id, command, profile)):
            raise ValueError("stable testing CLI cases require string id, command, and profile")
        if command not in {"list", "run-all"}:
            raise ValueError(f"unsupported stable testing CLI command: {command!r}")
        if not isinstance(argv, list) or not argv or argv[0] != command:
            raise ValueError(
                f"stable testing CLI case {case_id!r} argv must start with its command"
            )

        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(_resolve_cli_argv(argv))
        raw_stdout = stdout.getvalue()
        try:
            stdout_json = json.loads(raw_stdout)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"stable testing CLI case {case_id!r} did not emit JSON stdout: "
                f"{raw_stdout!r}"
            ) from error
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise ValueError(
                f"stable testing CLI case {case_id!r} returned a non-integer exit code"
            )
        cases.append(
            {
                "id": case_id,
                "profile": profile,
                "command": command,
                "argv": argv,
                "exitCode": exit_code,
                "stdoutJson": stdout_json,
                "stderr": stderr.getvalue(),
            }
        )
        case_ids.append(case_id)

    if len(case_ids) != len(set(case_ids)):
        raise ValueError("stable-testing-cli-cases.yaml contains duplicate case ids")
    return {
        "snapshotVersion": policy.get("snapshotVersion"),
        "targetRelease": policy.get("targetRelease"),
        "readiness": policy.get("readiness"),
        "stdoutContract": policy.get("stdoutContract"),
        "cases": cases,
    }


def _render(value: dict[str, object]) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _check_or_update(path: Path, actual: dict[str, object], *, update: bool) -> bool:
    rendered = _render(actual)
    if update:
        path.write_text(rendered, encoding="utf-8")
        print(f"updated {_display_path(path)}")
        return True
    expected = path.read_text(encoding="utf-8")
    if expected == rendered:
        print(f"OK {_display_path(path)}")
        return True
    display_path = _display_path(path)
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=display_path,
            tofile="current implementation",
        )
    )
    print(diff, file=sys.stderr)
    print(
        "compatibility snapshot drifted; review the compatibility impact and "
        "run tools/check_compatibility.py --update only for an intentional change",
        file=sys.stderr,
    )
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="intentionally replace snapshots with the current API and CLI contracts",
    )
    args = parser.parse_args(argv)
    try:
        api_snapshot = build_python_snapshot()
        cli_snapshot = build_cli_snapshot()
        testing_api_snapshot = build_testing_snapshot()
        testing_cli_snapshot = build_testing_cli_snapshot()
    except (ImportError, OSError, TypeError, ValueError) as error:
        print(f"compatibility snapshot error: {error}", file=sys.stderr)
        return 2
    results = [
        _check_or_update(PYTHON_SNAPSHOT_PATH, api_snapshot, update=args.update),
        _check_or_update(CLI_SNAPSHOT_PATH, cli_snapshot, update=args.update),
        _check_or_update(
            TESTING_SNAPSHOT_PATH,
            testing_api_snapshot,
            update=args.update,
        ),
        _check_or_update(
            TESTING_CLI_SNAPSHOT_PATH,
            testing_cli_snapshot,
            update=args.update,
        ),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

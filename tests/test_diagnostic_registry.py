from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest
import yaml

from graphblocks.diagnostics import Diagnostic, DiagnosticSet


ROOT = Path(__file__).parents[1]
REGISTRY_PATH = ROOT / "docs" / "specification" / "reference" / "diagnostic-codes.yaml"
STABLE_EMITTERS = {
    ROOT / "src" / "graphblocks" / "compiler.py": {"Diagnostic"},
    ROOT / "src" / "graphblocks" / "plugins.py": {"Diagnostic"},
    ROOT / "src" / "graphblocks" / "schema.py": {"ResourceSchemaViolation"},
}
TESTING_PACKAGE_PATH = (
    ROOT
    / "packages"
    / "graphblocks-testing"
    / "src"
    / "graphblocks_testing"
    / "__init__.py"
)


def test_diagnostic_records_validate_and_snapshot_public_values() -> None:
    source = [Diagnostic("GB0001", "first diagnostic")]
    diagnostics = DiagnosticSet(source)  # type: ignore[arg-type]
    source.append(Diagnostic("GB0002", "second diagnostic"))

    assert diagnostics.diagnostics == (Diagnostic("GB0001", "first diagnostic"),)
    with pytest.raises(ValueError, match="must contain Diagnostic records"):
        DiagnosticSet((object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be a collection"):
        DiagnosticSet(object())  # type: ignore[arg-type]

    for field_name, changes in (
        ("code", {"code": ""}),
        ("message", {"message": object()}),
        ("path", {"path": "\ud800"}),
    ):
        with pytest.raises(ValueError, match=field_name):
            Diagnostic(
                changes.get("code", "GB0001"),  # type: ignore[arg-type]
                changes.get("message", "diagnostic"),  # type: ignore[arg-type]
                changes.get("path", "$"),  # type: ignore[arg-type]
            )


def _literal_diagnostics(path: Path, constructors: set[str]) -> dict[str, str]:
    observed: dict[str, str] = {}
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id not in constructors:
            continue
        code_node: ast.expr | None = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "code":
                code_node = keyword.value
        if not isinstance(code_node, ast.Constant) or not isinstance(code_node.value, str):
            continue
        severity = "error"
        if node.func.id == "Diagnostic" and len(node.args) >= 4:
            severity_node = node.args[3]
            if isinstance(severity_node, ast.Constant) and isinstance(severity_node.value, str):
                severity = severity_node.value
        for keyword in node.keywords:
            if keyword.arg == "severity" and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, str):
                    severity = keyword.value.value
        previous = observed.setdefault(code_node.value, severity)
        assert previous == severity, f"{code_node.value} is emitted with multiple severities"
    return observed


def test_diagnostic_registry_is_unique_bounded_and_well_formed() -> None:
    registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    pattern = re.compile(registry["codePattern"])
    codes = registry["codes"]
    code_values = [entry["code"] for entry in codes]
    allocated_ranges = [
        (int(entry["range"][2:6]), int(entry["range"][9:13]))
        for entry in registry["ranges"]
    ]

    assert len(code_values) == len(set(code_values))
    assert all(pattern.fullmatch(code) for code in code_values)
    assert all(
        any(start <= int(code[2:]) <= end for start, end in allocated_ranges)
        for code in code_values
    )
    assert all(entry["status"] in registry["statusValues"] for entry in codes)
    assert all(entry["tier"] in registry["tierValues"] for entry in codes)
    assert all(entry["defaultSeverity"] in {"error", "warning", "info"} for entry in codes)
    assert all(str(entry["meaning"]).strip().endswith(".") for entry in codes)
    assert all(
        entry["status"] == "active"
        for entry in codes
        if entry["tier"] == "stable"
    )


def test_every_literal_diagnostic_from_a_stable_surface_is_registered() -> None:
    registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    pattern = re.compile(registry["codePattern"])
    registered = {entry["code"]: entry for entry in registry["codes"]}
    emitted: dict[str, str] = {}
    for path, constructors in STABLE_EMITTERS.items():
        for code, severity in _literal_diagnostics(path, constructors).items():
            previous = emitted.setdefault(code, severity)
            assert previous == severity, f"{code} is emitted with multiple severities"

    assert emitted
    assert all(pattern.fullmatch(code) for code in emitted)
    assert set(emitted) <= set(registered)
    for code, severity in emitted.items():
        assert registered[code]["defaultSeverity"] == severity


def test_stable_tck_suites_emit_registered_numeric_diagnostics() -> None:
    registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    pattern = re.compile(registry["codePattern"])
    registered = {entry["code"]: entry for entry in registry["codes"]}
    tree = ast.parse(TESTING_PACKAGE_PATH.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_STABLE_TCK_SUITE_DIAGNOSTIC_CODES"
            for target in node.targets
        )
    )
    codes = ast.literal_eval(assignment.value)

    assert set(codes) == {
        "application-events",
        "compiler",
        "retry",
        "runtime",
        "schema",
        "sequence",
        "tool-execution",
        "tool-lifecycle",
        "tool-result",
    }
    assert all(pattern.fullmatch(code) for code in codes.values())
    assert set(codes.values()) <= set(registered)
    assert all(registered[code]["tier"] == "stable" for code in codes.values())


def test_rust_schema_and_compiler_literal_diagnostics_use_the_same_registry() -> None:
    registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    pattern = re.compile(registry["codePattern"])
    registered = {entry["code"]: entry for entry in registry["codes"]}
    compiler_source = (
        ROOT / "crates" / "graphblocks-compiler" / "src" / "compiler.rs"
    ).read_text(encoding="utf-8")
    schema_source = (
        ROOT / "crates" / "graphblocks-schema" / "src" / "lib.rs"
    ).read_text(encoding="utf-8")
    emitted = {
        code: severity
        for severity, code in re.findall(
            r'Diagnostic::(error|warning)\(\s*"([^"]+)"',
            compiler_source,
        )
    }
    emitted.update(
        (code, "error")
        for code in re.findall(r'code:\s*"([^"]+)"', schema_source)
    )

    assert emitted
    assert all(pattern.fullmatch(code) for code in emitted)
    assert set(emitted) <= set(registered)
    for code, severity in emitted.items():
        assert registered[code]["defaultSeverity"] == severity

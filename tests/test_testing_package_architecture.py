from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
import pickle
from typing import get_type_hints

import graphblocks_testing


PACKAGE_ROOT = (
    Path(__file__).parents[1]
    / "packages"
    / "graphblocks-testing"
    / "src"
    / "graphblocks_testing"
)

EXPECTED_PUBLIC_EXPORTS = (
    "AcceptanceApplication",
    "AcceptanceApplicationExpectation",
    "AcceptanceApplicationReport",
    "AcceptanceCoverageIssue",
    "AcceptanceCoverageResult",
    "AcceptanceGateDiagnostic",
    "AcceptanceGateResult",
    "AcceptanceGateRunner",
    "AcceptanceManifest",
    "AcceptanceRunReport",
    "CancellationToken",
    "ConformanceClaimIssue",
    "ConformanceClaimRequirements",
    "ConformanceClaimValidation",
    "ConformanceProfile",
    "ConformanceProfileSet",
    "ExecutionJournal",
    "FaultChaosReport",
    "FaultChaosResult",
    "InMemoryRunStore",
    "InProcessRuntime",
    "JournalRecord",
    "JournalStateError",
    "MigrationCompatibilityCase",
    "MigrationCompatibilityReport",
    "MigrationCompatibilityResult",
    "MigrationCompatibilityRunner",
    "ModelVisibleToolRef",
    "PerformanceBenchmarkIssue",
    "PerformanceBenchmarkReport",
    "PerformanceThreshold",
    "ReleaseCandidateEvidence",
    "ReleaseCandidateGateReport",
    "ReleaseCandidateGateResult",
    "RunRecord",
    "RunTerminalStateError",
    "RunResult",
    "RuntimeRegistry",
    "SQLiteExecutionJournal",
    "SQLiteRunStore",
    "RunDeploymentProvenance",
    "StateConflictError",
    "TckCase",
    "TckReport",
    "TckResult",
    "TckRunner",
    "TckSuiteCoverageIssue",
    "TckSuiteCoverageResult",
    "TckSuiteManifest",
    "bundled_tck_root",
    "canonical_hash",
    "check_tck_suite_coverage",
    "compile_graph",
    "load_application_event_tck_cases",
    "load_application_protocol_tck_cases",
    "load_approval_review_tck_cases",
    "load_budget_race_tck_cases",
    "load_bundled_tck_cases_for_suite",
    "load_bundled_tck_suite_manifests",
    "load_compiler_tck_cases",
    "load_conversation_tck_cases",
    "load_deployment_tck_cases",
    "load_documents_tck_cases",
    "load_durable_tck_cases",
    "load_exhaustion_tck_cases",
    "load_migration_tck_cases",
    "load_orchestration_tck_cases",
    "load_outcome_tck_cases",
    "load_policy_tck_cases",
    "load_rag_tck_cases",
    "load_retry_tck_cases",
    "load_schema_resource_tck_cases",
    "load_schema_typed_value_tck_cases",
    "load_runtime_tck_cases",
    "load_schema_tck_cases",
    "load_sequence_tck_cases",
    "load_tck_cases_for_suite",
    "load_tck_suite_manifests",
    "load_tool_execution_tck_cases",
    "load_tool_lifecycle_tck_cases",
    "load_tool_result_tck_cases",
    "load_typed_ports_tck_cases",
    "load_usage_tck_cases",
    "load_voice_tck_cases",
    "main",
    "migrate_document",
    "run_bundled_tck_suite",
    "run_native_test_graph",
    "stdlib_registry",
)


def test_testing_package_root_is_a_bounded_export_only_facade() -> None:
    source = (PACKAGE_ROOT / "__init__.py").read_text(encoding="utf-8")
    module = ast.parse(source)

    assert len(source.splitlines()) <= 260
    assert not [
        node
        for node in module.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert tuple(graphblocks_testing.__all__) == EXPECTED_PUBLIC_EXPORTS
    assert len(set(graphblocks_testing.__all__)) == len(graphblocks_testing.__all__)
    assert all(
        hasattr(graphblocks_testing, name) for name in graphblocks_testing.__all__
    )


def test_testing_package_modules_form_an_acyclic_leaf_import_graph() -> None:
    module_names = {
        path.stem for path in PACKAGE_ROOT.glob("*.py") if path.name != "__init__.py"
    }
    dependencies = {name: set() for name in module_names}
    violations: list[str] = []
    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            (
                isinstance(node, ast.Import)
                and any(alias.name == "graphblocks_testing" for alias in node.names)
            )
            or (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module == "graphblocks_testing"
            )
            for node in ast.walk(module)
        ):
            violations.append(path.name)
        dependencies[path.stem].update(
            node.module
            for node in ast.walk(module)
            if isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module in module_names
        )

    assert violations == []
    remaining = {
        name: set(module_dependencies)
        for name, module_dependencies in dependencies.items()
    }
    while remaining:
        ready = {
            name
            for name, module_dependencies in remaining.items()
            if not module_dependencies.intersection(remaining)
        }
        assert ready, remaining
        for name in ready:
            del remaining[name]


def test_testing_package_root_preserves_owner_and_pickle_identity() -> None:
    owners = {
        "TckCase": "models",
        "TckResult": "reports",
        "TckReport": "reports",
        "TckSuiteManifest": "reports",
        "TckRunner": "runners",
    }
    for name, module_name in owners.items():
        owner = importlib.import_module(f"graphblocks_testing.{module_name}")
        exported = getattr(graphblocks_testing, name)
        assert exported is getattr(owner, name)
        assert exported.__module__ == "graphblocks_testing"
        assert exported.__qualname__ == name

    model_module = importlib.import_module("graphblocks_testing.models")
    for name in (
        "_FrozenCaseEvidenceList",
        "_FrozenEvidenceDict",
        "_FrozenEvidenceList",
    ):
        private_type = getattr(graphblocks_testing, name)
        assert private_type is getattr(model_module, name)
        assert private_type.__module__ == "graphblocks_testing"

    result = graphblocks_testing.TckResult(
        case_id="architecture/pickle",
        kind="schema",
        status="passed",
        observed={"nested": [{"ok": True}]},
    )
    report = graphblocks_testing.TckReport(
        profile="local",
        results=(result,),
        suite="schema",
        implementation="graphblocks-python",
        implementation_version="test",
        fixture_digest="sha256:" + ("0" * 64),
    )

    assert pickle.loads(pickle.dumps(report)) == report


def test_testing_package_root_resolves_exported_class_annotations() -> None:
    root_owned_classes = {
        name: value
        for name in graphblocks_testing.__all__
        if inspect.isclass(value := getattr(graphblocks_testing, name))
        and value.__module__ == "graphblocks_testing"
    }

    resolved = {
        name: get_type_hints(value) for name, value in root_owned_classes.items()
    }

    assert {
        "AcceptanceGateResult",
        "FaultChaosResult",
        "MigrationCompatibilityCase",
        "PerformanceBenchmarkReport",
        "PerformanceThreshold",
        "ReleaseCandidateGateResult",
        "TckRunner",
    } <= {name for name, annotations in resolved.items() if annotations}

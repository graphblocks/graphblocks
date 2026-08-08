"""Stable public facade for the GraphBlocks conformance toolkit."""

# ruff: noqa: F401

from __future__ import annotations

from collections.abc import Mapping
import importlib
from importlib.metadata import (
    distribution as installed_distribution,
    version as distribution_version,
)
from pathlib import Path
from typing import Literal

from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename

from graphblocks.canonical import canonical_hash_reference as canonical_hash
from graphblocks.compiler import (
    compile_graph as _compile_graph_normative,
    compile_graph_reference as compile_graph,
)
from graphblocks.evaluation import ModelVisibleToolRef
from graphblocks.exhaustion import (
    ContinuationEnvelope,
    ExhaustionController,
    ExhaustionPolicy,
)
from graphblocks.migration import migrate_document_reference as migrate_document
from graphblocks.plugins import BlockCatalog
from graphblocks.run_store import (
    InMemoryRunStore,
    RunDeploymentProvenance,
    RunRecord,
    RunTerminalStateError,
    SQLiteRunStore,
    StateConflictError,
)
from graphblocks.runtime import (
    CancellationToken,
    ExecutionJournal,
    InProcessRuntime,
    JournalRecord,
    JournalStateError,
    RunResult,
    RuntimeRegistry,
    SQLiteExecutionJournal,
    stdlib_registry,
)
from graphblocks.tools import ToolResultStreamError, ToolResultStreamState

from .acceptance import AcceptanceGateRunner, AcceptanceManifest
from .acceptance_models import (
    AcceptanceApplication,
    AcceptanceApplicationExpectation,
    AcceptanceApplicationReport,
    AcceptanceCoverageIssue,
    AcceptanceCoverageResult,
    AcceptanceGateDiagnostic,
    AcceptanceGateResult,
    AcceptanceRunReport,
)
from .cli import (
    _native_compiler_version,
    _native_compiler_wheel_artifact,
    main,
    run_bundled_tck_suite,
)
from .fixture_loading import (
    bundled_tck_root,
    load_application_event_tck_cases,
    load_application_protocol_tck_cases,
    load_approval_review_tck_cases,
    load_budget_race_tck_cases,
    load_bundled_tck_cases_for_suite,
    load_bundled_tck_suite_manifests,
    load_compiler_tck_cases,
    load_conversation_tck_cases,
    load_deployment_tck_cases,
    load_documents_tck_cases,
    load_durable_tck_cases,
    load_exhaustion_tck_cases,
    load_migration_tck_cases,
    load_orchestration_tck_cases,
    load_policy_tck_cases,
    load_rag_tck_cases,
    load_retry_tck_cases,
    load_runtime_tck_cases,
    load_schema_resource_tck_cases,
    load_schema_tck_cases,
    load_schema_typed_value_tck_cases,
    load_sequence_tck_cases,
    load_tck_cases_for_suite,
    load_tck_suite_manifests,
    load_tool_execution_tck_cases,
    load_tool_lifecycle_tck_cases,
    load_tool_result_tck_cases,
    load_usage_tck_cases,
    load_voice_tck_cases,
)
from .models import (
    FaultKind,
    MigrationDirection,
    PerformanceThresholdOperator,
    ReleaseCandidateGateStatus,
    TckCase,
    TckCaseKind,
    TckResultStatus,
    _FrozenCaseEvidenceList,
    _FrozenEvidenceDict,
    _FrozenEvidenceList,
    run_native_test_graph,
)
from .profiles import (
    ConformanceClaimIssue,
    ConformanceClaimRequirements,
    ConformanceClaimValidation,
    ConformanceProfile,
    ConformanceProfileSet,
    TckSuiteCoverageIssue,
    TckSuiteCoverageResult,
    check_tck_suite_coverage,
)
from .release import (
    ReleaseCandidateEvidence,
    ReleaseCandidateGateReport,
    ReleaseCandidateGateResult,
)
from .reports import (
    FaultChaosReport,
    FaultChaosResult,
    MigrationCompatibilityCase,
    MigrationCompatibilityReport,
    MigrationCompatibilityResult,
    MigrationCompatibilityRunner,
    PerformanceBenchmarkIssue,
    PerformanceBenchmarkReport,
    PerformanceThreshold,
    TckReport,
    TckResult,
    TckSuiteManifest,
)
from .runners import TckRunner, _NormativeCompilerTckRunner


__all__ = [
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
    "load_usage_tck_cases",
    "load_voice_tck_cases",
    "main",
    "migrate_document",
    "run_bundled_tck_suite",
    "run_native_test_graph",
    "stdlib_registry",
]


for _export_name in __all__:
    _export = globals()[_export_name]
    if getattr(_export, "__module__", "").startswith(f"{__name__}."):
        _export.__module__ = __name__
for _pickle_type in (
    _FrozenCaseEvidenceList,
    _FrozenEvidenceDict,
    _FrozenEvidenceList,
):
    _pickle_type.__module__ = __name__
del _export
del _export_name
del _pickle_type

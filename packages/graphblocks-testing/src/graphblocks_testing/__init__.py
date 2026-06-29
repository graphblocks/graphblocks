from __future__ import annotations

import argparse
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal
import json
import math
from pathlib import Path
from typing import Literal

from graphblocks.application_event import (
    ApplicationEvent,
    ApplicationEventMetadata,
    ApplicationEventStreamState,
)
from graphblocks.canonical import canonical_hash
from graphblocks.compiler import compile_graph
from graphblocks.conversation import ContentPart
from graphblocks.budget import (
    BudgetCompletionReserveStateError,
    BudgetExceededError,
    BudgetPermit,
    InMemoryBudgetLedger,
    UsageAmount,
)
from graphblocks.exhaustion import (
    ContinuationEnvelope,
    ExhaustionController,
    ExhaustionPolicy,
    MissingExhaustionBoundaryError,
    validate_exhaustion_policy,
)
from graphblocks.loader import load_documents
from graphblocks.migration import GRAPH_API_VERSION, migrate_document
from graphblocks.output_policy import (
    GenerationChunk,
    OutputDeliveryGate,
    OutputDeliveryPolicy,
    OutputGateError,
    OutputCutoff,
    OutputPolicyDecision,
)
from graphblocks.policy import PolicyDecision, ResourceRef as PolicyResourceRef
from graphblocks.plugins import BlockCatalog
from graphblocks.run_store import (
    InMemoryRunStore,
    RunDeploymentProvenance,
    RunRecord,
    RunTerminalStateError,
    SQLiteRunStore,
    StateConflictError,
)
from graphblocks.schema import SchemaId, SchemaIdError
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
from graphblocks.tools import (
    BlockToolImplementation,
    JsonSchema,
    JsonSchemaNode,
    ToolApprovalRecord,
    ToolApprovalRequest,
    ToolBinding,
    ToolCall,
    ToolCallDraft,
    ToolCallError,
    ToolCatalog,
    ToolDefinition,
    ToolExecutionPlan,
    ToolExecutionPlanError,
    ToolPlanCall,
    ToolResult,
    ToolResultEvent,
    ToolResolutionScope,
    ToolSchemaRegistry,
    admit_tool_call,
)
from graphblocks.usage import InMemoryUsageLedger, UsageRecord


TckCaseKind = Literal[
    "compiler",
    "runtime",
    "schema",
    "policy",
    "application-events",
    "sequence",
    "exhaustion",
    "budget-race",
    "retry",
    "tool-lifecycle",
    "tool-execution",
    "usage",
]
TckResultStatus = Literal["passed", "failed"]
PerformanceThresholdOperator = Literal["at_most", "at_least"]
MigrationDirection = Literal["upgrade", "downgrade"]
FaultKind = Literal[
    "telemetry_outage",
    "provider_timeout",
    "worker_crash",
    "budget_race",
    "storage_conflict",
    "network_partition",
]
ReleaseCandidateGateStatus = Literal["passed", "failed"]


def _first_mapping_value(mapping: Mapping[str, object], *keys: str, default: object = None) -> object:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value or ())


def _tool_execution_error_code(error: ToolExecutionPlanError) -> str:
    message = str(error)
    if "requires an effect key" in message:
        return "unsafe_parallel_effects"
    if "not pending" in message:
        return "tool_call_not_pending"
    if "not running" in message:
        return "tool_call_not_running"
    if "already running" in message:
        return "effect_conflict"
    if "maximum parallelism" in message:
        return "parallelism_exhausted"
    if "dependencies are not ready" in message:
        return "dependencies_not_ready"
    return type(error).__name__


@dataclass(frozen=True, slots=True)
class TckCase:
    case_id: str
    kind: TckCaseKind
    graph: dict[str, object] = field(default_factory=dict)
    inputs: dict[str, object] = field(default_factory=dict)
    expected_hash: str | None = None
    expected_error_codes: tuple[str, ...] = field(default_factory=tuple)
    expected_warning_codes: tuple[str, ...] = field(default_factory=tuple)
    expected_outputs: dict[str, object] | None = None
    expected_ok: bool = True
    expected_status: str = "succeeded"
    expected_terminal_kind: str | None = None
    block_catalog: tuple[dict[str, object], ...] = field(default_factory=tuple)
    schema_id: str | None = None
    expected_canonical_schema_id: str | None = None
    expected_schema_name: str | None = None
    expected_major_version: int | None = None
    expected_error: str | None = None
    policy_delivery: dict[str, object] = field(default_factory=dict)
    policy_operations: tuple[dict[str, object], ...] = field(default_factory=tuple)
    expected_gate_state: dict[str, object] = field(default_factory=dict)
    policy_stream_id: str = "stream-1"
    policy_response_id: str = "response-1"
    application_event_operations: tuple[dict[str, object], ...] = field(default_factory=tuple)
    expected_accepted_event_kinds: tuple[str, ...] = field(default_factory=tuple)
    sequence_capacity: int | None = None
    sequence_operations: tuple[dict[str, object], ...] = field(default_factory=tuple)
    expected_sequence_state: str | None = None
    expected_sequence_creation_error: str | None = None
    exhaustion_fixture: dict[str, object] = field(default_factory=dict)
    budget_race_fixture: dict[str, object] = field(default_factory=dict)
    retry_fixture: dict[str, object] = field(default_factory=dict)
    tool_lifecycle_fixture: dict[str, object] = field(default_factory=dict)
    tool_execution_fixture: dict[str, object] = field(default_factory=dict)
    usage_fixture: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("TCK case_id must not be empty")
        if self.kind not in {
            "compiler",
            "runtime",
            "schema",
            "policy",
            "application-events",
            "sequence",
            "exhaustion",
            "budget-race",
            "retry",
            "tool-lifecycle",
            "tool-execution",
            "usage",
        }:
            raise ValueError(f"invalid TCK case kind {self.kind}")
        object.__setattr__(self, "graph", dict(self.graph))
        object.__setattr__(self, "inputs", dict(self.inputs))
        object.__setattr__(self, "expected_error_codes", tuple(self.expected_error_codes))
        object.__setattr__(self, "expected_warning_codes", tuple(self.expected_warning_codes))
        object.__setattr__(self, "block_catalog", tuple(dict(block) for block in self.block_catalog))
        object.__setattr__(self, "policy_delivery", dict(self.policy_delivery))
        object.__setattr__(self, "policy_operations", tuple(dict(operation) for operation in self.policy_operations))
        object.__setattr__(self, "expected_gate_state", dict(self.expected_gate_state))
        object.__setattr__(
            self,
            "application_event_operations",
            tuple(dict(operation) for operation in self.application_event_operations),
        )
        object.__setattr__(self, "expected_accepted_event_kinds", tuple(self.expected_accepted_event_kinds))
        object.__setattr__(self, "sequence_operations", tuple(dict(operation) for operation in self.sequence_operations))
        object.__setattr__(self, "exhaustion_fixture", dict(self.exhaustion_fixture))
        object.__setattr__(self, "budget_race_fixture", dict(self.budget_race_fixture))
        object.__setattr__(self, "retry_fixture", dict(self.retry_fixture))
        object.__setattr__(self, "tool_lifecycle_fixture", dict(self.tool_lifecycle_fixture))
        object.__setattr__(self, "tool_execution_fixture", dict(self.tool_execution_fixture))
        object.__setattr__(self, "usage_fixture", dict(self.usage_fixture))
        if self.kind == "policy":
            if not self.policy_stream_id.strip():
                raise ValueError("policy TCK stream_id must not be empty")
            if not self.policy_response_id.strip():
                raise ValueError("policy TCK response_id must not be empty")
        if self.kind == "sequence":
            if self.sequence_capacity is None or isinstance(self.sequence_capacity, bool):
                raise ValueError("sequence TCK case requires integer capacity")
            if not isinstance(self.sequence_capacity, int):
                raise ValueError("sequence TCK case requires integer capacity")
            if self.expected_sequence_state is None and self.expected_sequence_creation_error is None:
                raise ValueError("sequence TCK case requires expected state or creation error")
        if self.kind == "exhaustion" and not self.exhaustion_fixture:
            raise ValueError("exhaustion TCK case requires fixture")
        if self.kind == "budget-race" and not self.budget_race_fixture:
            raise ValueError("budget-race TCK case requires fixture")
        if self.kind == "retry" and not self.retry_fixture:
            raise ValueError("retry TCK case requires fixture")
        if self.kind == "tool-lifecycle" and not self.tool_lifecycle_fixture:
            raise ValueError("tool-lifecycle TCK case requires fixture")
        if self.kind == "tool-execution" and not self.tool_execution_fixture:
            raise ValueError("tool-execution TCK case requires fixture")
        if self.kind == "usage" and not self.usage_fixture:
            raise ValueError("usage TCK case requires fixture")
        if self.expected_outputs is not None:
            object.__setattr__(self, "expected_outputs", dict(self.expected_outputs))
        if self.expected_terminal_kind is not None and not self.expected_terminal_kind.strip():
            raise ValueError("TCK expected_terminal_kind must not be empty")
        if self.kind == "schema":
            if not isinstance(self.schema_id, str) or not self.schema_id.strip():
                raise ValueError("schema TCK case requires schema_id")
            if self.expected_major_version is not None and self.expected_major_version <= 0:
                raise ValueError("schema TCK expected_major_version must be positive")

    @classmethod
    def compiler(
        cls,
        *,
        case_id: str,
        graph: dict[str, object],
        expected_hash: str | None = None,
        expected_error_codes: tuple[str, ...] = (),
        expected_warning_codes: tuple[str, ...] = (),
        expected_ok: bool = True,
        block_catalog: tuple[dict[str, object], ...] = (),
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="compiler",
            graph=graph,
            expected_hash=expected_hash,
            expected_error_codes=expected_error_codes,
            expected_warning_codes=expected_warning_codes,
            expected_ok=expected_ok,
            block_catalog=block_catalog,
        )

    @classmethod
    def runtime(
        cls,
        *,
        case_id: str,
        graph: dict[str, object],
        inputs: dict[str, object],
        expected_outputs: dict[str, object] | None = None,
        expected_status: str = "succeeded",
        expected_terminal_kind: str | None = None,
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="runtime",
            graph=graph,
            inputs=inputs,
            expected_outputs=expected_outputs,
            expected_status=expected_status,
            expected_terminal_kind=expected_terminal_kind,
        )

    @classmethod
    def schema(
        cls,
        *,
        case_id: str,
        schema_id: str,
        expected_ok: bool,
        expected_canonical_schema_id: str | None = None,
        expected_schema_name: str | None = None,
        expected_major_version: int | None = None,
        expected_error: str | None = None,
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="schema",
            schema_id=schema_id,
            expected_ok=expected_ok,
            expected_canonical_schema_id=expected_canonical_schema_id,
            expected_schema_name=expected_schema_name,
            expected_major_version=expected_major_version,
            expected_error=expected_error,
        )

    @classmethod
    def policy(
        cls,
        *,
        case_id: str,
        delivery: dict[str, object],
        operations: tuple[dict[str, object], ...],
        expected: dict[str, object],
        stream_id: str = "stream-1",
        response_id: str = "response-1",
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="policy",
            policy_delivery=delivery,
            policy_operations=operations,
            expected_gate_state=expected,
            policy_stream_id=stream_id,
            policy_response_id=response_id,
        )

    @classmethod
    def application_events(
        cls,
        *,
        case_id: str,
        operations: tuple[dict[str, object], ...],
        expected_accepted_kinds: tuple[str, ...],
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="application-events",
            application_event_operations=operations,
            expected_accepted_event_kinds=expected_accepted_kinds,
        )

    @classmethod
    def sequence(
        cls,
        *,
        case_id: str,
        capacity: int,
        operations: tuple[dict[str, object], ...],
        expected_state: str | None = None,
        expected_creation_error: str | None = None,
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="sequence",
            sequence_capacity=capacity,
            sequence_operations=operations,
            expected_sequence_state=expected_state,
            expected_sequence_creation_error=expected_creation_error,
        )

    @classmethod
    def exhaustion(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="exhaustion", exhaustion_fixture=fixture)

    @classmethod
    def budget_race(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="budget-race", budget_race_fixture=fixture)

    @classmethod
    def retry(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="retry", retry_fixture=fixture)

    @classmethod
    def tool_lifecycle(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="tool-lifecycle", tool_lifecycle_fixture=fixture)

    @classmethod
    def tool_execution(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="tool-execution", tool_execution_fixture=fixture)

    @classmethod
    def usage(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="usage", usage_fixture=fixture)


@dataclass(frozen=True, slots=True)
class TckResult:
    case_id: str
    kind: TckCaseKind
    status: TckResultStatus
    diagnostics: tuple[dict[str, str], ...] = field(default_factory=tuple)
    observed: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(dict(diagnostic) for diagnostic in self.diagnostics))
        object.__setattr__(self, "observed", dict(self.observed))

    def result_contract(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "status": self.status,
            "diagnostics": [dict(diagnostic) for diagnostic in self.diagnostics],
            "observed": dict(self.observed),
        }


@dataclass(frozen=True, slots=True)
class TckReport:
    profile: str
    results: tuple[TckResult, ...]

    @property
    def ok(self) -> bool:
        return all(result.status == "passed" for result in self.results)

    def report_contract(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "ok": self.ok,
            "results": [result.result_contract() for result in self.results],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.report_contract())


@dataclass(frozen=True, slots=True)
class TckSuiteManifest:
    suite_id: str
    path: str
    case_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.suite_id.strip():
            raise ValueError("TCK suite_id must not be empty")
        if not self.path.strip():
            raise ValueError("TCK suite path must not be empty")
        case_ids = tuple(str(case_id) for case_id in self.case_ids)
        if any(not case_id.strip() for case_id in case_ids):
            raise ValueError("TCK suite case ids must not be empty")
        object.__setattr__(self, "case_ids", case_ids)

    @property
    def case_count(self) -> int:
        return len(self.case_ids)

    def manifest_contract(self) -> dict[str, object]:
        return {
            "suite_id": self.suite_id,
            "path": self.path,
            "case_count": self.case_count,
            "case_ids": list(self.case_ids),
        }

    def content_digest(self) -> str:
        return canonical_hash(self.manifest_contract())


@dataclass(frozen=True, slots=True)
class PerformanceThreshold:
    metric_name: str
    operator: PerformanceThresholdOperator
    threshold: float
    unit: str | None = None

    def __post_init__(self) -> None:
        if not self.metric_name.strip():
            raise ValueError("performance threshold metric_name must not be empty")
        if self.operator not in {"at_most", "at_least"}:
            raise ValueError(f"invalid performance threshold operator {self.operator!r}")
        threshold = float(self.threshold)
        if not math.isfinite(threshold):
            raise ValueError("performance threshold must be finite")
        object.__setattr__(self, "threshold", threshold)
        if self.unit is not None:
            object.__setattr__(self, "unit", self.unit.strip() or None)

    @classmethod
    def at_most(cls, metric_name: str, threshold: float, *, unit: str | None = None) -> PerformanceThreshold:
        return cls(metric_name=metric_name, operator="at_most", threshold=threshold, unit=unit)

    @classmethod
    def at_least(cls, metric_name: str, threshold: float, *, unit: str | None = None) -> PerformanceThreshold:
        return cls(metric_name=metric_name, operator="at_least", threshold=threshold, unit=unit)

    def threshold_contract(self) -> dict[str, object]:
        return {
            "metric_name": self.metric_name,
            "operator": self.operator,
            "threshold": self.threshold,
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class PerformanceBenchmarkIssue:
    metric_name: str
    observed: float | None
    operator: PerformanceThresholdOperator
    threshold: float
    unit: str | None
    reason: str

    def issue_contract(self) -> dict[str, object]:
        return {
            "metric_name": self.metric_name,
            "observed": self.observed,
            "operator": self.operator,
            "threshold": self.threshold,
            "unit": self.unit,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PerformanceBenchmarkReport:
    benchmark_id: str
    measurements: Mapping[str, float]
    thresholds: tuple[PerformanceThreshold, ...] = field(default_factory=tuple)
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.benchmark_id.strip():
            raise ValueError("performance benchmark_id must not be empty")
        measurements: dict[str, float] = {}
        for metric_name, value in self.measurements.items():
            if not str(metric_name).strip():
                raise ValueError("performance benchmark measurement name must not be empty")
            numeric_value = float(value)
            if not math.isfinite(numeric_value):
                raise ValueError("performance benchmark measurement values must be finite")
            measurements[str(metric_name)] = numeric_value
        object.__setattr__(self, "measurements", dict(sorted(measurements.items())))
        object.__setattr__(
            self,
            "thresholds",
            tuple(sorted(self.thresholds, key=lambda item: (item.metric_name, item.operator, item.threshold))),
        )
        object.__setattr__(
            self,
            "metadata",
            {str(key): str(value) for key, value in sorted(dict(self.metadata).items())},
        )

    @property
    def issues(self) -> tuple[PerformanceBenchmarkIssue, ...]:
        issues: list[PerformanceBenchmarkIssue] = []
        for threshold in self.thresholds:
            observed = self.measurements.get(threshold.metric_name)
            if observed is None:
                issues.append(
                    PerformanceBenchmarkIssue(
                        metric_name=threshold.metric_name,
                        observed=None,
                        operator=threshold.operator,
                        threshold=threshold.threshold,
                        unit=threshold.unit,
                        reason="measurement_missing",
                    )
                )
                continue
            failed = (
                observed > threshold.threshold
                if threshold.operator == "at_most"
                else observed < threshold.threshold
            )
            if failed:
                issues.append(
                    PerformanceBenchmarkIssue(
                        metric_name=threshold.metric_name,
                        observed=observed,
                        operator=threshold.operator,
                        threshold=threshold.threshold,
                        unit=threshold.unit,
                        reason="threshold_failed",
                    )
                )
        return tuple(issues)

    @property
    def ok(self) -> bool:
        return not self.issues

    def report_contract(self) -> dict[str, object]:
        return {
            "benchmark_id": self.benchmark_id,
            "ok": self.ok,
            "metadata": dict(self.metadata),
            "measurements": dict(self.measurements),
            "thresholds": [threshold.threshold_contract() for threshold in self.thresholds],
            "issues": [issue.issue_contract() for issue in self.issues],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.report_contract())


@dataclass(frozen=True, slots=True)
class MigrationCompatibilityCase:
    case_id: str
    direction: MigrationDirection
    document: dict[str, object]
    expected_api_version: str = GRAPH_API_VERSION
    expected_hash: str | None = None
    expected_migrated_from: str | None = None

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("migration compatibility case_id must not be empty")
        if self.direction not in {"upgrade", "downgrade"}:
            raise ValueError(f"invalid migration compatibility direction {self.direction!r}")
        object.__setattr__(self, "document", deepcopy(self.document))

    @classmethod
    def upgrade(
        cls,
        *,
        case_id: str,
        document: dict[str, object],
        expected_hash: str | None = None,
        expected_api_version: str = GRAPH_API_VERSION,
        expected_migrated_from: str | None = None,
    ) -> MigrationCompatibilityCase:
        if expected_migrated_from is None:
            raw_version = document.get("apiVersion")
            expected_migrated_from = raw_version if isinstance(raw_version, str) else None
        return cls(
            case_id=case_id,
            direction="upgrade",
            document=document,
            expected_api_version=expected_api_version,
            expected_hash=expected_hash,
            expected_migrated_from=expected_migrated_from,
        )


@dataclass(frozen=True, slots=True)
class MigrationCompatibilityResult:
    case_id: str
    direction: MigrationDirection
    status: TckResultStatus
    diagnostics: tuple[dict[str, str], ...] = field(default_factory=tuple)
    observed: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(dict(diagnostic) for diagnostic in self.diagnostics))
        object.__setattr__(self, "observed", dict(self.observed))

    def result_contract(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "direction": self.direction,
            "status": self.status,
            "diagnostics": [dict(diagnostic) for diagnostic in self.diagnostics],
            "observed": dict(self.observed),
        }


@dataclass(frozen=True, slots=True)
class MigrationCompatibilityReport:
    profile: str
    results: tuple[MigrationCompatibilityResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(sorted(self.results, key=lambda item: item.case_id)))

    @property
    def ok(self) -> bool:
        return all(result.status == "passed" for result in self.results)

    def report_contract(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "ok": self.ok,
            "results": [result.result_contract() for result in self.results],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.report_contract())


@dataclass(frozen=True, slots=True)
class MigrationCompatibilityRunner:
    profile: str = "migration"

    def run_cases(self, cases: tuple[MigrationCompatibilityCase, ...]) -> MigrationCompatibilityReport:
        return MigrationCompatibilityReport(
            profile=self.profile,
            results=tuple(self._run_case(case) for case in cases),
        )

    def _run_case(self, case: MigrationCompatibilityCase) -> MigrationCompatibilityResult:
        before = deepcopy(case.document)
        if case.direction != "upgrade":
            return MigrationCompatibilityResult(
                case_id=case.case_id,
                direction=case.direction,
                status="failed",
                diagnostics=(
                    {
                        "code": "MigrationDirectionUnsupported",
                        "message": "only upgrade migration compatibility cases are currently supported",
                        "path": "$.direction",
                    },
                ),
                observed={"source_mutated": False},
            )
        migrated = migrate_document(case.document)
        annotations = migrated.get("metadata", {}).get("annotations", {}) if isinstance(migrated.get("metadata"), dict) else {}
        migrated_from = annotations.get("graphblocks.ai/migratedFrom") if isinstance(annotations, dict) else None
        observed = {
            "api_version": migrated.get("apiVersion"),
            "graph_hash": canonical_hash(migrated),
            "migrated_from": migrated_from,
            "source_mutated": case.document != before,
        }
        diagnostics: list[dict[str, str]] = []
        if observed["api_version"] != case.expected_api_version:
            diagnostics.append(
                {
                    "code": "MigrationApiVersionMismatch",
                    "message": "migrated document apiVersion did not match expected version",
                    "path": "$.expected_api_version",
                }
            )
        if case.expected_migrated_from is not None and observed["migrated_from"] != case.expected_migrated_from:
            diagnostics.append(
                {
                    "code": "MigrationSourceVersionMismatch",
                    "message": "migrated document source version annotation did not match expected version",
                    "path": "$.expected_migrated_from",
                }
            )
        if case.expected_hash is not None and observed["graph_hash"] != case.expected_hash:
            diagnostics.append(
                {
                    "code": "MigrationHashMismatch",
                    "message": "migrated document hash did not match expected hash",
                    "path": "$.expected_hash",
                }
            )
        if observed["source_mutated"]:
            diagnostics.append(
                {
                    "code": "MigrationMutatedSource",
                    "message": "migration mutated the source document",
                    "path": "$.document",
                }
            )
        return MigrationCompatibilityResult(
            case_id=case.case_id,
            direction=case.direction,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )


@dataclass(frozen=True, slots=True)
class FaultChaosResult:
    case_id: str
    fault_kind: FaultKind
    status: TckResultStatus
    diagnostics: tuple[dict[str, str], ...] = field(default_factory=tuple)
    observed: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("fault chaos case_id must not be empty")
        if not self.fault_kind.strip():
            raise ValueError("fault chaos fault_kind must not be empty")
        object.__setattr__(self, "diagnostics", tuple(dict(diagnostic) for diagnostic in self.diagnostics))
        object.__setattr__(self, "observed", dict(sorted(dict(self.observed).items())))

    @classmethod
    def from_observation(
        cls,
        *,
        case_id: str,
        fault_kind: FaultKind,
        expected_terminal_state: str,
        observed_terminal_state: str,
        recovery_expected: bool,
        recovered: bool,
        data_loss_events: int,
        audit_preserved: bool,
    ) -> FaultChaosResult:
        if data_loss_events < 0:
            raise ValueError("fault chaos data_loss_events must not be negative")
        observed = {
            "audit_preserved": audit_preserved,
            "data_loss_events": data_loss_events,
            "expected_terminal_state": expected_terminal_state,
            "observed_terminal_state": observed_terminal_state,
            "recovered": recovered,
            "recovery_expected": recovery_expected,
        }
        diagnostics: list[dict[str, str]] = []
        if observed_terminal_state != expected_terminal_state:
            diagnostics.append(
                {
                    "code": "ChaosTerminalStateMismatch",
                    "message": "fault scenario terminal state did not match expected state",
                    "path": "$.observed_terminal_state",
                }
            )
        if recovery_expected and not recovered:
            diagnostics.append(
                {
                    "code": "ChaosRecoveryFailed",
                    "message": "fault scenario did not recover as expected",
                    "path": "$.recovered",
                }
            )
        if data_loss_events:
            diagnostics.append(
                {
                    "code": "ChaosDataLossObserved",
                    "message": "fault scenario observed data loss events",
                    "path": "$.data_loss_events",
                }
            )
        if not audit_preserved:
            diagnostics.append(
                {
                    "code": "ChaosAuditNotPreserved",
                    "message": "fault scenario did not preserve audit evidence",
                    "path": "$.audit_preserved",
                }
            )
        return cls(
            case_id=case_id,
            fault_kind=fault_kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def result_contract(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "fault_kind": self.fault_kind,
            "status": self.status,
            "diagnostics": [dict(diagnostic) for diagnostic in self.diagnostics],
            "observed": dict(self.observed),
        }


@dataclass(frozen=True, slots=True)
class FaultChaosReport:
    profile: str
    results: tuple[FaultChaosResult, ...]

    def __post_init__(self) -> None:
        if not self.profile.strip():
            raise ValueError("fault chaos profile must not be empty")
        object.__setattr__(self, "results", tuple(sorted(self.results, key=lambda item: item.case_id)))

    @property
    def ok(self) -> bool:
        return all(result.status == "passed" for result in self.results)

    def report_contract(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "ok": self.ok,
            "results": [result.result_contract() for result in self.results],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.report_contract())


@dataclass(frozen=True, slots=True)
class ReleaseCandidateGateResult:
    gate: str
    status: ReleaseCandidateGateStatus
    evidence_digest: str
    diagnostics: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.gate.strip():
            raise ValueError("release candidate gate must not be empty")
        if self.status not in {"passed", "failed"}:
            raise ValueError(f"invalid release candidate gate status {self.status!r}")
        object.__setattr__(self, "diagnostics", tuple(dict(diagnostic) for diagnostic in self.diagnostics))

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def gate_contract(self) -> dict[str, object]:
        return {
            "gate": self.gate,
            "status": self.status,
            "evidence_digest": self.evidence_digest,
            "diagnostics": [dict(diagnostic) for diagnostic in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class ReleaseCandidateEvidence:
    evidence_id: str
    ok: bool
    digest: str
    diagnostics: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("release candidate evidence_id must not be empty")
        if not self.digest.startswith("sha256:"):
            raise ValueError("release candidate evidence digest must use sha256:<digest>")
        object.__setattr__(self, "diagnostics", tuple(dict(diagnostic) for diagnostic in self.diagnostics))

    def evidence_contract(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "ok": self.ok,
            "digest": self.digest,
            "diagnostics": [dict(diagnostic) for diagnostic in self.diagnostics],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.evidence_contract())


@dataclass(frozen=True, slots=True)
class ReleaseCandidateGateReport:
    release_id: str
    gates: tuple[ReleaseCandidateGateResult, ...]

    def __post_init__(self) -> None:
        if not self.release_id.strip():
            raise ValueError("release candidate release_id must not be empty")
        object.__setattr__(self, "gates", tuple(sorted(self.gates, key=lambda gate: gate.gate)))

    @property
    def ok(self) -> bool:
        return all(gate.ok for gate in self.gates)

    @classmethod
    def from_evidence(
        cls,
        *,
        release_id: str,
        tck_reports: Mapping[str, TckReport],
        required_tck_suites: tuple[str, ...],
        acceptance_coverage: AcceptanceCoverageResult,
        fault_chaos: FaultChaosReport,
        performance: PerformanceBenchmarkReport,
        wheel_matrix: object,
        migration: MigrationCompatibilityReport,
        oci_image_build: object | None = None,
        supply_chain: Mapping[str, str] | None = None,
    ) -> ReleaseCandidateGateReport:
        gates: list[ReleaseCandidateGateResult] = []

        tck_diagnostics: list[dict[str, str]] = []
        tck_digests: dict[str, str | None] = {}
        for suite in required_tck_suites:
            report = tck_reports.get(suite)
            if report is None:
                tck_digests[suite] = None
                tck_diagnostics.append(
                    {
                        "code": "ReleaseCandidateTckMissing",
                        "message": "required TCK suite has no report",
                        "path": f"$.tck_reports.{suite}",
                    }
                )
                continue
            tck_digests[suite] = report.content_digest()
            if not report.ok:
                tck_diagnostics.append(
                    {
                        "code": "ReleaseCandidateTckFailed",
                        "message": "required TCK suite did not pass",
                        "path": f"$.tck_reports.{suite}",
                    }
                )
        gates.append(
            ReleaseCandidateGateResult(
                gate="full_tck",
                status="passed" if not tck_diagnostics else "failed",
                evidence_digest=canonical_hash({"required": list(required_tck_suites), "reports": tck_digests}),
                diagnostics=tuple(tck_diagnostics),
            )
        )

        acceptance_diagnostics = ()
        if not acceptance_coverage.ok:
            acceptance_diagnostics = (
                {
                    "code": "ReleaseCandidateAcceptanceFailed",
                    "message": "acceptance application coverage did not pass",
                    "path": "$.acceptance_coverage",
                },
            )
        gates.append(
            ReleaseCandidateGateResult(
                gate="acceptance_applications",
                status="passed" if not acceptance_diagnostics else "failed",
                evidence_digest=canonical_hash(acceptance_coverage.issue_contracts()),
                diagnostics=acceptance_diagnostics,
            )
        )

        fault_diagnostics = ()
        if not fault_chaos.ok:
            fault_diagnostics = (
                {
                    "code": "ReleaseCandidateChaosFailed",
                    "message": "fault/chaos report did not pass",
                    "path": "$.fault_chaos",
                },
            )
        gates.append(
            ReleaseCandidateGateResult(
                gate="fault_chaos_tests",
                status="passed" if not fault_diagnostics else "failed",
                evidence_digest=fault_chaos.content_digest(),
                diagnostics=fault_diagnostics,
            )
        )

        performance_diagnostics = ()
        if not performance.ok:
            performance_diagnostics = (
                {
                    "code": "ReleaseCandidatePerformanceFailed",
                    "message": "performance benchmark did not pass",
                    "path": "$.performance",
                },
            )
        gates.append(
            ReleaseCandidateGateResult(
                gate="performance_benchmark",
                status="passed" if not performance_diagnostics else "failed",
                evidence_digest=performance.content_digest(),
                diagnostics=performance_diagnostics,
            )
        )

        oci_image_diagnostics = ()
        if oci_image_build is None:
            oci_image_digest = canonical_hash(None)
            oci_image_diagnostics = (
                {
                    "code": "ReleaseCandidateOciImageBuildMissing",
                    "message": "OCI image build evidence is required",
                    "path": "$.oci_image_build",
                },
            )
        else:
            oci_image_digest = str(oci_image_build.content_digest())
            if not bool(getattr(oci_image_build, "ok", True)):
                oci_image_diagnostics = (
                    {
                        "code": "ReleaseCandidateOciImageBuildFailed",
                        "message": "OCI image build evidence did not pass",
                        "path": "$.oci_image_build",
                    },
                )
        gates.append(
            ReleaseCandidateGateResult(
                gate="oci_image_build",
                status="passed" if not oci_image_diagnostics else "failed",
                evidence_digest=oci_image_digest,
                diagnostics=oci_image_diagnostics,
            )
        )

        supply_chain = dict(supply_chain or {})
        supply_chain_diagnostics: list[dict[str, str]] = []
        for artifact_name in ("sbom", "provenance", "signature"):
            digest = supply_chain.get(artifact_name)
            if not isinstance(digest, str) or not digest.startswith("sha256:"):
                supply_chain_diagnostics.append(
                    {
                        "code": "ReleaseCandidateSupplyChainEvidenceMissing",
                        "message": f"release candidate requires {artifact_name} digest evidence",
                        "path": f"$.supply_chain.{artifact_name}",
                    }
                )
        gates.append(
            ReleaseCandidateGateResult(
                gate="supply_chain",
                status="passed" if not supply_chain_diagnostics else "failed",
                evidence_digest=canonical_hash(supply_chain),
                diagnostics=tuple(supply_chain_diagnostics),
            )
        )

        wheel_ok = bool(getattr(wheel_matrix, "ok"))
        wheel_digest = str(wheel_matrix.content_digest())
        wheel_diagnostics = ()
        if not wheel_ok:
            wheel_diagnostics = (
                {
                    "code": "ReleaseCandidateWheelMatrixFailed",
                    "message": "wheel matrix did not pass",
                    "path": "$.wheel_matrix",
                },
            )
        gates.append(
            ReleaseCandidateGateResult(
                gate="wheel_matrix",
                status="passed" if not wheel_diagnostics else "failed",
                evidence_digest=wheel_digest,
                diagnostics=wheel_diagnostics,
            )
        )

        migration_diagnostics = ()
        if not migration.ok:
            migration_diagnostics = (
                {
                    "code": "ReleaseCandidateMigrationFailed",
                    "message": "migration compatibility report did not pass",
                    "path": "$.migration",
                },
            )
        gates.append(
            ReleaseCandidateGateResult(
                gate="migration_tests",
                status="passed" if not migration_diagnostics else "failed",
                evidence_digest=migration.content_digest(),
                diagnostics=migration_diagnostics,
            )
        )

        return cls(release_id=release_id, gates=tuple(gates))

    def report_contract(self) -> dict[str, object]:
        return {
            "release_id": self.release_id,
            "ok": self.ok,
            "gates": [gate.gate_contract() for gate in self.gates],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.report_contract())


def load_compiler_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list):
        raise ValueError("compiler TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"compiler TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"compiler TCK case {index} requires name")
        graph = _first_mapping_value(raw_case, "document", "graph")
        if not isinstance(graph, dict):
            raise ValueError(f"compiler TCK case {case_id} requires document")
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"compiler TCK case {case_id} requires expected result")
        expected_hash = _first_mapping_value(expected, "graph_hash", "graphHash")
        if not isinstance(expected_hash, str) or not expected_hash.strip():
            raise ValueError(f"compiler TCK case {case_id} requires expected graph_hash")
        raw_error_codes = _first_mapping_value(expected, "error_codes", "errorCodes")
        if not isinstance(raw_error_codes, list) or not all(isinstance(code, str) for code in raw_error_codes):
            raise ValueError(f"compiler TCK case {case_id} requires string error_codes")
        raw_warning_codes = _first_mapping_value(expected, "warning_codes", "warningCodes", default=[])
        if not isinstance(raw_warning_codes, list) or not all(isinstance(code, str) for code in raw_warning_codes):
            raise ValueError(f"compiler TCK case {case_id} requires string warning_codes")
        raw_block_catalog = _first_mapping_value(raw_case, "block_catalog", "blockCatalog", default=[])
        if not isinstance(raw_block_catalog, list) or not all(isinstance(block, dict) for block in raw_block_catalog):
            raise ValueError(f"compiler TCK case {case_id} block_catalog must be a list of mappings")
        cases.append(
            TckCase.compiler(
                case_id=case_id,
                graph=graph,
                expected_hash=expected_hash,
                expected_error_codes=tuple(raw_error_codes),
                expected_warning_codes=tuple(raw_warning_codes),
                expected_ok=not raw_error_codes,
                block_catalog=tuple(raw_block_catalog),
            )
        )
    return tuple(cases)


def load_runtime_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list):
        raise ValueError("runtime TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"runtime TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"runtime TCK case {index} requires name")
        graph = _first_mapping_value(raw_case, "document", "graph")
        if not isinstance(graph, dict):
            raise ValueError(f"runtime TCK case {case_id} requires document")
        inputs = raw_case.get("inputs", {})
        if not isinstance(inputs, dict):
            raise ValueError(f"runtime TCK case {case_id} inputs must be a mapping")
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"runtime TCK case {case_id} requires expected result")
        expected_status = _first_mapping_value(
            expected,
            "status",
            "expected_status",
            "expectedStatus",
            default="succeeded",
        )
        if not isinstance(expected_status, str) or not expected_status.strip():
            raise ValueError(f"runtime TCK case {case_id} requires expected status")
        expected_outputs = _first_mapping_value(
            expected,
            "outputs",
            "expected_outputs",
            "expectedOutputs",
        )
        if expected_outputs is not None and not isinstance(expected_outputs, dict):
            raise ValueError(f"runtime TCK case {case_id} expected outputs must be a mapping")
        expected_terminal_kind = _first_mapping_value(
            expected,
            "terminal_kind",
            "terminalKind",
            "expected_terminal_kind",
            "expectedTerminalKind",
        )
        if expected_terminal_kind is not None and (
            not isinstance(expected_terminal_kind, str) or not expected_terminal_kind.strip()
        ):
            raise ValueError(f"runtime TCK case {case_id} expected terminal_kind must be a string")
        cases.append(
            TckCase.runtime(
                case_id=case_id,
                graph=graph,
                inputs=inputs,
                expected_outputs=expected_outputs,
                expected_status=expected_status,
                expected_terminal_kind=expected_terminal_kind,
            )
        )
    return tuple(cases)


def load_application_event_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list):
        raise ValueError("application-events TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"application-events TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"application-events TCK case {index} requires name")
        operations = raw_case.get("operations")
        if not isinstance(operations, list) or not all(isinstance(operation, dict) for operation in operations):
            raise ValueError(f"application-events TCK case {case_id} operations must be a list of mappings")
        expected = raw_case.get("expectedAcceptedKinds")
        if not isinstance(expected, list) or not all(isinstance(kind, str) for kind in expected):
            raise ValueError(f"application-events TCK case {case_id} expectedAcceptedKinds must be strings")
        operations_with_defaults = []
        for operation in operations:
            operation_with_defaults = dict(operation)
            for key in ("runId", "responseId", "turnId", "releaseId", "policySnapshotId", "streamId"):
                if key in raw_case and key not in operation_with_defaults:
                    operation_with_defaults[key] = raw_case[key]
            operations_with_defaults.append(operation_with_defaults)
        cases.append(
            TckCase.application_events(
                case_id=case_id,
                operations=tuple(operations_with_defaults),
                expected_accepted_kinds=tuple(expected),
            )
        )
    return tuple(cases)


def load_exhaustion_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list):
        raise ValueError("exhaustion TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"exhaustion TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"exhaustion TCK case {index} requires name")
        policy = raw_case.get("policy")
        if not isinstance(policy, Mapping):
            raise ValueError(f"exhaustion TCK case {case_id} requires policy")
        cases.append(TckCase.exhaustion(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_budget_race_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list):
        raise ValueError("budget-race TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"budget-race TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"budget-race TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {"reservation_race", "completion_reserve_race"}:
            raise ValueError(f"budget-race TCK case {case_id} has unsupported kind {case_kind!r}")
        cases.append(TckCase.budget_race(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_retry_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list):
        raise ValueError("retry TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"retry TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"retry TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {"node_retry", "cancelled_before_retry"}:
            raise ValueError(f"retry TCK case {case_id} has unsupported kind {case_kind!r}")
        max_attempts = raw_case.get("maxAttempts", raw_case.get("max_attempts"))
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts <= 0:
            raise ValueError(f"retry TCK case {case_id} requires positive integer maxAttempts")
        failures_before_success = raw_case.get(
            "failuresBeforeSuccess",
            raw_case.get("failures_before_success"),
        )
        if (
            isinstance(failures_before_success, bool)
            or not isinstance(failures_before_success, int)
            or failures_before_success < 0
        ):
            raise ValueError(f"retry TCK case {case_id} requires non-negative integer failuresBeforeSuccess")
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"retry TCK case {case_id} requires expected result")
        cases.append(TckCase.retry(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_tool_lifecycle_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list):
        raise ValueError("tool-lifecycle TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"tool-lifecycle TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"tool-lifecycle TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {
            "incremental_arguments",
            "admission_invalid_arguments",
            "approval_argument_mutation",
        }:
            raise ValueError(f"tool-lifecycle TCK case {case_id} has unsupported kind {case_kind!r}")
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"tool-lifecycle TCK case {case_id} requires expected result")
        cases.append(TckCase.tool_lifecycle(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_tool_execution_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list):
        raise ValueError("tool-execution TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"tool-execution TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"tool-execution TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind != "execution_plan":
            raise ValueError(f"tool-execution TCK case {case_id} has unsupported kind {case_kind!r}")
        calls = raw_case.get("calls")
        if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
            raise ValueError(f"tool-execution TCK case {case_id} calls must be a list of mappings")
        operations = raw_case.get("operations", [])
        if not isinstance(operations, list) or not all(isinstance(operation, dict) for operation in operations):
            raise ValueError(f"tool-execution TCK case {case_id} operations must be a list of mappings")
        expected_states = raw_case.get("expectedStates", {})
        if not isinstance(expected_states, Mapping):
            raise ValueError(f"tool-execution TCK case {case_id} expectedStates must be a mapping")
        cases.append(TckCase.tool_execution(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_usage_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list):
        raise ValueError("usage TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"usage TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"usage TCK case {index} requires name")
        operations = raw_case.get("operations")
        if not isinstance(operations, list) or not all(isinstance(operation, dict) for operation in operations):
            raise ValueError(f"usage TCK case {case_id} operations must be a list of mappings")
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"usage TCK case {case_id} requires expected result")
        cases.append(TckCase.usage(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_policy_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list):
        raise ValueError("policy TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"policy TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"policy TCK case {index} requires name")
        delivery = raw_case.get("delivery", {})
        if not isinstance(delivery, dict):
            raise ValueError(f"policy TCK case {case_id} delivery must be a mapping")
        operations = raw_case.get("operations")
        if not isinstance(operations, list) or not all(isinstance(operation, dict) for operation in operations):
            raise ValueError(f"policy TCK case {case_id} operations must be a list of mappings")
        expected = raw_case.get("expected", {})
        if not isinstance(expected, dict):
            raise ValueError(f"policy TCK case {case_id} expected result must be a mapping")
        cases.append(
            TckCase.policy(
                case_id=case_id,
                delivery=delivery,
                operations=tuple(operations),
                expected=expected,
                stream_id=str(raw_case.get("streamId", raw_case.get("stream_id", "stream-1"))),
                response_id=str(raw_case.get("responseId", raw_case.get("response_id", "response-1"))),
            )
        )
    return tuple(cases)


def load_sequence_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list):
        raise ValueError("sequence TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"sequence TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"sequence TCK case {index} requires name")
        capacity = raw_case.get("capacity")
        if not isinstance(capacity, int) or isinstance(capacity, bool):
            raise ValueError(f"sequence TCK case {case_id} requires integer capacity")
        operations = raw_case.get("operations", [])
        if not isinstance(operations, list) or not all(isinstance(operation, dict) for operation in operations):
            raise ValueError(f"sequence TCK case {case_id} operations must be a list of mappings")
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"sequence TCK case {case_id} requires expected result")
        expected_state = expected.get("state")
        if expected_state is not None and (not isinstance(expected_state, str) or not expected_state.strip()):
            raise ValueError(f"sequence TCK case {case_id} expected state must be a string")
        expected_creation_error = expected.get("creation_error")
        if expected_creation_error is not None and (
            not isinstance(expected_creation_error, str) or not expected_creation_error.strip()
        ):
            raise ValueError(f"sequence TCK case {case_id} expected creation_error must be a string")
        cases.append(
            TckCase.sequence(
                case_id=case_id,
                capacity=capacity,
                operations=tuple(operations),
                expected_state=expected_state,
                expected_creation_error=expected_creation_error,
            )
        )
    return tuple(cases)


def load_schema_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list):
        raise ValueError("schema TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"schema TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"schema TCK case {index} requires name")
        schema_id = _first_mapping_value(raw_case, "schema_id", "schemaId", "id")
        if not isinstance(schema_id, str) or not schema_id.strip():
            raise ValueError(f"schema TCK case {case_id} requires schema_id")
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"schema TCK case {case_id} requires expected result")
        expected_ok = _first_mapping_value(expected, "valid", "expected_ok", "expectedOk")
        if not isinstance(expected_ok, bool):
            raise ValueError(f"schema TCK case {case_id} requires boolean expected valid")
        expected_canonical = _first_mapping_value(
            expected,
            "canonical",
            "canonical_schema_id",
            "canonicalSchemaId",
            "schema_id",
            "schemaId",
        )
        if expected_canonical is not None and not isinstance(expected_canonical, str):
            raise ValueError(f"schema TCK case {case_id} canonical schema id must be a string")
        expected_schema_name = _first_mapping_value(expected, "name", "schema_name", "schemaName")
        if expected_schema_name is not None and not isinstance(expected_schema_name, str):
            raise ValueError(f"schema TCK case {case_id} expected name must be a string")
        expected_major_version = _first_mapping_value(expected, "major_version", "majorVersion")
        if expected_major_version is not None:
            if isinstance(expected_major_version, bool) or not isinstance(expected_major_version, int):
                raise ValueError(f"schema TCK case {case_id} expected major_version must be an integer")
            if expected_major_version <= 0:
                raise ValueError(f"schema TCK case {case_id} expected major_version must be positive")
        expected_error = _first_mapping_value(expected, "error", "error_type", "errorType")
        if expected_error is not None and not isinstance(expected_error, str):
            raise ValueError(f"schema TCK case {case_id} expected error must be a string")
        cases.append(
            TckCase.schema(
                case_id=case_id,
                schema_id=schema_id,
                expected_ok=expected_ok,
                expected_canonical_schema_id=expected_canonical,
                expected_schema_name=expected_schema_name,
                expected_major_version=expected_major_version,
                expected_error=expected_error,
            )
        )
    return tuple(cases)


def load_tck_suite_manifests(root: str | Path) -> tuple[TckSuiteManifest, ...]:
    root_path = Path(root)
    if not root_path.is_dir():
        raise ValueError("TCK root must be a directory")
    manifests: list[TckSuiteManifest] = []
    for path in sorted(root_path.glob("*/cases.json"), key=lambda item: item.parent.name):
        suite_id = path.parent.name
        raw_cases = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw_cases, list):
            raise ValueError(f"TCK suite {suite_id} root must be a list")
        case_ids: list[str] = []
        seen: set[str] = set()
        for index, raw_case in enumerate(raw_cases):
            if not isinstance(raw_case, Mapping):
                raise ValueError(f"TCK suite {suite_id} case {index} must be a mapping")
            case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
            if not isinstance(case_id, str) or not case_id.strip():
                raise ValueError(f"TCK suite {suite_id} case {index} requires name")
            if case_id in seen:
                raise ValueError(f"TCK suite {suite_id} has duplicate case id {case_id!r}")
            seen.add(case_id)
            case_ids.append(case_id)
        manifests.append(
            TckSuiteManifest(
                suite_id=suite_id,
                path=path.relative_to(root_path).as_posix(),
                case_ids=tuple(case_ids),
            )
        )
    return tuple(manifests)


def load_tck_cases_for_suite(suite: str, path: str | Path) -> tuple[TckCase, ...]:
    if suite == "application-events":
        return load_application_event_tck_cases(path)
    if suite == "budget-race":
        return load_budget_race_tck_cases(path)
    if suite == "compiler":
        return load_compiler_tck_cases(path)
    if suite == "exhaustion":
        return load_exhaustion_tck_cases(path)
    if suite == "policy":
        return load_policy_tck_cases(path)
    if suite == "retry":
        return load_retry_tck_cases(path)
    if suite == "runtime":
        return load_runtime_tck_cases(path)
    if suite == "schema":
        return load_schema_tck_cases(path)
    if suite == "sequence":
        return load_sequence_tck_cases(path)
    if suite == "tool-lifecycle":
        return load_tool_lifecycle_tck_cases(path)
    if suite == "tool-execution":
        return load_tool_execution_tck_cases(path)
    if suite == "usage":
        return load_usage_tck_cases(path)
    raise ValueError(f"unsupported TCK suite {suite!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graphblocks-tck")
    subparsers = parser.add_subparsers(dest="command")
    list_parser = subparsers.add_parser("list", help="list shared TCK suite manifests")
    list_parser.add_argument("root", nargs="?", type=Path, default=Path("tck"))
    list_parser.add_argument("--json", action="store_true", help="emit JSON")
    check_parser = subparsers.add_parser("check", help="check TCK fixture coverage for conformance profiles")
    check_parser.add_argument("root", nargs="?", type=Path, default=Path("tck"))
    check_parser.add_argument("--profiles", required=True, type=Path, help="conformance profile YAML document")
    check_parser.add_argument("--profile", dest="profile_ids", action="append", required=True, help="claimed profile id")
    check_parser.add_argument("--json", action="store_true", help="emit JSON")
    run_parser = subparsers.add_parser("run", help="run a shared TCK fixture")
    run_parser.add_argument(
        "suite",
        choices=(
            "application-events",
            "compiler",
            "runtime",
            "schema",
            "policy",
            "retry",
            "sequence",
            "exhaustion",
            "budget-race",
            "tool-lifecycle",
            "tool-execution",
            "usage",
        ),
        help="TCK suite kind",
    )
    run_parser.add_argument("path", type=Path, help="cases.json fixture path")
    run_parser.add_argument("--profile", default="local", help="profile label for the generated report")
    run_parser.add_argument("--json", action="store_true", help="emit JSON")
    run_all_parser = subparsers.add_parser("run-all", help="run every supported shared TCK fixture under a root")
    run_all_parser.add_argument("root", nargs="?", type=Path, default=Path("tck"))
    run_all_parser.add_argument("--profile", default="local", help="profile label for the generated reports")
    run_all_parser.add_argument("--json", action="store_true", help="emit JSON")

    args = parser.parse_args(argv)
    if args.command == "list":
        manifests = load_tck_suite_manifests(args.root)
        payload = {
            "suiteCount": len(manifests),
            "suites": [manifest.manifest_contract() for manifest in manifests],
        }
        payload["contentDigest"] = canonical_hash(payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for manifest in manifests:
                print(f"{manifest.suite_id} cases={manifest.case_count} path={manifest.path}")
        return 0
    if args.command == "check":
        documents = load_documents(args.profiles)
        if not documents:
            raise ValueError("conformance profile document must not be empty")
        coverage = check_tck_suite_coverage(
            ConformanceProfileSet.from_document(documents[0]),
            tuple(args.profile_ids),
            load_tck_suite_manifests(args.root),
        )
        payload = coverage.coverage_contract()
        payload["contentDigest"] = coverage.content_digest()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        elif coverage.ok:
            print(f"OK {len(coverage.claim.tck_suites)} TCK suites covered")
        else:
            for issue in coverage.issues:
                print(f"{issue.code} {issue.suite}: {issue.message}")
        return 0 if coverage.ok else 1
    if args.command == "run":
        cases = load_tck_cases_for_suite(args.suite, args.path)
        report = TckRunner(stdlib_registry(), profile=args.profile).run_cases(cases)
        payload = report.report_contract()
        payload["contentDigest"] = report.content_digest()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"{'OK' if report.ok else 'FAILED'} {len(report.results)} {args.suite} TCK cases")
            for result in report.results:
                if result.status != "passed":
                    print(f"{result.case_id} {result.status}")
        return 0 if report.ok else 1
    if args.command == "run-all":
        reports: dict[str, dict[str, object]] = {}
        ok = True
        for manifest in load_tck_suite_manifests(args.root):
            report = TckRunner(stdlib_registry(), profile=args.profile).run_cases(
                load_tck_cases_for_suite(manifest.suite_id, args.root / manifest.path)
            )
            reports[manifest.suite_id] = report.report_contract()
            ok = ok and report.ok
        payload = {
            "profile": args.profile,
            "ok": ok,
            "reports": reports,
        }
        payload["contentDigest"] = canonical_hash(payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"{'OK' if ok else 'FAILED'} {len(reports)} TCK suites")
            for suite_id, report in reports.items():
                if not report["ok"]:
                    print(f"{suite_id} failed")
        return 0 if ok else 1
    parser.print_help()
    return 0


@dataclass(frozen=True, slots=True)
class AcceptanceApplication:
    application_id: str
    profiles: tuple[str, ...]
    scenario_path: str
    gates: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""

    def __post_init__(self) -> None:
        if not self.application_id.strip():
            raise ValueError("acceptance application_id must not be empty")
        if not self.profiles:
            raise ValueError("acceptance application profiles must not be empty")
        if not self.scenario_path.strip():
            raise ValueError("acceptance application scenario_path must not be empty")
        for profile in self.profiles:
            if not profile.strip():
                raise ValueError("acceptance application profile ids must not be empty")
        for gate in self.gates:
            if not gate.strip():
                raise ValueError("acceptance application gates must not be empty strings")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> AcceptanceApplication:
        application_id = mapping.get("id")
        if not isinstance(application_id, str):
            raise ValueError("acceptance application id must be a string")
        raw_profiles = mapping.get("profiles", ())
        if isinstance(raw_profiles, str):
            profiles = (raw_profiles,)
        else:
            profiles = tuple(str(profile) for profile in raw_profiles or ())
        scenario_path = mapping.get("scenarioPath", mapping.get("scenario_path"))
        if not isinstance(scenario_path, str):
            raise ValueError("acceptance application scenarioPath must be a string")
        raw_gates = mapping.get("gates", ())
        if isinstance(raw_gates, str):
            gates = (raw_gates,)
        else:
            gates = tuple(str(gate) for gate in raw_gates or ())
        description = mapping.get("description", "")
        return cls(
            application_id=application_id,
            profiles=profiles,
            scenario_path=scenario_path,
            gates=gates,
            description=str(description),
        )

    def application_contract(self) -> dict[str, object]:
        return {
            "application_id": self.application_id,
            "profiles": list(self.profiles),
            "scenario_path": self.scenario_path,
            "gates": list(self.gates),
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceCoverageIssue:
    code: str
    application_id: str
    profile_id: str
    path: str
    message: str

    def issue_contract(self) -> dict[str, str]:
        return {
            "code": self.code,
            "application_id": self.application_id,
            "profile_id": self.profile_id,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceCoverageResult:
    issues: tuple[AcceptanceCoverageIssue, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.issues

    def issue_contracts(self) -> list[dict[str, str]]:
        return [issue.issue_contract() for issue in self.issues]


@dataclass(frozen=True, slots=True)
class ConformanceProfile:
    profile_id: str
    status: str
    extends: tuple[str, ...] = field(default_factory=tuple)
    requires: tuple[str, ...] = field(default_factory=tuple)
    tck_suites: tuple[str, ...] = field(default_factory=tuple)
    acceptance_applications: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("conformance profile id must not be empty")
        object.__setattr__(self, "extends", tuple(str(profile_id) for profile_id in self.extends))
        object.__setattr__(self, "requires", tuple(str(requirement) for requirement in self.requires))
        object.__setattr__(self, "tck_suites", tuple(str(suite) for suite in self.tck_suites))
        object.__setattr__(
            self,
            "acceptance_applications",
            tuple(str(application_id) for application_id in self.acceptance_applications),
        )


@dataclass(frozen=True, slots=True)
class ConformanceClaimRequirements:
    profile_ids: tuple[str, ...]
    tck_suites: tuple[str, ...]
    acceptance_applications: tuple[str, ...]

    def claim_contract(self) -> dict[str, object]:
        return {
            "profile_ids": list(self.profile_ids),
            "tck_suites": list(self.tck_suites),
            "acceptance_applications": list(self.acceptance_applications),
        }


@dataclass(frozen=True, slots=True)
class ConformanceClaimIssue:
    code: str
    profile_id: str
    suite: str
    path: str
    message: str

    def issue_contract(self) -> dict[str, str]:
        return {
            "code": self.code,
            "profile_id": self.profile_id,
            "suite": self.suite,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ConformanceClaimValidation:
    claim: ConformanceClaimRequirements
    issues: tuple[ConformanceClaimIssue, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.issues

    def issue_contracts(self) -> list[dict[str, str]]:
        return [issue.issue_contract() for issue in self.issues]


@dataclass(frozen=True, slots=True)
class TckSuiteCoverageIssue:
    code: str
    profile_id: str
    suite: str
    path: str
    message: str

    def issue_contract(self) -> dict[str, str]:
        return {
            "code": self.code,
            "profile_id": self.profile_id,
            "suite": self.suite,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class TckSuiteCoverageResult:
    claim: ConformanceClaimRequirements
    available_suites: tuple[str, ...]
    missing_suites: tuple[str, ...]
    issues: tuple[TckSuiteCoverageIssue, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_suites", tuple(str(suite) for suite in self.available_suites))
        object.__setattr__(self, "missing_suites", tuple(str(suite) for suite in self.missing_suites))
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def ok(self) -> bool:
        return not self.issues

    def issue_contracts(self) -> list[dict[str, str]]:
        return [issue.issue_contract() for issue in self.issues]

    def coverage_contract(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "claim": self.claim.claim_contract(),
            "available_suites": list(self.available_suites),
            "missing_suites": list(self.missing_suites),
            "issues": self.issue_contracts(),
        }

    def content_digest(self) -> str:
        return canonical_hash(self.coverage_contract())


@dataclass(frozen=True, slots=True)
class ConformanceProfileSet:
    profiles: tuple[ConformanceProfile, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for profile in self.profiles:
            if profile.profile_id in seen:
                raise ValueError(f"duplicate conformance profile id {profile.profile_id!r}")
            seen.add(profile.profile_id)

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> ConformanceProfileSet:
        if document.get("kind") != "ConformanceProfileSet":
            raise ValueError("conformance profile document kind must be ConformanceProfileSet")
        spec = document.get("spec")
        if not isinstance(spec, Mapping):
            raise ValueError("conformance profile document spec must be a mapping")
        raw_profiles = spec.get("profiles", ())
        if not isinstance(raw_profiles, list):
            raise ValueError("conformance profile spec.profiles must be a list")
        profiles: list[ConformanceProfile] = []
        for index, raw_profile in enumerate(raw_profiles):
            if not isinstance(raw_profile, Mapping):
                raise ValueError(f"conformance profile {index} must be a mapping")
            profile_id = raw_profile.get("id")
            if not isinstance(profile_id, str):
                raise ValueError(f"conformance profile {index} id must be a string")
            status = raw_profile.get("status", "")
            raw_extends = raw_profile.get("extends", ())
            raw_requires = raw_profile.get("requires", ())
            raw_tck = raw_profile.get("tck", ())
            raw_acceptance = raw_profile.get("acceptanceApplications", ())
            profiles.append(
                ConformanceProfile(
                    profile_id=profile_id,
                    status=str(status),
                    extends=_string_tuple(raw_extends),
                    requires=_string_tuple(raw_requires),
                    tck_suites=_string_tuple(raw_tck),
                    acceptance_applications=_string_tuple(raw_acceptance),
                )
            )
        return cls(tuple(profiles))

    def by_id(self, profile_id: str) -> ConformanceProfile:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        raise KeyError(profile_id)

    def claim_requirements(self, profile_ids: tuple[str, ...]) -> ConformanceClaimRequirements:
        included: set[str] = set()

        def include(profile_id: str) -> None:
            if profile_id in included:
                return
            profile = self.by_id(profile_id)
            for parent_id in profile.extends:
                include(parent_id)
            included.add(profile.profile_id)

        for profile_id in profile_ids:
            include(profile_id)

        ordered_profiles = tuple(profile.profile_id for profile in self.profiles if profile.profile_id in included)
        tck_suites = tuple(
            sorted(
                {
                    suite
                    for profile in self.profiles
                    if profile.profile_id in included
                    for suite in profile.tck_suites
                    if suite
                }
            )
        )
        acceptance_applications: list[str] = []
        seen_acceptance: set[str] = set()
        for profile in self.profiles:
            if profile.profile_id not in included:
                continue
            for application_id in profile.acceptance_applications:
                if application_id not in seen_acceptance:
                    acceptance_applications.append(application_id)
                    seen_acceptance.add(application_id)
        return ConformanceClaimRequirements(
            profile_ids=ordered_profiles,
            tck_suites=tck_suites,
            acceptance_applications=tuple(acceptance_applications),
        )

    def validate_claim(
        self,
        profile_ids: tuple[str, ...],
        *,
        tck_reports: Mapping[str, TckReport],
        acceptance_coverage: AcceptanceCoverageResult,
    ) -> ConformanceClaimValidation:
        claim = self.claim_requirements(profile_ids)
        issues: list[ConformanceClaimIssue] = []
        claimed_profile = profile_ids[-1] if profile_ids else ""
        for suite in claim.tck_suites:
            report = tck_reports.get(suite)
            if report is None:
                issues.append(
                    ConformanceClaimIssue(
                        code="ConformanceTckMissing",
                        profile_id=claimed_profile,
                        suite=suite,
                        path=f"$.profiles.{claimed_profile}.tck.{suite}",
                        message="claimed conformance profile requires a passing TCK suite with no report",
                    )
                )
            elif not report.ok:
                issues.append(
                    ConformanceClaimIssue(
                        code="ConformanceTckFailed",
                        profile_id=claimed_profile,
                        suite=suite,
                        path=f"$.profiles.{claimed_profile}.tck.{suite}",
                        message="claimed conformance profile requires a passing TCK suite but the report failed",
                    )
                )
        if claim.acceptance_applications and not acceptance_coverage.ok:
            for coverage_issue in acceptance_coverage.issues:
                issues.append(
                    ConformanceClaimIssue(
                        code="ConformanceAcceptanceCoverageFailed",
                        profile_id=coverage_issue.profile_id or claimed_profile,
                        suite="acceptance",
                        path=coverage_issue.path,
                        message=coverage_issue.message,
                    )
                )
        return ConformanceClaimValidation(claim=claim, issues=tuple(issues))


def check_tck_suite_coverage(
    profile_set: ConformanceProfileSet,
    profile_ids: tuple[str, ...],
    manifests: tuple[TckSuiteManifest, ...],
) -> TckSuiteCoverageResult:
    claim = profile_set.claim_requirements(profile_ids)
    available_suites = tuple(sorted({manifest.suite_id for manifest in manifests}))
    available = set(available_suites)
    missing_suites = tuple(suite for suite in claim.tck_suites if suite not in available)
    claimed_profile = profile_ids[-1] if profile_ids else ""
    issues = tuple(
        TckSuiteCoverageIssue(
            code="TckSuiteFixtureMissing",
            profile_id=claimed_profile,
            suite=suite,
            path=f"$.profiles.{claimed_profile}.tck.{suite}",
            message="conformance profile requires a TCK suite with no shared fixture manifest",
        )
        for suite in missing_suites
    )
    return TckSuiteCoverageResult(
        claim=claim,
        available_suites=available_suites,
        missing_suites=missing_suites,
        issues=issues,
    )


@dataclass(frozen=True, slots=True)
class AcceptanceManifest:
    applications: tuple[AcceptanceApplication, ...]

    def __post_init__(self) -> None:
        applications = tuple(sorted(self.applications, key=lambda application: application.application_id))
        seen: set[str] = set()
        for application in applications:
            if application.application_id in seen:
                raise ValueError(f"duplicate acceptance application id {application.application_id!r}")
            seen.add(application.application_id)
        object.__setattr__(self, "applications", applications)

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> AcceptanceManifest:
        if document.get("kind") != "AcceptanceApplicationSet":
            raise ValueError("acceptance manifest kind must be AcceptanceApplicationSet")
        spec = document.get("spec")
        if not isinstance(spec, Mapping):
            raise ValueError("acceptance manifest spec must be a mapping")
        raw_applications = spec.get("applications", ())
        if not isinstance(raw_applications, list):
            raise ValueError("acceptance manifest spec.applications must be a list")
        applications = []
        for index, raw_application in enumerate(raw_applications):
            if not isinstance(raw_application, Mapping):
                raise ValueError(f"acceptance manifest application {index} must be a mapping")
            applications.append(AcceptanceApplication.from_mapping(raw_application))
        return cls(tuple(applications))

    def application_ids(self) -> tuple[str, ...]:
        return tuple(application.application_id for application in self.applications)

    def by_id(self, application_id: str) -> AcceptanceApplication:
        for application in self.applications:
            if application.application_id == application_id:
                return application
        raise KeyError(application_id)

    def coverage_for_conformance(
        self,
        conformance_document: Mapping[str, object],
        *,
        root: Path | None = None,
    ) -> AcceptanceCoverageResult:
        applications_by_id = {application.application_id: application for application in self.applications}
        issues: list[AcceptanceCoverageIssue] = []
        spec = conformance_document.get("spec", {})
        profiles = spec.get("profiles", ()) if isinstance(spec, Mapping) else ()
        for profile_index, profile in enumerate(profiles):
            if not isinstance(profile, Mapping):
                continue
            profile_id = str(profile.get("id", ""))
            acceptance_applications = profile.get("acceptanceApplications", ())
            for application_index, raw_application_id in enumerate(acceptance_applications or ()):
                application_id = str(raw_application_id)
                application = applications_by_id.get(application_id)
                if application is None:
                    issues.append(
                        AcceptanceCoverageIssue(
                            code="AcceptanceApplicationMissing",
                            application_id=application_id,
                            profile_id=profile_id,
                            path=f"$.spec.profiles[{profile_index}].acceptanceApplications[{application_index}]",
                            message="profile references an acceptance application with no manifest entry",
                        )
                    )
                    continue
                if profile_id not in application.profiles:
                    issues.append(
                        AcceptanceCoverageIssue(
                            code="AcceptanceProfileNotDeclared",
                            application_id=application_id,
                            profile_id=profile_id,
                            path=f"$.spec.profiles[{profile_index}].acceptanceApplications[{application_index}]",
                            message="acceptance application does not declare the referencing conformance profile",
                        )
                    )
        for application in self.applications:
            if root is not None and not (root / application.scenario_path).exists():
                issues.append(
                    AcceptanceCoverageIssue(
                        code="AcceptanceFixtureMissing",
                        application_id=application.application_id,
                        profile_id="",
                        path=f"$.spec.applications[{application.application_id}].scenarioPath",
                        message="acceptance application scenario path does not exist",
                    )
                )
            if not application.gates:
                issues.append(
                    AcceptanceCoverageIssue(
                        code="AcceptanceGateMissing",
                        application_id=application.application_id,
                        profile_id="",
                        path=f"$.spec.applications[{application.application_id}].gates",
                        message="acceptance application must declare at least one verification gate",
                    )
                )
        return AcceptanceCoverageResult(tuple(issues))

    def manifest_contract(self) -> dict[str, object]:
        return {
            "applications": [application.application_contract() for application in self.applications],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.manifest_contract())


@dataclass(frozen=True, slots=True)
class TckRunner:
    registry: RuntimeRegistry
    profile: str = "local"

    def run_cases(self, cases: tuple[TckCase, ...]) -> TckReport:
        results: list[TckResult] = []
        for case in cases:
            if case.kind == "compiler":
                results.append(self._run_compiler_case(case))
            elif case.kind == "runtime":
                results.append(self._run_runtime_case(case))
            elif case.kind == "policy":
                results.append(self._run_policy_case(case))
            elif case.kind == "application-events":
                results.append(self._run_application_event_case(case))
            elif case.kind == "sequence":
                results.append(self._run_sequence_case(case))
            elif case.kind == "exhaustion":
                results.append(self._run_exhaustion_case(case))
            elif case.kind == "budget-race":
                results.append(self._run_budget_race_case(case))
            elif case.kind == "retry":
                results.append(self._run_retry_case(case))
            elif case.kind == "tool-execution":
                results.append(self._run_tool_execution_case(case))
            elif case.kind == "tool-lifecycle":
                results.append(self._run_tool_lifecycle_case(case))
            elif case.kind == "usage":
                results.append(self._run_usage_case(case))
            else:
                results.append(self._run_schema_case(case))
        return TckReport(profile=self.profile, results=tuple(results))

    def _run_compiler_case(self, case: TckCase) -> TckResult:
        if case.block_catalog:
            plan = compile_graph(case.graph, block_catalog=BlockCatalog.from_blocks(case.block_catalog))
        else:
            plan = compile_graph(case.graph)
        error_codes = tuple(
            diagnostic.code for diagnostic in plan.diagnostics.diagnostics if diagnostic.severity == "error"
        )
        warning_codes = tuple(
            diagnostic.code for diagnostic in plan.diagnostics.diagnostics if diagnostic.severity == "warning"
        )
        observed = {
            "hash": plan.graph_hash,
            "ok": plan.ok,
            "error_codes": list(error_codes),
            "warning_codes": list(warning_codes),
        }
        diagnostics: list[dict[str, str]] = []
        if plan.ok != case.expected_ok:
            diagnostics.append(
                {
                    "code": "CompilerOkMismatch",
                    "message": "compiler ok value did not match expected result",
                    "path": "$.expected_ok",
                }
            )
        if case.expected_hash is not None and plan.graph_hash != case.expected_hash:
            diagnostics.append(
                {
                    "code": "HashMismatch",
                    "message": "compiler graph hash did not match expected hash",
                    "path": "$.expected_hash",
                }
            )
        if error_codes != case.expected_error_codes:
            diagnostics.append(
                {
                    "code": "ErrorCodesMismatch",
                    "message": "compiler error codes did not match expected diagnostics",
                    "path": "$.expected_error_codes",
                }
            )
        if warning_codes != case.expected_warning_codes:
            diagnostics.append(
                {
                    "code": "WarningCodesMismatch",
                    "message": "compiler warning codes did not match expected diagnostics",
                    "path": "$.expected_warning_codes",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_schema_case(self, case: TckCase) -> TckResult:
        try:
            schema_id = SchemaId.parse(case.schema_id or "")
            observed = {
                "valid": True,
                "canonical": schema_id.as_str(),
                "name": schema_id.name,
                "major_version": schema_id.major_version,
            }
        except SchemaIdError as error:
            observed = {
                "valid": False,
                "error": type(error).__name__,
                "message": str(error),
            }
        diagnostics: list[dict[str, str]] = []
        if observed["valid"] != case.expected_ok:
            diagnostics.append(
                {
                    "code": "SchemaValidityMismatch",
                    "message": "schema id validity did not match expected result",
                    "path": "$.expected_ok",
                }
            )
        if (
            case.expected_canonical_schema_id is not None
            and observed.get("canonical") != case.expected_canonical_schema_id
        ):
            diagnostics.append(
                {
                    "code": "SchemaCanonicalMismatch",
                    "message": "schema id canonical value did not match expected value",
                    "path": "$.expected_canonical_schema_id",
                }
            )
        if case.expected_schema_name is not None and observed.get("name") != case.expected_schema_name:
            diagnostics.append(
                {
                    "code": "SchemaNameMismatch",
                    "message": "schema id name did not match expected value",
                    "path": "$.expected_schema_name",
                }
            )
        if case.expected_major_version is not None and observed.get("major_version") != case.expected_major_version:
            diagnostics.append(
                {
                    "code": "SchemaMajorVersionMismatch",
                    "message": "schema id major version did not match expected value",
                    "path": "$.expected_major_version",
                }
            )
        if case.expected_error is not None and observed.get("error") != case.expected_error:
            diagnostics.append(
                {
                    "code": "SchemaErrorMismatch",
                    "message": "schema id error type did not match expected error",
                    "path": "$.expected_error",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_application_event_case(self, case: TckCase) -> TckResult:
        state = ApplicationEventStreamState()
        diagnostics: list[dict[str, str]] = []
        for sequence, operation in enumerate(case.application_event_operations, start=1):
            response_id = str(operation.get("responseId", "response-1"))
            metadata = ApplicationEventMetadata(
                event_id=f"{case.case_id}:{sequence}",
                run_id=str(operation.get("runId", "run-1")),
                response_id=response_id,
                turn_id=str(operation["turnId"]) if operation.get("turnId") is not None else None,
                sequence=sequence,
                release_id=str(operation.get("releaseId", "release-1")),
                policy_snapshot_id=str(operation.get("policySnapshotId", "policy-1")),
                occurred_at=str(operation.get("occurredAt", "2026-06-23T00:00:00Z")),
            )
            if operation.get("op") == "output_cutoff":
                cutoff = OutputCutoff(
                    stream_id=str(operation.get("streamId", "stream-1")),
                    response_id=response_id,
                    turn_id=str(operation["turnId"]) if operation.get("turnId") is not None else None,
                    last_generated_sequence=int(operation.get("lastGeneratedSequence", 0)),
                    last_policy_accepted_sequence=int(operation.get("lastPolicyAcceptedSequence", 0)),
                    last_client_delivered_sequence=int(operation.get("lastClientDeliveredSequence", 0)),
                    terminal_reason=str(operation.get("terminalReason", "policy_denied")),
                    draft_disposition=str(operation.get("draftDisposition", "retract")),
                    durable_result=str(operation.get("durableResult", "none")),
                    policy_decision_id=(
                        str(operation["policyDecisionId"]) if operation.get("policyDecisionId") is not None else None
                    ),
                    occurred_at=str(operation.get("occurredAt", "2026-06-23T00:00:00Z")),
                )
                for event in ApplicationEvent.output_cutoff(metadata, cutoff):
                    if state.accept(event) is None:
                        diagnostics.append(
                            {
                                "code": "ApplicationEventUnexpectedRejection",
                                "message": "application event TCK output cutoff event was rejected",
                                "path": f"$.operations[{sequence - 1}]",
                            }
                        )
            elif operation.get("op") == "run_succeeded":
                event = ApplicationEvent.new(
                    "RunSucceeded",
                    metadata,
                    payload={"status": "succeeded", "outputs": operation.get("outputs", {})},
                )
                accepted = state.accept(event)
                if (accepted is not None) is not bool(operation.get("expectAccepted", True)):
                    diagnostics.append(
                        {
                            "code": "ApplicationEventAcceptanceMismatch",
                            "message": "application event acceptance did not match expected result",
                            "path": f"$.operations[{sequence - 1}].expectAccepted",
                        }
                    )
            elif operation.get("op") in {
                "tool_result_started",
                "tool_result_delta",
                "tool_result_completed",
            }:
                tool_call_id = str(operation.get("toolCallId", operation.get("tool_call_id", "")))
                tool_result_sequence = int(
                    operation.get("toolResultSequence", operation.get("tool_result_sequence", sequence))
                )
                op = str(operation["op"])
                if op == "tool_result_started":
                    result_event = ToolResultEvent.started(
                        tool_call_id,
                        tool_result_sequence,
                        started_at=str(operation.get("startedAt", "2026-06-23T00:00:00Z")),
                    )
                else:
                    raw_output = operation.get("output", [])
                    if not isinstance(raw_output, list):
                        diagnostics.append(
                            {
                                "code": "ApplicationEventToolResultOutputInvalid",
                                "message": "tool result output must be a list",
                                "path": f"$.operations[{sequence - 1}].output",
                            }
                        )
                        continue
                    output_parts: list[ContentPart] = []
                    invalid_output = False
                    for part_index, raw_part in enumerate(raw_output):
                        if not isinstance(raw_part, Mapping):
                            diagnostics.append(
                                {
                                    "code": "ApplicationEventToolResultOutputInvalid",
                                    "message": "tool result output part must be a mapping",
                                    "path": f"$.operations[{sequence - 1}].output[{part_index}]",
                                }
                            )
                            invalid_output = True
                            break
                        metadata_value = raw_part.get("metadata", {})
                        if not isinstance(metadata_value, dict):
                            diagnostics.append(
                                {
                                    "code": "ApplicationEventToolResultOutputInvalid",
                                    "message": "tool result output metadata must be a mapping",
                                    "path": f"$.operations[{sequence - 1}].output[{part_index}].metadata",
                                }
                            )
                            invalid_output = True
                            break
                        part_kind = str(raw_part.get("kind", "text"))
                        if part_kind == "text":
                            text = raw_part.get("text")
                            if not isinstance(text, str):
                                diagnostics.append(
                                    {
                                        "code": "ApplicationEventToolResultOutputInvalid",
                                        "message": "text tool result output part requires text",
                                        "path": f"$.operations[{sequence - 1}].output[{part_index}].text",
                                    }
                                )
                                invalid_output = True
                                break
                            output_parts.append(
                                ContentPart(kind="text", text=text, metadata=dict(metadata_value))
                            )
                        elif part_kind in {"json", "artifact_ref"}:
                            data = raw_part.get("data")
                            if not isinstance(data, dict):
                                diagnostics.append(
                                    {
                                        "code": "ApplicationEventToolResultOutputInvalid",
                                        "message": f"{part_kind} tool result output part requires object data",
                                        "path": f"$.operations[{sequence - 1}].output[{part_index}].data",
                                    }
                                )
                                invalid_output = True
                                break
                            output_parts.append(
                                ContentPart(kind=part_kind, data=dict(data), metadata=dict(metadata_value))
                            )
                        else:
                            diagnostics.append(
                                {
                                    "code": "ApplicationEventToolResultOutputInvalid",
                                    "message": f"unsupported tool result output kind {part_kind!r}",
                                    "path": f"$.operations[{sequence - 1}].output[{part_index}].kind",
                                }
                            )
                            invalid_output = True
                            break
                    if invalid_output:
                        continue
                    if op == "tool_result_delta":
                        result_event = ToolResultEvent.delta(
                            tool_call_id,
                            tool_result_sequence,
                            tuple(output_parts),
                        )
                    else:
                        result = ToolResult.completed(
                            tool_call_id,
                            tuple(output_parts),
                            started_at=str(operation.get("startedAt", "2026-06-23T00:00:00Z")),
                            completed_at=str(operation.get("completedAt", "2026-06-23T00:00:00Z")),
                        )
                        if operation.get("effectOutcome") is not None:
                            result = result.with_effect_outcome(str(operation["effectOutcome"]))
                        result_event = ToolResultEvent.completed(
                            tool_call_id,
                            tool_result_sequence,
                            result,
                        )
                event = ApplicationEvent.tool_result_event(metadata, result_event)
                accepted = event is not None and state.accept(event) is not None
                if accepted is not bool(operation.get("expectAccepted", True)):
                    diagnostics.append(
                        {
                            "code": "ApplicationEventAcceptanceMismatch",
                            "message": "application event acceptance did not match expected result",
                            "path": f"$.operations[{sequence - 1}].expectAccepted",
                        }
                    )
            else:
                diagnostics.append(
                    {
                        "code": "ApplicationEventOperationUnknown",
                        "message": f"application event TCK operation {operation.get('op')!r} is not supported",
                        "path": f"$.operations[{sequence - 1}].op",
                    }
                )

        accepted_kinds = [event.kind for event in state.accepted_events]
        if accepted_kinds != list(case.expected_accepted_event_kinds):
            diagnostics.append(
                {
                    "code": "ApplicationEventAcceptedKindsMismatch",
                    "message": "accepted application event kinds did not match expected kinds",
                    "path": "$.expectedAcceptedKinds",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed={"accepted_kinds": accepted_kinds},
        )

    def _run_exhaustion_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.exhaustion_fixture
        policy_mapping = fixture.get("policy")
        if not isinstance(policy_mapping, Mapping):
            return TckResult(
                case_id=case.case_id,
                kind=case.kind,
                status="failed",
                diagnostics=(
                    {
                        "code": "ExhaustionPolicyMissing",
                        "message": "exhaustion TCK case requires policy",
                        "path": "$.policy",
                    },
                ),
                observed={},
            )

        continuation = None
        continuation_mapping = policy_mapping.get("continuation")
        if isinstance(continuation_mapping, Mapping):
            max_additional_usage = []
            raw_usage = continuation_mapping.get("maxAdditionalUsage", [])
            if isinstance(raw_usage, list):
                for amount in raw_usage:
                    if isinstance(amount, Mapping):
                        max_additional_usage.append(
                            UsageAmount(
                                kind=str(amount.get("kind", "")),
                                amount=amount.get("amount", 0),
                                unit=str(amount.get("unit", "")),
                            )
                        )
            max_steps = continuation_mapping.get("maxAdditionalSteps")
            continuation = ContinuationEnvelope(
                allowed_work=set(str(item) for item in continuation_mapping.get("allowedWork", []) or []),
                forbidden_work=set(str(item) for item in continuation_mapping.get("forbiddenWork", []) or []),
                max_additional_usage=max_additional_usage,
                max_additional_steps=int(max_steps) if max_steps is not None else None,
                deadline=str(continuation_mapping["deadline"]) if continuation_mapping.get("deadline") else None,
            )

        policy = ExhaustionPolicy.from_preset(
            str(policy_mapping.get("preset", "")),
            unit=str(policy_mapping.get("unit", "")),
            continuation=continuation,
        )
        observed: dict[str, object] = {"admissions": []}

        validation = fixture.get("validate")
        if isinstance(validation, Mapping):
            validation_error = None
            try:
                validate_exhaustion_policy(policy, production=bool(validation.get("production", False)))
            except MissingExhaustionBoundaryError:
                validation_error = "missing_exhaustion_boundary"
            observed["validation_error"] = validation_error
            expected_error = validation.get("expectError")
            if expected_error is not None:
                if validation_error != expected_error:
                    diagnostics.append(
                        {
                            "code": "ExhaustionValidationErrorMismatch",
                            "message": "exhaustion validation error did not match expected result",
                            "path": "$.validate.expectError",
                        }
                    )
            elif validation_error is not None:
                diagnostics.append(
                    {
                        "code": "ExhaustionValidationUnexpectedError",
                        "message": "exhaustion validation failed unexpectedly",
                        "path": "$.validate",
                    }
                )

        atomic_unit = str(fixture.get("atomicUnit", "turn:1"))
        admission_epoch = int(fixture.get("admissionEpoch", 7))
        profile = str(policy.preset or "finish_current_turn")
        stored_permit = None
        stored_permit_mapping = fixture.get("continuationPermit")
        if isinstance(stored_permit_mapping, Mapping):
            authorized_usage = []
            raw_authorized_usage = stored_permit_mapping.get(
                "authorizedUsage",
                [{"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}],
            )
            if isinstance(raw_authorized_usage, list):
                for amount in raw_authorized_usage:
                    if isinstance(amount, Mapping):
                        authorized_usage.append(
                            UsageAmount(
                                kind=str(amount.get("kind", "")),
                                amount=amount.get("amount", 0),
                                unit=str(amount.get("unit", "")),
                            )
                        )
            stored_permit = BudgetPermit(
                permit_id=str(stored_permit_mapping.get("permitId", "permit-1")),
                reservation_refs=("reservation-1",),
                owner=PolicyResourceRef(str(stored_permit_mapping.get("owner", "worker:1"))),
                atomic_unit=PolicyResourceRef(
                    str(stored_permit_mapping.get("atomicUnit", atomic_unit)),
                    resource_kind="turn",
                ),
                admission_epoch=int(stored_permit_mapping.get("admissionEpoch", admission_epoch)),
                authorized_amounts=authorized_usage,
                continuation_profile=str(stored_permit_mapping.get("continuationProfile", profile)),
                policy_snapshot_digest="sha256:policy",
                expires_at=str(stored_permit_mapping.get("expiresAt", "2026-06-22T01:00:00Z")),
                fencing_tokens={"budget-1": 1},
            )

        controller = ExhaustionController(
            policy,
            atomic_unit_id=atomic_unit,
            admission_epoch=admission_epoch,
            continuation_permit=stored_permit,
            validation_time=str(fixture["validationTime"]) if fixture.get("validationTime") else None,
        )

        admission_results: list[dict[str, object]] = []
        admissions = fixture.get("admissions", [])
        if isinstance(admissions, list):
            for operation_index, operation in enumerate(admissions):
                if not isinstance(operation, Mapping):
                    diagnostics.append(
                        {
                            "code": "ExhaustionAdmissionInvalid",
                            "message": "exhaustion admission operation must be a mapping",
                            "path": f"$.admissions[{operation_index}]",
                        }
                    )
                    continue
                permit = None
                permit_value = operation.get("permit")
                if isinstance(permit_value, Mapping):
                    authorized_usage = []
                    raw_authorized_usage = permit_value.get(
                        "authorizedUsage",
                        [{"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}],
                    )
                    if isinstance(raw_authorized_usage, list):
                        for amount in raw_authorized_usage:
                            if isinstance(amount, Mapping):
                                authorized_usage.append(
                                    UsageAmount(
                                        kind=str(amount.get("kind", "")),
                                        amount=amount.get("amount", 0),
                                        unit=str(amount.get("unit", "")),
                                    )
                                )
                    permit = BudgetPermit(
                        permit_id=str(permit_value.get("permitId", "permit-1")),
                        reservation_refs=("reservation-1",),
                        owner=PolicyResourceRef(str(permit_value.get("owner", "worker:1"))),
                        atomic_unit=PolicyResourceRef(
                            str(permit_value.get("atomicUnit", atomic_unit)),
                            resource_kind="turn",
                        ),
                        admission_epoch=int(permit_value.get("admissionEpoch", admission_epoch)),
                        authorized_amounts=authorized_usage,
                        continuation_profile=str(permit_value.get("continuationProfile", profile)),
                        policy_snapshot_digest="sha256:policy",
                        expires_at=str(permit_value.get("expiresAt", "2026-06-22T01:00:00Z")),
                        fencing_tokens={"budget-1": 1},
                    )
                elif permit_value == "stored":
                    permit = stored_permit
                elif permit_value not in (None, "none"):
                    diagnostics.append(
                        {
                            "code": "ExhaustionPermitReferenceUnknown",
                            "message": "exhaustion admission references an unknown permit",
                            "path": f"$.admissions[{operation_index}].permit",
                        }
                    )

                requested_usage = []
                raw_requested_usage = operation.get("usage", [])
                if isinstance(raw_requested_usage, list):
                    for amount in raw_requested_usage:
                        if isinstance(amount, Mapping):
                            requested_usage.append(
                                UsageAmount(
                                    kind=str(amount.get("kind", "")),
                                    amount=amount.get("amount", 0),
                                    unit=str(amount.get("unit", "")),
                                )
                            )
                decision = controller.admit(
                    str(operation.get("workKind", "")),
                    work_epoch=int(operation.get("workEpoch", 0)),
                    permit=permit,
                    requested_usage=requested_usage or None,
                )
                admission_results.append({"allowed": decision.allowed, "reason": decision.reason})
                if decision.allowed is not operation.get("allowed"):
                    diagnostics.append(
                        {
                            "code": "ExhaustionAdmissionAllowedMismatch",
                            "message": "exhaustion admission allowed value did not match expected result",
                            "path": f"$.admissions[{operation_index}].allowed",
                        }
                    )
                if decision.reason != operation.get("reason"):
                    diagnostics.append(
                        {
                            "code": "ExhaustionAdmissionReasonMismatch",
                            "message": "exhaustion admission reason did not match expected result",
                            "path": f"$.admissions[{operation_index}].reason",
                        }
                    )
        observed["admissions"] = admission_results
        observed["usedAdditionalSteps"] = controller.used_additional_steps
        expected = fixture.get("expected")
        if isinstance(expected, Mapping) and "usedAdditionalSteps" in expected:
            if controller.used_additional_steps != expected["usedAdditionalSteps"]:
                diagnostics.append(
                    {
                        "code": "ExhaustionUsedStepsMismatch",
                        "message": "exhaustion used additional steps did not match expected result",
                        "path": "$.expected.usedAdditionalSteps",
                    }
                )

        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_budget_race_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.budget_race_fixture
        ledger = InMemoryBudgetLedger()

        allocated = []
        raw_allocated = fixture.get("allocated", [])
        if isinstance(raw_allocated, list):
            for amount in raw_allocated:
                if isinstance(amount, Mapping):
                    allocated.append(
                        UsageAmount(
                            kind=str(amount.get("kind", "")),
                            amount=amount.get("amount", 0),
                            unit=str(amount.get("unit", "")),
                        )
                    )
        budget_id = str(fixture.get("budgetId", ""))
        ledger.allocate(
            budget_id,
            PolicyResourceRef(str(fixture.get("scope", ""))),
            allocated,
            policy_ref=str(fixture.get("policyRef", "")),
        )

        outcomes: list[dict[str, object]] = []
        if fixture.get("kind") == "reservation_race":
            reservation_amounts = []
            raw_reservation_amounts = fixture.get("reservationAmounts", [])
            if isinstance(raw_reservation_amounts, list):
                for amount in raw_reservation_amounts:
                    if isinstance(amount, Mapping):
                        reservation_amounts.append(
                            UsageAmount(
                                kind=str(amount.get("kind", "")),
                                amount=amount.get("amount", 0),
                                unit=str(amount.get("unit", "")),
                            )
                        )
            for owner in fixture.get("owners", []) or []:
                try:
                    ledger.reserve(
                        budget_id,
                        PolicyResourceRef(str(owner)),
                        reservation_amounts,
                        purpose=str(fixture.get("reservationPurpose", "provider_call")),
                        expires_at=str(fixture.get("expiresAt", "")),
                    )
                    outcomes.append({"allowed": True, "error": None})
                except BudgetExceededError:
                    outcomes.append({"allowed": False, "error": "BudgetExceeded"})
            balance = ledger.balance(budget_id)
            observed_reserved = [
                {
                    "kind": amount.kind,
                    "amount": int(amount.amount) if amount.amount == amount.amount.to_integral_value() else str(amount.amount),
                    "unit": amount.unit,
                }
                for amount in balance.reserved
            ]
            observed_available = [
                {
                    "kind": amount.kind,
                    "amount": int(amount.amount) if amount.amount == amount.amount.to_integral_value() else str(amount.amount),
                    "unit": amount.unit,
                }
                for amount in balance.available
            ]
            expected_reserved = [
                {
                    "kind": str(amount.get("kind", "")),
                    "amount": int(amount.get("amount", 0)),
                    "unit": str(amount.get("unit", "")),
                }
                for amount in fixture.get("expectedReserved", [])
                if isinstance(amount, Mapping)
            ]
            expected_available = [
                {
                    "kind": str(amount.get("kind", "")),
                    "amount": int(amount.get("amount", 0)),
                    "unit": str(amount.get("unit", "")),
                }
                for amount in fixture.get("expectedAvailable", [])
                if isinstance(amount, Mapping)
            ]
            if observed_reserved != expected_reserved:
                diagnostics.append(
                    {
                        "code": "BudgetRaceReservedMismatch",
                        "message": "budget-race reserved amounts did not match expected result",
                        "path": "$.expectedReserved",
                    }
                )
            if observed_available != expected_available:
                diagnostics.append(
                    {
                        "code": "BudgetRaceAvailableMismatch",
                        "message": "budget-race available amounts did not match expected result",
                        "path": "$.expectedAvailable",
                    }
                )
            observed: dict[str, object] = {
                "allowed": sum(1 for outcome in outcomes if outcome["allowed"]),
                "denied": sum(1 for outcome in outcomes if not outcome["allowed"]),
                "denied_errors": [outcome["error"] for outcome in outcomes if not outcome["allowed"]],
                "reserved": observed_reserved,
                "available": observed_available,
            }
        elif fixture.get("kind") == "completion_reserve_race":
            reserve_amounts = []
            raw_reserve_amounts = fixture.get("reserveAmounts", [])
            if isinstance(raw_reserve_amounts, list):
                for amount in raw_reserve_amounts:
                    if isinstance(amount, Mapping):
                        reserve_amounts.append(
                            UsageAmount(
                                kind=str(amount.get("kind", "")),
                                amount=amount.get("amount", 0),
                                unit=str(amount.get("unit", "")),
                            )
                        )
            ledger.create_completion_reserve(
                str(fixture.get("reserveId", "")),
                budget_id,
                purpose=str(fixture.get("reservePurpose", "finalization")),
                amounts=reserve_amounts,
                spendable_by=tuple(str(spender) for spender in fixture.get("spendableBy", []) or []),
            )
            for spender in fixture.get("spenders", []) or []:
                try:
                    ledger.spend_completion_reserve(
                        str(fixture.get("reserveId", "")),
                        str(spender),
                        expires_at=str(fixture.get("expiresAt", "")),
                    )
                    outcomes.append({"allowed": True, "error": None})
                except BudgetCompletionReserveStateError:
                    outcomes.append({"allowed": False, "error": "CompletionReserveState"})
            reserve = ledger.completion_reserve(str(fixture.get("reserveId", "")))
            balance = ledger.balance(budget_id)
            observed_reserved = [
                {
                    "kind": amount.kind,
                    "amount": int(amount.amount) if amount.amount == amount.amount.to_integral_value() else str(amount.amount),
                    "unit": amount.unit,
                }
                for amount in balance.reserved
            ]
            expected_reserved = [
                {
                    "kind": str(amount.get("kind", "")),
                    "amount": int(amount.get("amount", 0)),
                    "unit": str(amount.get("unit", "")),
                }
                for amount in fixture.get("expectedReserved", [])
                if isinstance(amount, Mapping)
            ]
            if reserve.status != fixture.get("expectedReserveStatus"):
                diagnostics.append(
                    {
                        "code": "BudgetRaceReserveStatusMismatch",
                        "message": "budget-race completion reserve status did not match expected result",
                        "path": "$.expectedReserveStatus",
                    }
                )
            if observed_reserved != expected_reserved:
                diagnostics.append(
                    {
                        "code": "BudgetRaceReservedMismatch",
                        "message": "budget-race reserved amounts did not match expected result",
                        "path": "$.expectedReserved",
                    }
                )
            observed = {
                "allowed": sum(1 for outcome in outcomes if outcome["allowed"]),
                "denied": sum(1 for outcome in outcomes if not outcome["allowed"]),
                "denied_errors": [outcome["error"] for outcome in outcomes if not outcome["allowed"]],
                "reserve_status": reserve.status,
                "reserved": observed_reserved,
            }
        else:
            return TckResult(
                case_id=case.case_id,
                kind=case.kind,
                status="failed",
                diagnostics=(
                    {
                        "code": "BudgetRaceKindUnknown",
                        "message": f"budget-race TCK kind {fixture.get('kind')!r} is not supported",
                        "path": "$.kind",
                    },
                ),
                observed={},
            )

        if observed["allowed"] != fixture.get("expectedAllowed"):
            diagnostics.append(
                {
                    "code": "BudgetRaceAllowedMismatch",
                    "message": "budget-race allowed count did not match expected result",
                    "path": "$.expectedAllowed",
                }
            )
        if observed["denied"] != fixture.get("expectedDenied"):
            diagnostics.append(
                {
                    "code": "BudgetRaceDeniedMismatch",
                    "message": "budget-race denied count did not match expected result",
                    "path": "$.expectedDenied",
                }
            )
        expected_denied_error = fixture.get("expectedDeniedError")
        if expected_denied_error is not None and any(
            error != expected_denied_error for error in observed["denied_errors"]
        ):
            diagnostics.append(
                {
                    "code": "BudgetRaceDeniedErrorMismatch",
                    "message": "budget-race denied error did not match expected result",
                    "path": "$.expectedDeniedError",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_retry_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.retry_fixture
        kind = str(fixture.get("kind", ""))
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "RetryExpectedInvalid",
                    "message": "retry TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )

        attempts = {"count": 0}
        seen_idempotency_keys: list[str | None] = []
        registry = RuntimeRegistry()
        block_id = str(fixture.get("block", "test.flaky_write@1"))
        node_id = str(fixture.get("nodeId", fixture.get("node_id", "write")))
        max_attempts = int(fixture.get("maxAttempts", fixture.get("max_attempts", 1)))
        failures_before_success = int(
            fixture.get("failuresBeforeSuccess", fixture.get("failures_before_success", 0))
        )
        cancel_on_attempt = fixture.get("cancelOnAttempt", fixture.get("cancel_on_attempt"))
        cancel_reason = str(fixture.get("cancelReason", fixture.get("cancel_reason", "policy_stop")))
        idempotency_key = fixture.get("idempotencyKey", fixture.get("idempotency_key"))

        def retry_block(inputs: dict[str, object], config: dict[str, object], context: dict[str, object]) -> dict[str, object]:
            attempts["count"] += 1
            seen_idempotency_keys.append(
                str(context.get("idempotency_key")) if context.get("idempotency_key") is not None else None
            )
            if isinstance(cancel_on_attempt, int) and attempts["count"] == cancel_on_attempt:
                token = context.get("cancellation_token")
                if isinstance(token, CancellationToken):
                    token.cancel(cancel_reason)
            if attempts["count"] <= failures_before_success:
                raise RuntimeError(str(fixture.get("error", "temporary failure")))
            return {"value": fixture.get("outputValue", fixture.get("output_value", "committed"))}

        registry.register(block_id, retry_block)
        retry_config: dict[str, object] = {"maxAttempts": max_attempts}
        if idempotency_key is not None:
            retry_config["idempotencyKey"] = str(idempotency_key)
        raw_effects = fixture.get("effects", [])
        if isinstance(raw_effects, str):
            raw_effects = [raw_effects]
        graph = {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": f"retry-tck-{case.case_id}"},
            "spec": {
                "nodes": {
                    node_id: {
                        "block": block_id,
                        "effects": list(raw_effects) if isinstance(raw_effects, list) else [],
                        "flow": {"retry": retry_config},
                        "outputs": {"value": "$output.value"},
                    }
                }
            },
        }

        try:
            result = InProcessRuntime(registry).run(graph, {})
            retry_idempotency_keys = [
                record.payload.get("idempotencyKey")
                for record in result.journal.records
                if record.kind == "node_retry"
            ]
            observed: dict[str, object] = {
                "status": result.status,
                "terminalKind": result.journal.terminal_kind,
                "attempts": attempts["count"],
                "retryCount": len(retry_idempotency_keys),
                "retryIdempotencyKeys": retry_idempotency_keys,
                "contextIdempotencyKeys": seen_idempotency_keys,
                "outputs": result.outputs,
                "journalKinds": [record.kind for record in result.journal.records],
                "compileError": None,
            }
        except ValueError as error:
            observed = {
                "status": "compile_failed",
                "terminalKind": "compile_error",
                "attempts": attempts["count"],
                "retryCount": 0,
                "retryIdempotencyKeys": [],
                "contextIdempotencyKeys": seen_idempotency_keys,
                "outputs": {},
                "journalKinds": [],
                "compileError": str(error),
            }

        if kind not in {"node_retry", "cancelled_before_retry"}:
            diagnostics.append(
                {
                    "code": "RetryKindUnknown",
                    "message": f"retry TCK kind {kind!r} is not supported",
                    "path": "$.kind",
                }
            )
        for key, expected_value in expected.items():
            if observed.get(str(key)) != expected_value:
                diagnostics.append(
                    {
                        "code": "RetryExpectedMismatch",
                        "message": f"retry observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_tool_lifecycle_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.tool_lifecycle_fixture
        kind = str(fixture.get("kind", ""))
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "ToolLifecycleExpectedInvalid",
                    "message": "tool-lifecycle TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )
        observed: dict[str, object] = {}

        if kind == "incremental_arguments":
            draft = ToolCallDraft.proposed(
                str(fixture.get("responseId", "response-1")),
                str(fixture.get("toolCallId", "call-1")),
                str(fixture.get("toolName", "knowledge.search")),
            )
            statuses = [draft.status]
            fragments = fixture.get("fragments", [])
            if not isinstance(fragments, list):
                fragments = []
                diagnostics.append(
                    {
                        "code": "ToolLifecycleFragmentsInvalid",
                        "message": "incremental argument case requires fragments",
                        "path": "$.fragments",
                    }
                )
            for fragment in fragments:
                draft = draft.append_argument_fragment(str(fragment))
                statuses.append(draft.status)
            try:
                draft.into_tool_call(
                    str(fixture.get("resolvedToolId", "resolved-tool-1")),
                    created_at=str(fixture.get("createdAt", "2026-06-23T00:00:00Z")),
                )
                finalized_before_complete = True
            except ToolCallError:
                finalized_before_complete = False
            draft = draft.complete_arguments()
            statuses.append(draft.status)
            try:
                call = draft.into_tool_call(
                    str(fixture.get("resolvedToolId", "resolved-tool-1")),
                    created_at=str(fixture.get("createdAt", "2026-06-23T00:00:00Z")),
                )
                finalized_after_complete = True
                observed_arguments = call.arguments
                observed_status = call.status
            except ToolCallError as error:
                finalized_after_complete = False
                observed_arguments = None
                observed_status = f"error:{type(error).__name__}"
            observed = {
                "statuses": statuses,
                "finalizedBeforeComplete": finalized_before_complete,
                "finalizedAfterComplete": finalized_after_complete,
                "callStatus": observed_status,
                "arguments": observed_arguments,
            }
        elif kind == "admission_invalid_arguments":
            schema_id = str(fixture.get("schemaId", "schemas/ProcessRun@1"))
            tool_name = str(fixture.get("toolName", "process.run"))
            catalog = ToolCatalog(
                definitions=(
                    ToolDefinition(tool_name, "Run an approved process.", schema_id),
                ),
                bindings=(
                    ToolBinding(
                        "binding-process",
                        tool_name,
                        BlockToolImplementation("blocks.process"),
                        effects=frozenset({"process"}),
                        approval="always",
                        idempotency="required",
                    ),
                ),
            )
            resolved_tool = catalog.resolve(
                ToolResolutionScope(),
                effective_policy_snapshot_id="policy-snapshot-1",
            )[0]
            schemas = ToolSchemaRegistry(
                (
                    JsonSchema(
                        schema_id,
                        JsonSchemaNode.object().required_property(
                            "cmd",
                            JsonSchemaNode.array(JsonSchemaNode.string()),
                        ),
                    ),
                )
            )
            draft = ToolCallDraft.proposed("response-1", "call-1", tool_name)
            draft = draft.append_argument_fragment(json.dumps(fixture.get("arguments", {}), sort_keys=True))
            call = draft.complete_arguments().into_tool_call(
                resolved_tool.resolved_tool_id,
                created_at="2026-06-23T00:00:00Z",
            )
            policy_decision = PolicyDecision(
                decision_id="decision-allow-tool",
                effect="allow",
                reason_codes=("allow-process",),
                policy_refs=("allow-process",),
                evaluated_at="2026-06-23T00:00:01Z",
                input_digest="sha256:before-tool",
            )
            try:
                admit_tool_call(
                    call,
                    resolved_tool,
                    schemas,
                    policy_decision=policy_decision,
                    expected_policy_input_digest=policy_decision.input_digest,
                    approval=None,
                    principal_id="user-1",
                    idempotency_key="idem-1",
                    admitted_at="2026-06-23T00:00:02Z",
                    now=1200,
                )
                observed = {
                    "admitted": True,
                    "error": None,
                    "schemaRejectedBeforeApproval": False,
                }
            except Exception as error:
                message = str(error)
                observed = {
                    "admitted": False,
                    "error": message,
                    "schemaRejectedBeforeApproval": (
                        "arguments invalid" in message and "requires approval" not in message
                    ),
                }
        elif kind == "approval_argument_mutation":
            schema_id = str(fixture.get("schemaId", "schemas/ProcessRun@1"))
            tool_name = str(fixture.get("toolName", "process.run"))
            catalog = ToolCatalog(
                definitions=(
                    ToolDefinition(tool_name, "Run an approved process.", schema_id),
                ),
                bindings=(
                    ToolBinding(
                        "binding-process",
                        tool_name,
                        BlockToolImplementation("blocks.process"),
                        effects=frozenset({"process"}),
                        approval="always",
                        idempotency="required",
                    ),
                ),
            )
            resolved_tool = catalog.resolve(
                ToolResolutionScope(),
                effective_policy_snapshot_id="policy-snapshot-1",
            )[0]
            schemas = ToolSchemaRegistry(
                (
                    JsonSchema(
                        schema_id,
                        JsonSchemaNode.object().required_property(
                            "cmd",
                            JsonSchemaNode.array(JsonSchemaNode.string()),
                        ),
                    ),
                )
            )
            draft = ToolCallDraft.proposed("response-1", "call-1", tool_name)
            draft = draft.append_argument_fragment(
                json.dumps(fixture.get("initialArguments", {}), sort_keys=True)
            )
            call = draft.complete_arguments().into_tool_call(
                resolved_tool.resolved_tool_id,
                created_at="2026-06-23T00:00:00Z",
            )
            request = ToolApprovalRequest.for_call(
                "approval-1",
                resolved_tool,
                call,
                principal_id="user-1",
                requested_at=1000,
                expires_at=2000,
            )
            approval = ToolApprovalRecord.approve(request, approver_id="admin-1", decided_at=1100)
            revised = call.revise_arguments(fixture.get("mutatedArguments", {}))
            initial_valid = approval.is_valid_for(resolved_tool, call, principal_id="user-1", now=1500)
            revised_valid = approval.is_valid_for(resolved_tool, revised, principal_id="user-1", now=1500)
            policy_decision = PolicyDecision(
                decision_id="decision-allow-tool",
                effect="allow",
                reason_codes=("allow-process",),
                policy_refs=("allow-process",),
                evaluated_at="2026-06-23T00:00:01Z",
                input_digest="sha256:before-tool",
            )
            try:
                admit_tool_call(
                    revised,
                    resolved_tool,
                    schemas,
                    policy_decision=policy_decision,
                    expected_policy_input_digest=policy_decision.input_digest,
                    approval=approval,
                    principal_id="user-1",
                    idempotency_key="idem-1",
                    admitted_at="2026-06-23T00:00:02Z",
                    now=1500,
                )
                admission_with_stale_approval = True
                error_message = None
            except Exception as error:
                admission_with_stale_approval = False
                error_message = str(error)
            observed = {
                "initialApprovalValid": initial_valid,
                "mutatedApprovalValid": revised_valid,
                "digestChanged": revised.arguments_digest != call.arguments_digest,
                "revisedRevision": revised.revision,
                "admissionWithStaleApproval": admission_with_stale_approval,
                "error": error_message,
            }
        else:
            diagnostics.append(
                {
                    "code": "ToolLifecycleKindUnknown",
                    "message": f"tool-lifecycle TCK kind {kind!r} is not supported",
                    "path": "$.kind",
                }
            )

        error_contains = expected.get("errorContains")
        for key, expected_value in expected.items():
            if key == "errorContains":
                if expected_value is not None and str(expected_value) not in str(observed.get("error")):
                    diagnostics.append(
                        {
                            "code": "ToolLifecycleErrorMismatch",
                            "message": "tool-lifecycle observed error did not contain expected text",
                            "path": "$.expected.errorContains",
                        }
                    )
                continue
            if observed.get(key) != expected_value:
                diagnostics.append(
                    {
                        "code": "ToolLifecycleExpectedMismatch",
                        "message": f"tool-lifecycle observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        if error_contains is None and observed.get("error") is not None:
            diagnostics.append(
                {
                    "code": "ToolLifecycleUnexpectedError",
                    "message": "tool-lifecycle case produced an unexpected error",
                    "path": "$.observed.error",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_tool_execution_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.tool_execution_fixture
        response_id = str(fixture.get("responseId", "response-1"))
        effect_key_template = fixture.get("effectKeyTemplate")
        raw_calls = fixture.get("calls", [])
        planned_calls: list[ToolPlanCall] = []
        for call_index, raw_call in enumerate(raw_calls if isinstance(raw_calls, list) else []):
            if not isinstance(raw_call, Mapping):
                diagnostics.append(
                    {
                        "code": "ToolExecutionCallInvalid",
                        "message": "tool-execution TCK call must be a mapping",
                        "path": f"$.calls[{call_index}]",
                    }
                )
                continue
            raw_arguments = raw_call.get("arguments", {})
            if not isinstance(raw_arguments, Mapping):
                diagnostics.append(
                    {
                        "code": "ToolExecutionArgumentsInvalid",
                        "message": "tool-execution TCK call arguments must be a mapping",
                        "path": f"$.calls[{call_index}].arguments",
                    }
                )
                continue
            draft = ToolCallDraft.proposed(
                response_id,
                str(raw_call.get("toolCallId", "")),
                str(raw_call.get("toolName", "tool.run")),
            )
            draft = draft.append_argument_fragment(json.dumps(raw_arguments, sort_keys=True))
            call = draft.complete_arguments().into_tool_call(
                str(raw_call.get("resolvedToolId", "resolved-tool-1")),
                created_at=str(raw_call.get("createdAt", "2026-06-23T00:00:00Z")),
            )
            depends_on = raw_call.get("dependsOn", raw_call.get("depends_on", ()))
            if isinstance(depends_on, str):
                depends_on = (depends_on,)
            call = replace(call, depends_on=tuple(str(dependency) for dependency in depends_on or ()))
            raw_effects = raw_call.get("effects", [])
            if isinstance(raw_effects, str):
                raw_effects = [raw_effects]
            planned_call = ToolPlanCall(
                call,
                effect_key=(str(raw_call["effectKey"]) if raw_call.get("effectKey") is not None else None),
                effects=frozenset(str(effect) for effect in raw_effects or ()),
                cancellation=str(raw_call.get("cancellation", "cooperative")),
            )
            if effect_key_template is not None and raw_call.get("effectKey") is None:
                planned_call = planned_call.with_effect_key_template(str(effect_key_template))
            planned_calls.append(planned_call)

        observed: dict[str, object] = {"operations": []}
        expected_creation_error = fixture.get("expectedCreationError")
        try:
            plan = ToolExecutionPlan(
                plan_id=str(fixture.get("planId", "plan-1")),
                response_id=response_id,
                calls=tuple(planned_calls),
                maximum_parallelism=int(fixture.get("maximumParallelism", 1)),
                failure_policy=str(fixture.get("failurePolicy", "return_failures_to_model")),
                cancellation_policy=str(fixture.get("cancellationPolicy", "cancel_dependents")),
            )
            observed["creationError"] = None
        except ToolExecutionPlanError as error:
            creation_error = _tool_execution_error_code(error)
            observed["creationError"] = creation_error
            if expected_creation_error != creation_error:
                diagnostics.append(
                    {
                        "code": "ToolExecutionCreationErrorMismatch",
                        "message": "tool-execution plan creation error did not match expected result",
                        "path": "$.expectedCreationError",
                    }
                )
            return TckResult(
                case_id=case.case_id,
                kind=case.kind,
                status="passed" if not diagnostics else "failed",
                diagnostics=tuple(diagnostics),
                observed=observed,
            )

        if expected_creation_error is not None:
            diagnostics.append(
                {
                    "code": "ToolExecutionCreationUnexpectedSuccess",
                    "message": "tool-execution TCK case expected creation failure but plan was created",
                    "path": "$.expectedCreationError",
                }
            )

        operations = fixture.get("operations", [])
        if not isinstance(operations, list):
            operations = []
            diagnostics.append(
                {
                    "code": "ToolExecutionOperationsInvalid",
                    "message": "tool-execution TCK operations must be a list",
                    "path": "$.operations",
                }
            )
        operation_observations: list[dict[str, object]] = []
        for operation_index, operation in enumerate(operations):
            if not isinstance(operation, Mapping):
                diagnostics.append(
                    {
                        "code": "ToolExecutionOperationInvalid",
                        "message": "tool-execution TCK operation must be a mapping",
                        "path": f"$.operations[{operation_index}]",
                    }
                )
                continue
            op = str(operation.get("op", ""))
            if op == "ready":
                ready = plan.ready_call_ids()
                operation_observations.append({"op": "ready", "ready": ready})
                expected_ready = operation.get("expect", [])
                if ready != [str(call_id) for call_id in expected_ready or ()]:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionReadyMismatch",
                            "message": "tool-execution ready call ids did not match expected result",
                            "path": f"$.operations[{operation_index}].expect",
                        }
                    )
            elif op == "start":
                tool_call_id = str(operation.get("toolCallId", operation.get("tool_call_id", "")))
                expected_error = operation.get("expectError")
                actual_error = None
                try:
                    plan.record_started(tool_call_id)
                except ToolExecutionPlanError as error:
                    actual_error = _tool_execution_error_code(error)
                operation_observations.append(
                    {"op": "start", "toolCallId": tool_call_id, "error": actual_error}
                )
                if expected_error is not None:
                    if actual_error != expected_error:
                        diagnostics.append(
                            {
                                "code": "ToolExecutionOperationErrorMismatch",
                                "message": "tool-execution operation error did not match expected result",
                                "path": f"$.operations[{operation_index}].expectError",
                            }
                        )
                elif actual_error is not None:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionOperationUnexpectedError",
                            "message": "tool-execution start operation failed unexpectedly",
                            "path": f"$.operations[{operation_index}]",
                        }
                    )
            elif op == "complete":
                tool_call_id = str(operation.get("toolCallId", operation.get("tool_call_id", "")))
                expected_error = operation.get("expectError")
                actual_error = None
                try:
                    plan.record_completed(tool_call_id)
                except ToolExecutionPlanError as error:
                    actual_error = _tool_execution_error_code(error)
                operation_observations.append(
                    {"op": "complete", "toolCallId": tool_call_id, "error": actual_error}
                )
                if expected_error is not None:
                    if actual_error != expected_error:
                        diagnostics.append(
                            {
                                "code": "ToolExecutionOperationErrorMismatch",
                                "message": "tool-execution operation error did not match expected result",
                                "path": f"$.operations[{operation_index}].expectError",
                            }
                        )
                elif actual_error is not None:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionOperationUnexpectedError",
                            "message": "tool-execution complete operation failed unexpectedly",
                            "path": f"$.operations[{operation_index}]",
                        }
                    )
            elif op == "policy_stop":
                pending_tool_calls = str(operation.get("pendingToolCalls", "deny"))
                affected = plan.apply_policy_stop(pending_tool_calls)
                operation_observations.append(
                    {"op": "policy_stop", "pendingToolCalls": pending_tool_calls, "affected": affected}
                )
                expected_affected = operation.get("expectAffected", [])
                if affected != [str(tool_call_id) for tool_call_id in expected_affected or ()]:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionPolicyStopAffectedMismatch",
                            "message": "tool-execution policy_stop affected calls did not match expected result",
                            "path": f"$.operations[{operation_index}].expectAffected",
                        }
                    )
            else:
                diagnostics.append(
                    {
                        "code": "ToolExecutionOperationUnknown",
                        "message": f"tool-execution TCK operation {op!r} is not supported",
                        "path": f"$.operations[{operation_index}].op",
                    }
                )

        expected_states = fixture.get("expectedStates", {})
        observed_states = {
            planned_call.call.tool_call_id: plan.state(planned_call.call.tool_call_id)
            for planned_call in planned_calls
        }
        if isinstance(expected_states, Mapping):
            for tool_call_id, expected_state in expected_states.items():
                if observed_states.get(str(tool_call_id)) != expected_state:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionStateMismatch",
                            "message": "tool-execution final state did not match expected result",
                            "path": f"$.expectedStates.{tool_call_id}",
                        }
                    )
        else:
            diagnostics.append(
                {
                    "code": "ToolExecutionExpectedStatesInvalid",
                    "message": "tool-execution expectedStates must be a mapping",
                    "path": "$.expectedStates",
                }
            )
        observed["operations"] = operation_observations
        observed["states"] = observed_states
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_usage_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.usage_fixture
        ledger = InMemoryUsageLedger()
        append_results: list[str] = []
        operations = fixture.get("operations", [])
        if not isinstance(operations, list):
            operations = []
            diagnostics.append(
                {
                    "code": "UsageOperationsInvalid",
                    "message": "usage TCK operations must be a list",
                    "path": "$.operations",
                }
            )

        for operation_index, operation in enumerate(operations):
            if not isinstance(operation, Mapping):
                diagnostics.append(
                    {
                        "code": "UsageOperationInvalid",
                        "message": "usage TCK operation must be a mapping",
                        "path": f"$.operations[{operation_index}]",
                    }
                )
                continue
            op = str(operation.get("op", ""))
            if op == "append":
                record_mapping = operation.get("record")
                if not isinstance(record_mapping, Mapping):
                    diagnostics.append(
                        {
                            "code": "UsageRecordMissing",
                            "message": "append operation requires a usage record",
                            "path": f"$.operations[{operation_index}].record",
                        }
                    )
                    continue
                raw_amounts = record_mapping.get("amounts", [])
                if not isinstance(raw_amounts, list):
                    diagnostics.append(
                        {
                            "code": "UsageAmountsInvalid",
                            "message": "usage record amounts must be a list",
                            "path": f"$.operations[{operation_index}].record.amounts",
                        }
                    )
                    continue
                amounts: list[UsageAmount] = []
                for amount_index, amount in enumerate(raw_amounts):
                    if not isinstance(amount, Mapping):
                        diagnostics.append(
                            {
                                "code": "UsageAmountInvalid",
                                "message": "usage amount must be a mapping",
                                "path": f"$.operations[{operation_index}].record.amounts[{amount_index}]",
                            }
                        )
                        continue
                    dimensions = amount.get("dimensions", {})
                    if not isinstance(dimensions, Mapping):
                        diagnostics.append(
                            {
                                "code": "UsageAmountInvalid",
                                "message": "usage amount dimensions must be a mapping",
                                "path": f"$.operations[{operation_index}].record.amounts[{amount_index}].dimensions",
                            }
                        )
                        continue
                    amounts.append(
                        UsageAmount(
                            kind=str(amount.get("kind", "")),
                            amount=Decimal(str(amount.get("amount", "0"))),
                            unit=str(amount.get("unit", "")),
                            dimensions={str(key): str(value) for key, value in dimensions.items()},
                        )
                    )
                metadata = record_mapping.get("metadata", {})
                if not isinstance(metadata, Mapping):
                    diagnostics.append(
                        {
                            "code": "UsageMetadataInvalid",
                            "message": "usage record metadata must be a mapping",
                            "path": f"$.operations[{operation_index}].record.metadata",
                        }
                    )
                    continue
                optional_fields: dict[str, object] = {}
                for field_name, keys in (
                    ("run_id", ("runId", "run_id")),
                    ("attempt_id", ("attemptId", "attempt_id")),
                    ("provider_response_id", ("providerResponseId", "provider_response_id")),
                    ("pricing_ref", ("pricingRef", "pricing_ref")),
                    ("quota_window_id", ("quotaWindowId", "quota_window_id")),
                    ("execution_scope", ("executionScope", "execution_scope")),
                    ("reconciliation_of", ("reconciliationOf", "reconciliation_of")),
                ):
                    value = _first_mapping_value(record_mapping, *keys)
                    if value is not None:
                        optional_fields[field_name] = str(value)
                record = UsageRecord(
                    record_id=str(_first_mapping_value(record_mapping, "recordId", "record_id")),
                    source=str(record_mapping.get("source", "")),
                    confidence=str(record_mapping.get("confidence", "")),
                    amounts=tuple(amounts),
                    occurred_at=str(_first_mapping_value(record_mapping, "occurredAt", "occurred_at")),
                    metadata={str(key): value for key, value in metadata.items()},
                    **optional_fields,
                )
                append_results.append(ledger.append(record).record_id)
            elif op == "reconcile":
                raw_amounts = operation.get("amounts", [])
                if not isinstance(raw_amounts, list):
                    diagnostics.append(
                        {
                            "code": "UsageAmountsInvalid",
                            "message": "usage reconcile amounts must be a list",
                            "path": f"$.operations[{operation_index}].amounts",
                        }
                    )
                    continue
                amounts = []
                for amount_index, amount in enumerate(raw_amounts):
                    if not isinstance(amount, Mapping):
                        diagnostics.append(
                            {
                                "code": "UsageAmountInvalid",
                                "message": "usage amount must be a mapping",
                                "path": f"$.operations[{operation_index}].amounts[{amount_index}]",
                            }
                        )
                        continue
                    dimensions = amount.get("dimensions", {})
                    if not isinstance(dimensions, Mapping):
                        diagnostics.append(
                            {
                                "code": "UsageAmountInvalid",
                                "message": "usage amount dimensions must be a mapping",
                                "path": f"$.operations[{operation_index}].amounts[{amount_index}].dimensions",
                            }
                        )
                        continue
                    amounts.append(
                        UsageAmount(
                            kind=str(amount.get("kind", "")),
                            amount=Decimal(str(amount.get("amount", "0"))),
                            unit=str(amount.get("unit", "")),
                            dimensions={str(key): str(value) for key, value in dimensions.items()},
                        )
                    )
                reconciled = ledger.reconcile(
                    str(_first_mapping_value(operation, "sourceRecordId", "source_record_id")),
                    amounts=amounts,
                    occurred_at=str(_first_mapping_value(operation, "occurredAt", "occurred_at")),
                    record_id=(
                        str(_first_mapping_value(operation, "recordId", "record_id"))
                        if _first_mapping_value(operation, "recordId", "record_id") is not None
                        else None
                    ),
                )
                append_results.append(reconciled.record_id)
            else:
                diagnostics.append(
                    {
                        "code": "UsageOperationUnknown",
                        "message": f"usage TCK operation {op!r} is not supported",
                        "path": f"$.operations[{operation_index}].op",
                    }
                )

        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "UsageExpectedInvalid",
                    "message": "usage TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )
        run_id = str(expected.get("runId", expected.get("run_id", "")))
        records = ledger.records_for_run(run_id) if run_id else []
        totals = ledger.totals_for_run(run_id) if run_id else []
        observed_totals = [
            {
                "kind": amount.kind,
                "amount": (
                    int(amount.amount)
                    if amount.amount == amount.amount.to_integral_value()
                    else str(amount.amount)
                ),
                "unit": amount.unit,
                "dimensions": dict(amount.dimensions),
            }
            for amount in totals
        ]
        observed = {
            "appendResults": append_results,
            "recordIds": [record.record_id for record in records],
            "totals": observed_totals,
        }
        for key, path in (
            ("appendResults", "$.expected.appendResults"),
            ("recordIds", "$.expected.recordIds"),
            ("totals", "$.expected.totals"),
        ):
            expected_value = expected.get(key)
            if expected_value is not None and observed[key] != expected_value:
                diagnostics.append(
                    {
                        "code": "UsageExpectedMismatch",
                        "message": f"usage observed {key} did not match expected value",
                        "path": path,
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_policy_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        delivery = case.policy_delivery
        mode = delivery.get("mode", "bounded_holdback")
        if mode == "buffer_until_commit":
            policy = OutputDeliveryPolicy.buffer_until_commit(on_violation="abort_response")
        elif mode == "bounded_holdback":
            policy = OutputDeliveryPolicy.bounded_holdback(on_violation="abort_response")
        elif mode == "immediate_draft":
            policy = OutputDeliveryPolicy.immediate_draft(
                on_violation="abort_response",
                delivered_draft_disposition="retract",
            )
        else:
            policy = OutputDeliveryPolicy.bounded_holdback(on_violation="abort_response")
            diagnostics.append(
                {
                    "code": "PolicyDeliveryModeUnknown",
                    "message": f"policy TCK delivery mode {mode!r} is not supported",
                    "path": "$.delivery.mode",
                }
            )
        for source, target in (
            ("holdbackMaxTokens", "holdback_max_tokens"),
            ("holdbackMaxBytes", "holdback_max_bytes"),
            ("holdbackMaxDurationMs", "holdback_max_duration_ms"),
        ):
            value = delivery.get(source)
            if value is not None:
                policy = replace(policy, **{target: value})
        gate = OutputDeliveryGate(case.policy_stream_id, case.policy_response_id, delivery_policy=policy)

        for operation_index, operation in enumerate(case.policy_operations):
            op = operation.get("op")
            expected_error = operation.get("expectError")
            actual_error = None
            try:
                if op == "chunk":
                    result = gate.record_chunk(
                        GenerationChunk.text(
                            case.policy_stream_id,
                            case.policy_response_id,
                            int(operation.get("sequence", -1)),
                            str(operation.get("text", "")),
                        )
                    )
                    actual_deliver = [(chunk.sequence, chunk.text) for chunk in result]
                elif op == "allow":
                    accepted_through = operation.get("acceptedThrough")
                    decision = OutputPolicyDecision.allow(
                        str(operation.get("decisionId", "")),
                        accepted_through_sequence=int(accepted_through) if accepted_through is not None else None,
                        input_digest=str(operation.get("inputDigest", "")),
                    )
                    update = gate.apply_decision(decision, occurred_at=str(operation.get("occurredAt", "")))
                    actual_deliver = [(chunk.sequence, chunk.text) for chunk in update.deliverable]
                elif op in {"redact", "replace"}:
                    accepted_through = operation.get("acceptedThrough")
                    accepted_sequence = int(accepted_through) if accepted_through is not None else None
                    if op == "redact":
                        redactions = []
                        for redaction in operation.get("redactions", []):
                            if isinstance(redaction, Mapping):
                                redactions.append(
                                    {
                                        "path": str(redaction.get("path", "")),
                                        "start": int(redaction.get("start", -1)),
                                        "end": int(redaction.get("end", -1)),
                                        "replacement": str(redaction.get("replacement", "")),
                                    }
                                )
                        decision = OutputPolicyDecision.redact(
                            str(operation.get("decisionId", "")),
                            accepted_through_sequence=accepted_sequence,
                            redactions=tuple(redactions),
                            input_digest=str(operation.get("inputDigest", "")),
                        )
                    else:
                        replacement_parts = []
                        for chunk in operation.get("replacementChunks", []):
                            if isinstance(chunk, Mapping):
                                replacement_parts.append(ContentPart(kind="text", text=str(chunk.get("text", ""))))
                        decision = OutputPolicyDecision.replace(
                            str(operation.get("decisionId", "")),
                            accepted_through_sequence=accepted_sequence,
                            replacement_parts=tuple(replacement_parts),
                            input_digest=str(operation.get("inputDigest", "")),
                        )
                    update = gate.apply_decision(decision, occurred_at=str(operation.get("occurredAt", "")))
                    actual_deliver = [(chunk.sequence, chunk.text) for chunk in update.deliverable]
                elif op in {"abort_response", "abort_turn", "deny_commit"}:
                    if op == "abort_turn":
                        decision = OutputPolicyDecision.abort_turn(
                            str(operation.get("decisionId", "")),
                            input_digest=str(operation.get("inputDigest", "")),
                        )
                    elif op == "deny_commit":
                        decision = OutputPolicyDecision.deny_commit(
                            str(operation.get("decisionId", "")),
                            input_digest=str(operation.get("inputDigest", "")),
                        )
                    else:
                        decision = OutputPolicyDecision.abort_response(
                            str(operation.get("decisionId", "")),
                            input_digest=str(operation.get("inputDigest", "")),
                        )
                    accepted_through = operation.get("acceptedThrough")
                    if accepted_through is not None:
                        decision = decision.with_accepted_through_sequence(int(accepted_through))
                    provider_cancellation = operation.get("providerCancellation")
                    if isinstance(provider_cancellation, str):
                        decision = decision.with_provider_cancellation(provider_cancellation)
                    draft_disposition = operation.get("draftDisposition")
                    if isinstance(draft_disposition, str):
                        decision = decision.with_draft_disposition(draft_disposition)
                    pending_tool_calls = operation.get("pendingToolCalls")
                    if isinstance(pending_tool_calls, str):
                        decision = decision.with_pending_tool_calls(pending_tool_calls)
                    update = gate.apply_decision(decision, occurred_at=str(operation.get("occurredAt", "")))
                    actual_deliver = [(chunk.sequence, chunk.text) for chunk in update.deliverable]
                    expected_cutoff = operation.get("cutoff")
                    if isinstance(expected_cutoff, Mapping):
                        if update.cutoff is None:
                            diagnostics.append(
                                {
                                    "code": "PolicyCutoffMissing",
                                    "message": "policy TCK operation expected an output cutoff",
                                    "path": f"$.operations[{operation_index}].cutoff",
                                }
                            )
                        else:
                            cutoff_fields = {
                                "lastGeneratedSequence": update.cutoff.last_generated_sequence,
                                "lastPolicyAcceptedSequence": update.cutoff.last_policy_accepted_sequence,
                                "lastClientDeliveredSequence": update.cutoff.last_client_delivered_sequence,
                                "draftDisposition": update.cutoff.draft_disposition,
                                "policyDecisionId": update.cutoff.policy_decision_id,
                            }
                            for field_name, actual_value in cutoff_fields.items():
                                if field_name in expected_cutoff and expected_cutoff[field_name] != actual_value:
                                    diagnostics.append(
                                        {
                                            "code": "PolicyCutoffMismatch",
                                            "message": "policy TCK cutoff field did not match expected value",
                                            "path": f"$.operations[{operation_index}].cutoff.{field_name}",
                                        }
                                    )
                elif op == "commit":
                    result = gate.commit_accepted_output()
                    actual_deliver = [(chunk.sequence, chunk.text) for chunk in result]
                else:
                    diagnostics.append(
                        {
                            "code": "PolicyOperationUnknown",
                            "message": f"policy TCK operation {op!r} is not supported",
                            "path": f"$.operations[{operation_index}].op",
                        }
                    )
                    continue
            except (OutputGateError, ValueError) as error:
                message = str(error)
                if message == "output gate is policy stopped":
                    actual_error = "policy_stopped"
                elif "exceeds" in message and "bytes" in message:
                    actual_error = "bounded_holdback_bytes"
                elif "exceeds" in message and "tokens" in message:
                    actual_error = "bounded_holdback_tokens"
                elif message.startswith("accepted sequence"):
                    actual_error = "accepted_sequence_beyond_generated"
                elif "must be next after" in message:
                    actual_error = "non_contiguous_sequence"
                elif "must be greater than" in message:
                    actual_error = "non_monotonic_sequence"
                else:
                    actual_error = type(error).__name__
                actual_deliver = []

            if expected_error is not None:
                if actual_error != expected_error:
                    diagnostics.append(
                        {
                            "code": "PolicyExpectedErrorMismatch",
                            "message": "policy TCK operation error did not match expected error",
                            "path": f"$.operations[{operation_index}].expectError",
                        }
                    )
                continue
            if actual_error is not None:
                diagnostics.append(
                    {
                        "code": "PolicyUnexpectedError",
                        "message": f"policy TCK operation failed with {actual_error}",
                        "path": f"$.operations[{operation_index}]",
                    }
                )
                continue

            expected_deliver = [
                (int(chunk.get("sequence", -1)), str(chunk.get("text", "")))
                for chunk in operation.get("deliver", [])
                if isinstance(chunk, Mapping)
            ]
            if actual_deliver != expected_deliver:
                diagnostics.append(
                    {
                        "code": "PolicyDeliverableMismatch",
                        "message": "policy TCK delivered chunks did not match expected chunks",
                        "path": f"$.operations[{operation_index}].deliver",
                    }
                )

        observed = {
            "lastGeneratedSequence": gate.last_generated_sequence,
            "lastPolicyAcceptedSequence": gate.last_policy_accepted_sequence,
            "lastClientDeliveredSequence": gate.last_client_delivered_sequence,
            "stopped": gate.cutoff is not None,
        }
        for field_name, expected_value in case.expected_gate_state.items():
            if observed.get(field_name) != expected_value:
                diagnostics.append(
                    {
                        "code": "PolicyGateStateMismatch",
                        "message": "policy TCK final gate state did not match expected value",
                        "path": f"$.expected.{field_name}",
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_sequence_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        observed: dict[str, object] = {}
        capacity = case.sequence_capacity or 0
        if capacity < 1:
            observed["creation_error"] = "invalid_capacity"
            if case.expected_sequence_creation_error != "invalid_capacity":
                diagnostics.append(
                    {
                        "code": "SequenceCreationErrorMismatch",
                        "message": "sequence creation error did not match expected result",
                        "path": "$.expected.creation_error",
                    }
                )
            return TckResult(
                case_id=case.case_id,
                kind=case.kind,
                status="passed" if not diagnostics else "failed",
                diagnostics=tuple(diagnostics),
                observed=observed,
            )
        if case.expected_sequence_creation_error is not None:
            diagnostics.append(
                {
                    "code": "SequenceCreationUnexpectedSuccess",
                    "message": "sequence TCK case expected a creation error but sequence was created",
                    "path": "$.expected.creation_error",
                }
            )

        state = "open"
        buffer: list[str] = []
        for operation_index, operation in enumerate(case.sequence_operations):
            op = operation.get("op")
            if op == "send":
                if "value" not in operation:
                    diagnostics.append(
                        {
                            "code": "SequenceOperationInvalid",
                            "message": "sequence send operation requires value",
                            "path": f"$.operations[{operation_index}].value",
                        }
                    )
                    actual = "invalid_operation"
                elif state != "open":
                    actual = f"closed_{state}"
                elif len(buffer) >= capacity:
                    actual = "full"
                else:
                    buffer.append(str(operation["value"]))
                    actual = "ok"
                expected = operation.get("expect")
                if expected is not None and actual != expected:
                    diagnostics.append(
                        {
                            "code": "SequenceSendResultMismatch",
                            "message": "sequence send result did not match expected result",
                            "path": f"$.operations[{operation_index}].expect",
                        }
                    )
            elif op == "recv":
                actual_value = buffer.pop(0) if buffer else None
                expected_value = operation.get("value")
                if actual_value != expected_value:
                    diagnostics.append(
                        {
                            "code": "SequenceReceiveValueMismatch",
                            "message": "sequence receive value did not match expected value",
                            "path": f"$.operations[{operation_index}].value",
                        }
                    )
            elif op == "complete":
                if state == "open":
                    state = "completed"
                    actual = "ok"
                else:
                    actual = f"already_terminal_{state}"
                expected = operation.get("expect")
                if expected is not None and actual != expected:
                    diagnostics.append(
                        {
                            "code": "SequenceCompleteResultMismatch",
                            "message": "sequence complete result did not match expected result",
                            "path": f"$.operations[{operation_index}].expect",
                        }
                    )
            else:
                diagnostics.append(
                    {
                        "code": "SequenceOperationUnknown",
                        "message": f"sequence TCK operation {op!r} is not supported",
                        "path": f"$.operations[{operation_index}].op",
                    }
                )
                continue
            expected_len = operation.get("len")
            if expected_len is not None and len(buffer) != expected_len:
                diagnostics.append(
                    {
                        "code": "SequenceLengthMismatch",
                        "message": "sequence buffer length did not match expected length",
                        "path": f"$.operations[{operation_index}].len",
                    }
                )

        observed["state"] = state
        observed["len"] = len(buffer)
        if case.expected_sequence_state is not None and state != case.expected_sequence_state:
            diagnostics.append(
                {
                    "code": "SequenceStateMismatch",
                    "message": "sequence final state did not match expected state",
                    "path": "$.expected.state",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_runtime_case(self, case: TckCase) -> TckResult:
        try:
            result = InProcessRuntime(self.registry).run(case.graph, case.inputs)
            observed = {
                "status": result.status,
                "outputs": result.outputs,
                "terminal_kind": result.journal.terminal_kind,
            }
        except Exception as error:  # pragma: no cover - exercised by conformance fixtures.
            observed = {"status": "error", "error": type(error).__name__, "message": str(error)}
        diagnostics: list[dict[str, str]] = []
        if observed.get("status") != case.expected_status:
            diagnostics.append(
                {
                    "code": "StatusMismatch",
                    "message": "runtime status did not match expected status",
                    "path": "$.expected_status",
                }
            )
        if case.expected_outputs is not None and observed.get("outputs") != case.expected_outputs:
            diagnostics.append(
                {
                    "code": "OutputMismatch",
                    "message": "runtime outputs did not match expected outputs",
                    "path": "$.expected_outputs",
                }
            )
        if (
            case.expected_terminal_kind is not None
            and observed.get("terminal_kind") != case.expected_terminal_kind
        ):
            diagnostics.append(
                {
                    "code": "TerminalKindMismatch",
                    "message": "runtime terminal kind did not match expected terminal kind",
                    "path": "$.expected_terminal_kind",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )


__all__ = [
    "AcceptanceApplication",
    "AcceptanceCoverageIssue",
    "AcceptanceCoverageResult",
    "AcceptanceManifest",
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
    "canonical_hash",
    "check_tck_suite_coverage",
    "compile_graph",
    "load_application_event_tck_cases",
    "load_budget_race_tck_cases",
    "load_compiler_tck_cases",
    "load_exhaustion_tck_cases",
    "load_policy_tck_cases",
    "load_retry_tck_cases",
    "load_runtime_tck_cases",
    "load_schema_tck_cases",
    "load_sequence_tck_cases",
    "load_tck_cases_for_suite",
    "load_tck_suite_manifests",
    "load_tool_execution_tck_cases",
    "load_tool_lifecycle_tck_cases",
    "load_usage_tck_cases",
    "main",
    "migrate_document",
    "stdlib_registry",
]

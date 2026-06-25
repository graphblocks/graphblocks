from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Literal

from graphblocks.canonical import canonical_hash
from graphblocks.compiler import compile_graph
from graphblocks.migration import GRAPH_API_VERSION, migrate_document
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


TckCaseKind = Literal["compiler", "runtime"]
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


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value or ())


@dataclass(frozen=True, slots=True)
class TckCase:
    case_id: str
    kind: TckCaseKind
    graph: dict[str, object]
    inputs: dict[str, object] = field(default_factory=dict)
    expected_hash: str | None = None
    expected_error_codes: tuple[str, ...] = field(default_factory=tuple)
    expected_warning_codes: tuple[str, ...] = field(default_factory=tuple)
    expected_outputs: dict[str, object] | None = None
    expected_ok: bool = True
    expected_status: str = "succeeded"
    block_catalog: tuple[dict[str, object], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("TCK case_id must not be empty")
        if self.kind not in {"compiler", "runtime"}:
            raise ValueError(f"invalid TCK case kind {self.kind}")
        object.__setattr__(self, "graph", dict(self.graph))
        object.__setattr__(self, "inputs", dict(self.inputs))
        object.__setattr__(self, "expected_error_codes", tuple(self.expected_error_codes))
        object.__setattr__(self, "expected_warning_codes", tuple(self.expected_warning_codes))
        object.__setattr__(self, "block_catalog", tuple(dict(block) for block in self.block_catalog))
        if self.expected_outputs is not None:
            object.__setattr__(self, "expected_outputs", dict(self.expected_outputs))

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
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="runtime",
            graph=graph,
            inputs=inputs,
            expected_outputs=expected_outputs,
            expected_status=expected_status,
        )


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
        case_id = raw_case.get("name")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"compiler TCK case {index} requires name")
        graph = raw_case.get("document")
        if not isinstance(graph, dict):
            raise ValueError(f"compiler TCK case {case_id} requires document")
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"compiler TCK case {case_id} requires expected result")
        expected_hash = expected.get("graph_hash")
        if not isinstance(expected_hash, str) or not expected_hash.strip():
            raise ValueError(f"compiler TCK case {case_id} requires expected graph_hash")
        raw_error_codes = expected.get("error_codes")
        if not isinstance(raw_error_codes, list) or not all(isinstance(code, str) for code in raw_error_codes):
            raise ValueError(f"compiler TCK case {case_id} requires string error_codes")
        raw_warning_codes = expected.get("warning_codes", [])
        if not isinstance(raw_warning_codes, list) or not all(isinstance(code, str) for code in raw_warning_codes):
            raise ValueError(f"compiler TCK case {case_id} requires string warning_codes")
        raw_block_catalog = raw_case.get("block_catalog", [])
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
            else:
                results.append(self._run_runtime_case(case))
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
    "canonical_hash",
    "compile_graph",
    "load_compiler_tck_cases",
    "migrate_document",
    "stdlib_registry",
]

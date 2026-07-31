"""TCK reports, performance evidence, migration, and chaos contracts."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import math
from types import MappingProxyType


from graphblocks.canonical import (
    canonical_hash,
)
from graphblocks.migration import GRAPH_API_VERSION, migrate_document

from .models import (
    FaultKind,
    MigrationDirection,
    PerformanceThresholdOperator,
    TckCaseKind,
    TckResultStatus,
    _TCK_CASE_KINDS,
    _TCK_RESULT_STATUSES,
    _freeze_tck_evidence,
    _materialize_tck_evidence,
)


@dataclass(frozen=True, slots=True)
class TckResult:
    case_id: str
    kind: TckCaseKind
    status: TckResultStatus
    diagnostics: tuple[dict[str, str], ...] = field(default_factory=tuple)
    observed: dict[str, object] = field(default_factory=dict)

    def __getattribute__(self, name: str) -> object:
        value = object.__getattribute__(self, name)
        if name == "observed" and isinstance(value, MappingProxyType):
            return _materialize_tck_evidence(value, mutable=False)
        if (
            name == "diagnostics"
            and isinstance(value, tuple)
            and all(isinstance(diagnostic, MappingProxyType) for diagnostic in value)
        ):
            return tuple(
                _materialize_tck_evidence(diagnostic, mutable=False)
                for diagnostic in value
            )
        return value

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise ValueError("TCK result case_id must not be empty")
        if not isinstance(self.kind, str) or self.kind not in _TCK_CASE_KINDS:
            raise ValueError(f"unsupported TCK result kind {self.kind!r}")
        if not isinstance(self.status, str) or self.status not in _TCK_RESULT_STATUSES:
            raise ValueError(f"unsupported TCK result status {self.status!r}")
        diagnostics: list[Mapping[str, object]] = []
        for index, diagnostic in enumerate(self.diagnostics):
            if not isinstance(diagnostic, Mapping) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in diagnostic.items()
            ):
                raise ValueError(
                    f"TCK result diagnostic {index} must map strings to strings"
                )
            diagnostics.append(_freeze_tck_evidence(diagnostic))
        if not isinstance(self.observed, Mapping):
            raise ValueError("TCK result observed evidence must be a mapping")
        object.__setattr__(self, "diagnostics", tuple(diagnostics))
        object.__setattr__(self, "observed", _freeze_tck_evidence(self.observed))

    def __reduce__(self) -> tuple[object, tuple[object, ...]]:
        return (
            type(self),
            (self.case_id, self.kind, self.status, self.diagnostics, self.observed),
        )

    def result_contract(self) -> dict[str, object]:
        diagnostics = object.__getattribute__(self, "diagnostics")
        observed = object.__getattribute__(self, "observed")
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "status": self.status,
            "diagnostics": [
                _materialize_tck_evidence(diagnostic, mutable=True)
                for diagnostic in diagnostics
            ],
            "observed": _materialize_tck_evidence(observed, mutable=True),
        }


@dataclass(frozen=True, slots=True)
class TckReport:
    profile: str
    results: tuple[TckResult, ...]
    suite: str
    implementation: str
    implementation_version: str
    fixture_digest: str

    def __post_init__(self) -> None:
        for name, value in (("profile", self.profile), ("suite", self.suite)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"TCK report {name} must not be empty")
        try:
            results = tuple(self.results)
        except TypeError as error:
            raise ValueError("TCK report results must be an iterable") from error
        if any(not isinstance(result, TckResult) for result in results):
            raise ValueError("TCK report results must contain only TckResult values")
        case_ids = tuple(result.case_id for result in results)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("TCK report result case ids must be unique")
        if any(result.kind != self.suite for result in results):
            raise ValueError("TCK report result kinds must match the report suite")
        object.__setattr__(self, "results", results)

    @property
    def evidence_valid(self) -> bool:
        if not all(
            isinstance(value, str)
            for value in (
                self.profile,
                self.suite,
                self.implementation,
                self.implementation_version,
                self.fixture_digest,
            )
        ):
            return False
        digest = self.fixture_digest.removeprefix("sha256:")
        return (
            bool(self.profile.strip())
            and bool(self.suite.strip())
            and bool(self.implementation.strip())
            and bool(self.implementation_version.strip())
            and self.fixture_digest.startswith("sha256:")
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
        )

    @property
    def ok(self) -> bool:
        if not self.results or not self.evidence_valid:
            return False
        if self.profile == "native" and self.suite == "runtime":
            native_evidence = self.native_evidence_contract()
            if native_evidence["fallback_case_count"] or native_evidence[
                "native_case_count"
            ] != len(self.results):
                return False
        return all(result.status == "passed" for result in self.results)

    def native_evidence_contract(self) -> dict[str, object]:
        native_case_count = 0
        fallback_reasons: dict[str, int] = {}
        run_store_paths: set[str] = set()
        journal_store_paths: set[str] = set()
        for result in self.results:
            observed = result.observed
            runtime = observed.get("runtime")
            if runtime == "native":
                native_case_count += 1
            reason = observed.get("native_fallback_reason")
            if isinstance(reason, str) and reason:
                fallback_reasons[reason] = fallback_reasons.get(reason, 0) + 1
            run_store_path = observed.get("run_store_path")
            if isinstance(run_store_path, str) and run_store_path:
                run_store_paths.add(run_store_path)
            journal_store_path = observed.get("journal_store_path")
            if isinstance(journal_store_path, str) and journal_store_path:
                journal_store_paths.add(journal_store_path)
        return {
            "native_case_count": native_case_count,
            "fallback_case_count": sum(fallback_reasons.values()),
            "fallback_reasons": dict(sorted(fallback_reasons.items())),
            "run_store_paths": sorted(run_store_paths),
            "journal_store_paths": sorted(journal_store_paths),
        }

    def report_contract(self) -> dict[str, object]:
        contract: dict[str, object] = {
            "profile": self.profile,
            "ok": self.ok,
            "evidence": {
                "fixture_digest": self.fixture_digest,
                "implementation": self.implementation,
                "implementation_version": self.implementation_version,
                "suite": self.suite,
            },
            "results": [result.result_contract() for result in self.results],
        }
        native_evidence = self.native_evidence_contract()
        if (
            native_evidence["native_case_count"]
            or native_evidence["fallback_case_count"]
            or native_evidence["run_store_paths"]
            or native_evidence["journal_store_paths"]
        ):
            contract["native_evidence"] = native_evidence
        return contract

    def content_digest(self) -> str:
        return canonical_hash(self.report_contract())


@dataclass(frozen=True, slots=True)
class TckSuiteManifest:
    suite_id: str
    path: str
    case_ids: tuple[str, ...]
    fixture_digest: str
    auxiliary_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.suite_id, str) or not self.suite_id.strip():
            raise ValueError("TCK suite_id must not be empty")
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("TCK suite path must not be empty")
        if not isinstance(self.fixture_digest, str):
            raise ValueError(
                "TCK suite fixture_digest must be a canonical sha256 digest"
            )
        fixture_digest = self.fixture_digest.removeprefix("sha256:")
        if (
            not self.fixture_digest.startswith("sha256:")
            or len(fixture_digest) != 64
            or any(character not in "0123456789abcdef" for character in fixture_digest)
        ):
            raise ValueError(
                "TCK suite fixture_digest must be a canonical sha256 digest"
            )
        case_ids = tuple(str(case_id) for case_id in self.case_ids)
        if not case_ids:
            raise ValueError("TCK suite must contain at least one case")
        if any(not case_id.strip() for case_id in case_ids):
            raise ValueError("TCK suite case ids must not be empty")
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("TCK suite case ids must be unique")
        auxiliary_paths = tuple(str(path) for path in self.auxiliary_paths)
        if any(not path.strip() for path in auxiliary_paths):
            raise ValueError("TCK suite auxiliary paths must not be empty")
        object.__setattr__(self, "case_ids", case_ids)
        object.__setattr__(self, "auxiliary_paths", tuple(sorted(auxiliary_paths)))

    @property
    def case_count(self) -> int:
        return len(self.case_ids)

    def manifest_contract(self) -> dict[str, object]:
        contract: dict[str, object] = {
            "suite_id": self.suite_id,
            "path": self.path,
            "case_count": self.case_count,
            "case_ids": list(self.case_ids),
            "fixture_digest": self.fixture_digest,
        }
        if self.auxiliary_paths:
            contract["auxiliary_paths"] = list(self.auxiliary_paths)
        return contract

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
            raise ValueError(
                f"invalid performance threshold operator {self.operator!r}"
            )
        threshold = float(self.threshold)
        if not math.isfinite(threshold):
            raise ValueError("performance threshold must be finite")
        object.__setattr__(self, "threshold", threshold)
        if self.unit is not None:
            object.__setattr__(self, "unit", self.unit.strip() or None)

    @classmethod
    def at_most(
        cls, metric_name: str, threshold: float, *, unit: str | None = None
    ) -> PerformanceThreshold:
        return cls(
            metric_name=metric_name, operator="at_most", threshold=threshold, unit=unit
        )

    @classmethod
    def at_least(
        cls, metric_name: str, threshold: float, *, unit: str | None = None
    ) -> PerformanceThreshold:
        return cls(
            metric_name=metric_name, operator="at_least", threshold=threshold, unit=unit
        )

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
                raise ValueError(
                    "performance benchmark measurement name must not be empty"
                )
            numeric_value = float(value)
            if not math.isfinite(numeric_value):
                raise ValueError(
                    "performance benchmark measurement values must be finite"
                )
            measurements[str(metric_name)] = numeric_value
        object.__setattr__(self, "measurements", dict(sorted(measurements.items())))
        object.__setattr__(
            self,
            "thresholds",
            tuple(
                sorted(
                    self.thresholds,
                    key=lambda item: (item.metric_name, item.operator, item.threshold),
                )
            ),
        )
        object.__setattr__(
            self,
            "metadata",
            {
                str(key): str(value)
                for key, value in sorted(dict(self.metadata).items())
            },
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
            "thresholds": [
                threshold.threshold_contract() for threshold in self.thresholds
            ],
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
            raise ValueError(
                f"invalid migration compatibility direction {self.direction!r}"
            )
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
            expected_migrated_from = (
                raw_version if isinstance(raw_version, str) else None
            )
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
        object.__setattr__(
            self,
            "diagnostics",
            tuple(dict(diagnostic) for diagnostic in self.diagnostics),
        )
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
        object.__setattr__(
            self, "results", tuple(sorted(self.results, key=lambda item: item.case_id))
        )

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

    def run_cases(
        self, cases: tuple[MigrationCompatibilityCase, ...]
    ) -> MigrationCompatibilityReport:
        return MigrationCompatibilityReport(
            profile=self.profile,
            results=tuple(self._run_case(case) for case in cases),
        )

    def _run_case(
        self, case: MigrationCompatibilityCase
    ) -> MigrationCompatibilityResult:
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
        annotations = (
            migrated.get("metadata", {}).get("annotations", {})
            if isinstance(migrated.get("metadata"), dict)
            else {}
        )
        migrated_from = (
            annotations.get("graphblocks.ai/migratedFrom")
            if isinstance(annotations, dict)
            else None
        )
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
        if (
            case.expected_migrated_from is not None
            and observed["migrated_from"] != case.expected_migrated_from
        ):
            diagnostics.append(
                {
                    "code": "MigrationSourceVersionMismatch",
                    "message": "migrated document source version annotation did not match expected version",
                    "path": "$.expected_migrated_from",
                }
            )
        if (
            case.expected_hash is not None
            and observed["graph_hash"] != case.expected_hash
        ):
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
        object.__setattr__(
            self,
            "diagnostics",
            tuple(dict(diagnostic) for diagnostic in self.diagnostics),
        )
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
        object.__setattr__(
            self, "results", tuple(sorted(self.results, key=lambda item: item.case_id))
        )

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

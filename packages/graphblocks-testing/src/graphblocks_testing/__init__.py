from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from graphblocks.canonical import canonical_hash
from graphblocks.compiler import compile_graph
from graphblocks.run_store import (
    InMemoryRunStore,
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


@dataclass(frozen=True, slots=True)
class TckCase:
    case_id: str
    kind: TckCaseKind
    graph: dict[str, object]
    inputs: dict[str, object] = field(default_factory=dict)
    expected_hash: str | None = None
    expected_outputs: dict[str, object] | None = None
    expected_ok: bool = True
    expected_status: str = "succeeded"

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("TCK case_id must not be empty")
        if self.kind not in {"compiler", "runtime"}:
            raise ValueError(f"invalid TCK case kind {self.kind}")
        object.__setattr__(self, "graph", dict(self.graph))
        object.__setattr__(self, "inputs", dict(self.inputs))
        if self.expected_outputs is not None:
            object.__setattr__(self, "expected_outputs", dict(self.expected_outputs))

    @classmethod
    def compiler(
        cls,
        *,
        case_id: str,
        graph: dict[str, object],
        expected_hash: str | None = None,
        expected_ok: bool = True,
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="compiler",
            graph=graph,
            expected_hash=expected_hash,
            expected_ok=expected_ok,
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
        plan = compile_graph(case.graph)
        observed = {"hash": plan.graph_hash, "ok": plan.ok}
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
    "ExecutionJournal",
    "InMemoryRunStore",
    "InProcessRuntime",
    "JournalRecord",
    "JournalStateError",
    "RunRecord",
    "RunTerminalStateError",
    "RunResult",
    "RuntimeRegistry",
    "SQLiteExecutionJournal",
    "SQLiteRunStore",
    "StateConflictError",
    "TckCase",
    "TckReport",
    "TckResult",
    "TckRunner",
    "compile_graph",
    "stdlib_registry",
]

"""Acceptance application contracts and immutable report models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


from graphblocks.canonical import (
    canonical_hash_reference as canonical_hash,
)


def _acceptance_scenario_path_beneath_root(root: Path, scenario_path: str) -> Path:
    resolved_root = root.resolve()
    reference = Path(scenario_path)
    if reference.is_absolute():
        raise ValueError(
            "acceptance application scenario_path must be repository-local"
        )
    resolved_path = (resolved_root / reference).resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError(
            "acceptance application scenario_path must remain beneath root"
        )
    return resolved_path


@dataclass(frozen=True, slots=True)
class AcceptanceApplication:
    application_id: str
    profiles: tuple[str, ...]
    scenario_path: str
    gates: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""
    allow_unknown_blocks: bool = False

    def __post_init__(self) -> None:
        if not self.application_id.strip():
            raise ValueError("acceptance application_id must not be empty")
        if not self.profiles:
            raise ValueError("acceptance application profiles must not be empty")
        if not self.scenario_path.strip():
            raise ValueError("acceptance application scenario_path must not be empty")
        scenario_reference = Path(self.scenario_path)
        if scenario_reference.is_absolute() or ".." in scenario_reference.parts:
            raise ValueError(
                "acceptance application scenario_path must be repository-local"
            )
        for profile in self.profiles:
            if not profile.strip():
                raise ValueError("acceptance application profile ids must not be empty")
        for gate in self.gates:
            if not gate.strip():
                raise ValueError(
                    "acceptance application gates must not be empty strings"
                )
        if not isinstance(self.allow_unknown_blocks, bool):
            raise TypeError(
                "acceptance application allow_unknown_blocks must be a boolean"
            )

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
        allow_unknown_blocks = mapping.get(
            "allowUnknownBlocks",
            mapping.get("allow_unknown_blocks", False),
        )
        if not isinstance(allow_unknown_blocks, bool):
            raise ValueError(
                "acceptance application allowUnknownBlocks must be a boolean"
            )
        return cls(
            application_id=application_id,
            profiles=profiles,
            scenario_path=scenario_path,
            gates=gates,
            description=str(description),
            allow_unknown_blocks=allow_unknown_blocks,
        )

    def application_contract(self) -> dict[str, object]:
        return {
            "application_id": self.application_id,
            "profiles": list(self.profiles),
            "scenario_path": self.scenario_path,
            "gates": list(self.gates),
            "description": self.description,
            "allow_unknown_blocks": self.allow_unknown_blocks,
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


def _validate_acceptance_digest(owner: str, value: object) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError(f"{owner} must be a canonical sha256 digest")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(f"{owner} must be a canonical sha256 digest")
    return value


@dataclass(frozen=True, slots=True)
class AcceptanceApplicationExpectation:
    application_id: str
    scenario_path: str
    application_digest: str
    scenario_digest: str | None
    gates: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.application_id.strip():
            raise ValueError("acceptance expectation application_id must not be empty")
        if not self.scenario_path.strip():
            raise ValueError("acceptance expectation scenario_path must not be empty")
        _validate_acceptance_digest(
            "acceptance expectation application_digest",
            self.application_digest,
        )
        if self.scenario_digest is not None:
            _validate_acceptance_digest(
                "acceptance expectation scenario_digest",
                self.scenario_digest,
            )
        object.__setattr__(self, "gates", tuple(str(gate) for gate in self.gates))

    def expectation_contract(self) -> dict[str, object]:
        return {
            "application_id": self.application_id,
            "scenario_path": self.scenario_path,
            "application_digest": self.application_digest,
            "scenario_digest": self.scenario_digest,
            "gates": list(self.gates),
        }


@dataclass(frozen=True, slots=True)
class AcceptanceCoverageResult:
    issues: tuple[AcceptanceCoverageIssue, ...] = field(default_factory=tuple)
    application_ids: tuple[str, ...] = field(default_factory=tuple)
    manifest_digest: str | None = None
    expectations: tuple[AcceptanceApplicationExpectation, ...] = field(
        default_factory=tuple
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))
        expectations = tuple(
            sorted(
                self.expectations, key=lambda expectation: expectation.application_id
            )
        )
        expectation_ids = tuple(
            expectation.application_id for expectation in expectations
        )
        if len(expectation_ids) != len(set(expectation_ids)):
            raise ValueError("acceptance coverage expectation ids must be unique")
        object.__setattr__(self, "expectations", expectations)
        application_ids = tuple(
            sorted(str(application_id) for application_id in self.application_ids)
        )
        if expectations and application_ids and application_ids != expectation_ids:
            raise ValueError(
                "acceptance coverage application ids must match expectations"
            )
        object.__setattr__(
            self,
            "application_ids",
            expectation_ids if expectations else application_ids,
        )
        if self.manifest_digest is not None:
            _validate_acceptance_digest(
                "acceptance coverage manifest_digest",
                self.manifest_digest,
            )

    @property
    def ok(self) -> bool:
        return not self.issues

    def issue_contracts(self) -> list[dict[str, str]]:
        return [issue.issue_contract() for issue in self.issues]

    def coverage_contract(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "application_ids": list(self.application_ids),
            "manifest_digest": self.manifest_digest,
            "expectations": [
                expectation.expectation_contract() for expectation in self.expectations
            ],
            "issues": self.issue_contracts(),
        }

    def expectation_by_id(
        self,
        application_id: str,
    ) -> AcceptanceApplicationExpectation:
        for expectation in self.expectations:
            if expectation.application_id == application_id:
                return expectation
        raise KeyError(application_id)


@dataclass(frozen=True, slots=True)
class AcceptanceGateDiagnostic:
    code: str
    message: str
    path: str

    def diagnostic_contract(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceGateResult:
    application_id: str
    gate: str
    status: Literal["passed", "failed"]
    command: tuple[str, ...] = field(default_factory=tuple)
    output_digest: str | None = None
    diagnostics: tuple[AcceptanceGateDiagnostic, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.application_id.strip():
            raise ValueError("acceptance gate application_id must not be empty")
        if not self.gate.strip():
            raise ValueError("acceptance gate must not be empty")
        if self.status not in {"passed", "failed"}:
            raise ValueError(f"invalid acceptance gate status {self.status!r}")
        _validate_acceptance_digest(
            "acceptance gate output_digest",
            self.output_digest,
        )
        object.__setattr__(
            self, "command", tuple(str(argument) for argument in self.command)
        )
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def diagnostic_contracts(self) -> list[dict[str, str]]:
        return [diagnostic.diagnostic_contract() for diagnostic in self.diagnostics]

    def result_contract(self) -> dict[str, object]:
        return {
            "application_id": self.application_id,
            "gate": self.gate,
            "status": self.status,
            "command": list(self.command),
            "output_digest": self.output_digest,
            "diagnostics": self.diagnostic_contracts(),
        }

    def content_digest(self) -> str:
        return canonical_hash(self.result_contract())


@dataclass(frozen=True, slots=True)
class AcceptanceApplicationReport:
    application_id: str
    scenario_path: str
    application_digest: str
    scenario_digest: str
    results: tuple[AcceptanceGateResult, ...]

    def __post_init__(self) -> None:
        if not self.application_id.strip():
            raise ValueError("acceptance application report id must not be empty")
        if not self.scenario_path.strip():
            raise ValueError(
                "acceptance application report scenario_path must not be empty"
            )
        _validate_acceptance_digest(
            "acceptance application report application_digest",
            self.application_digest,
        )
        _validate_acceptance_digest(
            "acceptance application report scenario_digest",
            self.scenario_digest,
        )
        results = tuple(self.results)
        if any(result.application_id != self.application_id for result in results):
            raise ValueError(
                "acceptance application report result application_id must match"
            )
        object.__setattr__(self, "results", results)

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(result.ok for result in self.results)

    def report_contract(self) -> dict[str, object]:
        return {
            "application_id": self.application_id,
            "scenario_path": self.scenario_path,
            "application_digest": self.application_digest,
            "scenario_digest": self.scenario_digest,
            "ok": self.ok,
            "results": [result.result_contract() for result in self.results],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.report_contract())


@dataclass(frozen=True, slots=True)
class AcceptanceRunReport:
    manifest_digest: str
    applications: tuple[AcceptanceApplicationReport, ...]

    def __post_init__(self) -> None:
        _validate_acceptance_digest(
            "acceptance run report manifest_digest",
            self.manifest_digest,
        )
        applications = tuple(
            sorted(self.applications, key=lambda report: report.application_id)
        )
        application_ids = tuple(report.application_id for report in applications)
        if len(application_ids) != len(set(application_ids)):
            raise ValueError("acceptance run report application ids must be unique")
        object.__setattr__(self, "applications", applications)

    @property
    def ok(self) -> bool:
        return bool(self.applications) and all(
            application.ok for application in self.applications
        )

    def application_ids(self) -> tuple[str, ...]:
        return tuple(application.application_id for application in self.applications)

    def by_id(self, application_id: str) -> AcceptanceApplicationReport:
        for application in self.applications:
            if application.application_id == application_id:
                return application
        raise KeyError(application_id)

    def report_contract(self) -> dict[str, object]:
        return {
            "manifest_digest": self.manifest_digest,
            "ok": self.ok,
            "applications": [
                application.report_contract() for application in self.applications
            ],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.report_contract())


def _acceptance_report_matches_expectation(
    report: AcceptanceApplicationReport,
    expectation: AcceptanceApplicationExpectation,
) -> bool:
    return (
        report.application_id == expectation.application_id
        and report.scenario_path == expectation.scenario_path
        and report.application_digest == expectation.application_digest
        and (
            expectation.scenario_digest is None
            or report.scenario_digest == expectation.scenario_digest
        )
        and tuple(result.gate for result in report.results) == expectation.gates
    )

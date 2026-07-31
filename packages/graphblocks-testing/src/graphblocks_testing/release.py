"""Release-candidate evidence aggregation and gates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


from graphblocks.canonical import (
    canonical_hash,
)

from .acceptance_models import (
    AcceptanceCoverageResult,
    AcceptanceRunReport,
    _acceptance_report_matches_expectation,
)
from .models import ReleaseCandidateGateStatus
from .reports import (
    FaultChaosReport,
    MigrationCompatibilityReport,
    PerformanceBenchmarkReport,
    TckReport,
)


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
        object.__setattr__(
            self,
            "diagnostics",
            tuple(dict(diagnostic) for diagnostic in self.diagnostics),
        )

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
            raise ValueError(
                "release candidate evidence digest must use sha256:<digest>"
            )
        object.__setattr__(
            self,
            "diagnostics",
            tuple(dict(diagnostic) for diagnostic in self.diagnostics),
        )

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
        object.__setattr__(
            self, "gates", tuple(sorted(self.gates, key=lambda gate: gate.gate))
        )

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
        acceptance_report: AcceptanceRunReport | None = None,
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
                evidence_digest=canonical_hash(
                    {"required": list(required_tck_suites), "reports": tck_digests}
                ),
                diagnostics=tuple(tck_diagnostics),
            )
        )

        acceptance_diagnostics: tuple[dict[str, str], ...] = ()
        if not acceptance_coverage.ok:
            acceptance_diagnostics = (
                {
                    "code": "ReleaseCandidateAcceptanceFailed",
                    "message": "acceptance application coverage did not pass",
                    "path": "$.acceptance_coverage",
                },
            )
        elif acceptance_report is None:
            acceptance_diagnostics = (
                {
                    "code": "ReleaseCandidateAcceptanceReportMissing",
                    "message": "acceptance applications have no execution report",
                    "path": "$.acceptance_report",
                },
            )
        elif (
            acceptance_coverage.manifest_digest is not None
            and acceptance_report.manifest_digest != acceptance_coverage.manifest_digest
        ):
            acceptance_diagnostics = (
                {
                    "code": "ReleaseCandidateAcceptanceReportStale",
                    "message": "acceptance execution report does not match the covered manifest",
                    "path": "$.acceptance_report.manifest_digest",
                },
            )
        elif set(acceptance_report.application_ids()) != set(
            acceptance_coverage.application_ids
        ):
            acceptance_diagnostics = (
                {
                    "code": "ReleaseCandidateAcceptanceReportFailed",
                    "message": "acceptance execution report does not cover every application",
                    "path": "$.acceptance_report.applications",
                },
            )
        elif any(
            not _acceptance_report_matches_expectation(
                acceptance_report.by_id(expectation.application_id),
                expectation,
            )
            for expectation in acceptance_coverage.expectations
        ):
            acceptance_diagnostics = (
                {
                    "code": "ReleaseCandidateAcceptanceReportStale",
                    "message": "acceptance application report does not match manifest evidence",
                    "path": "$.acceptance_report.applications",
                },
            )
        elif not acceptance_report.ok:
            acceptance_diagnostics = (
                {
                    "code": "ReleaseCandidateAcceptanceReportFailed",
                    "message": "acceptance application execution did not pass",
                    "path": "$.acceptance_report.applications",
                },
            )
        gates.append(
            ReleaseCandidateGateResult(
                gate="acceptance_applications",
                status="passed" if not acceptance_diagnostics else "failed",
                evidence_digest=canonical_hash(
                    {
                        "coverage": acceptance_coverage.coverage_contract(),
                        "report": (
                            acceptance_report.report_contract()
                            if acceptance_report is not None
                            else None
                        ),
                    }
                ),
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

"""Conformance profiles, claims, and TCK coverage contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


from graphblocks.canonical import (
    canonical_hash_reference as canonical_hash,
)

from .acceptance_models import (
    AcceptanceCoverageResult,
    AcceptanceRunReport,
    _acceptance_report_matches_expectation,
)
from .reports import TckReport, TckSuiteManifest


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
        object.__setattr__(
            self, "extends", tuple(str(profile_id) for profile_id in self.extends)
        )
        object.__setattr__(
            self, "requires", tuple(str(requirement) for requirement in self.requires)
        )
        object.__setattr__(
            self, "tck_suites", tuple(str(suite) for suite in self.tck_suites)
        )
        object.__setattr__(
            self,
            "acceptance_applications",
            tuple(
                str(application_id) for application_id in self.acceptance_applications
            ),
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
        object.__setattr__(
            self,
            "available_suites",
            tuple(str(suite) for suite in self.available_suites),
        )
        object.__setattr__(
            self, "missing_suites", tuple(str(suite) for suite in self.missing_suites)
        )
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
                raise ValueError(
                    f"duplicate conformance profile id {profile.profile_id!r}"
                )
            seen.add(profile.profile_id)

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> ConformanceProfileSet:
        if document.get("kind") != "ConformanceProfileSet":
            raise ValueError(
                "conformance profile document kind must be ConformanceProfileSet"
            )
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
            normalized_lists: dict[str, tuple[str, ...]] = {}
            for field_name, raw_value in (
                ("extends", raw_profile.get("extends")),
                ("requires", raw_profile.get("requires")),
                ("tck", raw_profile.get("tck")),
                ("acceptanceApplications", raw_profile.get("acceptanceApplications")),
            ):
                if raw_value is None:
                    normalized_lists[field_name] = ()
                    continue
                if not isinstance(raw_value, list):
                    raise ValueError(
                        f"conformance profile {index} {field_name} must be a list of strings"
                    )
                values: list[str] = []
                for item_index, item in enumerate(raw_value):
                    if not isinstance(item, str):
                        raise ValueError(
                            f"conformance profile {index} {field_name}[{item_index}] must be a string"
                        )
                    values.append(item)
                normalized_lists[field_name] = tuple(values)
            profiles.append(
                ConformanceProfile(
                    profile_id=profile_id,
                    status=str(status),
                    extends=normalized_lists["extends"],
                    requires=normalized_lists["requires"],
                    tck_suites=normalized_lists["tck"],
                    acceptance_applications=normalized_lists["acceptanceApplications"],
                )
            )
        return cls(tuple(profiles))

    def by_id(self, profile_id: str) -> ConformanceProfile:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        raise KeyError(profile_id)

    def claim_requirements(
        self, profile_ids: tuple[str, ...]
    ) -> ConformanceClaimRequirements:
        included: set[str] = set()
        visiting: list[str] = []

        def include(profile_id: str) -> None:
            if profile_id in included:
                return
            if profile_id in visiting:
                cycle_start = visiting.index(profile_id)
                cycle = (*visiting[cycle_start:], profile_id)
                raise ValueError(
                    "conformance profile inheritance cycle: " + " -> ".join(cycle)
                )
            profile = self.by_id(profile_id)
            visiting.append(profile_id)
            try:
                for parent_id in profile.extends:
                    include(parent_id)
            finally:
                visiting.pop()
            included.add(profile.profile_id)

        for profile_id in profile_ids:
            include(profile_id)

        ordered_profiles = tuple(
            profile.profile_id
            for profile in self.profiles
            if profile.profile_id in included
        )
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
        tck_manifests: Mapping[str, TckSuiteManifest],
        tck_implementations: Mapping[str, tuple[str, str]],
        acceptance_coverage: AcceptanceCoverageResult,
        acceptance_report: AcceptanceRunReport | None = None,
    ) -> ConformanceClaimValidation:
        claim = self.claim_requirements(profile_ids)
        issues: list[ConformanceClaimIssue] = []
        claimed_profile = profile_ids[-1] if profile_ids else ""
        for suite in claim.tck_suites:
            report = tck_reports.get(suite)
            manifest = tck_manifests.get(suite)
            implementation_expectation = tck_implementations.get(suite)
            if manifest is None:
                issues.append(
                    ConformanceClaimIssue(
                        code="ConformanceTckManifestMissing",
                        profile_id=claimed_profile,
                        suite=suite,
                        path=f"$.profiles.{claimed_profile}.tck.{suite}.manifest",
                        message=(
                            "claimed conformance profile requires an authoritative "
                            "TCK suite manifest"
                        ),
                    )
                )
            elif implementation_expectation is None:
                issues.append(
                    ConformanceClaimIssue(
                        code="ConformanceTckImplementationExpectationMissing",
                        profile_id=claimed_profile,
                        suite=suite,
                        path=f"$.profiles.{claimed_profile}.tck.{suite}.implementation",
                        message=(
                            "claimed conformance profile requires an authoritative "
                            "TCK implementation expectation"
                        ),
                    )
                )
            elif report is None:
                issues.append(
                    ConformanceClaimIssue(
                        code="ConformanceTckMissing",
                        profile_id=claimed_profile,
                        suite=suite,
                        path=f"$.profiles.{claimed_profile}.tck.{suite}",
                        message="claimed conformance profile requires a passing TCK suite with no report",
                    )
                )
            elif not report.evidence_valid or report.suite != suite:
                issues.append(
                    ConformanceClaimIssue(
                        code="ConformanceTckEvidenceInvalid",
                        profile_id=claimed_profile,
                        suite=suite,
                        path=f"$.profiles.{claimed_profile}.tck.{suite}.evidence",
                        message=(
                            "claimed conformance profile requires identified TCK evidence "
                            "for the current suite fixture"
                        ),
                    )
                )
            elif (
                report.implementation,
                report.implementation_version,
            ) != implementation_expectation:
                issues.append(
                    ConformanceClaimIssue(
                        code="ConformanceTckImplementationMismatch",
                        profile_id=claimed_profile,
                        suite=suite,
                        path=f"$.profiles.{claimed_profile}.tck.{suite}.evidence.implementation",
                        message=(
                            "TCK report implementation identity or version does not match "
                            "the authoritative expectation"
                        ),
                    )
                )
            elif report.fixture_digest != manifest.fixture_digest:
                issues.append(
                    ConformanceClaimIssue(
                        code="ConformanceTckEvidenceStale",
                        profile_id=claimed_profile,
                        suite=suite,
                        path=f"$.profiles.{claimed_profile}.tck.{suite}.evidence.fixture_digest",
                        message=(
                            "TCK report fixture digest does not match the authoritative "
                            "suite manifest"
                        ),
                    )
                )
            elif (
                tuple(result.case_id for result in report.results) != manifest.case_ids
                or len({result.case_id for result in report.results})
                != len(report.results)
                or any(result.kind != suite for result in report.results)
            ):
                issues.append(
                    ConformanceClaimIssue(
                        code="ConformanceTckCoverageInvalid",
                        profile_id=claimed_profile,
                        suite=suite,
                        path=f"$.profiles.{claimed_profile}.tck.{suite}.results",
                        message=(
                            "TCK report results must exactly cover the authoritative case ids "
                            "with matching suite kinds"
                        ),
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
        if claim.acceptance_applications:
            if acceptance_report is None:
                issues.append(
                    ConformanceClaimIssue(
                        code="ConformanceAcceptanceReportMissing",
                        profile_id=claimed_profile,
                        suite="acceptance",
                        path=f"$.profiles.{claimed_profile}.acceptance",
                        message="claimed conformance profile requires executed acceptance reports",
                    )
                )
            elif (
                acceptance_coverage.manifest_digest is not None
                and acceptance_report.manifest_digest
                != acceptance_coverage.manifest_digest
            ):
                issues.append(
                    ConformanceClaimIssue(
                        code="ConformanceAcceptanceReportStale",
                        profile_id=claimed_profile,
                        suite="acceptance",
                        path=f"$.profiles.{claimed_profile}.acceptance.manifest_digest",
                        message="acceptance report does not match the covered manifest",
                    )
                )
            else:
                for application_id in claim.acceptance_applications:
                    try:
                        application_report = acceptance_report.by_id(application_id)
                    except KeyError:
                        issues.append(
                            ConformanceClaimIssue(
                                code="ConformanceAcceptanceReportMissing",
                                profile_id=claimed_profile,
                                suite="acceptance",
                                path=(
                                    f"$.profiles.{claimed_profile}.acceptance."
                                    f"{application_id}"
                                ),
                                message="required acceptance application has no execution report",
                            )
                        )
                        continue
                    try:
                        expectation = acceptance_coverage.expectation_by_id(
                            application_id
                        )
                    except KeyError:
                        issues.append(
                            ConformanceClaimIssue(
                                code="ConformanceAcceptanceReportStale",
                                profile_id=claimed_profile,
                                suite="acceptance",
                                path=(
                                    f"$.profiles.{claimed_profile}.acceptance."
                                    f"{application_id}"
                                ),
                                message="acceptance coverage has no immutable application expectation",
                            )
                        )
                        continue
                    if not _acceptance_report_matches_expectation(
                        application_report,
                        expectation,
                    ):
                        issues.append(
                            ConformanceClaimIssue(
                                code="ConformanceAcceptanceReportStale",
                                profile_id=claimed_profile,
                                suite="acceptance",
                                path=(
                                    f"$.profiles.{claimed_profile}.acceptance."
                                    f"{application_id}"
                                ),
                                message="acceptance application report does not match manifest evidence",
                            )
                        )
                    elif not application_report.ok:
                        issues.append(
                            ConformanceClaimIssue(
                                code="ConformanceAcceptanceReportFailed",
                                profile_id=claimed_profile,
                                suite="acceptance",
                                path=(
                                    f"$.profiles.{claimed_profile}.acceptance."
                                    f"{application_id}"
                                ),
                                message="required acceptance application did not pass",
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
    missing_suites = tuple(
        suite for suite in claim.tck_suites if suite not in available
    )
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

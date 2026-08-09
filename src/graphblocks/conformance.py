"""Machine-readable conformance authority contracts.

This preview module validates the authority projection owned by the stable
release matrix. It does not promote the matrix or its implementation crates to
the candidate-stable package-root API.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ._canonical_reference import canonical_hash


_PROFILE_CATALOG_PATH = "src/graphblocks/data/conformance-profiles.yaml"
_TCK_CLAIM_FIELDS = {
    "authorityRole",
    "executor",
    "profile",
}
_TCK_EXECUTOR_FIELDS = {
    "allowedSuites",
    "comparison",
    "implementation",
    "language",
    "referenceImplementation",
}
_TCK_VALIDATION_FIELDS = {
    "claimedProfiles",
    "exactImplementationIdentityRequired",
    "exactSuiteCoverageRequired",
    "executors",
    "profileCatalog",
    "suiteClaims",
}


def _require_non_empty_string(value: object, *, path: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value


def _require_string_list(value: object, *, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list of strings")
    result = tuple(
        _require_non_empty_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(value)
    )
    if len(result) != len(set(result)):
        raise ValueError(f"{path} must not contain duplicates")
    return result


@dataclass(frozen=True, slots=True)
class _TckExecutor:
    executor_id: str
    implementation: str
    language: str
    comparison: str
    reference_implementation: str
    allowed_suites: tuple[str, ...]

    @classmethod
    def from_mapping(cls, executor_id: object, value: object) -> _TckExecutor:
        executor = _require_non_empty_string(executor_id, path="authority executor id")
        if not isinstance(value, Mapping) or set(value) != _TCK_EXECUTOR_FIELDS:
            raise ValueError(
                f"authority executor {executor!r} must contain the closed fields"
            )
        language = _require_non_empty_string(
            value["language"],
            path=f"authority executor {executor!r} language",
        )
        if language not in {"python", "rust"}:
            raise ValueError(
                f"authority executor {executor!r} language must be python or rust"
            )
        comparison = _require_non_empty_string(
            value["comparison"],
            path=f"authority executor {executor!r} comparison",
        )
        if comparison not in {"exact-native-reference", "reference-only"}:
            raise ValueError(
                f"authority executor {executor!r} comparison is unsupported"
            )
        allowed_suites = _require_string_list(
            value["allowedSuites"],
            path=f"authority executor {executor!r} allowedSuites",
        )
        if not allowed_suites:
            raise ValueError(
                f"authority executor {executor!r} must allow at least one suite"
            )
        return cls(
            executor_id=executor,
            implementation=_require_non_empty_string(
                value["implementation"],
                path=f"authority executor {executor!r} implementation",
            ),
            language=language,
            comparison=comparison,
            reference_implementation=_require_non_empty_string(
                value["referenceImplementation"],
                path=f"authority executor {executor!r} referenceImplementation",
            ),
            allowed_suites=allowed_suites,
        )


@dataclass(frozen=True, slots=True)
class TckAuthorityClaim:
    """One suite's exact executor and profile authority role."""

    suite_id: str
    executor_id: str
    implementation: str
    language: str
    profile_id: str
    authority_role: str
    comparison: str
    reference_implementation: str

    @classmethod
    def from_mapping(
        cls,
        suite_id: object,
        value: object,
        *,
        executors: Mapping[str, _TckExecutor],
    ) -> TckAuthorityClaim:
        suite = _require_non_empty_string(suite_id, path="authority suite id")
        if not isinstance(value, Mapping) or set(value) != _TCK_CLAIM_FIELDS:
            raise ValueError(
                f"authority suite claim {suite!r} must contain the closed fields"
            )
        executor_id = _require_non_empty_string(
            value["executor"],
            path=f"authority suite claim {suite!r} executor",
        )
        executor = executors.get(executor_id)
        if executor is None:
            raise ValueError(
                f"authority suite claim {suite!r} names an unknown executor"
            )
        if suite not in executor.allowed_suites:
            raise ValueError(
                f"authority suite claim {suite!r} is not allowed by its executor"
            )
        return cls(
            suite_id=suite,
            executor_id=executor.executor_id,
            implementation=executor.implementation,
            language=executor.language,
            profile_id=_require_non_empty_string(
                value["profile"],
                path=f"authority suite claim {suite!r} profile",
            ),
            authority_role=_require_non_empty_string(
                value["authorityRole"],
                path=f"authority suite claim {suite!r} authorityRole",
            ),
            comparison=executor.comparison,
            reference_implementation=executor.reference_implementation,
        )

    def execution_contract(self) -> dict[str, str]:
        return {
            "executor_id": self.executor_id,
            "implementation": self.implementation,
            "language": self.language,
            "comparison": self.comparison,
            "reference_implementation": self.reference_implementation,
        }

    def claim_contract(self) -> dict[str, str]:
        return {
            **self.execution_contract(),
            "profile_id": self.profile_id,
            "authority_role": self.authority_role,
        }


@dataclass(frozen=True, slots=True)
class ConformanceAuthorityMatrix:
    """Validated TCK projection of the stable release authority matrix."""

    matrix_digest: str
    target_release: str
    decision_status: str
    target_normative_authority: str
    implicit_reference_fallback: bool
    profile_catalog: str
    claimed_profiles: tuple[str, ...]
    suite_claims: tuple[TckAuthorityClaim, ...]
    profile_authorities: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]

    @classmethod
    def from_document(
        cls, document: Mapping[str, object]
    ) -> ConformanceAuthorityMatrix:
        matrix_version = document.get("matrixVersion")
        if type(matrix_version) is not int or matrix_version != 1:
            raise ValueError("authority matrixVersion must be the integer 1")
        target_release = _require_non_empty_string(
            document.get("targetRelease"),
            path="authority targetRelease",
        )
        transition = document.get("authorityTransition")
        if not isinstance(transition, Mapping):
            raise ValueError("authorityTransition must be a mapping")
        decision_status = _require_non_empty_string(
            transition.get("decisionStatus"),
            path="authorityTransition.decisionStatus",
        )
        if decision_status != "accepted":
            raise ValueError("authority transition decision must be accepted")
        target_authority = _require_non_empty_string(
            transition.get("targetNormativeAuthority"),
            path="authorityTransition.targetNormativeAuthority",
        )
        if target_authority != "rust":
            raise ValueError("target normative authority must be rust")
        implicit_fallback = transition.get("implicitReferenceFallback")
        if type(implicit_fallback) is not bool or implicit_fallback:
            raise ValueError("implicit reference fallback must be the boolean false")
        validation = transition.get("tckClaimValidation")
        if not isinstance(validation, Mapping) or set(validation) != (
            _TCK_VALIDATION_FIELDS
        ):
            raise ValueError(
                "authorityTransition.tckClaimValidation must contain the closed fields"
            )
        profile_catalog = _require_non_empty_string(
            validation["profileCatalog"],
            path="authorityTransition.tckClaimValidation.profileCatalog",
        )
        if profile_catalog != _PROFILE_CATALOG_PATH:
            raise ValueError("authority TCK claim names another profile catalog")
        claimed_profiles = _require_string_list(
            validation["claimedProfiles"],
            path="authorityTransition.tckClaimValidation.claimedProfiles",
        )
        if not claimed_profiles:
            raise ValueError("authority TCK claim must name at least one profile")
        for field_name in (
            "exactImplementationIdentityRequired",
            "exactSuiteCoverageRequired",
        ):
            if type(validation[field_name]) is not bool or not validation[field_name]:
                raise ValueError(f"authority TCK claim {field_name} must be true")

        raw_executors = validation["executors"]
        if not isinstance(raw_executors, Mapping) or not raw_executors:
            raise ValueError("authority TCK executors must be a non-empty mapping")
        executors = {
            executor.executor_id: executor
            for executor in (
                _TckExecutor.from_mapping(executor_id, raw_executor)
                for executor_id, raw_executor in raw_executors.items()
            )
        }
        if len(executors) != len(raw_executors):
            raise ValueError("authority TCK executor ids must be unique")
        executors_by_implementation: dict[str, list[_TckExecutor]] = {}
        for executor in executors.values():
            implementation_executors = executors_by_implementation.setdefault(
                executor.implementation,
                [],
            )
            if any(
                set(prior.allowed_suites).intersection(executor.allowed_suites)
                for prior in implementation_executors
            ):
                raise ValueError(
                    "authority TCK executors for one implementation must own "
                    "disjoint suites"
                )
            implementation_executors.append(executor)
        for executor in executors.values():
            reference_executors = [
                candidate
                for candidate in executors_by_implementation.get(
                    executor.reference_implementation,
                    (),
                )
                if candidate.language == "python"
                and candidate.comparison == "reference-only"
            ]
            if len(reference_executors) != 1:
                raise ValueError(
                    f"authority executor {executor.executor_id!r} must name a "
                    "Python reference-only implementation"
                )
            if executor.comparison == "reference-only":
                if (
                    executor.language != "python"
                    or executor.implementation != executor.reference_implementation
                ):
                    raise ValueError(
                        f"authority executor {executor.executor_id!r} reference-only "
                        "mode must execute its Python reference implementation"
                    )
            elif (
                executor.language != "rust"
                or executor.implementation == executor.reference_implementation
            ):
                raise ValueError(
                    f"authority executor {executor.executor_id!r} exact comparison "
                    "must bind distinct Rust and Python implementations"
                )

        raw_profiles = document.get("profiles")
        if not isinstance(raw_profiles, list):
            raise ValueError("authority matrix profiles must be a list")
        authorities: dict[str, tuple[tuple[str, str], ...]] = {}
        for index, raw_profile in enumerate(raw_profiles):
            if not isinstance(raw_profile, Mapping):
                raise ValueError(f"authority matrix profile {index} must be a mapping")
            profile_id = _require_non_empty_string(
                raw_profile.get("id"),
                path=f"authority matrix profile {index} id",
            )
            if profile_id in authorities:
                raise ValueError(f"duplicate authority matrix profile {profile_id!r}")
            raw_authority = raw_profile.get("authority")
            if not isinstance(raw_authority, Mapping) or not raw_authority:
                raise ValueError(
                    f"authority matrix profile {profile_id!r} must define authority"
                )
            normalized_authority: list[tuple[str, str]] = []
            for role, implementation in raw_authority.items():
                normalized_authority.append(
                    (
                        _require_non_empty_string(
                            role,
                            path=f"authority matrix profile {profile_id!r} role",
                        ),
                        _require_non_empty_string(
                            implementation,
                            path=(
                                f"authority matrix profile {profile_id!r} role {role!r}"
                            ),
                        ),
                    )
                )
            authorities[profile_id] = tuple(sorted(normalized_authority))
        missing_profiles = set(claimed_profiles) - set(authorities)
        if missing_profiles:
            raise ValueError(
                "authority TCK claim names missing profiles: "
                + ", ".join(sorted(missing_profiles))
            )

        raw_suite_claims = validation["suiteClaims"]
        if not isinstance(raw_suite_claims, Mapping) or not raw_suite_claims:
            raise ValueError("authority TCK suiteClaims must be a non-empty mapping")
        suite_claims = tuple(
            sorted(
                (
                    TckAuthorityClaim.from_mapping(
                        suite_id,
                        raw_claim,
                        executors=executors,
                    )
                    for suite_id, raw_claim in raw_suite_claims.items()
                ),
                key=lambda claim: claim.suite_id,
            )
        )
        for claim in suite_claims:
            if claim.profile_id not in claimed_profiles:
                raise ValueError(
                    f"authority suite claim {claim.suite_id!r} names an unclaimed profile"
                )
            authority = dict(authorities[claim.profile_id])
            if authority.get(claim.authority_role) != claim.language:
                raise ValueError(
                    f"authority suite claim {claim.suite_id!r} does not match "
                    "its profile language role"
                )
        return cls(
            matrix_digest=canonical_hash(document),
            target_release=target_release,
            decision_status=decision_status,
            target_normative_authority=target_authority,
            implicit_reference_fallback=implicit_fallback,
            profile_catalog=profile_catalog,
            claimed_profiles=claimed_profiles,
            suite_claims=suite_claims,
            profile_authorities=tuple(sorted(authorities.items())),
        )

    def validate_tck_claims(
        self,
        *,
        claimed_profiles: Sequence[str],
        declared_suites_by_profile: Mapping[str, Sequence[str]],
        observed_execution_claims: Mapping[str, object],
    ) -> dict[str, object]:
        """Bind exact executed-suite identities to their declared language roles."""

        if tuple(claimed_profiles) != self.claimed_profiles:
            raise ValueError(
                "TCK evidence does not claim the authority matrix profiles"
            )
        required_suites: set[str] = set()
        for profile_id in self.claimed_profiles:
            declared_suites = declared_suites_by_profile.get(profile_id)
            if declared_suites is None or isinstance(declared_suites, (str, bytes)):
                raise ValueError(
                    f"authority profile {profile_id!r} has no declared TCK suite list"
                )
            required_suites.update(declared_suites)
        claims_by_suite = {claim.suite_id: claim for claim in self.suite_claims}
        if set(claims_by_suite) != required_suites:
            raise ValueError(
                "authority suite claims do not exactly cover the claimed profiles"
            )
        if set(observed_execution_claims) != required_suites:
            raise ValueError(
                "observed TCK executors do not exactly cover the authority suites"
            )
        for suite_id in sorted(required_suites):
            claim = claims_by_suite[suite_id]
            declaring_suites = declared_suites_by_profile.get(claim.profile_id, ())
            if suite_id not in declaring_suites:
                raise ValueError(
                    f"authority suite claim {suite_id!r} names a profile that does "
                    "not declare the suite"
                )
            observed_execution_claim = observed_execution_claims[suite_id]
            if (
                not isinstance(observed_execution_claim, Mapping)
                or dict(observed_execution_claim) != claim.execution_contract()
            ):
                raise ValueError(
                    f"TCK suite {suite_id!r} executor does not match its authority claim"
                )
        return self.claim_contract()

    def claim_contract(self) -> dict[str, object]:
        return {
            "matrix_digest": self.matrix_digest,
            "target_release": self.target_release,
            "decision_status": self.decision_status,
            "target_normative_authority": self.target_normative_authority,
            "implicit_reference_fallback": self.implicit_reference_fallback,
            "profile_catalog": self.profile_catalog,
            "claimed_profiles": list(self.claimed_profiles),
            "suite_claims": {
                claim.suite_id: claim.claim_contract() for claim in self.suite_claims
            },
        }


__all__ = ["ConformanceAuthorityMatrix", "TckAuthorityClaim"]

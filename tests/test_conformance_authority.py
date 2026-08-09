from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from graphblocks.conformance import ConformanceAuthorityMatrix


ROOT = Path(__file__).parents[1]
MATRIX_DOCUMENT = yaml.safe_load(
    (ROOT / "docs" / "project" / "stable-release-matrix.yaml").read_text(
        encoding="utf-8"
    )
)
PROFILE_DOCUMENT = yaml.safe_load(
    (ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml").read_text(
        encoding="utf-8"
    )
)


def test_authority_matrix_binds_stable_profile_suite_language_claims() -> None:
    matrix = ConformanceAuthorityMatrix.from_document(MATRIX_DOCUMENT)
    declared_suites = {
        profile["id"]: tuple(profile.get("tck", ()))
        for profile in PROFILE_DOCUMENT["spec"]["profiles"]
    }
    observed_execution_claims = {
        claim.suite_id: claim.execution_contract() for claim in matrix.suite_claims
    }

    contract = matrix.validate_tck_claims(
        claimed_profiles=("GB-C0-SCHEMA", "GB-C1-LOCAL-RUNTIME"),
        declared_suites_by_profile=declared_suites,
        observed_execution_claims=observed_execution_claims,
    )

    assert contract["matrix_digest"].startswith("sha256:")
    assert contract["target_release"] == "1.0"
    assert contract["decision_status"] == "accepted"
    assert contract["target_normative_authority"] == "rust"
    assert contract["implicit_reference_fallback"] is False
    assert contract["claimed_profiles"] == [
        "GB-C0-SCHEMA",
        "GB-C1-LOCAL-RUNTIME",
    ]
    suite_claims = contract["suite_claims"]
    assert suite_claims["compiler"] == {
        "executor_id": "rust-compiler-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "profile_id": "GB-C0-SCHEMA",
        "authority_role": "activeCompiler",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert suite_claims["schema"] == {
        "executor_id": "python-reference",
        "implementation": "graphblocks-python",
        "language": "python",
        "profile_id": "GB-C0-SCHEMA",
        "authority_role": "referenceOracle",
        "comparison": "reference-only",
        "reference_implementation": "graphblocks-python",
    }
    assert suite_claims["runtime"] == {
        "executor_id": "rust-runtime-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "profile_id": "GB-C1-LOCAL-RUNTIME",
        "authority_role": "activeLocalRuntime",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert suite_claims["application-events"] == {
        "executor_id": "rust-application-events-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "profile_id": "GB-C1-LOCAL-RUNTIME",
        "authority_role": "activeLocalRuntime",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert suite_claims["retry"] == {
        "executor_id": "rust-retry-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "profile_id": "GB-C1-LOCAL-RUNTIME",
        "authority_role": "activeLocalRuntime",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert suite_claims["sequence"] == {
        "executor_id": "rust-sequence-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "profile_id": "GB-C1-LOCAL-RUNTIME",
        "authority_role": "activeLocalRuntime",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert {
        claim["language"]
        for suite, claim in suite_claims.items()
        if suite
        not in {"application-events", "compiler", "retry", "runtime", "sequence"}
    } == {"python"}


def test_authority_matrix_decoder_rejects_open_or_inconsistent_claims() -> None:
    invalid_documents = []

    implicit_fallback = deepcopy(MATRIX_DOCUMENT)
    implicit_fallback["authorityTransition"]["implicitReferenceFallback"] = True
    invalid_documents.append(implicit_fallback)

    open_claim = deepcopy(MATRIX_DOCUMENT)
    open_claim["authorityTransition"]["tckClaimValidation"]["suiteClaims"]["compiler"][
        "unexpected"
    ] = "allow"
    invalid_documents.append(open_claim)

    wrong_language = deepcopy(MATRIX_DOCUMENT)
    wrong_language["authorityTransition"]["tckClaimValidation"]["executors"][
        "rust-compiler-exact-differential"
    ]["language"] = "python"
    invalid_documents.append(wrong_language)

    wrong_role = deepcopy(MATRIX_DOCUMENT)
    wrong_role["authorityTransition"]["tckClaimValidation"]["suiteClaims"]["compiler"][
        "authorityRole"
    ] = "referenceOracle"
    invalid_documents.append(wrong_role)

    relabeled_runtime = deepcopy(MATRIX_DOCUMENT)
    relabeled_runtime["authorityTransition"]["tckClaimValidation"]["suiteClaims"][
        "runtime"
    ] = {
        "executor": "rust-compiler-exact-differential",
        "profile": "GB-C1-LOCAL-RUNTIME",
        "authorityRole": "activeCompiler",
    }
    invalid_documents.append(relabeled_runtime)

    duplicate_profile = deepcopy(MATRIX_DOCUMENT)
    duplicate_profile["authorityTransition"]["tckClaimValidation"][
        "claimedProfiles"
    ].append("GB-C0-SCHEMA")
    invalid_documents.append(duplicate_profile)

    missing_profile = deepcopy(MATRIX_DOCUMENT)
    missing_profile["authorityTransition"]["tckClaimValidation"]["suiteClaims"][
        "compiler"
    ]["profile"] = "GB-MISSING"
    invalid_documents.append(missing_profile)

    for document in invalid_documents:
        with pytest.raises(ValueError):
            ConformanceAuthorityMatrix.from_document(document)


def test_authority_matrix_rejects_suite_or_executor_drift() -> None:
    matrix = ConformanceAuthorityMatrix.from_document(MATRIX_DOCUMENT)
    declared_suites = {
        profile["id"]: tuple(profile.get("tck", ()))
        for profile in PROFILE_DOCUMENT["spec"]["profiles"]
    }
    observed_execution_claims = {
        claim.suite_id: claim.execution_contract() for claim in matrix.suite_claims
    }

    with pytest.raises(ValueError, match="exactly cover"):
        matrix.validate_tck_claims(
            claimed_profiles=matrix.claimed_profiles,
            declared_suites_by_profile={
                **declared_suites,
                "GB-C1-LOCAL-RUNTIME": (
                    *declared_suites["GB-C1-LOCAL-RUNTIME"],
                    "new-suite",
                ),
            },
            observed_execution_claims=observed_execution_claims,
        )

    wrong_execution_claim = dict(observed_execution_claims)
    wrong_execution_claim["compiler"] = {
        **wrong_execution_claim["compiler"],
        "implementation": "graphblocks-python",
    }
    with pytest.raises(ValueError, match="executor"):
        matrix.validate_tck_claims(
            claimed_profiles=matrix.claimed_profiles,
            declared_suites_by_profile=declared_suites,
            observed_execution_claims=wrong_execution_claim,
        )

    coordinated_relabel = deepcopy(MATRIX_DOCUMENT)
    coordinated_relabel["authorityTransition"]["tckClaimValidation"]["executors"][
        "rust-compiler-exact-differential"
    ]["allowedSuites"].append("runtime")
    coordinated_relabel["authorityTransition"]["tckClaimValidation"]["suiteClaims"][
        "runtime"
    ] = {
        "executor": "rust-compiler-exact-differential",
        "profile": "GB-C1-LOCAL-RUNTIME",
        "authorityRole": "activeCompiler",
    }
    with pytest.raises(ValueError, match="disjoint suites"):
        ConformanceAuthorityMatrix.from_document(coordinated_relabel)

    with pytest.raises(ValueError, match="profiles"):
        matrix.validate_tck_claims(
            claimed_profiles=("GB-C0-SCHEMA",),
            declared_suites_by_profile=declared_suites,
            observed_execution_claims=observed_execution_claims,
        )

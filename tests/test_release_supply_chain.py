from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType
import xml.etree.ElementTree as ElementTree

import pytest
import yaml

from graphblocks.canonical import canonical_hash
from tools import stable_security_gates


COMMIT = "1" * 40
TREE = "2" * 40
CANDIDATE_COMMIT = "3" * 40
RELEASE_REF = "refs/tags/v1.0.0-rc.1"
RELEASE_VERSION = "1.0.0rc1"
BUILDER_ID = "https://github.com/graphblocks/graphblocks/.github/workflows/ci.yml"
INVOCATION_ID = "https://github.com/graphblocks/graphblocks/actions/runs/1"
RUSTC_OUTPUT = "rustc 1.94.0 (012345678 2026-01-01)"
COSIGN_OUTPUT = "GitVersion: v3.0.6\nGitCommit: 0123456789abcdef"
RUSTC_IDENTITY = {"version": "1.94.0", "output": RUSTC_OUTPUT}
COSIGN_IDENTITY = {"version": "3.0.6", "output": COSIGN_OUTPUT}
PROMOTION_INTEGRATED_TIME = 1781568000
PROMOTION_INTEGRATED_AT = datetime.fromtimestamp(
    PROMOTION_INTEGRATED_TIME, timezone.utc
)
PROMOTION_SOURCE_DIFF = {
    "digest": "sha256:" + "5" * 64,
    "changes": [
        {"path": "pyproject.toml", "status": "M"},
        {"path": "src/graphblocks/_version.py", "status": "M"},
    ],
}


def _load_module() -> ModuleType:
    module_path = Path(__file__).parents[1] / "tools" / "release_supply_chain.py"
    spec = importlib.util.spec_from_file_location("release_supply_chain", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _with_content_digest(payload: dict[str, object]) -> dict[str, object]:
    payload = dict(payload)
    payload["contentDigest"] = canonical_hash(payload)
    return payload


def _security_gate_junit(module: ModuleType) -> bytes:
    selectors = stable_security_gates.manifest_selectors(
        module.STABLE_SECURITY_GATE_MANIFEST
    )
    suites = ElementTree.Element("testsuites")
    suite = ElementTree.SubElement(
        suites,
        "testsuite",
        {
            "tests": str(len(selectors)),
            "failures": "0",
            "errors": "0",
            "skipped": "0",
        },
    )
    for selector in selectors:
        path, name = selector.split("::", 1)
        ElementTree.SubElement(
            suite,
            "testcase",
            {
                "classname": path.removesuffix(".py").replace("/", "."),
                "name": name,
            },
        )
    return ElementTree.tostring(suites, encoding="utf-8", xml_declaration=True)


def _security_gate_result(
    module: ModuleType,
    *,
    run_attempt: int = 1,
) -> tuple[dict[str, object], bytes]:
    junit_bytes = _security_gate_junit(module)
    source_blobs = {
        path: (Path(__file__).parents[1] / path).read_bytes()
        for path in module.SECURITY_GATE_EVIDENCE_PATHS
    }
    return (
        stable_security_gates.build_result(
            manifest=module.STABLE_SECURITY_GATE_MANIFEST,
            manifest_bytes=module.STABLE_SECURITY_GATE_MANIFEST_BYTES,
            candidate_commit=CANDIDATE_COMMIT,
            source_blobs=source_blobs,
            junit_bytes=junit_bytes,
            artifact_name=(
                f"{stable_security_gates.ARTIFACT_NAME_PREFIX}-{run_attempt}"
            ),
        ),
        junit_bytes,
    )


def _write_security_gate_evidence(
    module: ModuleType,
    directory: Path,
    *,
    run_attempt: int = 1,
) -> tuple[Path, Path, dict[str, object]]:
    result, junit_bytes = _security_gate_result(
        module,
        run_attempt=run_attempt,
    )
    directory.mkdir(parents=True)
    result_path = directory / stable_security_gates.RESULT_FILE
    junit_path = directory / stable_security_gates.JUNIT_FILE
    result_path.write_bytes(module._canonical_json_bytes(result))
    junit_path.write_bytes(junit_bytes)
    return result_path, junit_path, result


def _trust_test_source(
    module: ModuleType,
    *,
    stable_version: str = RELEASE_VERSION,
) -> None:
    module._resolve_git_commit = lambda _ref: COMMIT
    module._current_git_commit = lambda: COMMIT
    module._current_git_tree = lambda: TREE
    module._assert_clean_source_checkout = lambda: None
    module._observe_cosign_identity = lambda _executable="cosign": dict(COSIGN_IDENTITY)
    module._verify_promotion_report_signature = (
        lambda **_arguments: PROMOTION_INTEGRATED_AT
    )
    module._promotion_source_diff = lambda **_arguments: {
        "digest": PROMOTION_SOURCE_DIFF["digest"],
        "changes": [dict(change) for change in PROMOTION_SOURCE_DIFF["changes"]],
    }
    original_promotion_git_blob = module._promotion_git_blob
    integration_matrix_path = "docs/project/stable-release-matrix.yaml"
    audit_reproduction_manifest = yaml.safe_load(
        (Path(__file__).parents[1] / module.AUDIT_REPRODUCTION_MANIFEST_PATH).read_text(
            encoding="utf-8"
        )
    )
    audit_reproduction_paths = {
        record["path"]
        for field in ("capturedFiles", "reconstructedFiles")
        for record in audit_reproduction_manifest[field]
    }
    audit_reproduction_paths.update(
        selector.split("::", 1)[0]
        for finding in audit_reproduction_manifest["findings"]
        for selector in finding["currentSelectors"]
    )
    source_blobs = {
        path: (Path(__file__).parents[1] / path).read_bytes()
        for path in (
            integration_matrix_path,
            module.SECURITY_GATE_MANIFEST_PATH,
            *module.SECURITY_GATE_EVIDENCE_PATHS,
            *module.AUDIT_CLOSURE_SOURCE_PATHS,
            *sorted(audit_reproduction_paths),
        )
    }
    module._promotion_git_blob = (
        lambda commit, path: (
            source_blobs[path]
            if path in source_blobs
            else original_promotion_git_blob(commit, path)
        )
    )
    module._promotion_commit_is_ancestor = lambda _commit, _revision: True
    module._promotion_regular_blob_exists = lambda _revision, _path: True
    module._first_party_versions = lambda: {
        "graphblocks": stable_version,
        "graphblocks-runtime": "0.1.0",
        "graphblocks-testing": stable_version,
    }


def _release_evidence(
    module: ModuleType,
    expectations: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    expectations = expectations or module.release_evidence_expectations(module.ROOT)
    tck_expectations = expectations["TCK"]
    reports: dict[str, object] = {}
    for suite, expectation in tck_expectations["suites"].items():
        evidence = {
            "fixture_digest": expectation["fixture_digest"],
            "implementation": expectation["implementation"],
            "implementation_version": expectation["implementation_version"],
            "suite": suite,
            "case_ids_digest": expectation["case_ids_digest"],
            "suite_manifest_digest": expectation["suite_manifest_digest"],
        }
        if "authority_claim" in expectation:
            evidence["authority_claim"] = expectation["authority_claim"]
        if "execution_claim" in expectation:
            evidence["execution_claim"] = expectation["execution_claim"]
        if "reference_implementation_version" in expectation:
            evidence["reference_implementation_version"] = expectation[
                "reference_implementation_version"
            ]
        reports[suite] = {
            "ok": True,
            "evidence": evidence,
            "results": [
                {"case_id": case_id, "status": "passed"}
                for case_id in expectation["case_ids"]
            ],
        }
    tck = _with_content_digest(
        {
            "profile": "local",
            "ok": True,
            "suite_manifest_digest": tck_expectations["manifest_digest"],
            "claimed_profiles": list(tck_expectations["claimed_profiles"]),
            "authority_claim": tck_expectations["authority_claim"],
            "profile_catalog_digest": tck_expectations["profile_catalog_digest"],
            "schema_manifest_digest": tck_expectations["schema_manifest_digest"],
            "reports": reports,
        }
    )

    acceptance_expectations = expectations["acceptance"]
    applications = []
    for application_id, expectation in acceptance_expectations["applications"].items():
        applications.append(
            {
                "application_id": application_id,
                "scenario_path": expectation["scenario_path"],
                "application_digest": expectation["application_digest"],
                "scenario_digest": expectation["scenario_digest"],
                "ok": True,
                "results": [
                    {
                        "application_id": application_id,
                        "gate": gate,
                        "status": "passed",
                        "output_digest": "sha256:" + "a" * 64,
                    }
                    for gate in expectation["gates"]
                ],
            }
        )
    acceptance = _with_content_digest(
        {
            "ok": True,
            "manifest_digest": acceptance_expectations["manifest_digest"],
            "applications": applications,
        }
    )
    return tck, acceptance


def _artifact_component(
    module: ModuleType,
    *,
    filename: str,
    digest: str,
) -> dict[str, object]:
    distribution, version = module._artifact_identity(filename)
    return {
        "type": "file",
        "name": filename,
        "bom-ref": f"urn:sha256:{digest}",
        "hashes": [{"alg": "SHA-256", "content": digest}],
        "properties": [
            {"name": "graphblocks:release-artifact", "value": "true"},
            {"name": "graphblocks:distribution", "value": distribution},
            {"name": "graphblocks:version", "value": version},
            {"name": "graphblocks:artifact-type", "value": module._artifact_type(filename)},
        ],
    }


def _runtime_wheel(os_name: str, _python_version: str) -> str:
    platform_tag = (
        "win_amd64"
        if os_name == "windows-latest"
        else "manylinux_2_17_x86_64.manylinux2014_x86_64"
    )
    return f"graphblocks_runtime-0.1.0-cp311-abi3-{platform_tag}.whl"


def _write_platform_input(
    module: ModuleType,
    root: Path,
    *,
    os_name: str,
    python_version: str,
    stable_version: str = RELEASE_VERSION,
) -> Path:
    platform_root = root / f"input-{os_name}-py{python_version.replace('.', '')}"
    wheelhouse = platform_root / "platform-wheelhouse"
    sdist_root = platform_root / "platform-sdists"
    evidence_root = platform_root / "platform-evidence"
    wheelhouse.mkdir(parents=True)
    sdist_root.mkdir()
    evidence_root.mkdir()
    filenames = (
        f"graphblocks-{stable_version}-py3-none-any.whl",
        f"graphblocks_testing-{stable_version}-py3-none-any.whl",
        _runtime_wheel(os_name, python_version),
        f"graphblocks-{stable_version}.tar.gz",
        f"graphblocks_testing-{stable_version}.tar.gz",
        "graphblocks_runtime-0.1.0.tar.gz",
    )
    records: list[dict[str, object]] = []
    for filename in sorted(filenames):
        content = (
            f"sdist:{filename}".encode()
            if filename.endswith(".tar.gz")
            else b"graphblocks-universal"
            if filename.startswith(f"graphblocks-{stable_version}")
            else b"testing-universal"
            if filename.startswith("graphblocks_testing")
            else f"runtime:{os_name}".encode()
        )
        path = (sdist_root if filename.endswith(".tar.gz") else wheelhouse) / filename
        path.write_bytes(content)
        distribution, version = module._artifact_identity(filename)
        digest = module._sha256_bytes(content)
        records.append(
            {
                "filename": filename,
                "sha256": digest,
                "size": len(content),
                "distribution": distribution,
                "version": version,
                "artifactType": module._artifact_type(filename),
            }
        )

    expectations = module.release_evidence_expectations(module.ROOT)
    tck, acceptance = _release_evidence(module, expectations)
    native_compiler_artifact = next(
        record
        for record in records
        if record["distribution"] == "graphblocks-runtime"
        and record["artifactType"] == "wheel"
    )
    tck["reports"]["compiler"]["evidence"]["implementation_artifact"] = dict(
        native_compiler_artifact
    )
    tck.pop("contentDigest")
    tck["contentDigest"] = canonical_hash(tck)
    (evidence_root / "tck.json").write_text(
        json.dumps(tck, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (evidence_root / "acceptance.json").write_text(
        json.dumps(acceptance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    first_party_components = [
        {
            "type": "library",
            "name": distribution,
            "version": (
                "0.1.0" if distribution == "graphblocks-runtime" else stable_version
            ),
            "bom-ref": f"pkg:pypi/{distribution}@"
            + ("0.1.0" if distribution == "graphblocks-runtime" else stable_version),
        }
        for distribution in ("graphblocks", "graphblocks-runtime", "graphblocks-testing")
    ]
    dependency_components = [
        {
            "type": "library",
            "name": distribution,
            "version": version,
            "bom-ref": f"pkg:pypi/{distribution}@{version}",
        }
        for distribution, version in (
            ("jsonschema", "4.25.1"),
            ("packaging", "25.0"),
            ("PyYAML", "6.0.2"),
            ("referencing", "0.37.0"),
        )
    ]
    graphblocks_ref = f"pkg:pypi/graphblocks@{stable_version}"
    testing_ref = f"pkg:pypi/graphblocks-testing@{stable_version}"
    runtime_ref = "pkg:pypi/graphblocks-runtime@0.1.0"
    dependency_refs = [str(component["bom-ref"]) for component in dependency_components]
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "components": first_party_components
        + dependency_components
        + [
            _artifact_component(
                module,
                filename=str(record["filename"]),
                digest=str(record["sha256"]),
            )
            for record in records
        ],
        "dependencies": [
            {"ref": graphblocks_ref, "dependsOn": sorted(dependency_refs)},
            {
                "ref": testing_ref,
                "dependsOn": sorted(
                    [
                        graphblocks_ref,
                        "pkg:pypi/packaging@25.0",
                    ]
                ),
            },
            {"ref": runtime_ref, "dependsOn": []},
            *[
                {"ref": reference, "dependsOn": []}
                for reference in sorted(dependency_refs)
            ],
        ],
    }
    (evidence_root / "sbom.cdx.json").write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    platform = _with_content_digest(
        {
            "formatVersion": 1,
            "platform": {"os": os_name, "python": python_version},
            "artifacts": records,
            "buildTools": {
                **module.PINNED_BUILD_TOOLS,
                "rustc": module.PINNED_RUSTC_VERSION,
            },
            "buildEnvironment": {
                "python": {
                    "implementation": "CPython",
                    "version": f"{python_version}.10",
                },
                "platform": f"{os_name}-test-platform",
                "runnerImage": {
                    "name": os_name,
                    "version": "test-image-1",
                },
                "resolvedDistributions": [
                    {"name": name, "version": version}
                    for name, version in sorted(
                        {
                            **module.PINNED_BUILD_TOOLS,
                            "cyclonedx-bom": module.CYCLONEDX_BOM_VERSION,
                        }.items()
                    )
                ],
            },
            "installedDistributions": [
                {"name": name, "version": version}
                for name, version in sorted(
                    {
                        "graphblocks": stable_version,
                        "graphblocks-runtime": "0.1.0",
                        "graphblocks-testing": stable_version,
                        "jsonschema": "4.25.1",
                        "packaging": "25.0",
                        "pyyaml": "6.0.2",
                        "referencing": "0.37.0",
                    }.items()
                )
            ],
            "observedToolIdentities": {"rustc": dict(RUSTC_IDENTITY)},
            "sourceDateEpoch": "315532800",
            "evidence": {
                "tck": tck["contentDigest"],
                "acceptance": acceptance["contentDigest"],
            },
            "contracts": {
                "claimedProfiles": list(
                    expectations["TCK"]["claimed_profiles"]
                ),
                "conformanceProfileCatalogDigest": expectations["TCK"][
                    "profile_catalog_digest"
                ],
                "authorityMatrixDigest": expectations["TCK"]["authority_claim"][
                    "matrix_digest"
                ],
                "schemaManifestDigest": expectations["TCK"]["schema_manifest_digest"],
            },
        }
    )
    (evidence_root / "platform.json").write_text(
        json.dumps(platform, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return platform_root


def _inputs(
    module: ModuleType,
    tmp_path: Path,
    *,
    stable_version: str = RELEASE_VERSION,
) -> Path:
    _trust_test_source(module, stable_version=stable_version)
    inputs = tmp_path / "platform-inputs"
    inputs.mkdir(parents=True)
    for os_name, python_version in module.SUPPORTED_PLATFORM_MATRIX:
        _write_platform_input(
            module,
            inputs,
            os_name=os_name,
            python_version=python_version,
            stable_version=stable_version,
        )
    return inputs


def _audit_closure_report(module: ModuleType) -> dict[str, object]:
    return {
        "candidateRef": RELEASE_REF,
        "candidateCommit": CANDIDATE_COMMIT,
        **module._audit_closure_claim_from_blobs(
            {
                path: (Path(__file__).parents[1] / path).read_bytes()
                for path in module.AUDIT_CLOSURE_SOURCE_PATHS
            },
            is_ancestor=lambda _commit: True,
            regression_exists=lambda _path: True,
            read_file=lambda path: (Path(__file__).parents[1] / path).read_bytes(),
            regular_file_exists=lambda path: (
                Path(__file__).parents[1] / path
            ).is_file(),
        ),
    }


def _promotion_payload_and_files(
    module: ModuleType,
    *,
    integration_reports: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], dict[str, bytes]]:
    integration_reports = integration_reports or []
    promotion_workflow_identity = (
        f"https://github.com/{module.SIGSTORE_REPOSITORY}/"
        f"{module.PROMOTION_SIGSTORE_WORKFLOW}@{RELEASE_REF}"
    )
    ci_workflow_identity = (
        f"https://github.com/{module.SIGSTORE_REPOSITORY}/"
        f"{module.SIGSTORE_WORKFLOW}@{RELEASE_REF}"
    )
    reports: dict[str, dict[str, object]] = {
        "candidate-manifest": {
            "formatVersion": 1,
            "releaseRef": RELEASE_REF,
            "releaseVersion": RELEASE_VERSION,
            "gitCommit": CANDIDATE_COMMIT,
        },
        "audit-closure": _audit_closure_report(module),
    }
    report_files: dict[str, bytes] = {}
    candidate_manifest_bytes = module._canonical_json_bytes(
        reports["candidate-manifest"]
    )
    candidate_manifest_digest = "sha256:" + module._sha256_bytes(
        candidate_manifest_bytes
    )
    matrix_runs = [
        {
            "runId": (
                "https://github.com/graphblocks/graphblocks/actions/runs/"
                f"{1000 + index}/attempts/1"
            ),
            "status": "success",
            "complete": True,
            "candidateRef": RELEASE_REF,
            "candidateCommit": CANDIDATE_COMMIT,
            "candidateManifestDigest": candidate_manifest_digest,
            "supportedMatrix": [
                {"os": os_name, "python": python_version}
                for os_name, python_version in module.SUPPORTED_PLATFORM_MATRIX
            ],
            "securityGates": _security_gate_result(module)[0],
        }
        for index in range(1, 4)
    ]
    for index, run in enumerate(matrix_runs, start=1):
        reports[f"matrix-run-{index}"] = dict(run)
    for index, report in enumerate(integration_reports, start=1):
        reports[f"integration-run-{index}"] = dict(report)
    applications = [
        {
            "applicationId": "application-one",
            "nontrivial": True,
            "startedAt": "2026-06-01T00:00:00Z",
            "endedAt": "2026-06-15T00:00:00Z",
        },
        {
            "applicationId": "application-two",
            "nontrivial": True,
            "startedAt": "2026-06-01T00:00:00Z",
            "endedAt": "2026-06-15T00:00:00Z",
        },
    ]
    for application in applications:
        reports[str(application["applicationId"])] = dict(application)
    reviewed_matrix_run_digests = [
        "sha256:" + module._sha256_bytes(module._canonical_json_bytes(run))
        for run in matrix_runs
    ]
    for review_name, reviewer in (
        ("api", "reviewer-api@example.test"),
        ("security", "reviewer-security@example.test"),
    ):
        report: dict[str, object] = {
            "reviewerIdentity": reviewer,
            "approved": True,
            "candidateRef": RELEASE_REF,
            "candidateCommit": CANDIDATE_COMMIT,
        }
        if review_name == "security":
            report.update(
                {
                    "objectAuthorizationScope": list(
                        module.OBJECT_AUTHORIZATION_REVIEW_SCOPE
                    ),
                    "reviewedMatrixRunDigests": reviewed_matrix_run_digests,
                }
            )
        reports[f"{review_name}-review"] = report
    reports["protected-final-ref"] = {
        "releaseRef": "refs/tags/v1.0.0",
        "protected": True,
    }
    rehearsal_report = {
        "environment": "staging",
        "authorized": True,
        "realExternalActions": True,
        "authorizedBy": "release-operator@example.test",
        "operations": [
            {"operation": operation, "status": "success"}
            for operation in ("publish", "rollback", "yank", "restore")
        ],
    }
    reports["staged-rehearsal"] = rehearsal_report
    reports["stable-scope"] = {
        "unresolvedCritical": 0,
        "unresolvedHigh": 0,
        "unexplainedFlakes": 0,
    }

    report_digests: dict[str, str] = {}
    report_artifacts: list[dict[str, str]] = []
    for report_id, report in sorted(reports.items()):
        report_path = f"promotion-reports/{report_id}.json"
        signature_path = f"promotion-reports/{report_id}.sigstore.json"
        report_bytes = module._canonical_json_bytes(report)
        report_sha256 = module._sha256_bytes(report_bytes)
        signature_bytes = module._canonical_json_bytes(
            {
                "verificationMaterial": {
                    "tlogEntries": [
                        {"integratedTime": str(PROMOTION_INTEGRATED_TIME)}
                    ]
                },
                "signedReportSha256": report_sha256,
                "testFixture": True,
            }
        )
        report_files[report_path] = report_bytes
        report_files[signature_path] = signature_bytes
        report_digests[report_id] = f"sha256:{report_sha256}"
        certificate_identity = (
            ci_workflow_identity
            if set(report) == module.MATRIX_PROMOTION_REPORT_KEYS
            else (
                f"https://github.com/{module.SIGSTORE_REPOSITORY}/"
                f"{report['workflow']}@{RELEASE_REF}"
                if set(report) == module.INTEGRATION_PROMOTION_REPORT_KEYS
                else promotion_workflow_identity
            )
        )
        report_artifacts.append(
            {
                "path": report_path,
                "sha256": report_sha256,
                "signaturePath": signature_path,
                "signatureSha256": module._sha256_bytes(signature_bytes),
                "certificateIdentity": certificate_identity,
                "certificateOidcIssuer": module.SIGSTORE_ISSUER,
            }
        )

    payload: dict[str, object] = {
        "formatVersion": 1,
        "release": {
            "releaseRef": "refs/tags/v1.0.0",
            "releaseVersion": "1.0.0",
        },
        "upgradeGate": {
            "status": "not-applicable",
            "reason": "first-stable-release",
        },
        "candidate": {
            "releaseRef": RELEASE_REF,
            "gitCommit": CANDIDATE_COMMIT,
            "manifestDigest": report_digests["candidate-manifest"],
            "sourceDiff": {
                "digest": PROMOTION_SOURCE_DIFF["digest"],
                "changes": [
                    dict(change) for change in PROMOTION_SOURCE_DIFF["changes"]
                ],
            },
        },
        "auditClosure": {
            **reports["audit-closure"],
            "reportDigest": report_digests["audit-closure"],
        },
        "supportedMatrixRuns": [
            {
                **run,
                "attestationDigest": report_digests[f"matrix-run-{index}"],
            }
            for index, run in enumerate(matrix_runs, start=1)
        ],
        "integrationRuns": [
            {
                **report,
                "reportDigest": report_digests[f"integration-run-{index}"],
            }
            for index, report in enumerate(integration_reports, start=1)
        ],
        "soak": {
            "startedAt": "2026-06-01T00:00:00Z",
            "endedAt": "2026-06-15T00:00:00Z",
            "applications": [
                {
                    "applicationId": application["applicationId"],
                    "nontrivial": True,
                    "reportDigest": report_digests[str(application["applicationId"])],
                }
                for application in applications
            ],
        },
        "reviews": {
            "api": {
                "reviewerIdentity": "reviewer-api@example.test",
                "approved": True,
                "reportDigest": report_digests["api-review"],
            },
            "security": {
                "reviewerIdentity": "reviewer-security@example.test",
                "approved": True,
                "reportDigest": report_digests["security-review"],
                "objectAuthorizationScope": list(
                    module.OBJECT_AUTHORIZATION_REVIEW_SCOPE
                ),
                "reviewedMatrixRunDigests": reviewed_matrix_run_digests,
            },
        },
        "stableScope": {
            "unresolvedCritical": 0,
            "unresolvedHigh": 0,
            "unexplainedFlakes": 0,
            "reportDigest": report_digests["stable-scope"],
        },
        "protectedFinalRef": {
            "releaseRef": "refs/tags/v1.0.0",
            "protected": True,
            "reportDigest": report_digests["protected-final-ref"],
        },
        "stagedRehearsal": {
            "environment": "staging",
            "authorized": True,
            "realExternalActions": True,
            "authorizedBy": "release-operator@example.test",
            "reportDigest": report_digests["staged-rehearsal"],
            "operations": rehearsal_report["operations"],
        },
        "reportArtifacts": report_artifacts,
    }
    payload["contentDigest"] = canonical_hash(payload)
    return payload, report_files


def _promotion_payload(module: ModuleType) -> dict[str, object]:
    return _promotion_payload_and_files(module)[0]


def _write_promotion_payload(
    module: ModuleType,
    path: Path,
    payload: dict[str, object],
) -> Path:
    integration_reports = [
        {key: value for key, value in run.items() if key != "reportDigest"}
        for run in payload.get("integrationRuns", [])
    ]
    _baseline, report_files = _promotion_payload_and_files(
        module,
        integration_reports=integration_reports,
    )
    for relative_path, data in report_files.items():
        target = path.parent / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    path.write_bytes(module._canonical_json_bytes(payload))
    return path


def _write_promotion_evidence(module: ModuleType, path: Path) -> Path:
    return _write_promotion_payload(module, path, _promotion_payload(module))


def _assemble(module: ModuleType, tmp_path: Path) -> Path:
    inputs = _inputs(module, tmp_path)
    bundle = tmp_path / "bundle"
    module.assemble_release_bundle(
        platform_inputs_dir=inputs,
        output_dir=bundle,
        git_commit=COMMIT,
        release_ref=RELEASE_REF,
        builder_id=BUILDER_ID,
        invocation_id=INVOCATION_ID,
    )
    return bundle


@pytest.mark.parametrize(
    ("release_ref", "release_version"),
    [
        ("refs/tags/v1.0.0", "1.0.0"),
        ("refs/tags/v1.0.0-rc.1", "1.0.0rc1"),
        ("refs/tags/v1.0.0-rc.10", "1.0.0rc10"),
    ],
)
def test_release_ref_derives_exact_pep440_version(
    release_ref: str,
    release_version: str,
) -> None:
    module = _load_module()
    assert module._release_version_from_ref(release_ref) == release_version


def test_promotion_source_diff_allows_only_release_metadata(
    tmp_path: Path,
) -> None:
    module = _load_module()
    repository = tmp_path / "repository"
    (repository / "src" / "graphblocks").mkdir(parents=True)
    (repository / "docs" / "project").mkdir(parents=True)
    (repository / "compatibility").mkdir()
    (repository / "pyproject.toml").write_text(
        '[project]\nversion = "1.0.0rc1"\n'
        'classifiers = ["Development Status :: 4 - Beta"]\n',
        encoding="utf-8",
    )
    (repository / "src" / "graphblocks" / "_version.py").write_text(
        '__version__ = "1.0.0rc1"\n', encoding="utf-8"
    )
    (repository / "docs" / "project" / "status.md").write_text(
        "Candidate\n", encoding="utf-8"
    )
    cli_report = {
        "ok": True,
        "implementation_version": "1.0.0rc1",
    }
    cli_report["contentDigest"] = canonical_hash(cli_report)
    (repository / "compatibility" / "stable-testing-cli-contracts.json").write_text(
        json.dumps({"stdoutJson": cli_report}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "GraphBlocks test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "candidate"], cwd=repository, check=True
    )
    candidate_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "tag", "v1.0.0-rc.1", candidate_commit],
        cwd=repository,
        check=True,
    )

    (repository / "pyproject.toml").write_text(
        '[project]\nversion = "1.0.0"\n'
        'classifiers = ["Development Status :: 5 - Production/Stable"]\n',
        encoding="utf-8",
    )
    (repository / "src" / "graphblocks" / "_version.py").write_text(
        '__version__ = "1.0.0"\n', encoding="utf-8"
    )
    (repository / "docs" / "project" / "status.md").write_text(
        "Stable\n", encoding="utf-8"
    )
    (repository / "docs" / "project" / "releases").mkdir()
    (repository / "docs" / "project" / "releases" / "v1.0.0.json").write_text(
        "{}\n", encoding="utf-8"
    )
    candidate_snapshot = (
        repository / "compatibility" / "stable-testing-cli-contracts.json"
    ).read_bytes()
    (repository / "compatibility" / "stable-testing-cli-contracts.json").write_bytes(
        module._promoted_testing_cli_snapshot(
            candidate_snapshot,
            candidate_version="1.0.0rc1",
            final_version="1.0.0",
        )
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "final"], cwd=repository, check=True)
    final_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    final_tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    module.ROOT = repository

    observed = module._promotion_source_diff(
        candidate_commit=candidate_commit,
        final_commit=final_commit,
        final_tree=final_tree,
        candidate_ref=RELEASE_REF,
    )
    assert observed["changes"] == [
        {
            "path": "compatibility/stable-testing-cli-contracts.json",
            "status": "M",
        },
        {"path": "docs/project/releases/v1.0.0.json", "status": "A"},
        {"path": "docs/project/status.md", "status": "M"},
        {"path": "pyproject.toml", "status": "M"},
        {"path": "src/graphblocks/_version.py", "status": "M"},
    ]

    (repository / "src" / "graphblocks" / "runtime.py").write_text(
        "changed = True\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "implementation change"],
        cwd=repository,
        check=True,
    )
    changed_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    changed_tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with pytest.raises(module.ReleaseBundleError, match="non-release source"):
        module._promotion_source_diff(
            candidate_commit=candidate_commit,
            final_commit=changed_commit,
            final_tree=changed_tree,
            candidate_ref=RELEASE_REF,
        )


@pytest.mark.parametrize(
    "authority_path",
    (
        "docs/project/stable-release-matrix.yaml",
        "docs/project/stable-security-gates.yaml",
        "docs/project/audit-issues.json",
        "docs/project/audit-issue-status.yaml",
        "docs/project/audit-remediation-map.yaml",
        "tools/check_audit_inventory.py",
        "reproductions/audit-reproduction-manifest.yaml",
        "tools/check_audit_reproductions.py",
    ),
)
def test_promotion_source_diff_rejects_release_authority_drift(
    tmp_path: Path,
    authority_path: str,
) -> None:
    module = _load_module()
    repository = tmp_path / authority_path.rsplit("/", 1)[-1]
    authority_file = repository / authority_path
    authority_file.parent.mkdir(parents=True)
    authority_file.write_text("authority: candidate\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "GraphBlocks test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "add", authority_path], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "candidate authority"],
        cwd=repository,
        check=True,
    )
    candidate_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "tag", "v1.0.0-rc.1", candidate_commit],
        cwd=repository,
        check=True,
    )
    authority_file.write_text("authority: final\n", encoding="utf-8")
    subprocess.run(["git", "add", authority_path], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "drift authority"],
        cwd=repository,
        check=True,
    )
    final_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    final_tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    module.ROOT = repository

    with pytest.raises(module.ReleaseBundleError, match="non-release source"):
        module._promotion_source_diff(
            candidate_commit=candidate_commit,
            final_commit=final_commit,
            final_tree=final_tree,
            candidate_ref=RELEASE_REF,
        )


def test_promotion_source_diff_requires_candidate_ref_to_resolve_exact_commit(
    tmp_path: Path,
) -> None:
    module = _load_module()
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "GraphBlocks test"],
        cwd=repository,
        check=True,
    )
    (repository / "pyproject.toml").write_text(
        '[project]\nversion = "1.0.0rc1"\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=repository, check=True)
    candidate_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "pyproject.toml").write_text(
        '[project]\nversion = "1.0.0"\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "final"], cwd=repository, check=True)
    final_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    final_tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    module.ROOT = repository

    with pytest.raises(module.ReleaseBundleError, match="does not resolve"):
        module._promotion_source_diff(
            candidate_commit=candidate_commit,
            final_commit=final_commit,
            final_tree=final_tree,
            candidate_ref=RELEASE_REF,
        )


def test_release_ref_rejects_noncanonical_or_mismatched_stable_versions(
    tmp_path: Path,
) -> None:
    module = _load_module()
    for release_ref in (
        "refs/tags/v1.0.0-rc.0",
        "refs/tags/v1.0.0-rc.01",
        "refs/tags/v1.0.1",
    ):
        with pytest.raises(module.ReleaseBundleError, match="release ref"):
            module._release_version_from_ref(release_ref)

    inputs = _inputs(module, tmp_path)
    module._first_party_versions = lambda: {
        "graphblocks": "0.1.0",
        "graphblocks-runtime": "0.1.0",
        "graphblocks-testing": "0.1.0",
    }
    with pytest.raises(module.ReleaseBundleError, match="version does not match release ref"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


def test_final_release_requires_regular_explicit_promotion_evidence(
    tmp_path: Path,
    symlink_or_skip,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path, stable_version="1.0.0")
    with pytest.raises(module.ReleaseBundleError, match="requires explicit"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "missing-bundle",
            git_commit=COMMIT,
            release_ref="refs/tags/v1.0.0",
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )

    target = _write_promotion_evidence(module, tmp_path / "promotion-target.json")
    link = tmp_path / "promotion-link.json"
    symlink_or_skip(link, target)
    with pytest.raises(module.ReleaseBundleError, match="regular non-symlink"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "symlink-bundle",
            git_commit=COMMIT,
            release_ref="refs/tags/v1.0.0",
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
            promotion_evidence=link,
        )


def test_final_release_binds_promotion_evidence_and_requires_signature(
    tmp_path: Path,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path, stable_version="1.0.0")
    promotion = _write_promotion_evidence(module, tmp_path / "promotion.json")
    bundle = tmp_path / "bundle"
    manifest = module.assemble_release_bundle(
        platform_inputs_dir=inputs,
        output_dir=bundle,
        git_commit=COMMIT,
        release_ref="refs/tags/v1.0.0",
        builder_id=BUILDER_ID,
        invocation_id=INVOCATION_ID,
        promotion_evidence=promotion,
    )

    assert manifest["readiness"] == "promotion-authorized-signature-required"
    assert manifest["signaturePolicy"]["status"] == "signature-required"
    assert manifest["externalGates"] == ["keyless-signing-identity"]
    promotion_binding = manifest["promotionEvidence"]
    assert promotion_binding["path"] == module.PROMOTION_EVIDENCE_NAME
    assert promotion_binding["contentDigest"] == _promotion_payload(module)[
        "contentDigest"
    ]
    assert any(
        record["path"] == module.PROMOTION_EVIDENCE_NAME
        for record in manifest["metadata"]
    )
    provenance = json.loads(
        (bundle / "provenance.intoto.json").read_text(encoding="utf-8")
    )
    assert provenance["predicate"]["buildDefinition"]["internalParameters"][
        "promotionEvidence"
    ] == promotion_binding
    with pytest.raises(module.ReleaseBundleError, match="requires its Sigstore signature"):
        module.verify_release_bundle(bundle_dir=bundle)
    signature = bundle / module.SIGNATURE_BUNDLE_NAME
    signature.write_text("{}", encoding="utf-8")
    signature_verifications: list[dict[str, object]] = []
    module._verify_sigstore_signature = lambda **arguments: signature_verifications.append(
        arguments
    )
    certificate_identity = (
        "https://github.com/graphblocks/graphblocks/.github/workflows/ci.yml@"
        "refs/tags/v1.0.0"
    )
    assert module.verify_release_bundle(
        bundle_dir=bundle,
        signature_bundle=signature,
        certificate_identity=certificate_identity,
    )["readiness"] == "promotion-authorized-signature-required"
    assert len(signature_verifications) == 1
    self_declared = dict(manifest)
    self_declared["readiness"] = "stable"
    (bundle / "release-manifest.json").write_bytes(
        module._canonical_json_bytes(self_declared)
    )
    with pytest.raises(module.ReleaseBundleError, match="unsupported format or readiness"):
        module.verify_release_bundle(bundle_dir=bundle)


def test_final_promotion_consumes_signed_concrete_real_service_runs(
    tmp_path: Path,
) -> None:
    module = _load_module()
    _trust_test_source(module)
    integration_id = "graphblocks-qdrant"
    workflow = ".github/workflows/real-services.yml"
    run_id = (
        "https://github.com/graphblocks/graphblocks/actions/runs/123456/attempts/2"
    )
    result = {
        "formatVersion": 1,
        "integrationId": integration_id,
        "ok": True,
        "authentication": "api-key",
        "serviceOrSdkVersion": "1.2.3",
        "retryAndFailureModel": "bounded-exponential-retry",
        "checks": [
            "connectivity",
            "authentication",
            "version",
            "retry",
            "failure",
        ],
    }
    integration_report = {
        "integrationId": integration_id,
        "status": "success",
        "complete": True,
        "candidateRef": RELEASE_REF,
        "candidateCommit": CANDIDATE_COMMIT,
        "test": "tests/integration/test_qdrant_real_service.py",
        "workflow": workflow,
        "workflowJob": "qdrant",
        "testStep": "exercise-qdrant-real-service",
        "runId": run_id,
        "artifactName": (
            f"{integration_id}-{CANDIDATE_COMMIT}-123456-2"
        ),
        "result": result,
        "resultDigest": "sha256:"
        + module._sha256_bytes(module._canonical_json_bytes(result)),
    }
    payload, _report_files = _promotion_payload_and_files(
        module,
        integration_reports=[integration_report],
    )
    promotion_root = tmp_path / "with-integration"
    promotion_root.mkdir()
    promotion_path = _write_promotion_payload(
        module,
        promotion_root / "promotion.json",
        payload,
    )
    snapshot = module._snapshot_regular_file(
        promotion_path,
        owner="test promotion evidence",
    )
    integration_matrix = {
        "integrations": [
            {
                "id": integration_id,
                "implementationMaturity": "real-adapter",
                "supportedAuthentication": ["api-key"],
                "supportedServiceOrSdkVersions": ["1.2.3"],
                "retryAndFailureModel": "bounded-exponential-retry",
                "realServiceEvidence": [
                    {
                        "test": "tests/integration/test_qdrant_real_service.py",
                        "workflow": workflow,
                        "workflowJob": "qdrant",
                        "testStep": "exercise-qdrant-real-service",
                        "artifactName": (
                            f"{integration_id}-${{{{ github.sha }}}}-"
                            "${{ github.run_id }}-${{ github.run_attempt }}"
                        ),
                    }
                ],
            }
        ]
    }
    signature_calls: list[dict[str, object]] = []
    module._verify_promotion_report_signature = (
        lambda **arguments: signature_calls.append(arguments)
        or PROMOTION_INTEGRATED_AT
    )
    candidate_matrix_reads: list[tuple[str, str]] = []
    trusted_promotion_git_blob = module._promotion_git_blob
    module._promotion_git_blob = (
        lambda commit, path: (
            candidate_matrix_reads.append((commit, path))
            or yaml.safe_dump(integration_matrix).encode("utf-8")
            if path == "docs/project/stable-release-matrix.yaml"
            else trusted_promotion_git_blob(commit, path)
        )
    )

    validated, _content_digest, _snapshots = module._validate_promotion_evidence(
        snapshot,
        git_commit=COMMIT,
        git_tree=TREE,
        release_ref="refs/tags/v1.0.0",
        release_version="1.0.0",
        verify_source_diff=True,
    )

    assert validated["integrationRuns"] == payload["integrationRuns"]
    assert candidate_matrix_reads == [
        (CANDIDATE_COMMIT, "docs/project/stable-release-matrix.yaml"),
        (COMMIT, "docs/project/stable-release-matrix.yaml"),
    ]
    integration_identity = (
        f"https://github.com/{module.SIGSTORE_REPOSITORY}/"
        f"{workflow}@{RELEASE_REF}"
    )
    assert any(
        call["certificate_identity"] == integration_identity
        and call["expected_certificate_identity"] == integration_identity
        for call in signature_calls
    )

    changed_final_matrix = json.loads(json.dumps(integration_matrix))
    changed_final_matrix["integrations"][0]["supportedAuthentication"].append(
        "oauth2"
    )
    module._promotion_git_blob = (
        lambda commit, path: (
            yaml.safe_dump(
                integration_matrix
                if commit == CANDIDATE_COMMIT
                else changed_final_matrix
            ).encode("utf-8")
            if path == "docs/project/stable-release-matrix.yaml"
            else trusted_promotion_git_blob(commit, path)
        )
    )
    with pytest.raises(module.ReleaseBundleError, match="claims changed"):
        module._validate_promotion_evidence(
            snapshot,
            git_commit=COMMIT,
            git_tree=TREE,
            release_ref="refs/tags/v1.0.0",
            release_version="1.0.0",
            verify_source_diff=True,
        )

    unsupported_matrix = json.loads(json.dumps(integration_matrix))
    unsupported_matrix["integrations"][0]["supportedAuthentication"] = ["oauth2"]
    with pytest.raises(module.ReleaseBundleError, match="outside its support matrix"):
        module._validate_promotion_evidence(
            snapshot,
            git_commit=COMMIT,
            git_tree=TREE,
            release_ref="refs/tags/v1.0.0",
            release_version="1.0.0",
            verify_source_diff=True,
            integration_matrix=unsupported_matrix,
        )

    partial_coverage_matrix = json.loads(json.dumps(integration_matrix))
    partial_coverage_matrix["integrations"][0]["supportedAuthentication"].append(
        "oauth2"
    )
    with pytest.raises(module.ReleaseBundleError, match="declared support matrix"):
        module._validate_promotion_evidence(
            snapshot,
            git_commit=COMMIT,
            git_tree=TREE,
            release_ref="refs/tags/v1.0.0",
            release_version="1.0.0",
            verify_source_diff=True,
            integration_matrix=partial_coverage_matrix,
        )

    mismatched_digest_report = json.loads(json.dumps(integration_report))
    mismatched_digest_report["resultDigest"] = "sha256:" + "7" * 64
    mismatched_payload, _mismatched_files = _promotion_payload_and_files(
        module,
        integration_reports=[mismatched_digest_report],
    )
    mismatched_root = tmp_path / "mismatched-integration-digest"
    mismatched_root.mkdir()
    mismatched_path = _write_promotion_payload(
        module,
        mismatched_root / "promotion.json",
        mismatched_payload,
    )
    mismatched_snapshot = module._snapshot_regular_file(
        mismatched_path,
        owner="test promotion evidence",
    )
    with pytest.raises(module.ReleaseBundleError, match="digest does not match"):
        module._validate_promotion_evidence(
            mismatched_snapshot,
            git_commit=COMMIT,
            git_tree=TREE,
            release_ref="refs/tags/v1.0.0",
            release_version="1.0.0",
            verify_source_diff=True,
            integration_matrix=integration_matrix,
        )

    missing_root = tmp_path / "missing-integration"
    missing_root.mkdir()
    missing_path = _write_promotion_evidence(
        module,
        missing_root / "promotion.json",
    )
    missing_snapshot = module._snapshot_regular_file(
        missing_path,
        owner="test promotion evidence",
    )
    with pytest.raises(module.ReleaseBundleError, match="do not cover"):
        module._validate_promotion_evidence(
            missing_snapshot,
            git_commit=COMMIT,
            git_tree=TREE,
            release_ref="refs/tags/v1.0.0",
            release_version="1.0.0",
            verify_source_diff=True,
            integration_matrix=integration_matrix,
        )


@pytest.mark.parametrize(
    ("substitution", "message"),
    (
        ("final-source", "exact final ref and version"),
        ("source-diff", "does not match the candidate and final commits"),
        ("candidate-manifest", "does not resolve to a signed report"),
        ("short-soak", "at least 14 days"),
        ("reviewer", "independent object-authorization"),
        ("noncanonical-digest", "lowercase SHA-256 digest"),
        ("defect", "zero unresolved"),
        ("audit-closure", "audit report"),
        ("upgrade", "first-stable upgrade exemption"),
        ("rehearsal", "authorized real staged"),
    ),
)
def test_final_release_rejects_promotion_evidence_substitution(
    tmp_path: Path,
    substitution: str,
    message: str,
) -> None:
    module = _load_module()
    _trust_test_source(module)
    payload = _promotion_payload(module)
    if substitution == "final-source":
        payload["release"]["releaseVersion"] = "1.0.1"
    elif substitution == "source-diff":
        payload["candidate"]["sourceDiff"]["digest"] = "sha256:" + "9" * 64
    elif substitution == "candidate-manifest":
        payload["candidate"]["manifestDigest"] = "sha256:" + "9" * 64
    elif substitution == "short-soak":
        payload["soak"]["endedAt"] = "2026-06-14T23:59:59Z"
    elif substitution == "reviewer":
        payload["reviews"]["security"]["reviewerIdentity"] = payload["reviews"][
            "api"
        ]["reviewerIdentity"]
    elif substitution == "noncanonical-digest":
        payload["reviews"]["security"]["reportDigest"] = "sha256:" + "A" * 64
    elif substitution == "defect":
        payload["stableScope"]["unresolvedHigh"] = 1
    elif substitution == "audit-closure":
        payload["auditClosure"]["openBySeverity"]["P1"] = 1
    elif substitution == "upgrade":
        payload["upgradeGate"]["status"] = "passed"
    else:
        payload["stagedRehearsal"]["realExternalActions"] = False
    payload.pop("contentDigest")
    payload["contentDigest"] = canonical_hash(payload)
    evidence = tmp_path / f"promotion-{substitution}.json"
    _write_promotion_payload(module, evidence, payload)
    snapshot = module._snapshot_regular_file(evidence, owner="test promotion evidence")

    with pytest.raises(module.ReleaseBundleError, match=message):
        module._validate_promotion_evidence(
            snapshot,
            git_commit=COMMIT,
            git_tree=TREE,
            release_ref="refs/tags/v1.0.0",
            release_version="1.0.0",
            verify_source_diff=True,
        )


def test_final_release_rejects_audit_closure_drift_after_candidate(
    tmp_path: Path,
) -> None:
    module = _load_module()
    _trust_test_source(module)
    evidence = _write_promotion_evidence(module, tmp_path / "promotion.json")
    snapshot = module._snapshot_regular_file(evidence, owner="test promotion evidence")
    trusted_git_blob = module._promotion_git_blob
    module._promotion_git_blob = lambda revision, path: (
        trusted_git_blob(revision, path) + b"\n"
        if revision == COMMIT and path == module.AUDIT_STATUS_PATH
        else trusted_git_blob(revision, path)
    )

    with pytest.raises(module.ReleaseBundleError, match="changed after the candidate"):
        module._validate_promotion_evidence(
            snapshot,
            git_commit=COMMIT,
            git_tree=TREE,
            release_ref="refs/tags/v1.0.0",
            release_version="1.0.0",
            verify_source_diff=True,
        )


def test_final_release_rejects_substituted_captured_audit_reproduction(
    tmp_path: Path,
) -> None:
    module = _load_module()
    _trust_test_source(module)
    evidence = _write_promotion_evidence(module, tmp_path / "promotion.json")
    snapshot = module._snapshot_regular_file(evidence, owner="test promotion evidence")
    trusted_git_blob = module._promotion_git_blob
    captured_path = "reproductions/original/repro_canonical_bigint_cost.out"
    module._promotion_git_blob = lambda revision, path: (
        trusted_git_blob(revision, path) + b"substituted"
        if revision == CANDIDATE_COMMIT and path == captured_path
        else trusted_git_blob(revision, path)
    )

    with pytest.raises(module.ReleaseBundleError, match="content was substituted"):
        module._validate_promotion_evidence(
            snapshot,
            git_commit=COMMIT,
            git_tree=TREE,
            release_ref="refs/tags/v1.0.0",
            release_version="1.0.0",
            verify_source_diff=True,
        )


def test_final_release_verification_rejects_promotion_evidence_tampering(
    tmp_path: Path,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path, stable_version="1.0.0")
    promotion = _write_promotion_evidence(module, tmp_path / "promotion.json")
    bundle = tmp_path / "bundle"
    module.assemble_release_bundle(
        platform_inputs_dir=inputs,
        output_dir=bundle,
        git_commit=COMMIT,
        release_ref="refs/tags/v1.0.0",
        builder_id=BUILDER_ID,
        invocation_id=INVOCATION_ID,
        promotion_evidence=promotion,
    )
    (bundle / module.PROMOTION_EVIDENCE_NAME).write_bytes(b"{}\n")

    with pytest.raises(module.ReleaseBundleError, match="does not match manifest"):
        module.verify_release_bundle(bundle_dir=bundle)


def test_final_release_resolves_hashes_and_verifies_every_promotion_report(
    tmp_path: Path,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path, stable_version="1.0.0")
    promotion = _write_promotion_evidence(module, tmp_path / "promotion.json")
    verified: list[tuple[str, str]] = []

    def record_verification(**arguments: object) -> datetime:
        report_snapshot = arguments["report_snapshot"]
        assert isinstance(report_snapshot, module.FileSnapshot)
        expected_identity = arguments["expected_certificate_identity"]
        assert isinstance(expected_identity, str)
        verified.append((report_snapshot.path.name, expected_identity))
        return PROMOTION_INTEGRATED_AT

    module._verify_promotion_report_signature = record_verification
    module.assemble_release_bundle(
        platform_inputs_dir=inputs,
        output_dir=tmp_path / "bundle",
        git_commit=COMMIT,
        release_ref="refs/tags/v1.0.0",
        builder_id=BUILDER_ID,
        invocation_id=INVOCATION_ID,
        promotion_evidence=promotion,
    )

    assert len(verified) == 24
    assert len({name for name, _identity in verified}) == 12
    assert any(name == "audit-closure.json" for name, _identity in verified)
    ci_identity = (
        "https://github.com/graphblocks/graphblocks/.github/workflows/"
        "ci.yml@refs/tags/v1.0.0-rc.1"
    )
    promotion_identity = (
        "https://github.com/graphblocks/graphblocks/.github/workflows/"
        "promotion-reports.yml@refs/tags/v1.0.0-rc.1"
    )
    assert [identity for _name, identity in verified].count(ci_identity) == 6
    assert [identity for _name, identity in verified].count(promotion_identity) == 18

    missing_promotion = _write_promotion_evidence(
        module, tmp_path / "missing" / "promotion.json"
    )
    first_report = next((missing_promotion.parent / "promotion-reports").glob("*.json"))
    if first_report.name.endswith(".sigstore.json"):
        first_report = next(
            path
            for path in (missing_promotion.parent / "promotion-reports").glob("*.json")
            if not path.name.endswith(".sigstore.json")
        )
    first_report.unlink()
    with pytest.raises(module.ReleaseBundleError, match="missing"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "missing-bundle",
            git_commit=COMMIT,
            release_ref="refs/tags/v1.0.0",
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
            promotion_evidence=missing_promotion,
        )


def test_promotion_report_signature_rejects_a_self_declared_signer(tmp_path: Path) -> None:
    module = _load_module()
    report_path = tmp_path / "report.json"
    signature_path = tmp_path / "report.sigstore.json"
    report_path.write_bytes(module._canonical_json_bytes({"approved": True}))
    signature_path.write_text("{}", encoding="utf-8")

    with pytest.raises(module.ReleaseBundleError, match="trusted attestor"):
        module._verify_promotion_report_signature(
            report_snapshot=module._snapshot_regular_file(
                report_path, owner="test promotion report"
            ),
            signature_snapshot=module._snapshot_regular_file(
                signature_path, owner="test promotion signature"
            ),
            certificate_identity="https://github.com/attacker/workflow@refs/heads/main",
            certificate_oidc_issuer=module.SIGSTORE_ISSUER,
            expected_certificate_identity=(
                "https://github.com/graphblocks/graphblocks/.github/workflows/"
                "promotion-reports.yml@refs/tags/v1.0.0-rc.1"
            ),
            cosign="cosign",
        )


def test_promotion_signature_returns_one_unambiguous_rekor_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    module._observe_cosign_identity = lambda _executable="cosign": dict(
        COSIGN_IDENTITY
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )
    report_path = tmp_path / "report.json"
    signature_path = tmp_path / "report.sigstore.json"
    report_path.write_bytes(module._canonical_json_bytes({"approved": True}))
    signature_path.write_bytes(
        module._canonical_json_bytes(
            {
                "verificationMaterial": {
                    "tlogEntries": [
                        {"integratedTime": str(PROMOTION_INTEGRATED_TIME)},
                        {"integratedTime": PROMOTION_INTEGRATED_TIME},
                    ]
                }
            }
        )
    )
    identity = (
        "https://github.com/graphblocks/graphblocks/.github/workflows/"
        "promotion-reports.yml@refs/tags/v1.0.0-rc.1"
    )

    observed = module._verify_promotion_report_signature(
        report_snapshot=module._snapshot_regular_file(
            report_path, owner="test promotion report"
        ),
        signature_snapshot=module._snapshot_regular_file(
            signature_path, owner="test promotion signature"
        ),
        certificate_identity=identity,
        certificate_oidc_issuer=module.SIGSTORE_ISSUER,
        expected_certificate_identity=identity,
        cosign="cosign",
    )

    assert observed == PROMOTION_INTEGRATED_AT


@pytest.mark.parametrize(
    ("bundle", "error"),
    (
        ("{", "not valid JSON"),
        ({}, "verification material"),
        (
            {"verificationMaterial": {"tlogEntries": [{}]}},
            "has no integratedTime",
        ),
        (
            {
                "verificationMaterial": {
                    "tlogEntries": [{"integratedTime": True}]
                }
            },
            "malformed integratedTime",
        ),
        (
            {
                "verificationMaterial": {
                    "tlogEntries": [
                        {"integratedTime": "1781568000"},
                        {"integratedTime": "1781568001"},
                    ]
                }
            },
            "inconsistent Rekor integratedTime",
        ),
    ),
    ids=("invalid-json", "missing", "entry-missing", "malformed", "inconsistent"),
)
def test_promotion_signature_rejects_invalid_rekor_times_after_cosign_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bundle: object,
    error: str,
) -> None:
    module = _load_module()
    module._observe_cosign_identity = lambda _executable="cosign": dict(
        COSIGN_IDENTITY
    )
    cosign_calls = 0

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal cosign_calls
        cosign_calls += 1
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    report_path = tmp_path / "report.json"
    signature_path = tmp_path / "report.sigstore.json"
    report_path.write_bytes(module._canonical_json_bytes({"approved": True}))
    if isinstance(bundle, str):
        signature_path.write_text(bundle, encoding="utf-8")
    else:
        signature_path.write_bytes(module._canonical_json_bytes(bundle))
    identity = (
        "https://github.com/graphblocks/graphblocks/.github/workflows/"
        "promotion-reports.yml@refs/tags/v1.0.0-rc.1"
    )

    with pytest.raises(module.ReleaseBundleError, match=error):
        module._verify_promotion_report_signature(
            report_snapshot=module._snapshot_regular_file(
                report_path, owner="test promotion report"
            ),
            signature_snapshot=module._snapshot_regular_file(
                signature_path, owner="test promotion signature"
            ),
            certificate_identity=identity,
            certificate_oidc_issuer=module.SIGSTORE_ISSUER,
            expected_certificate_identity=identity,
            cosign="cosign",
        )

    assert cosign_calls == 1


@pytest.mark.parametrize(
    ("report_type", "payload"),
    (
        (
            "candidate-manifest",
            {
                "formatVersion": 1,
                "releaseRef": RELEASE_REF,
                "releaseVersion": RELEASE_VERSION,
                "gitCommit": CANDIDATE_COMMIT,
            },
        ),
        (
            "soak-application",
            {
                "applicationId": "application-one",
                "nontrivial": True,
                "startedAt": "2026-06-01T00:00:00Z",
                "endedAt": "2026-06-15T00:00:00Z",
            },
        ),
        (
            "api-review",
            {
                "reviewerIdentity": "reviewer-api@example.test",
                "approved": True,
                "candidateRef": RELEASE_REF,
                "candidateCommit": CANDIDATE_COMMIT,
            },
        ),
        (
            "security-review",
            {
                "reviewerIdentity": "reviewer-security@example.test",
                "approved": True,
                "candidateRef": RELEASE_REF,
                "candidateCommit": CANDIDATE_COMMIT,
                "objectAuthorizationScope": [
                    "run-create-list-status-delete-attach-detach-events-and-streams",
                    "run-cancel-pause-resume-and-expire",
                    "subscription-create-revoke-and-event-acknowledgement",
                    "callback-submit-register-and-revoke",
                    "delivery-redrive-and-dead-letter",
                ],
                "reviewedMatrixRunDigests": [
                    "sha256:" + str(index) * 64 for index in range(1, 4)
                ],
            },
        ),
        (
            "stable-scope",
            {
                "unresolvedCritical": 0,
                "unresolvedHigh": 0,
                "unexplainedFlakes": 0,
            },
        ),
        (
            "protected-final-ref",
            {"releaseRef": "refs/tags/v1.0.0", "protected": True},
        ),
        (
            "staged-rehearsal",
            {
                "environment": "staging",
                "authorized": True,
                "realExternalActions": True,
                "authorizedBy": "release-operator@example.test",
                "operations": [
                    {"operation": operation, "status": "success"}
                    for operation in ("publish", "rollback", "yank", "restore")
                ],
            },
        ),
    ),
)
def test_candidate_workflow_can_validate_and_freeze_each_promotion_report_type(
    tmp_path: Path,
    report_type: str,
    payload: dict[str, object],
) -> None:
    module = _load_module()
    input_path = tmp_path / f"{report_type}-input.json"
    input_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    output_dir = tmp_path / f"{report_type}-frozen"

    frozen = module.freeze_promotion_report(
        input_path=input_path,
        output_dir=output_dir,
        report_type=report_type,
        candidate_ref=RELEASE_REF,
        candidate_commit=CANDIDATE_COMMIT,
        workflow_actor=payload.get("reviewerIdentity"),
    )

    assert frozen.path == output_dir / "report.json"
    assert frozen.data == module._canonical_json_bytes(payload)
    assert frozen.sha256 == module._sha256_bytes(frozen.data)


def test_candidate_workflow_derives_and_freezes_audit_closure(
    tmp_path: Path,
) -> None:
    module = _load_module()
    payload = _audit_closure_report(module)
    input_path = tmp_path / "audit-closure-input.json"
    input_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _trust_test_source(module)
    module._current_git_commit = lambda: CANDIDATE_COMMIT

    frozen = module.freeze_promotion_report(
        input_path=input_path,
        output_dir=tmp_path / "audit-closure-frozen",
        report_type="audit-closure",
        candidate_ref=RELEASE_REF,
        candidate_commit=CANDIDATE_COMMIT,
    )

    assert frozen.data == module._canonical_json_bytes(payload)
    assert payload["openBySeverity"] == {"P0": 0, "P1": 0, "P2": 34, "P3": 8}
    assert payload["reproductions"]["findings"] == 9
    assert payload["reproductions"]["capturedFiles"] == 13
    assert payload["reproductions"]["reconstructedHarnesses"] == 5

    module._current_git_commit = lambda: COMMIT
    with pytest.raises(module.ReleaseBundleError, match="checkout does not match"):
        module.freeze_promotion_report(
            input_path=input_path,
            output_dir=tmp_path / "wrong-checkout",
            report_type="audit-closure",
            candidate_ref=RELEASE_REF,
            candidate_commit=CANDIDATE_COMMIT,
        )


def test_candidate_ci_freezes_one_canonical_matrix_run_attestation(
    tmp_path: Path,
) -> None:
    module = _load_module()
    run_id = (
        "https://github.com/graphblocks/graphblocks/actions/runs/123456/attempts/2"
    )
    result_path, junit_path, security_gate_result = _write_security_gate_evidence(
        module,
        tmp_path / "security-gates",
        run_attempt=2,
    )

    frozen = module.freeze_candidate_matrix_report(
        output_dir=tmp_path / "frozen",
        candidate_ref=RELEASE_REF,
        candidate_commit=CANDIDATE_COMMIT,
        run_id=run_id,
        security_gate_result_path=result_path,
        security_gate_junit_path=junit_path,
    )

    candidate_manifest = {
        "formatVersion": 1,
        "releaseRef": RELEASE_REF,
        "releaseVersion": RELEASE_VERSION,
        "gitCommit": CANDIDATE_COMMIT,
    }
    expected = {
        "runId": run_id,
        "status": "success",
        "complete": True,
        "candidateRef": RELEASE_REF,
        "candidateCommit": CANDIDATE_COMMIT,
        "candidateManifestDigest": "sha256:"
        + module._sha256_bytes(module._canonical_json_bytes(candidate_manifest)),
        "supportedMatrix": [
            {"os": os_name, "python": python_version}
            for os_name, python_version in module.SUPPORTED_PLATFORM_MATRIX
        ],
        "securityGates": security_gate_result,
    }
    assert frozen.data == module._canonical_json_bytes(expected)


@pytest.mark.parametrize(
    "substitution",
    (
        "junit-digest",
        "candidate-commit",
        "selector-manifest",
        "artifact-attempt",
    ),
)
def test_candidate_ci_rejects_substituted_security_gate_evidence(
    tmp_path: Path,
    substitution: str,
) -> None:
    module = _load_module()
    result_path, junit_path, result = _write_security_gate_evidence(
        module,
        tmp_path / substitution / "security-gates",
        run_attempt=2,
    )
    if substitution == "junit-digest":
        junit_path.write_bytes(junit_path.read_bytes() + b"<!-- substituted -->")
    else:
        if substitution == "candidate-commit":
            result["candidateCommit"] = "4" * 40
        elif substitution == "artifact-attempt":
            result["pytest"]["junit"]["artifactName"] = (
                f"{stable_security_gates.ARTIFACT_NAME_PREFIX}-1"
            )
        else:
            result["adversarialResources"]["categories"][0][
                "pytestSelectors"
            ].pop()
        result.pop("resultDigest")
        result["resultDigest"] = canonical_hash(result)
        result_path.write_bytes(module._canonical_json_bytes(result))

    with pytest.raises(module.ReleaseBundleError, match="candidate security gates"):
        module.freeze_candidate_matrix_report(
            output_dir=tmp_path / substitution / "frozen",
            candidate_ref=RELEASE_REF,
            candidate_commit=CANDIDATE_COMMIT,
            run_id=(
                "https://github.com/graphblocks/graphblocks/actions/runs/"
                "123456/attempts/2"
            ),
            security_gate_result_path=result_path,
            security_gate_junit_path=junit_path,
        )


def test_security_review_freeze_requires_actor_scope_and_matrix_evidence(
    tmp_path: Path,
) -> None:
    module = _load_module()
    payload = {
        "reviewerIdentity": "reviewer-security",
        "approved": True,
        "candidateRef": RELEASE_REF,
        "candidateCommit": CANDIDATE_COMMIT,
        "objectAuthorizationScope": list(module.OBJECT_AUTHORIZATION_REVIEW_SCOPE),
        "reviewedMatrixRunDigests": [
            "sha256:" + str(index) * 64 for index in range(1, 4)
        ],
    }
    generic_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"objectAuthorizationScope", "reviewedMatrixRunDigests"}
    }

    for name, mutation, actor, error in (
        (
            "generic",
            generic_payload,
            "reviewer-security",
            "invalid or incomplete shape",
        ),
        ("no-actor", payload, None, "workflow actor"),
        ("wrong-actor", payload, "another-reviewer", "does not approve"),
        (
            "partial-scope",
            {
                **payload,
                "objectAuthorizationScope": payload["objectAuthorizationScope"][:-1],
            },
            "reviewer-security",
            "object-authorization",
        ),
        (
            "duplicate-report",
            {
                **payload,
                "reviewedMatrixRunDigests": [
                    payload["reviewedMatrixRunDigests"][0],
                ]
                * 3,
            },
            "reviewer-security",
            "adversarial-resource evidence",
        ),
    ):
        input_path = tmp_path / f"{name}.json"
        input_path.write_text(json.dumps(mutation), encoding="utf-8")
        with pytest.raises(module.ReleaseBundleError, match=error):
            module.freeze_promotion_report(
                input_path=input_path,
                output_dir=tmp_path / f"{name}-output",
                report_type="security-review",
                candidate_ref=RELEASE_REF,
                candidate_commit=CANDIDATE_COMMIT,
                workflow_actor=actor,
            )


@pytest.mark.parametrize(
    "substitution",
    ("resource-status", "resource-category", "source-digest", "review-digest"),
)
def test_final_promotion_rejects_security_gate_substitution(
    tmp_path: Path,
    substitution: str,
) -> None:
    module = _load_module()
    _trust_test_source(module, stable_version="1.0.0")
    payload = _promotion_payload(module)
    first_run = payload["supportedMatrixRuns"][0]
    if substitution == "resource-status":
        first_run["securityGates"]["adversarialResources"]["status"] = "skipped"
    elif substitution == "resource-category":
        first_run["securityGates"]["adversarialResources"]["categories"].pop()
    elif substitution == "source-digest":
        first_run["securityGates"]["objectAuthorization"]["routeManifest"][
            "sha256"
        ] = "sha256:" + "9" * 64
    else:
        payload["reviews"]["security"]["reviewedMatrixRunDigests"][0] = (
            "sha256:" + "9" * 64
        )
    payload.pop("contentDigest")
    payload["contentDigest"] = canonical_hash(payload)
    evidence = _write_promotion_payload(
        module,
        tmp_path / substitution / "promotion.json",
        payload,
    )

    with pytest.raises(
        module.ReleaseBundleError,
        match="security gates|security review",
    ):
        module._validate_promotion_evidence(
            module._snapshot_regular_file(evidence, owner="mutated security evidence"),
            git_commit=COMMIT,
            git_tree=TREE,
            release_ref="refs/tags/v1.0.0",
            release_version="1.0.0",
            verify_source_diff=True,
        )


def test_candidate_ci_freezes_concrete_real_service_integration_evidence(
    tmp_path: Path,
) -> None:
    module = _load_module()
    integration_id = "graphblocks-qdrant"
    run_id = (
        "https://github.com/graphblocks/graphblocks/actions/runs/123456/attempts/2"
    )
    result = {
        "formatVersion": 1,
        "integrationId": integration_id,
        "ok": True,
        "authentication": "api-key",
        "serviceOrSdkVersion": "1.2.3",
        "retryAndFailureModel": "bounded-exponential-retry",
        "checks": [
            "connectivity",
            "authentication",
            "version",
            "retry",
            "failure",
        ],
    }
    result_path = tmp_path / "qdrant-result.json"
    result_path.write_bytes(module._canonical_json_bytes(result))
    output_path = tmp_path / "qdrant-report.json"
    artifact_name = (
        f"{integration_id}-{CANDIDATE_COMMIT}-123456-2"
    )

    frozen = module.freeze_integration_report(
        input_path=result_path,
        output_path=output_path,
        integration_id=integration_id,
        test="tests/integration/test_qdrant_real_service.py",
        workflow=".github/workflows/real-services.yml",
        workflow_job="qdrant",
        test_step="exercise-qdrant-real-service",
        run_id=run_id,
        artifact_name=artifact_name,
        candidate_ref=RELEASE_REF,
        candidate_commit=CANDIDATE_COMMIT,
    )

    assert json.loads(frozen.data) == {
        "integrationId": integration_id,
        "status": "success",
        "complete": True,
        "candidateRef": RELEASE_REF,
        "candidateCommit": CANDIDATE_COMMIT,
        "test": "tests/integration/test_qdrant_real_service.py",
        "workflow": ".github/workflows/real-services.yml",
        "workflowJob": "qdrant",
        "testStep": "exercise-qdrant-real-service",
        "runId": run_id,
        "artifactName": artifact_name,
        "result": result,
        "resultDigest": "sha256:"
        + module._sha256_bytes(module._canonical_json_bytes(result)),
    }
    assert frozen.path == output_path


def test_candidate_ci_rejects_incomplete_real_service_integration_evidence(
    tmp_path: Path,
) -> None:
    module = _load_module()
    result_path = tmp_path / "qdrant-result.json"
    result_path.write_bytes(
        module._canonical_json_bytes(
            {
                "formatVersion": 1,
                "integrationId": "graphblocks-qdrant",
                "ok": True,
                "authentication": "api-key",
                "serviceOrSdkVersion": "1.2.3",
                "retryAndFailureModel": "bounded-exponential-retry",
                "checks": ["connectivity"],
            }
        )
    )

    with pytest.raises(module.ReleaseBundleError, match="must prove"):
        module.freeze_integration_report(
            input_path=result_path,
            output_path=tmp_path / "qdrant-report.json",
            integration_id="graphblocks-qdrant",
            test="tests/integration/test_qdrant_real_service.py",
            workflow=".github/workflows/real-services.yml",
            workflow_job="qdrant",
            test_step="exercise-qdrant-real-service",
            run_id=(
                "https://github.com/graphblocks/graphblocks/actions/runs/"
                "123456/attempts/2"
            ),
            artifact_name=(
                f"graphblocks-qdrant-{CANDIDATE_COMMIT}-123456-2"
            ),
            candidate_ref=RELEASE_REF,
            candidate_commit=CANDIDATE_COMMIT,
        )


def test_candidate_ci_rejects_a_noncanonical_matrix_run_identity(tmp_path: Path) -> None:
    module = _load_module()

    with pytest.raises(module.ReleaseBundleError, match="run-attempt identity"):
        module.freeze_candidate_matrix_report(
            output_dir=tmp_path / "frozen",
            candidate_ref=RELEASE_REF,
            candidate_commit=CANDIDATE_COMMIT,
            run_id="matrix-run-1",
            security_gate_result_path=tmp_path / "missing-result.json",
            security_gate_junit_path=tmp_path / "missing-junit.xml",
        )


def test_candidate_manifest_freeze_rejects_extra_fields(tmp_path: Path) -> None:
    module = _load_module()
    input_path = tmp_path / "candidate-manifest.json"
    input_path.write_text(
        json.dumps(
            {
                "formatVersion": 1,
                "releaseRef": RELEASE_REF,
                "releaseVersion": RELEASE_VERSION,
                "gitCommit": CANDIDATE_COMMIT,
                "selfDeclaredSuccess": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.ReleaseBundleError, match="does not bind this candidate"):
        module.freeze_promotion_report(
            input_path=input_path,
            output_dir=tmp_path / "frozen",
            report_type="candidate-manifest",
            candidate_ref=RELEASE_REF,
            candidate_commit=CANDIDATE_COMMIT,
        )


def test_candidate_workflow_rejects_a_report_for_another_candidate(
    tmp_path: Path,
) -> None:
    module = _load_module()
    input_path = tmp_path / "review.json"
    input_path.write_text(
        json.dumps(
            {
                "reviewerIdentity": "reviewer-api@example.test",
                "approved": True,
                "candidateRef": RELEASE_REF,
                "candidateCommit": COMMIT,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.ReleaseBundleError, match="does not approve this candidate"):
        module.freeze_promotion_report(
            input_path=input_path,
            output_dir=tmp_path / "frozen",
            report_type="api-review",
            candidate_ref=RELEASE_REF,
            candidate_commit=CANDIDATE_COMMIT,
            workflow_actor="reviewer-api@example.test",
        )


def test_final_release_rejects_future_and_out_of_period_soak_evidence(
    tmp_path: Path,
) -> None:
    module = _load_module()
    _trust_test_source(module, stable_version="1.0.0")
    future = _promotion_payload(module)
    future["soak"]["endedAt"] = "2099-06-15T00:00:00Z"
    future.pop("contentDigest")
    future["contentDigest"] = canonical_hash(future)
    future_path = _write_promotion_payload(
        module, tmp_path / "future" / "promotion.json", future
    )
    with pytest.raises(module.ReleaseBundleError, match="must not end in the future"):
        module._validate_promotion_evidence(
            module._snapshot_regular_file(future_path, owner="future promotion"),
            git_commit=COMMIT,
            git_tree=TREE,
            release_ref="refs/tags/v1.0.0",
            release_version="1.0.0",
            verify_source_diff=True,
        )

    outside, report_files = _promotion_payload_and_files(module)
    report_path = "promotion-reports/application-one.json"
    report = json.loads(report_files[report_path])
    report["startedAt"] = "2026-05-31T23:59:59Z"
    report_bytes = module._canonical_json_bytes(report)
    report_sha256 = module._sha256_bytes(report_bytes)
    record = next(
        item for item in outside["reportArtifacts"] if item["path"] == report_path
    )
    old_digest = "sha256:" + record["sha256"]
    record["sha256"] = report_sha256
    application = next(
        item
        for item in outside["soak"]["applications"]
        if item["reportDigest"] == old_digest
    )
    application["reportDigest"] = "sha256:" + report_sha256
    outside.pop("contentDigest")
    outside["contentDigest"] = canonical_hash(outside)
    outside_path = _write_promotion_payload(
        module, tmp_path / "outside" / "promotion.json", outside
    )
    (outside_path.parent / report_path).write_bytes(report_bytes)
    with pytest.raises(module.ReleaseBundleError, match="does not cover the soak period"):
        module._validate_promotion_evidence(
            module._snapshot_regular_file(outside_path, owner="outside promotion"),
            git_commit=COMMIT,
            git_tree=TREE,
            release_ref="refs/tags/v1.0.0",
            release_version="1.0.0",
            verify_source_diff=True,
        )


def test_soak_report_signature_cannot_predate_its_claimed_end(tmp_path: Path) -> None:
    module = _load_module()
    _trust_test_source(module, stable_version="1.0.0")
    module._verify_promotion_report_signature = lambda **_arguments: datetime(
        2026, 6, 14, 23, 59, 59, tzinfo=timezone.utc
    )
    promotion = _write_promotion_evidence(module, tmp_path / "promotion.json")

    with pytest.raises(module.ReleaseBundleError, match="signed before its claimed end"):
        module._validate_promotion_evidence(
            module._snapshot_regular_file(promotion, owner="self-dated promotion"),
            git_commit=COMMIT,
            git_tree=TREE,
            release_ref="refs/tags/v1.0.0",
            release_version="1.0.0",
            verify_source_diff=True,
        )


def test_final_bundle_standalone_verification_rechecks_source_diff(
    tmp_path: Path,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path, stable_version="1.0.0")
    promotion = _write_promotion_evidence(module, tmp_path / "promotion.json")
    bundle = tmp_path / "bundle"
    module.assemble_release_bundle(
        platform_inputs_dir=inputs,
        output_dir=bundle,
        git_commit=COMMIT,
        release_ref="refs/tags/v1.0.0",
        builder_id=BUILDER_ID,
        invocation_id=INVOCATION_ID,
        promotion_evidence=promotion,
    )
    calls = 0

    def observe_source_diff(**_arguments: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "digest": PROMOTION_SOURCE_DIFF["digest"],
            "changes": [dict(change) for change in PROMOTION_SOURCE_DIFF["changes"]],
        }

    module._promotion_source_diff = observe_source_diff
    with pytest.raises(module.ReleaseBundleError, match="requires its Sigstore signature"):
        module.verify_release_bundle(bundle_dir=bundle)
    assert calls == 1


def test_release_artifact_set_requires_pep625_sdists_and_exact_seven_file_union() -> None:
    module = _load_module()
    filenames = (
        f"graphblocks-{RELEASE_VERSION}-py3-none-any.whl",
        f"graphblocks_testing-{RELEASE_VERSION}-py3-none-any.whl",
        _runtime_wheel("ubuntu-latest", "3.11"),
        _runtime_wheel("windows-latest", "3.11"),
        f"graphblocks-{RELEASE_VERSION}.tar.gz",
        f"graphblocks_testing-{RELEASE_VERSION}.tar.gz",
        "graphblocks_runtime-0.1.0.tar.gz",
    )
    module._validate_release_artifact_names(filenames)
    assert module._artifact_identity(f"graphblocks-{RELEASE_VERSION}.tar.gz") == (
        "graphblocks",
        RELEASE_VERSION,
    )
    assert not module._wheel_matches_platform(
        "graphblocks_runtime-0.1.0-cp311-abi3-manylinux_2_17_aarch64.whl",
        distribution="graphblocks-runtime",
        platform_identity=("ubuntu-latest", "3.11"),
    )
    assert not module._wheel_matches_platform(
        "graphblocks_runtime-0.1.0-cp311-abi3-win32.whl",
        distribution="graphblocks-runtime",
        platform_identity=("windows-latest", "3.11"),
    )

    with pytest.raises(module.ReleaseBundleError, match="PEP 625"):
        module._artifact_identity("graphblocks.tar.gz")
    with pytest.raises(module.ReleaseBundleError, match="exact supported"):
        module._validate_release_artifact_names(filenames[:-1])
    with pytest.raises(module.ReleaseBundleError, match="duplicate filenames"):
        module._validate_release_artifact_names((*filenames, filenames[0]))
    with pytest.raises(module.ReleaseBundleError, match="exact supported"):
        module._validate_release_artifact_names(
            (*filenames, "graphblocks_runtime-0.1.0.post1.tar.gz")
        )


def test_release_bundle_binds_exact_platform_artifacts_evidence_tools_and_rehearsal(
    tmp_path: Path,
) -> None:
    module = _load_module()
    bundle = _assemble(module, tmp_path)

    manifest = module.verify_release_bundle(bundle_dir=bundle)
    assert manifest["gitCommit"] == COMMIT
    assert manifest["gitTree"] == TREE
    assert manifest["releaseRef"] == RELEASE_REF
    assert manifest["releaseVersion"] == RELEASE_VERSION
    assert manifest["distributionVersions"] == {
        "graphblocks": RELEASE_VERSION,
        "graphblocks-runtime": "0.1.0",
        "graphblocks-testing": RELEASE_VERSION,
    }
    assert manifest["readiness"] == "candidate"
    assert manifest["externalGates"] == [
        "keyless-signing-identity",
        "release-index-credentials",
        "release-candidate-soak",
        "independent-api-review",
        "independent-security-review",
        "candidate-object-authorization-review",
        "candidate-adversarial-resource-attestations",
        "protected-final-ref",
        "authorized-real-staged-rehearsal",
    ]
    assert manifest["toolIdentities"] == dict(sorted(module.PINNED_RELEASE_TOOLS.items()))
    assert manifest["observedToolIdentities"] == {"cosign": COSIGN_IDENTITY}
    assert manifest["platforms"] == [
        {"os": os_name, "python": python_version}
        for os_name, python_version in module.SUPPORTED_PLATFORM_MATRIX
    ]
    assert len(manifest["artifacts"]) == 7
    assert len(manifest["evidence"]) == 12

    checksum_lines = (bundle / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    assert len(checksum_lines) == 7
    assert all("  artifacts/" in line for line in checksum_lines)
    assert sum(line.endswith(".whl") for line in checksum_lines) == 4
    assert sum(line.endswith(".tar.gz") for line in checksum_lines) == 3

    sbom = json.loads((bundle / "SBOM.cdx.json").read_text(encoding="utf-8"))
    release_components = module._sbom_release_artifacts(sbom)
    assert set(release_components) == {
        Path(record["path"]).name for record in manifest["artifacts"]
    }
    assert {component["name"] for component in sbom["components"]} >= {
        "jsonschema",
        "packaging",
        "PyYAML",
        "referencing",
    }
    dependency_graph = {
        relationship["ref"]: set(relationship["dependsOn"])
        for relationship in sbom["dependencies"]
    }
    graphblocks_ref = f"pkg:pypi/graphblocks@{RELEASE_VERSION}"
    assert {
        "pkg:pypi/jsonschema@4.25.1",
        "pkg:pypi/packaging@25.0",
        "pkg:pypi/PyYAML@6.0.2",
        "pkg:pypi/referencing@0.37.0",
    }.issubset(dependency_graph[graphblocks_ref])

    provenance = json.loads((bundle / "provenance.intoto.json").read_text(encoding="utf-8"))
    assert provenance["predicate"]["buildDefinition"]["externalParameters"] == {
        "targetRelease": "1.0",
        "releaseRef": RELEASE_REF,
        "releaseVersion": RELEASE_VERSION,
    }
    internal = provenance["predicate"]["buildDefinition"]["internalParameters"]
    assert len(internal["buildEnvironments"]) == 4
    assert internal["toolIdentities"] == [
        {"name": name, "version": version}
        for name, version in sorted(module.PINNED_RELEASE_TOOLS.items())
    ]
    assert internal["observedToolIdentities"] == {"cosign": COSIGN_IDENTITY}
    assert len(internal["releaseEvidence"]) == 12
    assert internal["releaseExpectations"]["path"] == "release-expectations.json"
    expectations = json.loads(
        (bundle / "release-expectations.json").read_text(encoding="utf-8")
    )
    assert expectations["source"] == {
        "gitCommit": COMMIT,
        "gitTree": TREE,
        "releaseRef": RELEASE_REF,
        "releaseVersion": RELEASE_VERSION,
    }
    assert expectations["expectations"] == json.loads(
        json.dumps(module.release_evidence_expectations(module.ROOT))
    )

    rehearsal = json.loads((bundle / "rehearsal.json").read_text(encoding="utf-8"))
    assert rehearsal["ok"] is True
    assert rehearsal["networkRequests"] == rehearsal["mutations"] == 0
    assert {transition["operation"] for transition in rehearsal["transitions"]} == {
        "publish",
        "rollback-before-promotion",
        "yank",
        "restore",
    }


def test_release_bundle_output_is_deterministic(tmp_path: Path) -> None:
    module = _load_module()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = _assemble(module, first_root)
    second = _assemble(module, second_root)

    assert {
        path.relative_to(first).as_posix(): path.read_bytes()
        for path in first.rglob("*")
        if path.is_file()
    } == {
        path.relative_to(second).as_posix(): path.read_bytes()
        for path in second.rglob("*")
        if path.is_file()
    }


def test_direct_assembly_resolves_release_ref_to_requested_commit(
    tmp_path: Path,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    resolved_refs: list[str] = []

    def resolve_release_ref(ref: str) -> str:
        resolved_refs.append(ref)
        return COMMIT

    module._resolve_git_commit = resolve_release_ref
    manifest = module.assemble_release_bundle(
        platform_inputs_dir=inputs,
        output_dir=tmp_path / "bundle",
        git_commit=COMMIT,
        release_ref=RELEASE_REF,
        builder_id=BUILDER_ID,
        invocation_id=INVOCATION_ID,
    )

    assert resolved_refs == [RELEASE_REF]
    assert manifest["gitCommit"] == COMMIT


def test_direct_assembly_rejects_release_ref_at_a_different_commit(
    tmp_path: Path,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    output = tmp_path / "bundle"
    module._resolve_git_commit = lambda _ref: "3" * 40

    with pytest.raises(module.ReleaseBundleError, match="not requested Git commit"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=output,
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )

    assert not output.exists()


@pytest.mark.parametrize("ref_state", ["missing", "non-commit"])
def test_direct_assembly_rejects_release_ref_without_a_commit_target(
    tmp_path: Path,
    ref_state: str,
) -> None:
    module = _load_module()
    real_resolver = module._resolve_git_commit
    inputs = _inputs(module, tmp_path)
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    if ref_state == "non-commit":
        subprocess.run(
            ["git", "config", "user.email", "test@example.test"],
            cwd=repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "GraphBlocks test"],
            cwd=repository,
            check=True,
        )
        (repository / "source.txt").write_text("source\n", encoding="utf-8")
        subprocess.run(["git", "add", "source.txt"], cwd=repository, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "source"], cwd=repository, check=True
        )
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "update-ref", RELEASE_REF, tree],
            cwd=repository,
            check=True,
        )

    module.ROOT = repository
    module._resolve_git_commit = real_resolver
    output = tmp_path / "bundle"
    with pytest.raises(module.ReleaseBundleError, match="does not resolve to a commit"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=output,
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )

    assert not output.exists()


def test_direct_assembly_rejects_a_declared_commit_that_is_not_checked_out(
    tmp_path: Path,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    module._current_git_commit = lambda: "3" * 40

    with pytest.raises(module.ReleaseBundleError, match="checked-out HEAD"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


def test_direct_assembly_rejects_a_dirty_source_checkout(tmp_path: Path) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)

    def reject_dirty_source() -> None:
        raise module.ReleaseBundleError("release source checkout is not clean")

    module._assert_clean_source_checkout = reject_dirty_source
    with pytest.raises(module.ReleaseBundleError, match="not clean"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


def test_standalone_verification_uses_frozen_expectations_not_live_checkout(
    tmp_path: Path,
) -> None:
    module = _load_module()
    bundle = _assemble(module, tmp_path)

    def live_checkout_must_not_be_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("standalone verification consulted the live checkout")

    module.release_evidence_expectations = live_checkout_must_not_be_read
    module._first_party_versions = live_checkout_must_not_be_read
    module._resolve_git_commit = live_checkout_must_not_be_read
    manifest = module.verify_release_bundle(bundle_dir=bundle)

    assert manifest["gitCommit"] == COMMIT


def test_release_bundle_rejects_missing_platform_and_dependency_contamination(
    tmp_path: Path,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    missing = next(inputs.iterdir())
    for path in sorted(missing.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        else:
            path.rmdir()
    missing.rmdir()
    with pytest.raises(module.ReleaseBundleError, match="exact supported platform matrix"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "missing-bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )

    contaminated_root = tmp_path / "contaminated"
    contaminated = _inputs(module, contaminated_root)
    wheelhouse = next(contaminated.iterdir()) / "platform-wheelhouse"
    (wheelhouse / "jsonschema-4.25.1-py3-none-any.whl").write_bytes(b"dependency")
    with pytest.raises(
        module.ReleaseBundleError,
        match="exact first-party|bind its exact wheels|unexpected first-party",
    ):
        module.assemble_release_bundle(
            platform_inputs_dir=contaminated,
            output_dir=tmp_path / "contaminated-bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


def test_standalone_assembly_revalidates_exact_checked_in_tck_semantics(tmp_path: Path) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    platform_root = next(inputs.iterdir())
    evidence_root = platform_root / "platform-evidence"
    tck_path = evidence_root / "tck.json"
    tck = json.loads(tck_path.read_text(encoding="utf-8"))
    first_report = next(iter(tck["reports"].values()))
    first_report["evidence"]["implementation_version"] = "9.9.9"
    tck.pop("contentDigest")
    tck["contentDigest"] = canonical_hash(tck)
    tck_path.write_text(json.dumps(tck, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    platform_path = evidence_root / "platform.json"
    platform = json.loads(platform_path.read_text(encoding="utf-8"))
    platform["evidence"]["tck"] = tck["contentDigest"]
    platform.pop("contentDigest")
    platform["contentDigest"] = canonical_hash(platform)
    platform_path.write_text(
        json.dumps(platform, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(module.ReleaseBundleError, match="implementation_version"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


def test_standalone_assembly_rejects_substituted_compiler_wheel_evidence(
    tmp_path: Path,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    evidence_root = next(inputs.iterdir()) / "platform-evidence"
    tck_path = evidence_root / "tck.json"
    tck = json.loads(tck_path.read_text(encoding="utf-8"))
    tck["reports"]["compiler"]["evidence"]["implementation_artifact"][
        "sha256"
    ] = "f" * 64
    tck.pop("contentDigest")
    tck["contentDigest"] = canonical_hash(tck)
    tck_path.write_text(
        json.dumps(tck, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    platform_path = evidence_root / "platform.json"
    platform = json.loads(platform_path.read_text(encoding="utf-8"))
    platform["evidence"]["tck"] = tck["contentDigest"]
    platform.pop("contentDigest")
    platform["contentDigest"] = canonical_hash(platform)
    platform_path.write_text(
        json.dumps(platform, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        module.ReleaseBundleError,
        match="exact graphblocks-runtime wheel",
    ):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


def test_retained_evidence_rejects_substituted_compiler_wheel_identity(
    tmp_path: Path,
) -> None:
    module = _load_module()
    bundle = _assemble(module, tmp_path)
    os_name, python_version = next(iter(module.SUPPORTED_PLATFORM_MATRIX))
    evidence_root = (
        bundle
        / "evidence"
        / f"{os_name}-py{python_version.replace('.', '')}"
    )
    tck_path = evidence_root / "tck.json"
    tck = json.loads(tck_path.read_text(encoding="utf-8"))
    tck["reports"]["compiler"]["evidence"]["implementation_artifact"][
        "sha256"
    ] = "f" * 64
    tck.pop("contentDigest")
    tck["contentDigest"] = canonical_hash(tck)
    tck_path.write_text(
        json.dumps(tck, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    platform_path = evidence_root / "platform.json"
    platform = json.loads(platform_path.read_text(encoding="utf-8"))
    platform["evidence"]["tck"] = tck["contentDigest"]
    platform.pop("contentDigest")
    platform["contentDigest"] = canonical_hash(platform)
    platform_path.write_text(
        json.dumps(platform, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path = bundle / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    snapshots, _directories = module._bundle_snapshots(
        bundle,
        manifest_snapshot=module._snapshot_regular_file(
            manifest_path,
            owner="test release manifest",
        ),
    )

    with pytest.raises(
        module.ReleaseBundleError,
        match="exact graphblocks-runtime wheel",
    ):
        module._verify_platform_evidence(
            snapshots=snapshots,
            artifacts={
                record["path"]: record
                for record in manifest["artifacts"]
            },
            expectations=module.release_evidence_expectations(module.ROOT),
        )


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("schema_manifest_digest", "schema manifest"),
        ("profile_catalog_digest", "conformance profile catalog"),
    ),
)
def test_release_bundle_rejects_tck_contract_digest_substitution(
    tmp_path: Path,
    field: str,
    message: str,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    evidence_root = next(inputs.iterdir()) / "platform-evidence"
    tck_path = evidence_root / "tck.json"
    tck = json.loads(tck_path.read_text(encoding="utf-8"))
    tck[field] = "sha256:" + "f" * 64
    tck.pop("contentDigest")
    tck["contentDigest"] = canonical_hash(tck)
    tck_path.write_text(json.dumps(tck, sort_keys=True) + "\n", encoding="utf-8")
    platform_path = evidence_root / "platform.json"
    platform = json.loads(platform_path.read_text(encoding="utf-8"))
    platform["evidence"]["tck"] = tck["contentDigest"]
    platform.pop("contentDigest")
    platform["contentDigest"] = canonical_hash(platform)
    platform_path.write_text(
        json.dumps(platform, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(module.ReleaseBundleError, match=message):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


def test_release_bundle_rejects_platform_contract_binding_substitution(
    tmp_path: Path,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    platform_path = next(inputs.iterdir()) / "platform-evidence" / "platform.json"
    platform = json.loads(platform_path.read_text(encoding="utf-8"))
    platform["contracts"]["claimedProfiles"] = ["GB-C0-SCHEMA"]
    platform.pop("contentDigest")
    platform["contentDigest"] = canonical_hash(platform)
    platform_path.write_text(
        json.dumps(platform, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(module.ReleaseBundleError, match="stable conformance contracts"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


def test_release_bundle_rejects_incomplete_build_environment_identity(
    tmp_path: Path,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    platform_path = next(inputs.iterdir()) / "platform-evidence" / "platform.json"
    platform = json.loads(platform_path.read_text(encoding="utf-8"))
    platform["buildEnvironment"]["resolvedDistributions"] = [
        item
        for item in platform["buildEnvironment"]["resolvedDistributions"]
        if item["name"] != "pip"
    ]
    platform.pop("contentDigest")
    platform["contentDigest"] = canonical_hash(platform)
    platform_path.write_text(
        json.dumps(platform, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(module.ReleaseBundleError, match="pinned release tools"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


def test_release_bundle_rejects_sbom_artifact_hash_substitution(tmp_path: Path) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    sbom_path = next(inputs.iterdir()) / "platform-evidence" / "sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    component = next(
        item
        for item in sbom["components"]
        if module._component_properties(item).get("graphblocks:release-artifact") == "true"
    )
    component["hashes"][0]["content"] = "f" * 64
    sbom_path.write_text(json.dumps(sbom), encoding="utf-8")

    with pytest.raises(module.ReleaseBundleError, match="filenames and hashes"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


def test_release_bundle_rejects_sbom_without_dependency_graph(tmp_path: Path) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    sbom_path = next(inputs.iterdir()) / "platform-evidence" / "sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    sbom.pop("dependencies")
    sbom_path.write_text(json.dumps(sbom, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(module.ReleaseBundleError, match="dependency graph"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


@pytest.mark.parametrize("mutation", ("missing-runtime-row", "extra-testing-edge"))
def test_release_bundle_requires_exact_first_party_sbom_dependency_rows(
    tmp_path: Path,
    mutation: str,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    sbom_path = next(inputs.iterdir()) / "platform-evidence" / "sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    if mutation == "missing-runtime-row":
        sbom["dependencies"] = [
            row
            for row in sbom["dependencies"]
            if row["ref"] != "pkg:pypi/graphblocks-runtime@0.1.0"
        ]
        message = "omits installed distribution rows"
    else:
        testing_row = next(
            row
            for row in sbom["dependencies"]
            if row["ref"].startswith("pkg:pypi/graphblocks-testing@")
        )
        testing_row["dependsOn"].append("pkg:pypi/PyYAML@6.0.2")
        message = "exact graphblocks-testing runtime edges"
    sbom_path.write_text(json.dumps(sbom, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(module.ReleaseBundleError, match=message):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


def test_first_party_dependency_manifest_identity_failure_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module.tomllib,
        "loads",
        lambda _source: {"project": {"name": "unexpected", "dependencies": []}},
    )

    with pytest.raises(module.ReleaseBundleError, match="runtime dependencies are invalid"):
        module._first_party_runtime_dependencies()


def test_release_bundle_rejects_sbom_missing_installed_distribution(
    tmp_path: Path,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    platform_path = next(inputs.iterdir()) / "platform-evidence" / "platform.json"
    platform = json.loads(platform_path.read_text(encoding="utf-8"))
    platform["installedDistributions"].append(
        {"name": "unexpected-installed", "version": "9.9.9"}
    )
    platform["installedDistributions"].sort(key=lambda item: item["name"])
    platform.pop("contentDigest")
    platform["contentDigest"] = canonical_hash(platform)
    platform_path.write_text(
        json.dumps(platform, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(module.ReleaseBundleError, match="installed distribution closure"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("not-installed", "not-installed==9.9.9"),
        ("alternate-version", "installed distribution closure"),
        ("duplicate-reference", "duplicate component reference"),
        ("malformed-reference", "malformed component reference"),
        ("missing-dependency-row", "omits installed distribution rows"),
    ),
)
def test_release_bundle_requires_exact_installed_sbom_component_closure(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    sbom_path = next(inputs.iterdir()) / "platform-evidence" / "sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    jsonschema_component = next(
        component
        for component in sbom["components"]
        if component.get("name") == "jsonschema"
    )
    if mutation == "not-installed":
        reference = "pkg:pypi/not-installed@9.9.9"
        sbom["components"].append(
            {
                "type": "library",
                "name": "not-installed",
                "version": "9.9.9",
                "bom-ref": reference,
            }
        )
        sbom["dependencies"].append({"ref": reference, "dependsOn": []})
    elif mutation == "alternate-version":
        alternate = dict(jsonschema_component)
        alternate["version"] = "9.9.9"
        alternate["bom-ref"] = "pkg:pypi/jsonschema@9.9.9"
        sbom["components"].append(alternate)
        sbom["dependencies"].append(
            {"ref": alternate["bom-ref"], "dependsOn": []}
        )
    elif mutation == "duplicate-reference":
        sbom["components"].append(dict(jsonschema_component))
    elif mutation == "malformed-reference":
        malformed = dict(jsonschema_component)
        malformed["bom-ref"] = " "
        sbom["components"].append(malformed)
    else:
        sbom["dependencies"] = [
            relationship
            for relationship in sbom["dependencies"]
            if relationship["ref"] != jsonschema_component["bom-ref"]
        ]
    sbom_path.write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(module.ReleaseBundleError, match=message):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


def test_release_bundle_rejects_cross_platform_sdist_nondeterminism(
    tmp_path: Path,
) -> None:
    module = _load_module()
    inputs = _inputs(module, tmp_path)
    platform_root = sorted(inputs.iterdir())[1]
    sdist = platform_root / "platform-sdists" / f"graphblocks-{RELEASE_VERSION}.tar.gz"
    sdist.write_bytes(b"nondeterministic-sdist")
    digest = module._sha256_bytes(sdist.read_bytes())
    evidence_root = platform_root / "platform-evidence"
    platform_path = evidence_root / "platform.json"
    platform = json.loads(platform_path.read_text(encoding="utf-8"))
    record = next(
        item for item in platform["artifacts"] if item["filename"] == sdist.name
    )
    record["sha256"] = digest
    record["size"] = sdist.stat().st_size
    platform.pop("contentDigest")
    platform["contentDigest"] = canonical_hash(platform)
    platform_path.write_text(json.dumps(platform, sort_keys=True) + "\n", encoding="utf-8")
    sbom_path = evidence_root / "sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    component = next(item for item in sbom["components"] if item.get("name") == sdist.name)
    component["hashes"][0]["content"] = digest
    sbom_path.write_text(json.dumps(sbom, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(module.ReleaseBundleError, match="not deterministic"):
        module.assemble_release_bundle(
            platform_inputs_dir=inputs,
            output_dir=tmp_path / "bundle",
            git_commit=COMMIT,
            release_ref=RELEASE_REF,
            builder_id=BUILDER_ID,
            invocation_id=INVOCATION_ID,
        )


def test_release_bundle_verification_uses_one_snapshot_and_rejects_symlinks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    symlink_or_skip,
) -> None:
    module = _load_module()
    bundle = _assemble(module, tmp_path)
    original = module._snapshot_regular_file
    calls: list[Path] = []

    def recording_snapshot(path: Path, *, owner: str) -> object:
        calls.append(path)
        return original(path, owner=owner)

    monkeypatch.setattr(module, "_snapshot_regular_file", recording_snapshot)
    module.verify_release_bundle(bundle_dir=bundle)
    relative_calls = [
        path.relative_to(bundle).as_posix() for path in calls if path.is_relative_to(bundle)
    ]
    assert len(relative_calls) == len(set(relative_calls))

    manifest = bundle / "release-manifest.json"
    manifest_bytes = manifest.read_bytes()
    manifest.unlink()
    target = tmp_path / "manifest-target.json"
    target.write_bytes(manifest_bytes)
    symlink_or_skip(manifest, target)
    with pytest.raises(module.ReleaseBundleError, match="non-symlink"):
        module.verify_release_bundle(bundle_dir=bundle)


def test_release_bundle_signature_is_in_closure_and_pinned_to_release_workflow_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    symlink_or_skip,
) -> None:
    module = _load_module()
    observe_cosign = module._observe_cosign_identity
    bundle = _assemble(module, tmp_path)
    module._observe_cosign_identity = observe_cosign
    signature = bundle / module.SIGNATURE_BUNDLE_NAME
    signature.write_text("{}", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["check"] is True
        if command[1:] == ["version"]:
            return subprocess.CompletedProcess(command, 0, stdout=COSIGN_OUTPUT + "\n")
        assert Path(command[2]).read_bytes() == (bundle / "release-manifest.json").read_bytes()
        assert Path(command[command.index("--bundle") + 1]).read_bytes() == b"{}"
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    identity = (
        "https://github.com/graphblocks/graphblocks/.github/workflows/ci.yml@"
        "refs/tags/v1.0.0-rc.1"
    )
    module.verify_release_bundle(
        bundle_dir=bundle,
        signature_bundle=signature,
        certificate_identity=identity,
    )
    verify_call = calls[1]
    assert verify_call[verify_call.index("--certificate-identity") + 1] == identity
    assert verify_call[verify_call.index("--certificate-oidc-issuer") + 1] == module.SIGSTORE_ISSUER

    with pytest.raises(module.ReleaseBundleError, match="does not match the release ref"):
        module.verify_release_bundle(
            bundle_dir=bundle,
            signature_bundle=signature,
            certificate_identity=(
                "https://github.com/graphblocks/graphblocks/.github/workflows/ci.yml@"
                "refs/heads/main"
            ),
        )
    with pytest.raises(module.ReleaseBundleError, match="does not match the release ref"):
        module.verify_release_bundle(
            bundle_dir=bundle,
            signature_bundle=signature,
            certificate_identity=(
                "https://github.com/graphblocks/graphblocks/.github/workflows/ci.yml@"
                "refs/tags/v1.0.0"
            ),
        )
    with pytest.raises(module.ReleaseBundleError, match="inside the release closure"):
        module.verify_release_bundle(
            bundle_dir=bundle,
            signature_bundle=tmp_path / "external.sigstore.json",
            certificate_identity=identity,
        )

    signature.unlink()
    outside = tmp_path / "outside.sigstore.json"
    outside.write_text("{}", encoding="utf-8")
    symlink_or_skip(signature, outside)
    with pytest.raises(module.ReleaseBundleError, match="symlink"):
        module.verify_release_bundle(
            bundle_dir=bundle,
            signature_bundle=signature,
            certificate_identity=identity,
        )


def test_release_bundle_wraps_cosign_signature_verification_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    observe_cosign = module._observe_cosign_identity
    bundle = _assemble(module, tmp_path)
    module._observe_cosign_identity = observe_cosign
    signature = bundle / module.SIGNATURE_BUNDLE_NAME
    signature.write_text("{}", encoding="utf-8")

    def failing_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[1:] == ["version"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=COSIGN_OUTPUT + "\n",
            )
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(module.subprocess, "run", failing_run)
    identity = (
        "https://github.com/graphblocks/graphblocks/.github/workflows/ci.yml@"
        "refs/tags/v1.0.0-rc.1"
    )

    with pytest.raises(
        module.ReleaseBundleError,
        match="release manifest signature verification failed",
    ):
        module.verify_release_bundle(
            bundle_dir=bundle,
            signature_bundle=signature,
            certificate_identity=identity,
        )


@pytest.mark.parametrize("pattern", ("*.whl", "*.tar.gz"))
def test_release_bundle_verification_fails_after_artifact_tampering(
    tmp_path: Path,
    pattern: str,
) -> None:
    module = _load_module()
    bundle = _assemble(module, tmp_path)
    artifact = next((bundle / "artifacts").glob(pattern))
    artifact.write_bytes(b"tampered")

    with pytest.raises(module.ReleaseBundleError, match="does not match manifest"):
        module.verify_release_bundle(bundle_dir=bundle)


def test_release_bundle_rejects_unexpected_files(tmp_path: Path) -> None:
    module = _load_module()
    bundle = _assemble(module, tmp_path)
    (bundle / "untracked.txt").write_text("not signed", encoding="utf-8")

    with pytest.raises(module.ReleaseBundleError, match="missing or unexpected files"):
        module.verify_release_bundle(bundle_dir=bundle)


def test_rustc_and_cosign_versions_are_observed_and_fail_closed(tmp_path: Path) -> None:
    module = _load_module()
    verify_path = Path(__file__).parents[1] / "tools" / "verify_wheelhouse.py"
    spec = importlib.util.spec_from_file_location("verify_wheelhouse_for_tools", verify_path)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)

    fake_tool = tmp_path / "fake_tool.py"
    fake_tool.write_text(
        "import sys\n"
        "kind, version = sys.argv[1:3]\n"
        "print(f'rustc {version} (012345678 2026-01-01)' if kind == 'rustc' "
        "else f'GitVersion: v{version}\\nGitCommit: 0123456789abcdef')\n",
        encoding="utf-8",
    )
    rustc = [sys.executable, str(fake_tool), "rustc", "1.94.0"]
    wrong_rustc = [sys.executable, str(fake_tool), "rustc", "1.93.1"]
    cosign = [sys.executable, str(fake_tool), "cosign", "3.0.6"]
    wrong_cosign = [sys.executable, str(fake_tool), "cosign", "3.0.5"]

    assert verifier.observe_rustc_identity(rustc) == RUSTC_IDENTITY
    with pytest.raises(RuntimeError, match="rustc==1.94.0"):
        verifier.observe_rustc_identity(wrong_rustc)
    assert module._observe_cosign_identity(cosign) == COSIGN_IDENTITY
    with pytest.raises(module.ReleaseBundleError, match="Cosign 3.0.6"):
        module._observe_cosign_identity(wrong_cosign)


def test_rustc_observation_reports_sanitized_process_diagnostics(
    tmp_path: Path,
) -> None:
    verify_path = Path(__file__).parents[1] / "tools" / "verify_wheelhouse.py"
    spec = importlib.util.spec_from_file_location(
        "verify_wheelhouse_for_diagnostics", verify_path
    )
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)

    failing_tool = tmp_path / "failing_rustc.py"
    failing_tool.write_text(
        "import sys\n"
        "print('partial\\trustc\\nidentity')\n"
        "print('toolchain\\0sync\\nfailed', file=sys.stderr)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError) as raised:
        verifier.observe_rustc_identity([sys.executable, str(failing_tool)])

    message = str(raised.value)
    assert "rustc --version failed with exit code 7" in message
    assert "stdout='partial rustc identity'" in message
    assert "stderr='toolchain sync failed'" in message
    assert "\n" not in message
    assert "\0" not in message


def test_release_digest_inputs_are_checked_out_with_lf_line_endings() -> None:
    root = Path(__file__).parents[1]
    paths = (
        "tck/durable/cases.json",
        "crates/graphblocks-runtime-durable/tests/fixtures/durable-cases.json",
        "compatibility/stable-testing-cli-contracts.json",
    )

    completed = subprocess.run(
        ["git", "check-attr", "text", "eol", "--", *paths],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )

    for path in paths:
        assert f"{path}: text: set" in completed.stdout
        assert f"{path}: eol: lf" in completed.stdout


def test_release_evidence_snapshot_preserves_arbitrary_precision_json_numbers(
    tmp_path: Path,
) -> None:
    module = _load_module()
    payload = {"ok": True, "observed": {"value": Decimal("1e400")}}
    payload["contentDigest"] = canonical_hash(payload)
    path = tmp_path / "evidence.json"
    path.write_bytes(module._canonical_json_bytes(payload))

    snapshot = module._snapshot_regular_file(path, owner="test evidence")
    observed = module._json_from_snapshot(snapshot, owner="test evidence")

    assert observed["observed"]["value"] == Decimal("1e400")
    assert module._require_content_digest(observed, owner="test evidence") == payload[
        "contentDigest"
    ]


def test_release_evidence_snapshot_uses_binary_mode_when_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    path = tmp_path / "evidence.json"
    path.write_bytes(b'{"line":"one\\r\\ntwo"}\r\n')
    binary_flag = 1 << 29
    opened_flags: list[int] = []
    platform_open = module.os.open
    platform_binary_flag = getattr(module.os, "O_BINARY", 0)

    monkeypatch.setattr(module.os, "O_BINARY", binary_flag, raising=False)

    def recording_open(candidate: Path, flags: int) -> int:
        opened_flags.append(flags)
        return platform_open(
            candidate,
            (flags & ~binary_flag) | platform_binary_flag,
        )

    monkeypatch.setattr(module.os, "open", recording_open)

    snapshot = module._snapshot_regular_file(path, owner="test evidence")

    assert snapshot.data == path.read_bytes()
    assert len(opened_flags) == 1
    assert opened_flags[0] & binary_flag == binary_flag


def test_ci_enforces_pinned_platform_aggregation_and_isolated_release_signing() -> None:
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / ".github" / "workflows" / "ci.yml").read_text())
    jobs = workflow["jobs"]

    for job in jobs.values():
        for step in job.get("steps", []):
            action = step.get("uses")
            if action is None:
                continue
            _repository, separator, revision = action.partition("@")
            assert separator == "@"
            assert re.fullmatch(r"[0-9a-f]{40}", revision), action
            if action.startswith("actions/upload-artifact@"):
                assert "${{ github.run_attempt }}" in step["with"]["name"]

    installed = jobs["installed-artifacts"]
    assert installed["strategy"]["matrix"] == {
        "os": ["ubuntu-latest", "windows-latest"],
        "python-version": ["3.11", "3.12"],
    }
    installed_steps = {step["name"]: step for step in installed["steps"]}
    tooling = installed_steps["Install wheel verification tooling"]["run"]
    assert "pip==25.1.1" in tooling
    assert "build==1.5.1" in tooling
    assert "hatchling==1.31.0" in tooling
    assert "maturin==1.14.1" in tooling
    installed_command = installed_steps[
        "Build once, install, and run installed-artifact gates"
    ]["run"]
    assert "--wheelhouse dist/platform-wheelhouse" in installed_command
    assert "--sdist-dir dist/platform-sdists" in installed_command
    assert "--dependency-wheelhouse dist/platform-dependencies" in installed_command
    assert "--release-evidence-dir dist/platform-evidence" in installed_command
    assert "--sbom-output dist/platform-evidence/sbom.cdx.json" in installed_command
    assert "--rustc rustc" in installed_command
    retained = installed_steps["Retain platform release inputs and conformance evidence"]
    assert "dist/platform-wheelhouse" in retained["with"]["path"]
    assert "dist/platform-sdists" in retained["with"]["path"]
    assert "dist/platform-evidence" in retained["with"]["path"]

    python_steps = {step["name"]: step for step in jobs["python"]["steps"]}
    stable_security_run = python_steps["Run manifest-bound stable security gates"]
    assert stable_security_run["if"] == (
        "${{ matrix.os == 'ubuntu-latest' && matrix.python-version == '3.11' }}"
    )
    assert stable_security_run["env"] == {
        "SECURITY_GATE_RUNNER_OS": "${{ matrix.os }}",
        "SECURITY_GATE_RUNNER_PYTHON": "${{ matrix.python-version }}",
        "SECURITY_GATE_ARTIFACT_NAME": (
            "graphblocks-stable-security-gates-${{ github.run_attempt }}"
        ),
    }
    assert "python tools/stable_security_gates.py" in stable_security_run["run"]
    assert "--candidate-commit \"$GITHUB_SHA\"" in stable_security_run["run"]
    assert '--runner-os "$SECURITY_GATE_RUNNER_OS"' in stable_security_run["run"]
    assert '--runner-python "$SECURITY_GATE_RUNNER_PYTHON"' in stable_security_run["run"]
    assert '--artifact-name "$SECURITY_GATE_ARTIFACT_NAME"' in stable_security_run[
        "run"
    ]
    stable_security_upload = python_steps[
        "Retain manifest-bound stable security-gate evidence"
    ]
    assert stable_security_upload["with"]["name"] == (
        "graphblocks-stable-security-gates-${{ github.run_attempt }}"
    )
    assert "dist/ci/stable-security-gates.json" in stable_security_upload["with"][
        "path"
    ]
    assert "dist/ci/stable-security-gates.xml" in stable_security_upload["with"][
        "path"
    ]

    ref_gate = jobs["release-ref-gate"]
    assert ref_gate["permissions"] == {}
    assert "github.repository == 'graphblocks/graphblocks'" in ref_gate["if"]
    assert "startsWith(github.ref, 'refs/tags/v1.0.0')" in ref_gate["if"]
    release_ref_pattern = ref_gate["env"]["RELEASE_REF_PATTERN"]
    assert release_ref_pattern == r"^refs/tags/v1\.0\.0(-rc\.[1-9][0-9]*)?$"
    for allowed_ref in (
        "refs/tags/v1.0.0",
        "refs/tags/v1.0.0-rc.1",
        "refs/tags/v1.0.0-rc.10",
    ):
        assert re.fullmatch(release_ref_pattern, allowed_ref)
    for rejected_ref in (
        "refs/tags/v1.0.0-rc.0",
        "refs/tags/v1.0.0-rc.01",
        "refs/tags/v1.0.0-rc.foo",
        "refs/tags/v1.0.0-rc.1.0",
        "refs/tags/v1.0.0-preview.1",
        "refs/tags/v1.0.1",
    ):
        assert re.fullmatch(release_ref_pattern, rejected_ref) is None
    assert ref_gate["outputs"] == {
        "release_ref": "${{ steps.release_ref.outputs.release_ref }}"
    }
    ref_gate_step = ref_gate["steps"][0]
    assert ref_gate_step["id"] == "release_ref"
    assert '[[ ! "$GITHUB_REF" =~ $RELEASE_REF_PATTERN ]]' in ref_gate_step["run"]
    assert "GITHUB_OUTPUT" in ref_gate_step["run"]

    aggregate = jobs["release-evidence"]
    assert aggregate["needs"] == [
        "required-gates",
        "release-ref-gate",
    ]
    assert "needs.required-gates.result == 'success'" in aggregate["if"]
    assert (
        "needs.release-ref-gate.outputs.release_ref == github.ref"
        in aggregate["if"]
    )
    assert aggregate["permissions"] == {"contents": "read"}
    assert "id-token" not in json.dumps(aggregate)
    aggregate_steps = {step["name"]: step for step in aggregate["steps"]}
    assert aggregate_steps["Check out repository"]["with"] == {"fetch-depth": 0}
    download = aggregate_steps["Download exact supported-platform release inputs"]
    assert download["with"]["pattern"] == (
        "graphblocks-release-input-*-attempt-${{ github.run_attempt }}"
    )
    security_download = aggregate_steps[
        "Download exact manifest-bound stable security-gate evidence"
    ]
    assert security_download["with"] == {
        "name": "graphblocks-stable-security-gates-${{ github.run_attempt }}",
        "path": "dist/stable-security-gates",
    }
    assemble = aggregate_steps["Assemble and verify the offline release bundle"]["run"]
    assert "--platform-inputs-dir dist/platform-inputs" in assemble
    assert '--release-ref "$GITHUB_REF"' in assemble
    assert '[[ "$GITHUB_REF" == "refs/tags/v1.0.0" ]]' in assemble
    assert 'promotion_args=(--promotion-evidence "$PROMOTION_EVIDENCE_PATH")' in assemble
    assert '"${promotion_args[@]}"' in assemble
    assert "--cosign cosign" in assemble
    assert aggregate_steps["Assemble and verify the offline release bundle"]["env"][
        "PROMOTION_EVIDENCE_PATH"
    ] == "docs/project/releases/v1.0.0-promotion-evidence.json"
    assert not (
        root / "docs" / "project" / "releases" / "v1.0.0-promotion-evidence.json"
    ).exists()
    freeze_matrix = aggregate_steps[
        "Freeze the successful candidate matrix attestation"
    ]
    assert freeze_matrix["if"] == "startsWith(github.ref, 'refs/tags/v1.0.0-rc.')"
    assert freeze_matrix["env"] == {
        "RUN_ATTEMPT_ID": "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}/attempts/${{ github.run_attempt }}"
    }
    freeze_matrix_command = freeze_matrix["run"]
    assert "freeze-candidate-matrix-report" in freeze_matrix_command
    assert '--candidate-ref "$GITHUB_REF"' in freeze_matrix_command
    assert '--candidate-commit "$GITHUB_SHA"' in freeze_matrix_command
    assert '--run-id "$RUN_ATTEMPT_ID"' in freeze_matrix_command
    assert (
        "--security-gate-result "
        "dist/stable-security-gates/stable-security-gates.json"
        in freeze_matrix_command
    )
    assert (
        "--security-gate-junit "
        "dist/stable-security-gates/stable-security-gates.xml"
        in freeze_matrix_command
    )
    frozen_matrix_upload = aggregate_steps[
        "Retain the exact frozen candidate matrix attestation"
    ]
    assert frozen_matrix_upload["if"] == (
        "startsWith(github.ref, 'refs/tags/v1.0.0-rc.')"
    )
    assert frozen_matrix_upload["with"]["name"] == (
        "graphblocks-frozen-candidate-matrix-report-${{ github.run_attempt }}"
    )
    assert frozen_matrix_upload["with"]["path"] == (
        "dist/frozen-candidate-matrix-report/report.json"
    )
    unsigned_upload = aggregate_steps["Retain frozen unsigned release bundle"]
    assert unsigned_upload["with"]["name"] == (
        "graphblocks-unsigned-release-candidate-bundle-${{ github.run_attempt }}"
    )
    assert unsigned_upload["with"]["path"] == "dist/release-bundle"

    matrix_signing = jobs["candidate-matrix-signing"]
    assert matrix_signing["needs"] == ["release-ref-gate", "release-evidence"]
    assert "needs.release-ref-gate.outputs.release_ref == github.ref" in matrix_signing[
        "if"
    ]
    assert "startsWith(github.ref, 'refs/tags/v1.0.0-rc.')" in matrix_signing[
        "if"
    ]
    assert matrix_signing["permissions"] == {"id-token": "write"}
    matrix_signing_steps = {
        step["name"]: step for step in matrix_signing["steps"]
    }
    matrix_signing_actions = [
        step["uses"] for step in matrix_signing["steps"] if "uses" in step
    ]
    assert matrix_signing_actions == [
        "actions/download-artifact@018cc2cf5baa6db3ef3c5f8a56943fffe632ef53",
        "sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6",
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f",
    ]
    matrix_download = matrix_signing_steps[
        "Download the exact frozen candidate matrix attestation"
    ]
    assert matrix_download["with"] == {
        "name": "graphblocks-frozen-candidate-matrix-report-${{ github.run_attempt }}",
        "path": "dist/signed-candidate-matrix-report",
    }
    matrix_signing_command = matrix_signing_steps[
        "Keyless-sign and directly verify the fixed matrix attestation"
    ]
    assert matrix_signing_command["env"] == {
        "CERTIFICATE_IDENTITY": "https://github.com/graphblocks/graphblocks/.github/workflows/ci.yml@${{ needs.release-ref-gate.outputs.release_ref }}",
        "CERTIFICATE_OIDC_ISSUER": "https://token.actions.githubusercontent.com",
    }
    matrix_command = matrix_signing_command["run"]
    assert matrix_command.count("cosign ") == 2
    assert matrix_command.count(
        "dist/signed-candidate-matrix-report/report.json"
    ) == 2
    assert matrix_command.count(
        "dist/signed-candidate-matrix-report/report.sigstore.json"
    ) == 2
    all_matrix_signing_commands = "\n".join(
        step["run"] for step in matrix_signing["steps"] if "run" in step
    ).lower()
    for forbidden in ("python", "pip", "install -e", "tools/", "release_supply_chain"):
        assert forbidden not in all_matrix_signing_commands
    matrix_upload = matrix_signing_steps[
        "Retain the signed candidate matrix attestation"
    ]
    assert matrix_upload["with"]["name"] == (
        "graphblocks-signed-candidate-matrix-report-"
        "${{ github.run_id }}-${{ github.run_attempt }}"
    )
    assert matrix_upload["with"]["path"].splitlines() == [
        "dist/signed-candidate-matrix-report/report.json",
        "dist/signed-candidate-matrix-report/report.sigstore.json",
    ]

    signing = jobs["release-signing"]
    assert signing["needs"] == ["release-ref-gate", "release-evidence"]
    assert signing["if"] == "needs.release-ref-gate.outputs.release_ref == github.ref"
    assert signing["permissions"] == {"id-token": "write"}
    signing_steps = {step["name"]: step for step in signing["steps"]}
    signing_actions = [step["uses"] for step in signing["steps"] if "uses" in step]
    assert signing_actions == [
        "actions/download-artifact@018cc2cf5baa6db3ef3c5f8a56943fffe632ef53",
        "sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6",
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f",
    ]
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action) for action in signing_actions)
    exact_download = signing_steps["Download exact frozen unsigned release bundle"]
    assert exact_download["with"] == {
        "name": "graphblocks-unsigned-release-candidate-bundle-${{ github.run_attempt }}",
        "path": "dist/release-bundle",
    }
    cosign_install = signing_steps["Install pinned Cosign"]
    assert cosign_install["with"] == {"cosign-release": "v3.0.6"}
    signing_command = signing_steps[
        "Keyless-sign and directly verify the fixed release manifest"
    ]
    assert signing_command["env"] == {
        "CERTIFICATE_IDENTITY": "https://github.com/graphblocks/graphblocks/.github/workflows/ci.yml@${{ needs.release-ref-gate.outputs.release_ref }}",
        "CERTIFICATE_OIDC_ISSUER": "https://token.actions.githubusercontent.com",
    }
    command = signing_command["run"]
    assert command.count("cosign ") == 2
    assert "cosign sign-blob" in command
    assert "cosign verify-blob" in command
    assert "--certificate-identity \"$CERTIFICATE_IDENTITY\"" in command
    assert "--certificate-oidc-issuer \"$CERTIFICATE_OIDC_ISSUER\"" in command
    assert command.count("dist/release-bundle/release-manifest.json") == 2
    assert command.count("dist/release-bundle/release-manifest.sigstore.json") == 2
    all_signing_commands = "\n".join(
        step["run"] for step in signing["steps"] if "run" in step
    ).lower()
    for forbidden in ("python", "pip", "install -e", "tools/", "release_supply_chain"):
        assert forbidden not in all_signing_commands
    signed_upload = signing_steps[
        "Retain signed release artifacts, evidence, and attestations"
    ]
    assert signed_upload["with"]["name"] == (
        "graphblocks-release-candidate-bundle-${{ github.run_attempt }}"
    )
    assert signed_upload["with"]["path"] == "dist/release-bundle"


def test_ci_primes_offline_rust_inputs_and_retains_failure_diagnostics() -> None:
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / ".github" / "workflows" / "ci.yml").read_text())
    jobs = workflow["jobs"]

    for job_name in ("python", "installed-artifacts", "examples", "rust"):
        steps = {step["name"]: step for step in jobs[job_name]["steps"]}
        setup = steps["Set up pinned Rust"]["run"]
        assert (
            "rustup toolchain install 1.94.0 --profile minimal "
            "--component clippy,rustfmt"
        ) in setup
        assert "rustup default 1.94.0" in setup
        preflight = steps["Verify pinned Rust toolchain"]["run"]
        assert "rustup run 1.94.0 rustc --version" in preflight
        assert "rustup run 1.94.0 cargo --version" in preflight
        assert "rustup run 1.94.0 cargo clippy --version" in preflight
        assert "rustup run 1.94.0 cargo fmt --version" in preflight

    python_job = jobs["python"]
    python_steps = {step["name"]: step for step in python_job["steps"]}
    assert python_job["env"]["PYTHONPATH"] == (
        "${{ github.workspace }}/packages/graphblocks-testing/src"
    )
    assert "Install GraphBlocks testing package" not in python_steps
    cargo_fetch = python_steps["Prime offline Rust example dependencies"]["run"]
    assert "cargo fetch --locked --manifest-path" in cargo_fetch
    assert (
        "examples/01-enterprise-federated-rag/1-3-rust-runtime/Cargo.toml"
        in cargo_fetch
    )
    assert "examples/12-custom-python-rust-blocks/rust/Cargo.toml" in cargo_fetch
    compatibility = python_steps["Check candidate stable API and CLI snapshots"]["run"]
    assert "dist/ci/compatibility.log" in compatibility
    python_tests = python_steps["Run Python tests"]["run"]
    assert "--junitxml=dist/ci/python-tests.xml" in python_tests
    assert "dist/ci/python-tests.log" in python_tests
    python_diagnostics = python_steps["Retain Python CI diagnostics"]
    assert python_diagnostics["if"] == "always()"
    assert python_diagnostics["with"]["if-no-files-found"] == "warn"

    installed_steps = {
        step["name"]: step for step in jobs["installed-artifacts"]["steps"]
    }
    installed_gate = installed_steps[
        "Build once, install, and run installed-artifact gates"
    ]["run"]
    assert "dist/ci/verify-wheelhouse.log" in installed_gate
    installed_diagnostics = installed_steps["Retain installed-artifact diagnostics"]
    assert installed_diagnostics["if"] == "always()"
    assert installed_diagnostics["with"]["if-no-files-found"] == "warn"

    example_steps = {step["name"]: step for step in jobs["examples"]["steps"]}
    example_dependencies = example_steps["Install test dependencies"]["run"]
    assert "python -m pip install maturin==1.14.1" in example_dependencies
    native_install = example_steps["Install native compiler target"]["run"]
    assert native_install == (
        "python -m pip install --no-build-isolation --no-deps --editable "
        "./packages/graphblocks-runtime"
    )
    example_fetch = example_steps["Prime offline Rust example dependencies"]["run"]
    assert "cargo fetch --locked --manifest-path" in example_fetch
    assert (
        "examples/01-enterprise-federated-rag/1-3-rust-runtime/Cargo.toml"
        in example_fetch
    )
    assert "examples/12-custom-python-rust-blocks/rust/Cargo.toml" in example_fetch
    example_tests = example_steps["Run example integration tests"]["run"]
    assert "--junitxml=dist/ci/example-tests.xml" in example_tests
    assert "dist/ci/example-tests.log" in example_tests
    example_diagnostics = example_steps["Retain example CI diagnostics"]
    assert example_diagnostics["if"] == "always()"
    assert example_diagnostics["with"]["if-no-files-found"] == "warn"

    required = jobs["required-gates"]
    assert required["needs"] == ["python", "installed-artifacts", "examples", "rust"]
    assert required["if"] == "always()"
    assert required["permissions"] == {}
    required_step = required["steps"][0]
    assert required_step["env"] == {
        "PYTHON_RESULT": "${{ needs.python.result }}",
        "INSTALLED_ARTIFACTS_RESULT": "${{ needs.installed-artifacts.result }}",
        "EXAMPLES_RESULT": "${{ needs.examples.result }}",
        "RUST_RESULT": "${{ needs.rust.result }}",
    }
    for variable in (
        "PYTHON_RESULT",
        "INSTALLED_ARTIFACTS_RESULT",
        "EXAMPLES_RESULT",
        "RUST_RESULT",
    ):
        assert variable in required_step["run"]


def test_ci_retains_each_rust_gate_log_on_failure() -> None:
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / ".github" / "workflows" / "ci.yml").read_text())
    rust_steps = {
        step["name"]: step for step in workflow["jobs"]["rust"]["steps"]
    }

    expected_logs = {
        "Check Rust formatting": "dist/ci/rust-fmt.log",
        "Run Clippy": "dist/ci/rust-clippy.log",
        "Run Rust tests": "dist/ci/rust-tests.log",
        "Verify Rust packages": "dist/ci/rust-packages.log",
    }
    for step_name, log_path in expected_logs.items():
        command = rust_steps[step_name]["run"]
        assert "set -o pipefail" in command
        assert f"2>&1 | tee {log_path}" in command

    diagnostics = rust_steps["Retain Rust CI diagnostics"]
    assert diagnostics["if"] == "always()"
    assert diagnostics["with"] == {
        "name": "graphblocks-ci-rust-attempt-${{ github.run_attempt }}",
        "path": "dist/ci",
        "if-no-files-found": "warn",
        "retention-days": 30,
    }


def test_release_candidate_tag_workflow_only_tags_green_main_sha() -> None:
    root = Path(__file__).parents[1]
    workflow_path = root / ".github" / "workflows" / "cut-release-candidate.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))

    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"] == {
        "candidate_number": {
            "description": "Positive RC sequence number for v1.0.0-rc.N",
            "required": True,
            "type": "string",
        },
        "commit_sha": {
            "description": "Exact 40-character main commit SHA to promote",
            "required": True,
            "type": "string",
        },
    }
    assert workflow["permissions"] == {}

    jobs = workflow["jobs"]
    admission = jobs["admit-green-sha"]
    assert admission["permissions"] == {"actions": "read", "contents": "read"}
    assert "github.repository == 'graphblocks/graphblocks'" in admission["if"]
    assert "github.ref == 'refs/heads/main'" in admission["if"]
    admission_step = admission["steps"][0]
    admission_command = admission_step["run"]
    assert "actions/workflows/ci.yml/runs" in admission_command
    assert '.head_branch == "main"' in admission_command
    assert '.event == "push"' in admission_command
    assert '.head_sha == $sha' in admission_command
    assert '.conclusion == "success"' in admission_command
    assert '.name == "Required gates"' in admission_command
    assert "refs/tags/v1.0.0-rc.$CANDIDATE_NUMBER" in admission_command

    creation = jobs["create-candidate-tag"]
    assert creation["needs"] == ["admit-green-sha"]
    assert creation["permissions"] == {"contents": "write"}
    assert "needs.admit-green-sha.outputs.candidate_ref" in creation["if"]
    creation_step = creation["steps"][0]
    assert creation_step["env"]["CANDIDATE_REF"] == (
        "${{ needs.admit-green-sha.outputs.candidate_ref }}"
    )
    assert creation_step["env"]["CANDIDATE_SHA"] == (
        "${{ needs.admit-green-sha.outputs.candidate_sha }}"
    )
    assert "git/refs" in creation_step["run"]
    assert '"sha": $sha' in creation_step["run"]


def test_candidate_promotion_report_workflow_freezes_before_isolated_signing() -> None:
    root = Path(__file__).parents[1]
    workflow_path = root / ".github" / "workflows" / "promotion-reports.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))

    assert set(triggers) == {"workflow_dispatch"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    module = _load_module()
    assert inputs["report_type"]["options"] == list(module.PROMOTION_REPORT_TYPES)
    assert "matrix-run" not in inputs["report_type"]["options"]
    assert inputs["report_json"] == {
        "description": (
            "Public JSON report; review identity must equal the dispatcher and security "
            "review must bind candidate matrix digests"
        ),
        "required": True,
        "type": "string",
    }
    assert workflow["permissions"] == {}

    jobs = workflow["jobs"]
    for job in jobs.values():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/upload-artifact@"):
                assert "${{ github.run_attempt }}" in step["with"]["name"]
    validation = jobs["validate-report"]
    assert validation["permissions"] == {"contents": "read"}
    assert "id-token" not in json.dumps(validation)
    assert "github.repository == 'graphblocks/graphblocks'" in validation["if"]
    assert "startsWith(github.ref, 'refs/tags/v1.0.0-rc.')" in validation["if"]
    validation_steps = {step["name"]: step for step in validation["steps"]}
    freeze_command = validation_steps["Validate and freeze the public report"]["run"]
    assert "freeze-promotion-report" in freeze_command
    assert "--candidate-ref \"$GITHUB_REF\"" in freeze_command
    assert "--candidate-commit \"$GITHUB_SHA\"" in freeze_command
    assert "--workflow-actor \"$GITHUB_ACTOR\"" in freeze_command
    frozen_upload = validation_steps[
        "Retain the exact frozen report for the signing boundary"
    ]
    assert frozen_upload["with"]["name"] == (
        "graphblocks-frozen-promotion-report-${{ github.run_attempt }}"
    )
    assert frozen_upload["with"]["path"] == "dist/frozen-promotion-report/report.json"

    signing = jobs["sign-report"]
    assert signing["needs"] == ["validate-report"]
    assert signing["permissions"] == {"id-token": "write"}
    signing_steps = {step["name"]: step for step in signing["steps"]}
    signing_actions = [step["uses"] for step in signing["steps"] if "uses" in step]
    assert signing_actions == [
        "actions/download-artifact@018cc2cf5baa6db3ef3c5f8a56943fffe632ef53",
        "sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6",
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f",
    ]
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action) for action in signing_actions)
    exact_download = signing_steps["Download the exact frozen report"]
    assert exact_download["with"] == {
        "name": "graphblocks-frozen-promotion-report-${{ github.run_attempt }}",
        "path": "dist/signed-promotion-report",
    }
    signing_command = signing_steps[
        "Keyless-sign and directly verify the fixed promotion report"
    ]
    assert signing_command["env"] == {
        "CERTIFICATE_IDENTITY": "https://github.com/graphblocks/graphblocks/.github/workflows/promotion-reports.yml@${{ needs.validate-report.outputs.candidate_ref }}",
        "CERTIFICATE_OIDC_ISSUER": "https://token.actions.githubusercontent.com",
    }
    command = signing_command["run"]
    assert command.count("cosign ") == 2
    assert command.count("dist/signed-promotion-report/report.json") == 2
    assert command.count("dist/signed-promotion-report/report.sigstore.json") == 2
    all_signing_commands = "\n".join(
        step["run"] for step in signing["steps"] if "run" in step
    ).lower()
    for forbidden in ("python", "pip", "install -e", "tools/", "release_supply_chain"):
        assert forbidden not in all_signing_commands

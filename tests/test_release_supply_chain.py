from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType

import pytest
import yaml

from graphblocks.canonical import canonical_hash


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
        {"path": "src/graphblocks/__init__.py", "status": "M"},
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
        reports[suite] = {
            "ok": True,
            "evidence": {
                "fixture_digest": expectation["fixture_digest"],
                "implementation": expectation["implementation"],
                "implementation_version": expectation["implementation_version"],
                "suite": suite,
                "case_ids_digest": expectation["case_ids_digest"],
                "suite_manifest_digest": expectation["suite_manifest_digest"],
            },
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
            {"ref": testing_ref, "dependsOn": [graphblocks_ref]},
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


def _promotion_payload_and_files(
    module: ModuleType,
) -> tuple[dict[str, object], dict[str, bytes]]:
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
        }
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
        }
        for index in range(1, 4)
    ]
    for index, run in enumerate(matrix_runs, start=1):
        reports[f"matrix-run-{index}"] = dict(run)
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
    for review_name, reviewer in (
        ("api", "reviewer-api@example.test"),
        ("security", "reviewer-security@example.test"),
    ):
        reports[f"{review_name}-review"] = {
            "reviewerIdentity": reviewer,
            "approved": True,
            "candidateRef": RELEASE_REF,
            "candidateCommit": CANDIDATE_COMMIT,
        }
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
            else promotion_workflow_identity
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
        "supportedMatrixRuns": [
            {
                **run,
                "attestationDigest": report_digests[f"matrix-run-{index}"],
            }
            for index, run in enumerate(matrix_runs, start=1)
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
    _baseline, report_files = _promotion_payload_and_files(module)
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
    (repository / "src" / "graphblocks" / "__init__.py").write_text(
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
    (repository / "src" / "graphblocks" / "__init__.py").write_text(
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
        {"path": "src/graphblocks/__init__.py", "status": "M"},
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


@pytest.mark.parametrize(
    ("substitution", "message"),
    (
        ("final-source", "exact final ref and version"),
        ("source-diff", "does not match the candidate and final commits"),
        ("candidate-manifest", "does not resolve to a signed report"),
        ("short-soak", "at least 14 days"),
        ("reviewer", "reviews must be independent"),
        ("noncanonical-digest", "lowercase SHA-256 digest"),
        ("defect", "zero unresolved"),
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

    assert len(verified) == 22
    assert len({name for name, _identity in verified}) == 11
    ci_identity = (
        "https://github.com/graphblocks/graphblocks/.github/workflows/"
        "ci.yml@refs/tags/v1.0.0-rc.1"
    )
    promotion_identity = (
        "https://github.com/graphblocks/graphblocks/.github/workflows/"
        "promotion-reports.yml@refs/tags/v1.0.0-rc.1"
    )
    assert [identity for _name, identity in verified].count(ci_identity) == 6
    assert [identity for _name, identity in verified].count(promotion_identity) == 16

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
    )

    assert frozen.path == output_dir / "report.json"
    assert frozen.data == module._canonical_json_bytes(payload)
    assert frozen.sha256 == module._sha256_bytes(frozen.data)


def test_candidate_ci_freezes_one_canonical_matrix_run_attestation(
    tmp_path: Path,
) -> None:
    module = _load_module()
    run_id = (
        "https://github.com/graphblocks/graphblocks/actions/runs/123456/attempts/2"
    )

    frozen = module.freeze_candidate_matrix_report(
        output_dir=tmp_path / "frozen",
        candidate_ref=RELEASE_REF,
        candidate_commit=CANDIDATE_COMMIT,
        run_id=run_id,
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
    }
    assert frozen.data == module._canonical_json_bytes(expected)


def test_candidate_ci_rejects_a_noncanonical_matrix_run_identity(tmp_path: Path) -> None:
    module = _load_module()

    with pytest.raises(module.ReleaseBundleError, match="run-attempt identity"):
        module.freeze_candidate_matrix_report(
            output_dir=tmp_path / "frozen",
            candidate_ref=RELEASE_REF,
            candidate_commit=CANDIDATE_COMMIT,
            run_id="matrix-run-1",
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
    }
    dependency_graph = {
        relationship["ref"]: set(relationship["dependsOn"])
        for relationship in sbom["dependencies"]
    }
    graphblocks_ref = f"pkg:pypi/graphblocks@{RELEASE_VERSION}"
    assert {
        f"pkg:pypi/jsonschema@4.25.1",
        f"pkg:pypi/packaging@25.0",
        f"pkg:pypi/PyYAML@6.0.2",
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
        {"name": "referencing", "version": "0.36.2"}
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
        "python",
        "installed-artifacts",
        "examples",
        "rust",
        "release-ref-gate",
    ]
    assert aggregate["if"] == "needs.release-ref-gate.outputs.release_ref == github.ref"
    assert aggregate["permissions"] == {"contents": "read"}
    assert "id-token" not in json.dumps(aggregate)
    aggregate_steps = {step["name"]: step for step in aggregate["steps"]}
    assert aggregate_steps["Check out repository"]["with"] == {"fetch-depth": 0}
    download = aggregate_steps["Download exact supported-platform release inputs"]
    assert download["with"]["pattern"] == "graphblocks-release-input-*"
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
    frozen_matrix_upload = aggregate_steps[
        "Retain the exact frozen candidate matrix attestation"
    ]
    assert frozen_matrix_upload["if"] == (
        "startsWith(github.ref, 'refs/tags/v1.0.0-rc.')"
    )
    assert frozen_matrix_upload["with"]["name"] == (
        "graphblocks-frozen-candidate-matrix-report"
    )
    assert frozen_matrix_upload["with"]["path"] == (
        "dist/frozen-candidate-matrix-report/report.json"
    )
    unsigned_upload = aggregate_steps["Retain frozen unsigned release bundle"]
    assert unsigned_upload["with"]["name"] == (
        "graphblocks-unsigned-release-candidate-bundle"
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
        "name": "graphblocks-frozen-candidate-matrix-report",
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
        "name": "graphblocks-unsigned-release-candidate-bundle",
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
    assert signed_upload["with"]["name"] == "graphblocks-release-candidate-bundle"
    assert signed_upload["with"]["path"] == "dist/release-bundle"


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
        "description": "Public JSON report content; the validation job canonicalizes it",
        "required": True,
        "type": "string",
    }
    assert workflow["permissions"] == {}

    jobs = workflow["jobs"]
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
    frozen_upload = validation_steps[
        "Retain the exact frozen report for the signing boundary"
    ]
    assert frozen_upload["with"]["name"] == "graphblocks-frozen-promotion-report"
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
        "name": "graphblocks-frozen-promotion-report",
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

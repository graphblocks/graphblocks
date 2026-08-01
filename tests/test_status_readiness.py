from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).parents[1]
MATRIX_PATH = ROOT / "docs" / "project" / "stable-release-matrix.yaml"
STATUS_PATH = ROOT / "docs" / "project" / "status.md"
GENERATOR = ROOT / "tools" / "generate_status_readiness.py"


def _write_fixture(
    root: Path,
    matrix: dict[str, object],
    status: str,
) -> None:
    matrix_path = root / "docs" / "project" / MATRIX_PATH.name
    status_path = root / "docs" / "project" / STATUS_PATH.name
    matrix_path.parent.mkdir(parents=True)
    matrix_path.write_text(yaml.safe_dump(matrix), encoding="utf-8")
    status_path.write_text(status, encoding="utf-8")


def _run_generator(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR), "--root", str(root), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_readiness_axes_are_independent_generated_and_release_blocking(
    tmp_path: Path,
) -> None:
    matrix = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))
    readiness = matrix["readinessAxes"]
    assert set(readiness) == {
        "formatVersion",
        "generatedBy",
        "statusDocument",
        "axes",
    }
    assert readiness["formatVersion"] == 1
    assert readiness["generatedBy"] == "tools/generate_status_readiness.py"
    assert readiness["statusDocument"] == "docs/project/status.md"

    axes = readiness["axes"]
    assert [axis["id"] for axis in axes] == [
        "supply-chain",
        "api",
        "runtime-security",
        "durability",
        "adapters",
    ]
    assert all(
        set(axis)
        == {
            "id",
            "label",
            "readiness",
            "primaryGates",
            "targetReleaseClaim",
            "blocksTargetRelease",
            "shippedP0P1RemainBlocking",
            "claimBoundary",
        }
        for axis in axes
    )
    release_gates = {gate["id"]: gate for gate in matrix["releaseGates"]}
    assert all(
        gate_id in release_gates for axis in axes for gate_id in axis["primaryGates"]
    )
    assert all(axis["shippedP0P1RemainBlocking"] is True for axis in axes)

    by_id = {axis["id"]: axis for axis in axes}
    assert by_id["supply-chain"]["readiness"] == (
        "promotion-contract-enforced-external-evidence-absent"
    )
    assert by_id["supply-chain"]["primaryGates"] == ["REL-SUPPLY-CHAIN"]
    assert by_id["supply-chain"]["targetReleaseClaim"] == (
        "release-wide-artifact-supply-chain"
    )
    assert by_id["supply-chain"]["blocksTargetRelease"] is True
    assert by_id["supply-chain"]["claimBoundary"] == (
        "Artifact integrity, provenance, signing, and promotion only; this does not "
        "imply runtime security, durability, or adapter readiness."
    )
    assert by_id["runtime-security"]["readiness"] == "remediation-blocked"
    assert by_id["runtime-security"]["primaryGates"] == [
        "REL-AUDIT-REMEDIATION",
        "REL-OBJECT-AUTHORIZATION-REVIEW",
        "REL-ADVERSARIAL-RESOURCE-TESTS",
    ]
    assert by_id["runtime-security"]["blocksTargetRelease"] is True
    assert by_id["durability"]["readiness"] == (
        "core-c1-authority-blocked-production-preview"
    )
    assert by_id["durability"]["targetReleaseClaim"] == (
        "core-c1-local-runtime-authority"
    )
    assert by_id["durability"]["blocksTargetRelease"] is True
    assert release_gates["REL-NORMATIVE-AUTHORITY"]["blocksTargetRelease"] is True
    assert by_id["adapters"]["readiness"] == "contract-only-no-real-adapters"
    assert by_id["adapters"]["blocksTargetRelease"] is False

    assert {
        "REL-OBJECT-AUTHORIZATION-REVIEW",
        "REL-ADVERSARIAL-RESOURCE-TESTS",
    } <= set(matrix["globalRequiredGates"])
    object_review_gate = release_gates["REL-OBJECT-AUTHORIZATION-REVIEW"]
    resource_gate = release_gates["REL-ADVERSARIAL-RESOURCE-TESTS"]
    assert object_review_gate["blocksTargetRelease"] is True
    assert resource_gate["blocksTargetRelease"] is True
    assert object_review_gate["scope"] == [
        "run-create-list-status-delete-attach-detach-events-and-streams",
        "run-cancel-pause-resume-and-expire",
        "subscription-create-revoke-and-event-acknowledgement",
        "callback-submit-register-and-revoke",
        "delivery-redrive-and-dead-letter",
    ]
    assert resource_gate["categories"] == [
        "request-body-and-routing",
        "response-body-and-streaming",
        "schema-and-regex",
        "yaml-parser-bounds",
        "canonical-numbers",
    ]

    audit_evidence = set(
        release_gates["REL-AUDIT-REMEDIATION"]["exitCriteria"]["requiredEvidence"]
    )
    assert {
        "independent-object-authorization-review",
        "adversarial-request-response-schema-canonical-resource-testing",
    } <= audit_evidence
    first_stable = (ROOT / "docs" / "project" / "first-stable-release.md").read_text(
        encoding="utf-8"
    )
    first_stable_words = " ".join(first_stable.split())
    assert "independent object-authorization review" in first_stable
    assert (
        "adversarial request, response, schema, YAML-parser, and canonical "
        "resource-budget"
    ) in first_stable_words

    checked = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr

    status = STATUS_PATH.read_text(encoding="utf-8").replace(
        "`remediation-blocked`",
        "`ready`",
        1,
    )
    _write_fixture(tmp_path, matrix, status)
    stale = _run_generator(tmp_path)
    assert stale.returncode == 1
    assert "generated readiness projection is stale" in stale.stderr


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        ("duplicate-axis", "missing, reordered, or relabeled"),
        ("unknown-gate", "unknown primary gates"),
        ("nonblocking-blocking-gate", "cannot exclude target release"),
        ("unknown-field", "invalid shape"),
        ("duplicate-marker", "one readiness marker pair"),
    ),
)
def test_readiness_generator_rejects_invalid_authority_and_markers(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    matrix = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))
    status = STATUS_PATH.read_text(encoding="utf-8")
    if mutation == "duplicate-axis":
        matrix["readinessAxes"]["axes"][1]["id"] = "supply-chain"
    elif mutation == "unknown-gate":
        matrix["readinessAxes"]["axes"][0]["primaryGates"][0] = "REL-UNKNOWN"
    elif mutation == "nonblocking-blocking-gate":
        matrix["readinessAxes"]["axes"][3]["blocksTargetRelease"] = False
    elif mutation == "unknown-field":
        matrix["readinessAxes"]["axes"][0]["score"] = 100
    else:
        status += "\n<!-- BEGIN GENERATED READINESS AXES -->\n"

    _write_fixture(tmp_path, matrix, status)
    checked = _run_generator(tmp_path)
    assert checked.returncode == 2
    assert error in checked.stderr

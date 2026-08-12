from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from typing import cast

import pytest

from tools.audited_source_claim import IdentifiedGitAuditedSource, ProvenanceBinding
from tools.audited_source_evidence import AuditArtifactBinding
from tools.audited_source_provenance import (
    AuditedSourceProvenanceError,
    ProvenanceTrustPolicy,
    UnavailableProvenanceTrustPolicy,
    canonical_provenance_attestation_bytes,
    decode_provenance_trust_policy,
    provenance_trust_policy_claim,
    verify_audited_source_provenance,
)


AUDIT_ARTIFACTS = {
    "reportDigest": "sha256:" + "a" * 64,
    "inventoryDigest": "sha256:" + "b" * 64,
    "evidenceBundleDigest": "sha256:" + "c" * 64,
}
AUDITOR_IDENTITY = (
    "https://github.com/example/audit-custody/"
    ".github/workflows/attest.yml@refs/tags/audit-2026-07-27"
)
OIDC_ISSUER = "https://token.actions.githubusercontent.com"


def _attestation() -> dict[str, object]:
    return {
        "attestationType": "graphblocks.ai/audit-source-provenance/v1",
        "schemaVersion": 1,
        "repository": "graphblocks/graphblocks",
        "auditArtifacts": dict(AUDIT_ARTIFACTS),
        "sourceIdentity": {
            "kind": "git",
            "objectFormat": "sha1",
            "gitRevision": "1" * 40,
            "gitTree": "2" * 40,
        },
        "fileEvidenceManifestDigest": "sha256:" + "3" * 64,
        "authority": {"type": "auditor", "identity": AUDITOR_IDENTITY},
        "issuedAt": "2026-07-27T12:00:00Z",
    }


def _claim(attestation_bytes: bytes) -> IdentifiedGitAuditedSource:
    return IdentifiedGitAuditedSource(
        description="Original audit input",
        git_revision="1" * 40,
        git_tree="2" * 40,
        file_evidence_manifest_digest="sha256:" + "3" * 64,
        provenance_binding=ProvenanceBinding(
            kind="signed-attestation",
            digest="sha256:" + hashlib.sha256(attestation_bytes).hexdigest(),
        ),
    )


def _trust_policy(
    *,
    repository: str = "graphblocks/graphblocks",
    authority_type: str = "auditor",
    certificate_identity: str = AUDITOR_IDENTITY,
    certificate_oidc_issuer: str = OIDC_ISSUER,
    allow_project_release_workflow: bool = False,
) -> ProvenanceTrustPolicy:
    return ProvenanceTrustPolicy(
        repository=repository,
        authority_type=authority_type,
        certificate_identity=certificate_identity,
        certificate_oidc_issuer=certificate_oidc_issuer,
        allow_project_release_workflow=allow_project_release_workflow,
    )


def _fake_cosign(tmp_path: Path) -> tuple[list[str], Path]:
    log = tmp_path / "cosign-arguments.json"
    executable = tmp_path / "cosign.py"
    executable.write_text(
        "import json, os, sys\n"
        "open(os.environ['COSIGN_ARGUMENT_LOG'], 'w', encoding='utf-8').write("
        "json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    return [sys.executable, str(executable)], log


def test_authorized_provenance_binds_source_files_and_audit_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation_bytes = canonical_provenance_attestation_bytes(_attestation())
    attestation = tmp_path / "provenance.json"
    signature = tmp_path / "provenance.sigstore.json"
    attestation.write_bytes(attestation_bytes)
    signature.write_text("{}", encoding="utf-8")
    cosign, argument_log = _fake_cosign(tmp_path)
    monkeypatch.setenv("COSIGN_ARGUMENT_LOG", str(argument_log))

    verified = verify_audited_source_provenance(
        _claim(attestation_bytes),
        attestation=attestation,
        signature_bundle=signature,
        expected_audit_artifacts=AUDIT_ARTIFACTS,
        trust_policy=_trust_policy(),
        cosign=cosign,
    )

    assert verified.attestation_sha256 == hashlib.sha256(attestation_bytes).hexdigest()
    assert verified.authority_identity == AUDITOR_IDENTITY
    assert verified.file_evidence_manifest_digest == "sha256:" + "3" * 64
    assert verified.audit_artifacts == AuditArtifactBinding(
        report_digest=AUDIT_ARTIFACTS["reportDigest"],
        inventory_digest=AUDIT_ARTIFACTS["inventoryDigest"],
        evidence_bundle_digest=AUDIT_ARTIFACTS["evidenceBundleDigest"],
    )
    arguments = json.loads(argument_log.read_text(encoding="utf-8"))
    assert arguments[0] == "verify-blob"
    assert Path(arguments[1]).name == "attestation.json"
    assert Path(arguments[arguments.index("--bundle") + 1]).name == "bundle.json"
    assert arguments[arguments.index("--certificate-identity") + 1] == AUDITOR_IDENTITY
    assert arguments[arguments.index("--certificate-oidc-issuer") + 1] == OIDC_ISSUER


@pytest.mark.parametrize(
    "substitution",
    ("source", "file-manifest", "artifact", "authority", "repository"),
)
def test_provenance_rejects_bound_field_substitution(
    tmp_path: Path,
    substitution: str,
) -> None:
    payload = _attestation()
    if substitution == "source":
        cast(dict[str, object], payload["sourceIdentity"])["gitTree"] = "9" * 40
    elif substitution == "file-manifest":
        payload["fileEvidenceManifestDigest"] = "sha256:" + "9" * 64
    elif substitution == "artifact":
        cast(dict[str, object], payload["auditArtifacts"])["reportDigest"] = (
            "sha256:" + "9" * 64
        )
    elif substitution == "authority":
        cast(dict[str, object], payload["authority"])["identity"] = (
            "attacker@example.test"
        )
    else:
        payload["repository"] = "attacker/repository"
    attestation_bytes = canonical_provenance_attestation_bytes(payload)
    attestation = tmp_path / "provenance.json"
    signature = tmp_path / "provenance.sigstore.json"
    attestation.write_bytes(attestation_bytes)
    signature.write_text("{}", encoding="utf-8")

    with pytest.raises(AuditedSourceProvenanceError):
        verify_audited_source_provenance(
            _claim(attestation_bytes),
            attestation=attestation,
            signature_bundle=signature,
            expected_audit_artifacts=AUDIT_ARTIFACTS,
            trust_policy=_trust_policy(),
            cosign=[sys.executable, "-c", "raise SystemExit(99)"],
        )


def test_provenance_rejects_project_release_workflow_as_historical_authority(
    tmp_path: Path,
) -> None:
    identity = (
        "https://github.com/graphblocks/graphblocks/"
        ".github/workflows/ci.yml@refs/tags/v1.0.0-rc.10"
    )
    payload = _attestation()
    payload["authority"] = {"type": "auditor", "identity": identity}
    attestation_bytes = canonical_provenance_attestation_bytes(payload)
    attestation = tmp_path / "provenance.json"
    signature = tmp_path / "provenance.sigstore.json"
    attestation.write_bytes(attestation_bytes)
    signature.write_text("{}", encoding="utf-8")

    with pytest.raises(AuditedSourceProvenanceError, match="AUTHORITY"):
        verify_audited_source_provenance(
            _claim(attestation_bytes),
            attestation=attestation,
            signature_bundle=signature,
            expected_audit_artifacts=AUDIT_ARTIFACTS,
            trust_policy=_trust_policy(certificate_identity=identity),
            cosign=[sys.executable, "-c", "raise SystemExit(99)"],
        )


def test_provenance_rejects_noncanonical_duplicate_and_symlink_inputs(
    tmp_path: Path,
) -> None:
    payload = _attestation()
    pretty = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    duplicate = b'{"schemaVersion":1,"schemaVersion":1}\n'
    signature = tmp_path / "provenance.sigstore.json"
    signature.write_text("{}", encoding="utf-8")
    for index, data in enumerate((pretty, duplicate)):
        attestation = tmp_path / f"provenance-{index}.json"
        attestation.write_bytes(data)
        with pytest.raises(AuditedSourceProvenanceError, match="ATTESTATION"):
            verify_audited_source_provenance(
                _claim(data),
                attestation=attestation,
                signature_bundle=signature,
                expected_audit_artifacts=AUDIT_ARTIFACTS,
                trust_policy=_trust_policy(),
                cosign=[sys.executable, "-c", "raise SystemExit(99)"],
            )

    target = tmp_path / "target.json"
    data = canonical_provenance_attestation_bytes(payload)
    target.write_bytes(data)
    link = tmp_path / "provenance-link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(AuditedSourceProvenanceError, match="ATTESTATION_FILE"):
        verify_audited_source_provenance(
            _claim(data),
            attestation=link,
            signature_bundle=signature,
            expected_audit_artifacts=AUDIT_ARTIFACTS,
            trust_policy=_trust_policy(),
            cosign=[sys.executable, "-c", "raise SystemExit(99)"],
        )


def test_provenance_verification_failure_is_closed_and_sanitized(
    tmp_path: Path,
) -> None:
    data = canonical_provenance_attestation_bytes(_attestation())
    attestation = tmp_path / "provenance.json"
    signature = tmp_path / "provenance.sigstore.json"
    attestation.write_bytes(data)
    signature.write_text("{}", encoding="utf-8")

    with pytest.raises(AuditedSourceProvenanceError, match="SIGNATURE") as raised:
        verify_audited_source_provenance(
            _claim(data),
            attestation=attestation,
            signature_bundle=signature,
            expected_audit_artifacts=AUDIT_ARTIFACTS,
            trust_policy=_trust_policy(),
            cosign=[
                sys.executable,
                "-c",
                "import sys; print('secret output', file=sys.stderr); raise SystemExit(7)",
            ],
        )

    assert "secret output" not in str(raised.value)
    assert os.fspath(attestation) not in str(raised.value)


def test_provenance_trust_policy_decoder_preserves_explicit_unavailability() -> None:
    decoded = decode_provenance_trust_policy(
        b"formatVersion: 1\n"
        b"status: unavailable\n"
        b"repository: graphblocks/graphblocks\n"
        b"limitation: Independent audit authority identity has not been supplied\n"
    )

    assert decoded == UnavailableProvenanceTrustPolicy(
        repository="graphblocks/graphblocks",
        limitation="Independent audit authority identity has not been supplied",
    )
    assert provenance_trust_policy_claim(decoded) == {
        "formatVersion": 1,
        "status": "unavailable",
        "repository": "graphblocks/graphblocks",
        "limitation": "Independent audit authority identity has not been supplied",
    }


def test_provenance_trust_policy_decoder_returns_closed_configured_policy() -> None:
    decoded = decode_provenance_trust_policy(
        b"formatVersion: 1\n"
        b"status: configured\n"
        b"repository: graphblocks/graphblocks\n"
        b"authorityType: auditor\n"
        b"certificateIdentity: https://github.com/example/audit/.github/workflows/attest.yml@refs/tags/audit-1\n"
        b"certificateOidcIssuer: https://token.actions.githubusercontent.com\n"
        b"allowProjectReleaseWorkflow: false\n"
    )

    assert decoded == ProvenanceTrustPolicy(
        repository="graphblocks/graphblocks",
        authority_type="auditor",
        certificate_identity=(
            "https://github.com/example/audit/"
            ".github/workflows/attest.yml@refs/tags/audit-1"
        ),
        certificate_oidc_issuer=OIDC_ISSUER,
    )
    assert provenance_trust_policy_claim(decoded)["status"] == "configured"


@pytest.mark.parametrize(
    "data",
    (
        b"formatVersion: 1\nstatus: configured\nrepository: graphblocks/graphblocks\n",
        b"formatVersion: 1\nformatVersion: 1\nstatus: unavailable\nrepository: graphblocks/graphblocks\nlimitation: missing\n",
        b"formatVersion: 1\nstatus: unavailable\nrepository: graphblocks/graphblocks\nlimitation: missing\nunknown: true\n",
    ),
)
def test_provenance_trust_policy_decoder_rejects_partial_duplicate_or_open_yaml(
    data: bytes,
) -> None:
    with pytest.raises(AuditedSourceProvenanceError, match="TRUST_POLICY"):
        decode_provenance_trust_policy(data)

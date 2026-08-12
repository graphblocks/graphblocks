from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping
import zipfile

import pytest

from tools.audited_source_claim import (
    IdentifiedArchiveAuditedSource,
    IdentifiedGitAuditedSource,
    ProvenanceBinding,
)
from tools.audited_source_evidence import canonical_file_evidence_manifest_bytes
from tools.audited_source_package import (
    AuditedSourcePackageError,
    verify_audited_source_package,
)
from tools.audited_source_provenance import (
    ProvenanceTrustPolicy,
    canonical_provenance_attestation_bytes,
)


ARTIFACTS = {
    "reportDigest": "sha256:" + "a" * 64,
    "inventoryDigest": "sha256:" + "b" * 64,
    "evidenceBundleDigest": "sha256:" + "c" * 64,
}
AUDITOR_IDENTITY = (
    "https://github.com/example/audit-custody/"
    ".github/workflows/attest.yml@refs/tags/audit-2026-07-27"
)
TRUST_POLICY = ProvenanceTrustPolicy(
    repository="graphblocks/graphblocks",
    authority_type="auditor",
    certificate_identity=AUDITOR_IDENTITY,
    certificate_oidc_issuer="https://token.actions.githubusercontent.com",
)


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fake_cosign(tmp_path: Path) -> list[str]:
    executable = tmp_path / "cosign.py"
    executable.write_text("raise SystemExit(0)\n", encoding="utf-8")
    return [sys.executable, str(executable)]


def _attestation(
    *,
    source_identity: Mapping[str, object],
    manifest_digest: str,
) -> dict[str, object]:
    return {
        "attestationType": "graphblocks.ai/audit-source-provenance/v1",
        "schemaVersion": 1,
        "repository": "graphblocks/graphblocks",
        "auditArtifacts": dict(ARTIFACTS),
        "sourceIdentity": source_identity,
        "fileEvidenceManifestDigest": manifest_digest,
        "authority": {"type": "auditor", "identity": AUDITOR_IDENTITY},
        "issuedAt": "2026-07-27T12:00:00Z",
    }


def _write_package_common(
    package: Path,
    *,
    manifest: dict[str, Any],
    source_identity: Mapping[str, object],
) -> tuple[bytes, bytes]:
    manifest_bytes = canonical_file_evidence_manifest_bytes(manifest)
    manifest_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    attestation_bytes = canonical_provenance_attestation_bytes(
        _attestation(
            source_identity=source_identity,
            manifest_digest=manifest_digest,
        )
    )
    (package / "file-evidence-manifest.json").write_bytes(manifest_bytes)
    (package / "provenance.json").write_bytes(attestation_bytes)
    (package / "provenance.sigstore.json").write_text("{}", encoding="utf-8")
    return manifest_bytes, attestation_bytes


def _git_fixture(
    tmp_path: Path,
) -> tuple[IdentifiedGitAuditedSource, Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "GraphBlocks Test")
    _git(repository, "config", "user.email", "graphblocks@example.test")
    source_bytes = b"audited Git source\n"
    (repository / "source.txt").write_bytes(source_bytes)
    _git(repository, "add", "source.txt")
    _git(repository, "commit", "-qm", "audited source")
    commit = _git(repository, "rev-parse", "HEAD")
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    blob = _git(repository, "rev-parse", "HEAD:source.txt")
    package = tmp_path / "package"
    (package / "evidence" / "captured").mkdir(parents=True)
    (package / "evidence" / "captured" / "source.txt").write_bytes(source_bytes)
    manifest = {
        "formatVersion": 1,
        "auditArtifacts": dict(ARTIFACTS),
        "sourceIdentity": {"kind": "git", "commit": commit, "tree": tree},
        "capturedFiles": [
            {
                "sourcePath": "source.txt",
                "evidencePath": "captured/source.txt",
                "sha256": "sha256:" + hashlib.sha256(source_bytes).hexdigest(),
                "gitBlob": blob,
            }
        ],
        "reconstructedFiles": [],
    }
    source_identity = {
        "kind": "git",
        "objectFormat": "sha1",
        "gitRevision": commit,
        "gitTree": tree,
    }
    manifest_bytes, attestation_bytes = _write_package_common(
        package,
        manifest=manifest,
        source_identity=source_identity,
    )
    claim = IdentifiedGitAuditedSource(
        description="Original Git audit input",
        git_revision=commit,
        git_tree=tree,
        file_evidence_manifest_digest=(
            "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        ),
        provenance_binding=ProvenanceBinding(
            kind="signed-attestation",
            digest="sha256:" + hashlib.sha256(attestation_bytes).hexdigest(),
        ),
    )
    return claim, package, repository


def _archive_fixture(
    tmp_path: Path,
) -> tuple[IdentifiedArchiveAuditedSource, Path]:
    source_bytes = b"audited archive source\n"
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("graphblocks-main/source.txt", source_bytes)
    archive_bytes = buffer.getvalue()
    archive_digest = "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
    package = tmp_path / "package"
    (package / "evidence" / "captured").mkdir(parents=True)
    (package / "evidence" / "captured" / "source.txt").write_bytes(source_bytes)
    (package / "source.zip").write_bytes(archive_bytes)
    manifest = {
        "formatVersion": 1,
        "auditArtifacts": dict(ARTIFACTS),
        "sourceIdentity": {"kind": "archive", "archiveDigest": archive_digest},
        "capturedFiles": [
            {
                "sourcePath": "graphblocks-main/source.txt",
                "evidencePath": "captured/source.txt",
                "sha256": "sha256:" + hashlib.sha256(source_bytes).hexdigest(),
            }
        ],
        "reconstructedFiles": [],
    }
    manifest_bytes, attestation_bytes = _write_package_common(
        package,
        manifest=manifest,
        source_identity={"kind": "archive", "archiveDigest": archive_digest},
    )
    claim = IdentifiedArchiveAuditedSource(
        description="Recovered original archive",
        archive_digest=archive_digest,
        file_evidence_manifest_digest=(
            "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        ),
        provenance_binding=ProvenanceBinding(
            kind="recovered-audit-input",
            digest="sha256:" + hashlib.sha256(attestation_bytes).hexdigest(),
        ),
    )
    return claim, package


def test_closed_git_audit_source_package_verifies_end_to_end(tmp_path: Path) -> None:
    claim, package, repository = _git_fixture(tmp_path)

    verified = verify_audited_source_package(
        claim,
        package_root=package,
        expected_audit_artifacts=ARTIFACTS,
        trust_policy=TRUST_POLICY,
        git_repository=repository,
        cosign=_fake_cosign(tmp_path),
    )

    assert verified.claim == claim
    assert verified.file_evidence.captured_files == 1


def test_closed_archive_audit_source_package_verifies_end_to_end(
    tmp_path: Path,
) -> None:
    claim, package = _archive_fixture(tmp_path)

    verified = verify_audited_source_package(
        claim,
        package_root=package,
        expected_audit_artifacts=ARTIFACTS,
        trust_policy=TRUST_POLICY,
        cosign=_fake_cosign(tmp_path),
    )

    assert verified.claim == claim
    assert verified.file_evidence.captured_files == 1


@pytest.mark.parametrize("extra", ("root", "evidence"))
def test_audit_source_package_rejects_unlisted_files(
    tmp_path: Path,
    extra: str,
) -> None:
    claim, package = _archive_fixture(tmp_path)
    target = package / "unexpected.txt"
    if extra == "evidence":
        target = package / "evidence" / "unexpected.txt"
    target.write_text("not in closure", encoding="utf-8")

    with pytest.raises(AuditedSourcePackageError, match="CLOSURE"):
        verify_audited_source_package(
            claim,
            package_root=package,
            expected_audit_artifacts=ARTIFACTS,
            trust_policy=TRUST_POLICY,
            cosign=_fake_cosign(tmp_path),
        )


def test_audit_source_package_requires_explicit_git_repository(tmp_path: Path) -> None:
    claim, package, _repository = _git_fixture(tmp_path)

    with pytest.raises(AuditedSourcePackageError, match="GIT_REPOSITORY"):
        verify_audited_source_package(
            claim,
            package_root=package,
            expected_audit_artifacts=ARTIFACTS,
            trust_policy=TRUST_POLICY,
            cosign=_fake_cosign(tmp_path),
        )

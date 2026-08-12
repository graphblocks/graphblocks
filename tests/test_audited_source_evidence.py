from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from tools.audited_source_claim import IdentifiedGitAuditedSource, ProvenanceBinding
from tools.audited_source_evidence import (
    AuditArtifactBinding,
    AuditedSourceEvidenceError,
    canonical_file_evidence_manifest_bytes,
    decode_git_file_evidence_manifest,
    verify_git_file_evidence,
)
from tools.audited_source_verification import (
    VerifiedGitSourceIdentity,
    verify_git_source_identity,
)


AUDIT_ARTIFACTS = {
    "reportDigest": "sha256:" + "a" * 64,
    "inventoryDigest": "sha256:" + "b" * 64,
    "evidenceBundleDigest": "sha256:" + "c" * 64,
}


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture(
    tmp_path: Path,
) -> tuple[
    Path,
    Path,
    dict[str, Any],
    VerifiedGitSourceIdentity,
]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "GraphBlocks Test")
    _git(repository, "config", "user.email", "graphblocks@example.test")
    source_bytes = b"exact audited source bytes\n"
    (repository / "src").mkdir()
    (repository / "src" / "source.txt").write_bytes(source_bytes)
    _git(repository, "add", "src/source.txt")
    _git(repository, "commit", "-qm", "source")
    commit = _git(repository, "rev-parse", "HEAD")
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    blob = _git(repository, "rev-parse", "HEAD:src/source.txt")

    evidence_root = tmp_path / "evidence"
    (evidence_root / "captured").mkdir(parents=True)
    (evidence_root / "reconstructed").mkdir()
    (evidence_root / "captured" / "source.txt").write_bytes(source_bytes)
    reconstructed = b"reconstructed harness\n"
    (evidence_root / "reconstructed" / "harness.py").write_bytes(reconstructed)

    manifest = {
        "formatVersion": 1,
        "auditArtifacts": dict(AUDIT_ARTIFACTS),
        "sourceIdentity": {"kind": "git", "commit": commit, "tree": tree},
        "capturedFiles": [
            {
                "sourcePath": "src/source.txt",
                "evidencePath": "captured/source.txt",
                "sha256": "sha256:" + hashlib.sha256(source_bytes).hexdigest(),
                "gitBlob": blob,
            }
        ],
        "reconstructedFiles": [
            {
                "evidencePath": "reconstructed/harness.py",
                "sha256": "sha256:" + hashlib.sha256(reconstructed).hexdigest(),
                "classification": "reconstructed",
            }
        ],
    }
    manifest_bytes = canonical_file_evidence_manifest_bytes(manifest)
    claim = IdentifiedGitAuditedSource(
        description="Synthetic audited source",
        git_revision=commit,
        git_tree=tree,
        file_evidence_manifest_digest=(
            "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        ),
        provenance_binding=ProvenanceBinding(
            kind="signed-attestation",
            digest="sha256:" + "d" * 64,
        ),
    )
    verified = verify_git_source_identity(claim, repository=repository)
    return repository, evidence_root, manifest, verified


def test_git_file_evidence_matches_source_blob_and_captured_bytes(
    tmp_path: Path,
) -> None:
    repository, evidence_root, manifest, verified = _fixture(tmp_path)
    manifest_bytes = canonical_file_evidence_manifest_bytes(manifest)

    result = verify_git_file_evidence(
        verified,
        repository=repository,
        manifest_bytes=manifest_bytes,
        evidence_root=evidence_root,
        expected_audit_artifacts=AUDIT_ARTIFACTS,
    )

    assert result.manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()
    assert result.captured_files == 1
    assert result.reconstructed_files == 1
    assert result.audit_artifacts == AuditArtifactBinding(
        report_digest=AUDIT_ARTIFACTS["reportDigest"],
        inventory_digest=AUDIT_ARTIFACTS["inventoryDigest"],
        evidence_bundle_digest=AUDIT_ARTIFACTS["evidenceBundleDigest"],
    )


@pytest.mark.parametrize(
    "substitution",
    ("source-path", "blob", "evidence-bytes", "classification", "artifact"),
)
def test_git_file_evidence_rejects_substitution(
    tmp_path: Path,
    substitution: str,
) -> None:
    repository, evidence_root, manifest, verified = _fixture(tmp_path)
    if substitution == "source-path":
        manifest["capturedFiles"][0]["sourcePath"] = "../source.txt"
    elif substitution == "blob":
        manifest["capturedFiles"][0]["gitBlob"] = "f" * 40
    elif substitution == "evidence-bytes":
        (evidence_root / "captured" / "source.txt").write_bytes(b"substituted")
    elif substitution == "classification":
        manifest["reconstructedFiles"][0]["classification"] = "captured"
    else:
        manifest["auditArtifacts"]["reportDigest"] = "sha256:" + "e" * 64
    manifest_bytes = canonical_file_evidence_manifest_bytes(manifest)
    claim = verified.claim
    verified = VerifiedGitSourceIdentity(
        claim=IdentifiedGitAuditedSource(
            description=claim.description,
            git_revision=claim.git_revision,
            git_tree=claim.git_tree,
            file_evidence_manifest_digest=(
                "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
            ),
            provenance_binding=claim.provenance_binding,
        ),
        commit=verified.commit,
        tree=verified.tree,
    )

    with pytest.raises(AuditedSourceEvidenceError):
        verify_git_file_evidence(
            verified,
            repository=repository,
            manifest_bytes=manifest_bytes,
            evidence_root=evidence_root,
            expected_audit_artifacts=AUDIT_ARTIFACTS,
        )


def test_file_evidence_manifest_rejects_noncanonical_and_duplicate_json(
    tmp_path: Path,
) -> None:
    repository, evidence_root, manifest, verified = _fixture(tmp_path)
    pretty = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    duplicate = b'{"formatVersion":1,"formatVersion":1}\n'

    for manifest_bytes in (pretty, duplicate):
        with pytest.raises(AuditedSourceEvidenceError, match="MANIFEST"):
            verify_git_file_evidence(
                verified,
                repository=repository,
                manifest_bytes=manifest_bytes,
                evidence_root=evidence_root,
                expected_audit_artifacts=AUDIT_ARTIFACTS,
            )


def test_git_file_evidence_rejects_symlinked_evidence(tmp_path: Path) -> None:
    repository, evidence_root, manifest, verified = _fixture(tmp_path)
    captured = evidence_root / "captured" / "source.txt"
    target = evidence_root / "target.txt"
    target.write_bytes(captured.read_bytes())
    captured.unlink()
    try:
        captured.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(AuditedSourceEvidenceError, match="EVIDENCE_FILE"):
        verify_git_file_evidence(
            verified,
            repository=repository,
            manifest_bytes=canonical_file_evidence_manifest_bytes(manifest),
            evidence_root=evidence_root,
            expected_audit_artifacts=AUDIT_ARTIFACTS,
        )


def test_git_file_evidence_rejects_casefold_duplicate_paths(tmp_path: Path) -> None:
    _repository, _evidence_root, manifest, _verified = _fixture(tmp_path)
    duplicate = dict(manifest["reconstructedFiles"][0])
    duplicate["evidencePath"] = "CAPTURED/SOURCE.TXT"
    manifest["reconstructedFiles"].append(duplicate)

    with pytest.raises(AuditedSourceEvidenceError, match="FILE_PATH"):
        canonical = canonical_file_evidence_manifest_bytes(manifest)
        decode_git_file_evidence_manifest(canonical)


def test_git_file_evidence_rejects_symlink_source_entry(tmp_path: Path) -> None:
    repository, evidence_root, manifest, verified = _fixture(tmp_path)
    link = repository / "src" / "source-link.txt"
    try:
        link.symlink_to("source.txt")
    except OSError:
        pytest.skip("symlinks are unavailable")
    _git(repository, "add", "src/source-link.txt")
    _git(repository, "commit", "-qm", "symlink source")
    commit = _git(repository, "rev-parse", "HEAD")
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    blob = _git(repository, "rev-parse", "HEAD:src/source-link.txt")
    link_bytes = b"source.txt"
    (evidence_root / "captured" / "source.txt").write_bytes(link_bytes)
    manifest["sourceIdentity"] = {"kind": "git", "commit": commit, "tree": tree}
    manifest["capturedFiles"][0] = {
        "sourcePath": "src/source-link.txt",
        "evidencePath": "captured/source.txt",
        "sha256": "sha256:" + hashlib.sha256(link_bytes).hexdigest(),
        "gitBlob": blob,
    }
    manifest_bytes = canonical_file_evidence_manifest_bytes(manifest)
    claim = verified.claim
    verified = verify_git_source_identity(
        IdentifiedGitAuditedSource(
            description=claim.description,
            git_revision=commit,
            git_tree=tree,
            file_evidence_manifest_digest=(
                "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
            ),
            provenance_binding=claim.provenance_binding,
        ),
        repository=repository,
    )

    with pytest.raises(AuditedSourceEvidenceError, match="GIT_FILE"):
        verify_git_file_evidence(
            verified,
            repository=repository,
            manifest_bytes=manifest_bytes,
            evidence_root=evidence_root,
            expected_audit_artifacts=AUDIT_ARTIFACTS,
        )

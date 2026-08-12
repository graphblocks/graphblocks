from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

import pytest

from tools.audited_source_claim import (
    IdentifiedArchiveAuditedSource,
    IdentifiedGitAuditedSource,
    ProvenanceBinding,
)
from tools.audited_source_verification import (
    AuditedSourceVerificationError,
    VerifiedArchiveSourceIdentity,
    VerifiedGitSourceIdentity,
    verify_archive_source_identity,
    verify_git_source_identity,
)


def _git(repository: Path, *arguments: str, input: str | None = None) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        input=input,
    ).stdout.strip()


def _git_claim(repository: Path) -> IdentifiedGitAuditedSource:
    return IdentifiedGitAuditedSource(
        description="Synthetic audited source",
        git_revision=_git(repository, "rev-parse", "HEAD"),
        git_tree=_git(repository, "rev-parse", "HEAD^{tree}"),
        file_evidence_manifest_digest="sha256:" + "3" * 64,
        provenance_binding=ProvenanceBinding(
            kind="signed-attestation",
            digest="sha256:" + "4" * 64,
        ),
    )


@pytest.fixture
def source_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "GraphBlocks Test")
    _git(repository, "config", "user.email", "graphblocks@example.test")
    (repository / "source.txt").write_text("audited bytes\n", encoding="utf-8")
    _git(repository, "add", "source.txt")
    _git(repository, "commit", "-qm", "audited source")
    return repository


def test_git_source_identity_verifies_real_commit_and_tree(
    source_repository: Path,
) -> None:
    claim = _git_claim(source_repository)

    verified = verify_git_source_identity(claim, repository=source_repository)

    assert verified == VerifiedGitSourceIdentity(
        claim=claim,
        commit=claim.git_revision,
        tree=claim.git_tree,
    )


def test_git_source_identity_rejects_wrong_tree(source_repository: Path) -> None:
    claim = _git_claim(source_repository)
    other_tree = _git(source_repository, "mktree", input="")
    wrong = IdentifiedGitAuditedSource(
        description=claim.description,
        git_revision=claim.git_revision,
        git_tree=other_tree,
        file_evidence_manifest_digest=claim.file_evidence_manifest_digest,
        provenance_binding=claim.provenance_binding,
    )

    with pytest.raises(AuditedSourceVerificationError, match="AUDIT_SOURCE_GIT_TREE"):
        verify_git_source_identity(wrong, repository=source_repository)


def test_git_source_identity_rejects_blob_or_missing_commit(
    source_repository: Path,
) -> None:
    claim = _git_claim(source_repository)
    blob = _git(source_repository, "hash-object", "source.txt")
    for revision in (blob, "f" * 40):
        invalid = IdentifiedGitAuditedSource(
            description=claim.description,
            git_revision=revision,
            git_tree=claim.git_tree,
            file_evidence_manifest_digest=claim.file_evidence_manifest_digest,
            provenance_binding=claim.provenance_binding,
        )
        with pytest.raises(
            AuditedSourceVerificationError,
            match="AUDIT_SOURCE_GIT_OBJECT",
        ):
            verify_git_source_identity(invalid, repository=source_repository)


def _archive_claim(data: bytes) -> IdentifiedArchiveAuditedSource:
    return IdentifiedArchiveAuditedSource(
        description="Synthetic audit archive",
        archive_digest="sha256:" + hashlib.sha256(data).hexdigest(),
        file_evidence_manifest_digest="sha256:" + "6" * 64,
        provenance_binding=ProvenanceBinding(
            kind="recovered-audit-input",
            digest="sha256:" + "7" * 64,
        ),
    )


def test_archive_source_identity_hashes_exact_regular_bytes(tmp_path: Path) -> None:
    data = b"deterministic archive bytes"
    archive = tmp_path / "source.zip"
    archive.write_bytes(data)
    claim = _archive_claim(data)

    verified = verify_archive_source_identity(claim, archive=archive)

    assert verified == VerifiedArchiveSourceIdentity(
        claim=claim,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )


def test_archive_source_identity_rejects_substitution_symlink_and_size(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "source.zip"
    archive.write_bytes(b"archive")
    claim = _archive_claim(b"different")
    with pytest.raises(
        AuditedSourceVerificationError, match="AUDIT_SOURCE_ARCHIVE_DIGEST"
    ):
        verify_archive_source_identity(claim, archive=archive)

    matching = _archive_claim(archive.read_bytes())
    link = tmp_path / "source-link.zip"
    try:
        link.symlink_to(archive)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(
        AuditedSourceVerificationError, match="AUDIT_SOURCE_ARCHIVE_FILE"
    ):
        verify_archive_source_identity(matching, archive=link)
    with pytest.raises(
        AuditedSourceVerificationError, match="AUDIT_SOURCE_ARCHIVE_BUDGET"
    ):
        verify_archive_source_identity(matching, archive=archive, max_bytes=3)

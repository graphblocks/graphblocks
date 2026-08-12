"""Method-specific verification for structurally valid audited-source identities."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import subprocess

from tools.audited_source_claim import (
    IdentifiedArchiveAuditedSource,
    IdentifiedGitAuditedSource,
)


DEFAULT_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024


class AuditedSourceVerificationError(ValueError):
    """Raised when source identity cannot be established from real objects or bytes."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class VerifiedGitSourceIdentity:
    claim: IdentifiedGitAuditedSource
    commit: str
    tree: str


@dataclass(frozen=True, slots=True)
class VerifiedArchiveSourceIdentity:
    claim: IdentifiedArchiveAuditedSource
    sha256: str
    size: int


def _git_object_type(
    repository: Path,
    object_id: str,
) -> str:
    try:
        completed = subprocess.run(
            ["git", "cat-file", "-t", object_id],
            cwd=repository,
            env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise AuditedSourceVerificationError(
            "AUDIT_SOURCE_GIT_OBJECT",
            "audited-source Git object is unavailable",
        ) from error
    return completed.stdout.strip()


def verify_git_source_identity(
    claim: IdentifiedGitAuditedSource,
    *,
    repository: Path,
) -> VerifiedGitSourceIdentity:
    """Verify that a real commit exists and resolves to the exact claimed tree."""

    if repository.is_symlink() or not repository.is_dir():
        raise AuditedSourceVerificationError(
            "AUDIT_SOURCE_GIT_REPOSITORY",
            "audited-source Git repository must be a regular directory",
        )
    if _git_object_type(repository, claim.git_revision) != "commit":
        raise AuditedSourceVerificationError(
            "AUDIT_SOURCE_GIT_OBJECT",
            "audited-source Git revision is not a commit object",
        )
    if _git_object_type(repository, claim.git_tree) != "tree":
        raise AuditedSourceVerificationError(
            "AUDIT_SOURCE_GIT_OBJECT",
            "audited-source Git tree is not a tree object",
        )
    try:
        observed_tree = subprocess.run(
            ["git", "rev-parse", f"{claim.git_revision}^{{tree}}"],
            cwd=repository,
            env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise AuditedSourceVerificationError(
            "AUDIT_SOURCE_GIT_OBJECT",
            "audited-source Git commit tree could not be resolved",
        ) from error
    if observed_tree != claim.git_tree:
        raise AuditedSourceVerificationError(
            "AUDIT_SOURCE_GIT_TREE",
            "audited-source Git commit does not resolve to the claimed tree",
        )
    return VerifiedGitSourceIdentity(
        claim=claim,
        commit=claim.git_revision,
        tree=observed_tree,
    )


def verify_archive_source_identity(
    claim: IdentifiedArchiveAuditedSource,
    *,
    archive: Path,
    max_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
) -> VerifiedArchiveSourceIdentity:
    """Hash an exact bounded regular archive without following links."""

    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("archive byte budget must be a positive integer")
    try:
        file_status = archive.lstat()
    except OSError as error:
        raise AuditedSourceVerificationError(
            "AUDIT_SOURCE_ARCHIVE_FILE",
            "audited-source archive is unavailable",
        ) from error
    if not stat.S_ISREG(file_status.st_mode) or file_status.st_size > max_bytes:
        code = (
            "AUDIT_SOURCE_ARCHIVE_BUDGET"
            if stat.S_ISREG(file_status.st_mode) and file_status.st_size > max_bytes
            else "AUDIT_SOURCE_ARCHIVE_FILE"
        )
        raise AuditedSourceVerificationError(
            code,
            "audited-source archive must be a bounded regular file",
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(archive, flags)
    except OSError as error:
        raise AuditedSourceVerificationError(
            "AUDIT_SOURCE_ARCHIVE_FILE",
            "audited-source archive could not be opened safely",
        ) from error
    digest = hashlib.sha256()
    size = 0
    try:
        descriptor_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_status.st_mode)
            or (descriptor_status.st_dev, descriptor_status.st_ino)
            != (file_status.st_dev, file_status.st_ino)
            or descriptor_status.st_size != file_status.st_size
        ):
            raise AuditedSourceVerificationError(
                "AUDIT_SOURCE_ARCHIVE_FILE",
                "audited-source archive changed before verification",
            )
        while True:
            chunk = os.read(descriptor, READ_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise AuditedSourceVerificationError(
                    "AUDIT_SOURCE_ARCHIVE_BUDGET",
                    "audited-source archive exceeds its byte budget",
                )
            digest.update(chunk)
        final_status = os.fstat(descriptor)
        if (
            final_status.st_dev,
            final_status.st_ino,
            final_status.st_mode,
            final_status.st_size,
            final_status.st_mtime_ns,
            final_status.st_ctime_ns,
        ) != (
            descriptor_status.st_dev,
            descriptor_status.st_ino,
            descriptor_status.st_mode,
            descriptor_status.st_size,
            descriptor_status.st_mtime_ns,
            descriptor_status.st_ctime_ns,
        ):
            raise AuditedSourceVerificationError(
                "AUDIT_SOURCE_ARCHIVE_FILE",
                "audited-source archive changed during verification",
            )
    finally:
        os.close(descriptor)
    observed_digest = digest.hexdigest()
    if claim.archive_digest != "sha256:" + observed_digest:
        raise AuditedSourceVerificationError(
            "AUDIT_SOURCE_ARCHIVE_DIGEST",
            "audited-source archive bytes do not match the claimed digest",
        )
    return VerifiedArchiveSourceIdentity(
        claim=claim,
        sha256=observed_digest,
        size=size,
    )

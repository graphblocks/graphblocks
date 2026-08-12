"""End-to-end verification for closed external audited-source evidence packages."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat
from typing import Mapping, Sequence, TypeAlias

from tools.audited_source_archive import (
    AuditedSourceArchiveError,
    read_verified_zip_members,
)
from tools.audited_source_archive_evidence import (
    AuditedSourceArchiveEvidenceError,
    decode_archive_file_evidence_manifest,
    verify_archive_file_evidence,
)
from tools.audited_source_claim import (
    IdentifiedArchiveAuditedSource,
    IdentifiedGitAuditedSource,
)
from tools.audited_source_eligibility import (
    AuditedSourceEligibilityError,
    EligibleArchiveAuditedSource,
    EligibleGitAuditedSource,
    qualify_verified_archive_audited_source,
    qualify_verified_git_audited_source,
)
from tools.audited_source_evidence import (
    AuditedSourceEvidenceError,
    decode_git_file_evidence_manifest,
    verify_git_file_evidence,
)
from tools.audited_source_provenance import (
    AuditedSourceProvenanceError,
    ProvenanceTrustPolicy,
    verify_audited_source_provenance,
)
from tools.audited_source_verification import (
    AuditedSourceVerificationError,
    verify_archive_source_identity,
    verify_git_source_identity,
)


FILE_EVIDENCE_MANIFEST = "file-evidence-manifest.json"
EVIDENCE_DIRECTORY = "evidence"
PROVENANCE_ATTESTATION = "provenance.json"
PROVENANCE_SIGNATURE_BUNDLE = "provenance.sigstore.json"
ARCHIVE_SOURCE = "source.zip"
DEFAULT_MAX_MANIFEST_BYTES = 256 * 1024
READ_CHUNK_BYTES = 64 * 1024


class AuditedSourcePackageError(ValueError):
    """Raised when an external audit-source evidence package is not closed."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


EligibleAuditedSource: TypeAlias = (
    EligibleGitAuditedSource | EligibleArchiveAuditedSource
)
IdentifiedAuditedSource: TypeAlias = (
    IdentifiedGitAuditedSource | IdentifiedArchiveAuditedSource
)


def _read_bounded_regular_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        path_status = path.lstat()
    except OSError as error:
        raise AuditedSourcePackageError(
            "AUDIT_SOURCE_PACKAGE_FILE",
            "audit-source package file is unavailable",
        ) from error
    if not stat.S_ISREG(path_status.st_mode) or path_status.st_size > max_bytes:
        raise AuditedSourcePackageError(
            "AUDIT_SOURCE_PACKAGE_FILE",
            "audit-source package file must be a bounded regular file",
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AuditedSourcePackageError(
            "AUDIT_SOURCE_PACKAGE_FILE",
            "audit-source package file could not be opened safely",
        ) from error
    chunks: list[bytes] = []
    size = 0
    try:
        descriptor_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_status.st_mode)
            or (descriptor_status.st_dev, descriptor_status.st_ino)
            != (path_status.st_dev, path_status.st_ino)
            or descriptor_status.st_size != path_status.st_size
        ):
            raise AuditedSourcePackageError(
                "AUDIT_SOURCE_PACKAGE_FILE",
                "audit-source package file changed before verification",
            )
        while True:
            chunk = os.read(descriptor, READ_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise AuditedSourcePackageError(
                    "AUDIT_SOURCE_PACKAGE_FILE",
                    "audit-source package file exceeds its byte budget",
                )
            chunks.append(chunk)
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
            raise AuditedSourcePackageError(
                "AUDIT_SOURCE_PACKAGE_FILE",
                "audit-source package file changed during verification",
            )
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _require_package_root(
    package_root: Path,
    *,
    archive_source: bool,
) -> None:
    try:
        root_status = package_root.lstat()
    except OSError as error:
        raise AuditedSourcePackageError(
            "AUDIT_SOURCE_PACKAGE_ROOT",
            "audit-source package root is unavailable",
        ) from error
    if not stat.S_ISDIR(root_status.st_mode):
        raise AuditedSourcePackageError(
            "AUDIT_SOURCE_PACKAGE_ROOT",
            "audit-source package root must be a real directory",
        )
    expected = {
        FILE_EVIDENCE_MANIFEST,
        EVIDENCE_DIRECTORY,
        PROVENANCE_ATTESTATION,
        PROVENANCE_SIGNATURE_BUNDLE,
    }
    if archive_source:
        expected.add(ARCHIVE_SOURCE)
    try:
        observed = {entry.name for entry in package_root.iterdir()}
    except OSError as error:
        raise AuditedSourcePackageError(
            "AUDIT_SOURCE_PACKAGE_ROOT",
            "audit-source package root could not be inspected",
        ) from error
    if observed != expected:
        raise AuditedSourcePackageError(
            "AUDIT_SOURCE_PACKAGE_CLOSURE",
            "audit-source package contains missing or unlisted root entries",
        )


def _manifest_evidence_paths(
    claim: IdentifiedAuditedSource,
    manifest_bytes: bytes,
) -> set[str]:
    if isinstance(claim, IdentifiedGitAuditedSource):
        git_manifest = decode_git_file_evidence_manifest(manifest_bytes)
        return {record.evidence_path for record in git_manifest.captured_files} | {
            record.evidence_path for record in git_manifest.reconstructed_files
        }
    archive_manifest = decode_archive_file_evidence_manifest(manifest_bytes)
    return {record.evidence_path for record in archive_manifest.captured_files} | {
        record.evidence_path for record in archive_manifest.reconstructed_files
    }


def _require_evidence_closure(evidence_root: Path, expected_files: set[str]) -> None:
    try:
        root_status = evidence_root.lstat()
    except OSError as error:
        raise AuditedSourcePackageError(
            "AUDIT_SOURCE_PACKAGE_CLOSURE",
            "audit evidence directory is unavailable",
        ) from error
    if not stat.S_ISDIR(root_status.st_mode):
        raise AuditedSourcePackageError(
            "AUDIT_SOURCE_PACKAGE_CLOSURE",
            "audit evidence root must be a real directory",
        )
    expected_directories = {
        PurePosixPath(*relative.parts[:index]).as_posix()
        for path in expected_files
        for relative in (PurePosixPath(path),)
        for index in range(1, len(relative.parts))
    }
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    pending = [evidence_root]
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(directory.iterdir())
        except OSError as error:
            raise AuditedSourcePackageError(
                "AUDIT_SOURCE_PACKAGE_CLOSURE",
                "audit evidence directory could not be inspected",
            ) from error
        for entry in entries:
            try:
                entry_status = entry.lstat()
            except OSError as error:
                raise AuditedSourcePackageError(
                    "AUDIT_SOURCE_PACKAGE_CLOSURE",
                    "audit evidence entry could not be inspected",
                ) from error
            relative = entry.relative_to(evidence_root).as_posix()
            if stat.S_ISDIR(entry_status.st_mode):
                observed_directories.add(relative)
                pending.append(entry)
            elif stat.S_ISREG(entry_status.st_mode):
                observed_files.add(relative)
            else:
                raise AuditedSourcePackageError(
                    "AUDIT_SOURCE_PACKAGE_CLOSURE",
                    "audit evidence contains a link or special file",
                )
    if observed_files != expected_files or observed_directories != expected_directories:
        raise AuditedSourcePackageError(
            "AUDIT_SOURCE_PACKAGE_CLOSURE",
            "audit evidence tree contains missing or unlisted entries",
        )


def verify_audited_source_package(
    claim: IdentifiedAuditedSource,
    *,
    package_root: Path,
    expected_audit_artifacts: Mapping[str, str],
    trust_policy: ProvenanceTrustPolicy,
    git_repository: Path | None = None,
    cosign: str | Sequence[str] = "cosign",
) -> EligibleAuditedSource:
    """Verify one closed package and return the only stable-eligible source type."""

    if not isinstance(
        claim,
        (IdentifiedGitAuditedSource, IdentifiedArchiveAuditedSource),
    ):
        raise TypeError("audit-source package requires an identified source claim")
    package_root = package_root.absolute()
    _require_package_root(
        package_root,
        archive_source=isinstance(claim, IdentifiedArchiveAuditedSource),
    )
    manifest_bytes = _read_bounded_regular_file(
        package_root / FILE_EVIDENCE_MANIFEST,
        max_bytes=DEFAULT_MAX_MANIFEST_BYTES,
    )
    try:
        evidence_paths = _manifest_evidence_paths(claim, manifest_bytes)
        evidence_root = package_root / EVIDENCE_DIRECTORY
        _require_evidence_closure(evidence_root, evidence_paths)
        provenance = verify_audited_source_provenance(
            claim,
            attestation=package_root / PROVENANCE_ATTESTATION,
            signature_bundle=package_root / PROVENANCE_SIGNATURE_BUNDLE,
            expected_audit_artifacts=expected_audit_artifacts,
            trust_policy=trust_policy,
            cosign=cosign,
        )
        if isinstance(claim, IdentifiedGitAuditedSource):
            if git_repository is None:
                raise AuditedSourcePackageError(
                    "AUDIT_SOURCE_PACKAGE_GIT_REPOSITORY",
                    "Git audit-source verification requires an explicit repository",
                )
            git_source = verify_git_source_identity(claim, repository=git_repository)
            file_evidence = verify_git_file_evidence(
                git_source,
                repository=git_repository,
                manifest_bytes=manifest_bytes,
                evidence_root=evidence_root,
                expected_audit_artifacts=expected_audit_artifacts,
            )
            eligible: EligibleAuditedSource = qualify_verified_git_audited_source(
                source=git_source,
                file_evidence=file_evidence,
                provenance=provenance,
            )
        else:
            archive_source_identity = verify_archive_source_identity(
                claim,
                archive=package_root / ARCHIVE_SOURCE,
            )
            archive = read_verified_zip_members(
                archive_source_identity,
                archive=package_root / ARCHIVE_SOURCE,
            )
            archive_file_evidence = verify_archive_file_evidence(
                archive,
                manifest_bytes=manifest_bytes,
                evidence_root=evidence_root,
                expected_audit_artifacts=expected_audit_artifacts,
            )
            eligible = qualify_verified_archive_audited_source(
                source=archive_source_identity,
                file_evidence=archive_file_evidence,
                provenance=provenance,
            )
        _require_package_root(
            package_root,
            archive_source=isinstance(claim, IdentifiedArchiveAuditedSource),
        )
        _require_evidence_closure(evidence_root, evidence_paths)
        return eligible
    except AuditedSourcePackageError:
        raise
    except (
        AuditedSourceArchiveError,
        AuditedSourceArchiveEvidenceError,
        AuditedSourceEligibilityError,
        AuditedSourceEvidenceError,
        AuditedSourceProvenanceError,
        AuditedSourceVerificationError,
    ) as error:
        raise AuditedSourcePackageError(
            "AUDIT_SOURCE_PACKAGE_VERIFICATION",
            "audit-source package failed closed verification",
        ) from error

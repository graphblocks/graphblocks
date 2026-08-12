"""Closed file-level evidence manifests for verified audited-source archives."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Mapping

from tools.audited_source_archive import VerifiedZipArchive
from tools.audited_source_evidence import (
    AuditArtifactBinding,
    canonical_file_evidence_manifest_bytes,
)
from tools.audited_source_verification import VerifiedArchiveSourceIdentity


SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
DEFAULT_MAX_MANIFEST_BYTES = 256 * 1024
DEFAULT_MAX_EVIDENCE_FILE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TOTAL_EVIDENCE_BYTES = 128 * 1024 * 1024
MAX_FILE_RECORDS = 1_024
READ_CHUNK_BYTES = 64 * 1024
AUDIT_ARTIFACT_FIELDS = frozenset(
    {"reportDigest", "inventoryDigest", "evidenceBundleDigest"}
)


class AuditedSourceArchiveEvidenceError(ValueError):
    """Raised when archive evidence is malformed, unsafe, or unbound."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class ArchiveCapturedFileEvidence:
    source_path: str
    evidence_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ArchiveReconstructedFileEvidence:
    evidence_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ArchiveFileEvidenceManifest:
    audit_artifacts: AuditArtifactBinding
    archive_digest: str
    captured_files: tuple[ArchiveCapturedFileEvidence, ...]
    reconstructed_files: tuple[ArchiveReconstructedFileEvidence, ...]


@dataclass(frozen=True, slots=True)
class VerifiedArchiveFileEvidence:
    source: VerifiedArchiveSourceIdentity
    manifest_sha256: str
    captured_files: int
    reconstructed_files: int
    audit_artifacts: AuditArtifactBinding


def _path(value: object, *, owner: str) -> str:
    if type(value) is not str:
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_PATH",
            f"{owner} is not text",
        )
    relative = PurePosixPath(value)
    if (
        not value
        or value != relative.as_posix()
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_PATH",
            f"{owner} is not a normalized repository-relative path",
        )
    return value


def _digest(value: object, *, owner: str) -> str:
    if type(value) is not str or SHA256_DIGEST.fullmatch(value) is None:
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_MANIFEST",
            f"{owner} is not a canonical SHA-256 digest",
        )
    return value


def decode_archive_file_evidence_manifest(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
) -> ArchiveFileEvidenceManifest:
    """Strictly decode a canonical archive file-evidence manifest."""

    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("archive file-evidence manifest byte budget must be positive")
    if type(data) is not bytes or len(data) > max_bytes:
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_MANIFEST_BUDGET",
            "archive file-evidence manifest exceeds its byte budget",
        )

    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AuditedSourceArchiveEvidenceError(
                    "AUDIT_SOURCE_ARCHIVE_FILE_MANIFEST",
                    "archive file-evidence manifest contains a duplicate key",
                )
            result[key] = value
        return result

    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicate_pairs)
    except AuditedSourceArchiveEvidenceError:
        raise
    except RecursionError as error:
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_MANIFEST_BUDGET",
            "archive file-evidence manifest exceeds its parser depth budget",
        ) from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_MANIFEST",
            "archive file-evidence manifest is not strict UTF-8 JSON",
        ) from error
    try:
        canonical = canonical_file_evidence_manifest_bytes(raw)
    except ValueError as error:
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_MANIFEST",
            "archive file-evidence manifest is not canonical JSON",
        ) from error
    if type(raw) is not dict or data != canonical:
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_MANIFEST",
            "archive file-evidence manifest must use canonical JSON",
        )
    if (
        set(raw)
        != {
            "formatVersion",
            "auditArtifacts",
            "sourceIdentity",
            "capturedFiles",
            "reconstructedFiles",
        }
        or type(raw["formatVersion"]) is not int
        or raw["formatVersion"] != 1
    ):
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_MANIFEST",
            "archive file-evidence manifest has an unsupported shape or version",
        )
    artifacts = raw["auditArtifacts"]
    if (
        type(artifacts) is not dict
        or set(artifacts) != AUDIT_ARTIFACT_FIELDS
        or any(
            type(value) is not str or SHA256_DIGEST.fullmatch(value) is None
            for value in artifacts.values()
        )
    ):
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_MANIFEST",
            "archive file-evidence audit artifact binding is invalid",
        )
    source_identity = raw["sourceIdentity"]
    if (
        type(source_identity) is not dict
        or set(source_identity) != {"kind", "archiveDigest"}
        or source_identity["kind"] != "archive"
    ):
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_MANIFEST",
            "archive file-evidence source identity is invalid",
        )
    archive_digest = _digest(
        source_identity["archiveDigest"],
        owner="archive source identity",
    )
    captured = raw["capturedFiles"]
    reconstructed = raw["reconstructedFiles"]
    if (
        type(captured) is not list
        or not captured
        or type(reconstructed) is not list
        or len(captured) + len(reconstructed) > MAX_FILE_RECORDS
    ):
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_MANIFEST_BUDGET",
            "archive file-evidence inventory is invalid or oversized",
        )
    captured_records: list[ArchiveCapturedFileEvidence] = []
    reconstructed_records: list[ArchiveReconstructedFileEvidence] = []
    source_paths: set[str] = set()
    evidence_paths: set[str] = set()
    for index, value in enumerate(captured):
        if type(value) is not dict or set(value) != {
            "sourcePath",
            "evidencePath",
            "sha256",
        }:
            raise AuditedSourceArchiveEvidenceError(
                "AUDIT_SOURCE_ARCHIVE_FILE_MANIFEST",
                f"captured archive file record {index} has an invalid shape",
            )
        source_path = _path(value["sourcePath"], owner="captured source path")
        evidence_path = _path(value["evidencePath"], owner="captured evidence path")
        if (
            source_path.casefold() in source_paths
            or evidence_path.casefold() in evidence_paths
        ):
            raise AuditedSourceArchiveEvidenceError(
                "AUDIT_SOURCE_ARCHIVE_FILE_PATH",
                "captured archive source or evidence path is duplicated",
            )
        source_paths.add(source_path.casefold())
        evidence_paths.add(evidence_path.casefold())
        captured_records.append(
            ArchiveCapturedFileEvidence(
                source_path=source_path,
                evidence_path=evidence_path,
                sha256=_digest(value["sha256"], owner="captured file digest"),
            )
        )
    for index, value in enumerate(reconstructed):
        if (
            type(value) is not dict
            or set(value) != {"evidencePath", "sha256", "classification"}
            or value["classification"] != "reconstructed"
        ):
            raise AuditedSourceArchiveEvidenceError(
                "AUDIT_SOURCE_ARCHIVE_FILE_CLASSIFICATION",
                f"reconstructed archive evidence record {index} is invalid",
            )
        evidence_path = _path(
            value["evidencePath"], owner="reconstructed evidence path"
        )
        if evidence_path.casefold() in evidence_paths:
            raise AuditedSourceArchiveEvidenceError(
                "AUDIT_SOURCE_ARCHIVE_FILE_PATH",
                "archive evidence path is duplicated",
            )
        evidence_paths.add(evidence_path.casefold())
        reconstructed_records.append(
            ArchiveReconstructedFileEvidence(
                evidence_path=evidence_path,
                sha256=_digest(value["sha256"], owner="reconstructed file digest"),
            )
        )
    return ArchiveFileEvidenceManifest(
        audit_artifacts=AuditArtifactBinding(
            report_digest=artifacts["reportDigest"],
            inventory_digest=artifacts["inventoryDigest"],
            evidence_bundle_digest=artifacts["evidenceBundleDigest"],
        ),
        archive_digest=archive_digest,
        captured_files=tuple(captured_records),
        reconstructed_files=tuple(reconstructed_records),
    )


def _read_evidence_files(
    evidence_root: Path,
    records: list[tuple[str, str]],
    *,
    max_file_bytes: int,
    max_total_bytes: int,
) -> dict[str, bytes]:
    if (
        type(max_file_bytes) is not int
        or type(max_total_bytes) is not int
        or max_file_bytes < 1
        or max_total_bytes < max_file_bytes
        or evidence_root.is_symlink()
        or not evidence_root.is_dir()
    ):
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_EVIDENCE_ROOT",
            "archive evidence root or resource budgets are invalid",
        )
    root = evidence_root.resolve()
    result: dict[str, bytes] = {}
    total_bytes = 0
    for relative_text, expected_digest in records:
        relative = PurePosixPath(relative_text)
        candidate = root.joinpath(*relative.parts)
        parent = root
        for part in relative.parts[:-1]:
            parent /= part
            if parent.is_symlink() or not parent.is_dir():
                raise AuditedSourceArchiveEvidenceError(
                    "AUDIT_SOURCE_ARCHIVE_EVIDENCE_FILE",
                    "archive evidence path traverses a symlink or non-directory",
                )
        try:
            path_status = candidate.lstat()
        except OSError as error:
            raise AuditedSourceArchiveEvidenceError(
                "AUDIT_SOURCE_ARCHIVE_EVIDENCE_FILE",
                "archive evidence entry is unavailable",
            ) from error
        if (
            not stat.S_ISREG(path_status.st_mode)
            or path_status.st_size > max_file_bytes
        ):
            raise AuditedSourceArchiveEvidenceError(
                "AUDIT_SOURCE_ARCHIVE_EVIDENCE_FILE",
                "archive evidence entry must be a bounded regular file",
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(candidate, flags)
        except OSError as error:
            raise AuditedSourceArchiveEvidenceError(
                "AUDIT_SOURCE_ARCHIVE_EVIDENCE_FILE",
                "archive evidence entry could not be opened safely",
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
                raise AuditedSourceArchiveEvidenceError(
                    "AUDIT_SOURCE_ARCHIVE_EVIDENCE_FILE",
                    "archive evidence entry changed before verification",
                )
            while True:
                chunk = os.read(descriptor, READ_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                total_bytes += len(chunk)
                if size > max_file_bytes or total_bytes > max_total_bytes:
                    raise AuditedSourceArchiveEvidenceError(
                        "AUDIT_SOURCE_ARCHIVE_EVIDENCE_BUDGET",
                        "archive evidence exceeds its byte budget",
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
                raise AuditedSourceArchiveEvidenceError(
                    "AUDIT_SOURCE_ARCHIVE_EVIDENCE_FILE",
                    "archive evidence entry changed during verification",
                )
        finally:
            os.close(descriptor)
        data = b"".join(chunks)
        if expected_digest != "sha256:" + hashlib.sha256(data).hexdigest():
            raise AuditedSourceArchiveEvidenceError(
                "AUDIT_SOURCE_ARCHIVE_EVIDENCE_DIGEST",
                "archive evidence entry does not match its digest",
            )
        result[relative_text] = data
    return result


def verify_archive_file_evidence(
    archive: VerifiedZipArchive,
    *,
    manifest_bytes: bytes,
    evidence_root: Path,
    expected_audit_artifacts: Mapping[str, str],
    max_file_bytes: int = DEFAULT_MAX_EVIDENCE_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_EVIDENCE_BYTES,
) -> VerifiedArchiveFileEvidence:
    """Bind safe archive members to captured and reconstructed evidence bytes."""

    manifest = decode_archive_file_evidence_manifest(manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    source = archive.source
    if source.claim.file_evidence_manifest_digest != "sha256:" + manifest_sha256:
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_MANIFEST_DIGEST",
            "archive file-evidence manifest does not match the source claim",
        )
    if (
        source.claim.archive_digest != "sha256:" + source.sha256
        or manifest.archive_digest != source.claim.archive_digest
    ):
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_FILE_IDENTITY",
            "archive file-evidence manifest does not bind the verified source",
        )
    if {
        "reportDigest": manifest.audit_artifacts.report_digest,
        "inventoryDigest": manifest.audit_artifacts.inventory_digest,
        "evidenceBundleDigest": manifest.audit_artifacts.evidence_bundle_digest,
    } != dict(expected_audit_artifacts):
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_ARTIFACT_BINDING",
            "archive file-evidence manifest does not bind the audit artifacts",
        )
    members: dict[str, bytes] = {}
    total_member_bytes = 0
    for member in archive.members:
        if (
            member.path in members
            or member.size != len(member.data)
            or member.sha256 != hashlib.sha256(member.data).hexdigest()
        ):
            raise AuditedSourceArchiveEvidenceError(
                "AUDIT_SOURCE_ARCHIVE_MEMBER",
                "verified archive member inventory is inconsistent",
            )
        members[member.path] = member.data
        total_member_bytes += member.size
    if total_member_bytes != archive.total_uncompressed_bytes:
        raise AuditedSourceArchiveEvidenceError(
            "AUDIT_SOURCE_ARCHIVE_MEMBER",
            "verified archive total size is inconsistent",
        )
    records = [
        (record.evidence_path, record.sha256) for record in manifest.captured_files
    ]
    records.extend(
        (record.evidence_path, record.sha256) for record in manifest.reconstructed_files
    )
    evidence = _read_evidence_files(
        evidence_root,
        records,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    for record in manifest.captured_files:
        source_bytes = members.get(record.source_path)
        if (
            source_bytes is None
            or len(source_bytes) > max_file_bytes
            or record.sha256 != "sha256:" + hashlib.sha256(source_bytes).hexdigest()
            or source_bytes != evidence[record.evidence_path]
        ):
            raise AuditedSourceArchiveEvidenceError(
                "AUDIT_SOURCE_ARCHIVE_FILE_MISMATCH",
                "captured evidence does not match the identified archive member",
            )
    return VerifiedArchiveFileEvidence(
        source=source,
        manifest_sha256=manifest_sha256,
        captured_files=len(manifest.captured_files),
        reconstructed_files=len(manifest.reconstructed_files),
        audit_artifacts=manifest.audit_artifacts,
    )

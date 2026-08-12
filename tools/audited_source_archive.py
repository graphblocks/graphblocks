"""Safe bounded ZIP inspection for verified audited-source archives."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path, PurePosixPath
import stat
import zipfile

from tools.audited_source_verification import (
    AuditedSourceVerificationError,
    VerifiedArchiveSourceIdentity,
    snapshot_archive_bytes,
)


DEFAULT_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 4_096
DEFAULT_MAX_MEMBER_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_COMPRESSION_RATIO = 100
READ_CHUNK_BYTES = 64 * 1024
SUPPORTED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


class AuditedSourceArchiveError(ValueError):
    """Raised when an audited-source archive is unsafe or exceeds its budgets."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class VerifiedZipMember:
    path: str
    data: bytes
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class VerifiedZipArchive:
    source: VerifiedArchiveSourceIdentity
    members: tuple[VerifiedZipMember, ...]
    total_uncompressed_bytes: int


def read_verified_zip_members(
    source: VerifiedArchiveSourceIdentity,
    *,
    archive: Path,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_compression_ratio: int = DEFAULT_MAX_COMPRESSION_RATIO,
) -> VerifiedZipArchive:
    """Read safe ZIP members from the exact verified archive bytes without extraction."""

    budgets = (
        max_archive_bytes,
        max_entries,
        max_member_bytes,
        max_total_bytes,
        max_compression_ratio,
    )
    if any(type(value) is not int or value < 1 for value in budgets):
        raise ValueError("archive inspection budgets must be positive integers")
    try:
        snapshot = snapshot_archive_bytes(archive, max_bytes=max_archive_bytes)
    except AuditedSourceVerificationError as error:
        raise AuditedSourceArchiveError(error.code, str(error)) from error
    if (
        snapshot.sha256 != source.sha256
        or snapshot.size != source.size
        or source.claim.archive_digest != "sha256:" + snapshot.sha256
    ):
        raise AuditedSourceArchiveError(
            "AUDIT_SOURCE_ARCHIVE_IDENTITY",
            "archive bytes changed after source identity verification",
        )
    try:
        zip_archive = zipfile.ZipFile(BytesIO(snapshot.data), "r")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise AuditedSourceArchiveError(
            "AUDIT_SOURCE_ARCHIVE_FORMAT",
            "audited-source archive must be a valid bounded ZIP file",
        ) from error
    members: list[VerifiedZipMember] = []
    observed_paths: set[str] = set()
    total_bytes = 0
    try:
        entries = zip_archive.infolist()
        if len(entries) > max_entries:
            raise AuditedSourceArchiveError(
                "AUDIT_SOURCE_ARCHIVE_BUDGET",
                "audited-source archive contains too many entries",
            )
        for entry in entries:
            raw_path = entry.filename
            is_directory = entry.is_dir()
            normalized_input = raw_path[:-1] if is_directory else raw_path
            relative = PurePosixPath(normalized_input)
            if (
                entry.orig_filename != raw_path
                or "\0" in entry.orig_filename
                or not normalized_input
                or "\\" in raw_path
                or ":" in raw_path
                or relative.is_absolute()
                or relative.as_posix() != normalized_input
                or any(part in {"", ".", ".."} for part in relative.parts)
                or any(ord(character) < 32 for character in raw_path)
                or (is_directory and raw_path != normalized_input + "/")
                or relative.as_posix().casefold() in observed_paths
            ):
                raise AuditedSourceArchiveError(
                    "AUDIT_SOURCE_ARCHIVE_PATH",
                    "audited-source archive contains an escaping or ambiguous path",
                )
            observed_paths.add(relative.as_posix().casefold())
            unix_mode = entry.external_attr >> 16
            unix_type = stat.S_IFMT(unix_mode) if unix_mode else 0
            if entry.create_system == 3 and unix_type not in {
                0,
                stat.S_IFREG,
                stat.S_IFDIR,
            }:
                raise AuditedSourceArchiveError(
                    "AUDIT_SOURCE_ARCHIVE_ENTRY_TYPE",
                    "audited-source archive contains a link or special file",
                )
            if is_directory:
                if unix_type not in {0, stat.S_IFDIR}:
                    raise AuditedSourceArchiveError(
                        "AUDIT_SOURCE_ARCHIVE_ENTRY_TYPE",
                        "audited-source archive directory has an invalid file type",
                    )
                continue
            if (
                unix_type == stat.S_IFDIR
                or entry.compress_type not in SUPPORTED_COMPRESSION
            ):
                raise AuditedSourceArchiveError(
                    "AUDIT_SOURCE_ARCHIVE_ENTRY_TYPE",
                    "audited-source archive contains an unsupported entry",
                )
            if entry.flag_bits & 0x1:
                raise AuditedSourceArchiveError(
                    "AUDIT_SOURCE_ARCHIVE_ENCRYPTED",
                    "audited-source archive must not contain encrypted entries",
                )
            if (
                entry.file_size > max_member_bytes
                or total_bytes + entry.file_size > max_total_bytes
            ):
                raise AuditedSourceArchiveError(
                    "AUDIT_SOURCE_ARCHIVE_BUDGET",
                    "audited-source archive exceeds its uncompressed byte budget",
                )
            if entry.file_size and (
                entry.compress_size == 0
                or entry.file_size > entry.compress_size * max_compression_ratio
            ):
                raise AuditedSourceArchiveError(
                    "AUDIT_SOURCE_ARCHIVE_RATIO",
                    "audited-source archive entry exceeds its compression ratio budget",
                )
            chunks: list[bytes] = []
            observed_size = 0
            digest = hashlib.sha256()
            try:
                with zip_archive.open(entry, "r") as member_file:
                    while True:
                        chunk = member_file.read(READ_CHUNK_BYTES)
                        if not chunk:
                            break
                        observed_size += len(chunk)
                        total_bytes += len(chunk)
                        if (
                            observed_size > max_member_bytes
                            or total_bytes > max_total_bytes
                            or observed_size > entry.file_size
                        ):
                            raise AuditedSourceArchiveError(
                                "AUDIT_SOURCE_ARCHIVE_BUDGET",
                                "audited-source archive expanded beyond declared budgets",
                            )
                        digest.update(chunk)
                        chunks.append(chunk)
            except AuditedSourceArchiveError:
                raise
            except (OSError, RuntimeError, zipfile.BadZipFile) as error:
                raise AuditedSourceArchiveError(
                    "AUDIT_SOURCE_ARCHIVE_FORMAT",
                    "audited-source archive entry could not be decoded safely",
                ) from error
            if observed_size != entry.file_size:
                raise AuditedSourceArchiveError(
                    "AUDIT_SOURCE_ARCHIVE_FORMAT",
                    "audited-source archive entry size is inconsistent",
                )
            members.append(
                VerifiedZipMember(
                    path=relative.as_posix(),
                    data=b"".join(chunks),
                    sha256=digest.hexdigest(),
                    size=observed_size,
                )
            )
    finally:
        zip_archive.close()
    return VerifiedZipArchive(
        source=source,
        members=tuple(sorted(members, key=lambda member: member.path)),
        total_uncompressed_bytes=total_bytes,
    )

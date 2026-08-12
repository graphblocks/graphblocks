"""Closed file-level evidence manifests for identified audited sources."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from typing import Mapping

from tools.audited_source_verification import VerifiedGitSourceIdentity


SHA1_OBJECT_ID = re.compile(r"[0-9a-f]{40}")
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
DEFAULT_MAX_MANIFEST_BYTES = 256 * 1024
DEFAULT_MAX_EVIDENCE_FILE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TOTAL_EVIDENCE_BYTES = 128 * 1024 * 1024
MAX_FILE_RECORDS = 1_024
READ_CHUNK_BYTES = 64 * 1024


class AuditedSourceEvidenceError(ValueError):
    """Raised when file-level audit evidence is malformed or unbound."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class CapturedFileEvidence:
    source_path: str
    evidence_path: str
    sha256: str
    git_blob: str


@dataclass(frozen=True, slots=True)
class ReconstructedFileEvidence:
    evidence_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class AuditArtifactBinding:
    report_digest: str
    inventory_digest: str
    evidence_bundle_digest: str


@dataclass(frozen=True, slots=True)
class GitFileEvidenceManifest:
    audit_artifacts: AuditArtifactBinding
    commit: str
    tree: str
    captured_files: tuple[CapturedFileEvidence, ...]
    reconstructed_files: tuple[ReconstructedFileEvidence, ...]


@dataclass(frozen=True, slots=True)
class VerifiedGitFileEvidence:
    source: VerifiedGitSourceIdentity
    manifest_sha256: str
    captured_files: int
    reconstructed_files: int


def canonical_file_evidence_manifest_bytes(value: Mapping[str, object]) -> bytes:
    """Serialize the closed manifest using its exact canonical JSON wire form."""

    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise AuditedSourceEvidenceError(
            "AUDIT_SOURCE_FILE_MANIFEST",
            "file-evidence manifest cannot be canonically serialized",
        ) from error


def decode_git_file_evidence_manifest(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
) -> GitFileEvidenceManifest:
    """Strictly decode a canonical Git file-evidence manifest."""

    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("file-evidence manifest byte budget must be positive")
    if type(data) is not bytes or len(data) > max_bytes:
        raise AuditedSourceEvidenceError(
            "AUDIT_SOURCE_FILE_MANIFEST_BUDGET",
            "file-evidence manifest exceeds its byte budget",
        )

    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AuditedSourceEvidenceError(
                    "AUDIT_SOURCE_FILE_MANIFEST",
                    "file-evidence manifest contains a duplicate key",
                )
            result[key] = value
        return result

    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicate_pairs)
    except AuditedSourceEvidenceError:
        raise
    except RecursionError as error:
        raise AuditedSourceEvidenceError(
            "AUDIT_SOURCE_FILE_MANIFEST_BUDGET",
            "file-evidence manifest exceeds its parser depth budget",
        ) from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AuditedSourceEvidenceError(
            "AUDIT_SOURCE_FILE_MANIFEST",
            "file-evidence manifest is not strict UTF-8 JSON",
        ) from error
    if type(raw) is not dict or data != canonical_file_evidence_manifest_bytes(raw):
        raise AuditedSourceEvidenceError(
            "AUDIT_SOURCE_FILE_MANIFEST",
            "file-evidence manifest must use canonical JSON",
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
        raise AuditedSourceEvidenceError(
            "AUDIT_SOURCE_FILE_MANIFEST",
            "file-evidence manifest has an unsupported shape or version",
        )
    audit_artifacts = raw["auditArtifacts"]
    if (
        type(audit_artifacts) is not dict
        or set(audit_artifacts)
        != {
            "reportDigest",
            "inventoryDigest",
            "evidenceBundleDigest",
        }
        or any(
            type(value) is not str or SHA256_DIGEST.fullmatch(value) is None
            for value in audit_artifacts.values()
        )
    ):
        raise AuditedSourceEvidenceError(
            "AUDIT_SOURCE_FILE_MANIFEST",
            "file-evidence audit artifact binding is invalid",
        )
    source_identity = raw["sourceIdentity"]
    if (
        type(source_identity) is not dict
        or set(source_identity) != {"kind", "commit", "tree"}
        or source_identity["kind"] != "git"
        or type(source_identity["commit"]) is not str
        or SHA1_OBJECT_ID.fullmatch(source_identity["commit"]) is None
        or type(source_identity["tree"]) is not str
        or SHA1_OBJECT_ID.fullmatch(source_identity["tree"]) is None
    ):
        raise AuditedSourceEvidenceError(
            "AUDIT_SOURCE_FILE_MANIFEST",
            "file-evidence source identity is invalid",
        )
    captured = raw["capturedFiles"]
    reconstructed = raw["reconstructedFiles"]
    if (
        type(captured) is not list
        or not captured
        or type(reconstructed) is not list
        or len(captured) + len(reconstructed) > MAX_FILE_RECORDS
    ):
        raise AuditedSourceEvidenceError(
            "AUDIT_SOURCE_FILE_MANIFEST_BUDGET",
            "file-evidence record inventory is invalid or oversized",
        )
    captured_records: list[CapturedFileEvidence] = []
    reconstructed_records: list[ReconstructedFileEvidence] = []
    source_paths: set[str] = set()
    evidence_paths: set[str] = set()
    for index, record in enumerate(captured):
        if type(record) is not dict or set(record) != {
            "sourcePath",
            "evidencePath",
            "sha256",
            "gitBlob",
        }:
            raise AuditedSourceEvidenceError(
                "AUDIT_SOURCE_FILE_MANIFEST",
                f"captured file record {index} has an invalid shape",
            )
        source_path = record["sourcePath"]
        evidence_path = record["evidencePath"]
        if any(
            type(path) is not str
            or not path
            or path != PurePosixPath(path).as_posix()
            or PurePosixPath(path).is_absolute()
            or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)
            or "\\" in path
            or any(ord(character) < 32 for character in path)
            for path in (source_path, evidence_path)
        ):
            raise AuditedSourceEvidenceError(
                "AUDIT_SOURCE_FILE_PATH",
                "captured file path is not normalized repository-relative text",
            )
        if (
            source_path.casefold() in source_paths
            or evidence_path.casefold() in evidence_paths
            or type(record["sha256"]) is not str
            or SHA256_DIGEST.fullmatch(record["sha256"]) is None
            or type(record["gitBlob"]) is not str
            or SHA1_OBJECT_ID.fullmatch(record["gitBlob"]) is None
        ):
            raise AuditedSourceEvidenceError(
                "AUDIT_SOURCE_FILE_MANIFEST",
                "captured file identity is invalid or duplicated",
            )
        source_paths.add(source_path.casefold())
        evidence_paths.add(evidence_path.casefold())
        captured_records.append(
            CapturedFileEvidence(
                source_path=source_path,
                evidence_path=evidence_path,
                sha256=record["sha256"],
                git_blob=record["gitBlob"],
            )
        )
    for index, record in enumerate(reconstructed):
        if (
            type(record) is not dict
            or set(record) != {"evidencePath", "sha256", "classification"}
            or record["classification"] != "reconstructed"
        ):
            raise AuditedSourceEvidenceError(
                "AUDIT_SOURCE_FILE_CLASSIFICATION",
                f"reconstructed file record {index} is not explicitly classified",
            )
        evidence_path = record["evidencePath"]
        if (
            type(evidence_path) is not str
            or not evidence_path
            or evidence_path != PurePosixPath(evidence_path).as_posix()
            or PurePosixPath(evidence_path).is_absolute()
            or any(
                part in {"", ".", ".."} for part in PurePosixPath(evidence_path).parts
            )
            or "\\" in evidence_path
            or any(ord(character) < 32 for character in evidence_path)
            or evidence_path.casefold() in evidence_paths
            or type(record["sha256"]) is not str
            or SHA256_DIGEST.fullmatch(record["sha256"]) is None
        ):
            raise AuditedSourceEvidenceError(
                "AUDIT_SOURCE_FILE_PATH",
                "reconstructed evidence path or digest is invalid",
            )
        evidence_paths.add(evidence_path.casefold())
        reconstructed_records.append(
            ReconstructedFileEvidence(
                evidence_path=evidence_path,
                sha256=record["sha256"],
            )
        )
    return GitFileEvidenceManifest(
        audit_artifacts=AuditArtifactBinding(
            report_digest=audit_artifacts["reportDigest"],
            inventory_digest=audit_artifacts["inventoryDigest"],
            evidence_bundle_digest=audit_artifacts["evidenceBundleDigest"],
        ),
        commit=source_identity["commit"],
        tree=source_identity["tree"],
        captured_files=tuple(captured_records),
        reconstructed_files=tuple(reconstructed_records),
    )


def verify_git_file_evidence(
    source: VerifiedGitSourceIdentity,
    *,
    repository: Path,
    manifest_bytes: bytes,
    evidence_root: Path,
    expected_audit_artifacts: Mapping[str, str],
    max_file_bytes: int = DEFAULT_MAX_EVIDENCE_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_EVIDENCE_BYTES,
) -> VerifiedGitFileEvidence:
    """Verify exact evidence bytes against the identified Git tree."""

    manifest = decode_git_file_evidence_manifest(manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if source.claim.file_evidence_manifest_digest != "sha256:" + manifest_sha256:
        raise AuditedSourceEvidenceError(
            "AUDIT_SOURCE_FILE_MANIFEST_DIGEST",
            "file-evidence manifest does not match the audited-source claim",
        )
    if {
        "reportDigest": manifest.audit_artifacts.report_digest,
        "inventoryDigest": manifest.audit_artifacts.inventory_digest,
        "evidenceBundleDigest": manifest.audit_artifacts.evidence_bundle_digest,
    } != dict(expected_audit_artifacts):
        raise AuditedSourceEvidenceError(
            "AUDIT_SOURCE_ARTIFACT_BINDING",
            "file-evidence manifest does not bind the expected audit artifacts",
        )
    if (manifest.commit, manifest.tree) != (source.commit, source.tree):
        raise AuditedSourceEvidenceError(
            "AUDIT_SOURCE_FILE_IDENTITY",
            "file-evidence manifest does not bind the verified Git identity",
        )
    if (
        type(max_file_bytes) is not int
        or type(max_total_bytes) is not int
        or max_file_bytes < 1
        or max_total_bytes < max_file_bytes
        or evidence_root.is_symlink()
        or not evidence_root.is_dir()
    ):
        raise AuditedSourceEvidenceError(
            "AUDIT_SOURCE_EVIDENCE_ROOT",
            "file-evidence root or resource budgets are invalid",
        )
    evidence_root = evidence_root.resolve()
    evidence_bytes: dict[str, bytes] = {}
    total_bytes = 0
    all_records = [
        (record.evidence_path, record.sha256) for record in manifest.captured_files
    ]
    all_records.extend(
        (record.evidence_path, record.sha256) for record in manifest.reconstructed_files
    )
    for evidence_path, expected_digest in all_records:
        relative = PurePosixPath(evidence_path)
        candidate = evidence_root.joinpath(*relative.parts)
        parent = evidence_root
        for part in relative.parts[:-1]:
            parent /= part
            if parent.is_symlink() or not parent.is_dir():
                raise AuditedSourceEvidenceError(
                    "AUDIT_SOURCE_EVIDENCE_FILE",
                    "file-evidence path traverses a non-directory or symlink",
                )
        try:
            file_status = candidate.lstat()
        except OSError as error:
            raise AuditedSourceEvidenceError(
                "AUDIT_SOURCE_EVIDENCE_FILE",
                "file-evidence entry is unavailable",
            ) from error
        if (
            not stat.S_ISREG(file_status.st_mode)
            or file_status.st_size > max_file_bytes
        ):
            raise AuditedSourceEvidenceError(
                "AUDIT_SOURCE_EVIDENCE_FILE",
                "file-evidence entry must be a bounded regular file",
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(candidate, flags)
        except OSError as error:
            raise AuditedSourceEvidenceError(
                "AUDIT_SOURCE_EVIDENCE_FILE",
                "file-evidence entry could not be opened safely",
            ) from error
        chunks: list[bytes] = []
        size = 0
        try:
            descriptor_status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(descriptor_status.st_mode)
                or (descriptor_status.st_dev, descriptor_status.st_ino)
                != (file_status.st_dev, file_status.st_ino)
                or descriptor_status.st_size != file_status.st_size
            ):
                raise AuditedSourceEvidenceError(
                    "AUDIT_SOURCE_EVIDENCE_FILE",
                    "file-evidence entry changed before verification",
                )
            while True:
                chunk = os.read(descriptor, READ_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                total_bytes += len(chunk)
                if size > max_file_bytes or total_bytes > max_total_bytes:
                    raise AuditedSourceEvidenceError(
                        "AUDIT_SOURCE_EVIDENCE_BUDGET",
                        "file-evidence bytes exceed their resource budget",
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
                raise AuditedSourceEvidenceError(
                    "AUDIT_SOURCE_EVIDENCE_FILE",
                    "file-evidence entry changed during verification",
                )
        finally:
            os.close(descriptor)
        data = b"".join(chunks)
        if expected_digest != "sha256:" + hashlib.sha256(data).hexdigest():
            raise AuditedSourceEvidenceError(
                "AUDIT_SOURCE_EVIDENCE_DIGEST",
                "file-evidence entry bytes do not match their digest",
            )
        evidence_bytes[evidence_path] = data
    git_environment = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    for record in manifest.captured_files:
        try:
            listing = subprocess.run(
                ["git", "ls-tree", "-z", source.tree, "--", record.source_path],
                cwd=repository,
                env=git_environment,
                check=True,
                capture_output=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as error:
            raise AuditedSourceEvidenceError(
                "AUDIT_SOURCE_GIT_FILE",
                "captured source path could not be resolved from the Git tree",
            ) from error
        fields, separator, listed_path = listing.rstrip(b"\0").partition(b"\t")
        metadata = fields.split(b" ")
        if (
            separator != b"\t"
            or listed_path != record.source_path.encode("utf-8")
            or len(metadata) != 3
            or metadata[0] not in {b"100644", b"100755"}
            or metadata[1] != b"blob"
            or metadata[2].decode("ascii", "ignore") != record.git_blob
        ):
            raise AuditedSourceEvidenceError(
                "AUDIT_SOURCE_GIT_FILE",
                "captured source path is missing, unsupported, or bound to another blob",
            )
        try:
            size = int(
                subprocess.run(
                    ["git", "cat-file", "-s", record.git_blob],
                    cwd=repository,
                    env=git_environment,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
            if size > max_file_bytes or total_bytes + size > max_total_bytes:
                raise AuditedSourceEvidenceError(
                    "AUDIT_SOURCE_EVIDENCE_BUDGET",
                    "captured Git blob exceeds the file resource budget",
                )
            blob_bytes = subprocess.run(
                ["git", "cat-file", "blob", record.git_blob],
                cwd=repository,
                env=git_environment,
                check=True,
                capture_output=True,
            ).stdout
        except AuditedSourceEvidenceError:
            raise
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            raise AuditedSourceEvidenceError(
                "AUDIT_SOURCE_GIT_FILE",
                "captured Git blob could not be read",
            ) from error
        if (
            len(blob_bytes) != size
            or blob_bytes != evidence_bytes[record.evidence_path]
            or record.sha256 != "sha256:" + hashlib.sha256(blob_bytes).hexdigest()
        ):
            raise AuditedSourceEvidenceError(
                "AUDIT_SOURCE_FILE_MISMATCH",
                "captured evidence bytes do not match the identified Git blob",
            )
    return VerifiedGitFileEvidence(
        source=source,
        manifest_sha256=manifest_sha256,
        captured_files=len(manifest.captured_files),
        reconstructed_files=len(manifest.reconstructed_files),
    )

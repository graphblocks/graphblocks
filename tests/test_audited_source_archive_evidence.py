from __future__ import annotations

from dataclasses import replace
import hashlib
from io import BytesIO
import json
from pathlib import Path
from typing import Any
import zipfile

import pytest

from tools.audited_source_archive import VerifiedZipArchive, read_verified_zip_members
from tools.audited_source_archive_evidence import (
    AuditedSourceArchiveEvidenceError,
    decode_archive_file_evidence_manifest,
    verify_archive_file_evidence,
)
from tools.audited_source_claim import IdentifiedArchiveAuditedSource, ProvenanceBinding
from tools.audited_source_evidence import canonical_file_evidence_manifest_bytes
from tools.audited_source_verification import verify_archive_source_identity


AUDIT_ARTIFACTS = {
    "reportDigest": "sha256:" + "a" * 64,
    "inventoryDigest": "sha256:" + "b" * 64,
    "evidenceBundleDigest": "sha256:" + "c" * 64,
}


def _zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in entries:
            archive.writestr(path, data)
    return output.getvalue()


def _fixture(tmp_path: Path) -> tuple[VerifiedZipArchive, Path, dict[str, Any]]:
    source_bytes = b"exact archive source bytes\n"
    archive_bytes = _zip_bytes([("graphblocks-main/src/source.txt", source_bytes)])
    archive_path = tmp_path / "graphblocks-main.zip"
    archive_path.write_bytes(archive_bytes)
    evidence_root = tmp_path / "evidence"
    (evidence_root / "captured").mkdir(parents=True)
    (evidence_root / "reconstructed").mkdir()
    (evidence_root / "captured" / "source.txt").write_bytes(source_bytes)
    reconstructed = b"reconstructed harness\n"
    (evidence_root / "reconstructed" / "harness.py").write_bytes(reconstructed)
    archive_digest = "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
    manifest = {
        "formatVersion": 1,
        "auditArtifacts": dict(AUDIT_ARTIFACTS),
        "sourceIdentity": {"kind": "archive", "archiveDigest": archive_digest},
        "capturedFiles": [
            {
                "sourcePath": "graphblocks-main/src/source.txt",
                "evidencePath": "captured/source.txt",
                "sha256": "sha256:" + hashlib.sha256(source_bytes).hexdigest(),
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
    claim = IdentifiedArchiveAuditedSource(
        description="Synthetic audited archive",
        archive_digest=archive_digest,
        file_evidence_manifest_digest=(
            "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        ),
        provenance_binding=ProvenanceBinding(
            kind="recovered-audit-input",
            digest="sha256:" + "d" * 64,
        ),
    )
    source = verify_archive_source_identity(claim, archive=archive_path)
    verified_archive = read_verified_zip_members(source, archive=archive_path)
    return verified_archive, evidence_root, manifest


def test_archive_file_evidence_matches_safe_source_member_and_captured_bytes(
    tmp_path: Path,
) -> None:
    archive, evidence_root, manifest = _fixture(tmp_path)
    manifest_bytes = canonical_file_evidence_manifest_bytes(manifest)

    verified = verify_archive_file_evidence(
        archive,
        manifest_bytes=manifest_bytes,
        evidence_root=evidence_root,
        expected_audit_artifacts=AUDIT_ARTIFACTS,
    )

    assert verified.manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()
    assert verified.captured_files == 1
    assert verified.reconstructed_files == 1


@pytest.mark.parametrize(
    "substitution",
    ("source-path", "evidence-bytes", "classification", "artifact", "identity"),
)
def test_archive_file_evidence_rejects_substitution(
    tmp_path: Path,
    substitution: str,
) -> None:
    archive, evidence_root, manifest = _fixture(tmp_path)
    if substitution == "source-path":
        manifest["capturedFiles"][0]["sourcePath"] = "missing/source.txt"
    elif substitution == "evidence-bytes":
        (evidence_root / "captured" / "source.txt").write_bytes(b"substituted")
    elif substitution == "classification":
        manifest["reconstructedFiles"][0]["classification"] = "captured"
    elif substitution == "artifact":
        manifest["auditArtifacts"]["reportDigest"] = "sha256:" + "e" * 64
    else:
        manifest["sourceIdentity"]["archiveDigest"] = "sha256:" + "f" * 64
    manifest_bytes = canonical_file_evidence_manifest_bytes(manifest)
    claim = archive.source.claim
    archive = replace(
        archive,
        source=replace(
            archive.source,
            claim=IdentifiedArchiveAuditedSource(
                description=claim.description,
                archive_digest=claim.archive_digest,
                file_evidence_manifest_digest=(
                    "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
                ),
                provenance_binding=claim.provenance_binding,
            ),
        ),
    )

    with pytest.raises(AuditedSourceArchiveEvidenceError):
        verify_archive_file_evidence(
            archive,
            manifest_bytes=manifest_bytes,
            evidence_root=evidence_root,
            expected_audit_artifacts=AUDIT_ARTIFACTS,
        )


def test_archive_file_evidence_manifest_rejects_noncanonical_duplicate_and_casefold(
    tmp_path: Path,
) -> None:
    _archive, _evidence_root, manifest = _fixture(tmp_path)
    duplicate_path = dict(manifest["reconstructedFiles"][0])
    duplicate_path["evidencePath"] = "CAPTURED/SOURCE.TXT"
    manifest["reconstructedFiles"].append(duplicate_path)
    with pytest.raises(AuditedSourceArchiveEvidenceError, match="FILE_PATH"):
        decode_archive_file_evidence_manifest(
            canonical_file_evidence_manifest_bytes(manifest)
        )

    for data in (
        (json.dumps(manifest, indent=2) + "\n").encode("utf-8"),
        b'{"formatVersion":1,"formatVersion":1}\n',
    ):
        with pytest.raises(AuditedSourceArchiveEvidenceError, match="MANIFEST"):
            decode_archive_file_evidence_manifest(data)


def test_archive_file_evidence_rejects_symlinked_evidence(tmp_path: Path) -> None:
    archive, evidence_root, manifest = _fixture(tmp_path)
    captured = evidence_root / "captured" / "source.txt"
    target = evidence_root / "target.txt"
    target.write_bytes(captured.read_bytes())
    captured.unlink()
    try:
        captured.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(AuditedSourceArchiveEvidenceError, match="EVIDENCE_FILE"):
        verify_archive_file_evidence(
            archive,
            manifest_bytes=canonical_file_evidence_manifest_bytes(manifest),
            evidence_root=evidence_root,
            expected_audit_artifacts=AUDIT_ARTIFACTS,
        )

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import stat
import zipfile

import pytest

from tools.audited_source_archive import (
    AuditedSourceArchiveError,
    read_verified_zip_members,
)
from tools.audited_source_claim import IdentifiedArchiveAuditedSource, ProvenanceBinding
from tools.audited_source_verification import (
    VerifiedArchiveSourceIdentity,
    verify_archive_source_identity,
)


def _zip_bytes(entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


def _verified_archive(
    tmp_path: Path,
    data: bytes,
) -> tuple[Path, VerifiedArchiveSourceIdentity]:
    path = tmp_path / "source.zip"
    path.write_bytes(data)
    claim = IdentifiedArchiveAuditedSource(
        description="Synthetic audit archive",
        archive_digest="sha256:" + hashlib.sha256(data).hexdigest(),
        file_evidence_manifest_digest="sha256:" + "6" * 64,
        provenance_binding=ProvenanceBinding(
            kind="recovered-audit-input",
            digest="sha256:" + "7" * 64,
        ),
    )
    return path, verify_archive_source_identity(claim, archive=path)


def test_verified_zip_members_are_read_without_filesystem_extraction(
    tmp_path: Path,
) -> None:
    data = _zip_bytes(
        [
            ("src/source.txt", b"source bytes\n"),
            ("docs/readme.md", b"documentation\n"),
        ]
    )
    path, source = _verified_archive(tmp_path, data)

    verified = read_verified_zip_members(source, archive=path)

    assert {member.path: member.data for member in verified.members} == {
        "docs/readme.md": b"documentation\n",
        "src/source.txt": b"source bytes\n",
    }
    assert not any(candidate.name == "src" for candidate in tmp_path.iterdir())


@pytest.mark.parametrize(
    "name",
    ("../escape.txt", "/absolute.txt", "windows\\escape.txt", "C:/drive.txt"),
)
def test_verified_zip_rejects_escaping_or_ambiguous_paths(
    tmp_path: Path,
    name: str,
) -> None:
    path, source = _verified_archive(tmp_path, _zip_bytes([(name, b"bad")]))

    with pytest.raises(AuditedSourceArchiveError, match="ARCHIVE_PATH"):
        read_verified_zip_members(source, archive=path)


def test_verified_zip_rejects_duplicate_casefold_paths(tmp_path: Path) -> None:
    path, source = _verified_archive(
        tmp_path,
        _zip_bytes([("src/File.txt", b"one"), ("src/file.txt", b"two")]),
    )

    with pytest.raises(AuditedSourceArchiveError, match="ARCHIVE_PATH"):
        read_verified_zip_members(source, archive=path)


def test_verified_zip_rejects_symlink_members(tmp_path: Path) -> None:
    link = zipfile.ZipInfo("src/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    path, source = _verified_archive(tmp_path, _zip_bytes([(link, b"target")]))

    with pytest.raises(AuditedSourceArchiveError, match="ARCHIVE_ENTRY_TYPE"):
        read_verified_zip_members(source, archive=path)


def test_verified_zip_rejects_entry_count_size_and_compression_budgets(
    tmp_path: Path,
) -> None:
    path, source = _verified_archive(
        tmp_path,
        _zip_bytes([("one", b"1"), ("two", b"2")]),
    )
    with pytest.raises(AuditedSourceArchiveError, match="ARCHIVE_BUDGET"):
        read_verified_zip_members(source, archive=path, max_entries=1)

    path, source = _verified_archive(tmp_path, _zip_bytes([("large", b"x" * 100)]))
    with pytest.raises(AuditedSourceArchiveError, match="ARCHIVE_BUDGET"):
        read_verified_zip_members(source, archive=path, max_member_bytes=50)

    path, source = _verified_archive(
        tmp_path,
        _zip_bytes([("bomb", b"a" * 10_000)]),
    )
    with pytest.raises(AuditedSourceArchiveError, match="ARCHIVE_RATIO"):
        read_verified_zip_members(source, archive=path, max_compression_ratio=2)


def test_verified_zip_rejects_bytes_changed_after_identity_verification(
    tmp_path: Path,
) -> None:
    path, source = _verified_archive(tmp_path, _zip_bytes([("one", b"1")]))
    path.write_bytes(_zip_bytes([("two", b"2")]))

    with pytest.raises(AuditedSourceArchiveError, match="ARCHIVE_IDENTITY"):
        read_verified_zip_members(source, archive=path)

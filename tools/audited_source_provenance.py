"""Cryptographic authority verification for audited-source provenance claims."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Mapping, Sequence, TypeAlias

from tools.audited_source_claim import (
    IdentifiedArchiveAuditedSource,
    IdentifiedGitAuditedSource,
)


DEFAULT_MAX_ATTESTATION_BYTES = 256 * 1024
DEFAULT_MAX_SIGNATURE_BUNDLE_BYTES = 2 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
SHA1_OBJECT_ID = re.compile(r"[0-9a-f]{40}")
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
ATTESTATION_TYPE = "graphblocks.ai/audit-source-provenance/v1"
AUTHORITY_TYPES = frozenset({"auditor", "evidence-custodian"})
AUDIT_ARTIFACT_FIELDS = frozenset(
    {"reportDigest", "inventoryDigest", "evidenceBundleDigest"}
)


class AuditedSourceProvenanceError(ValueError):
    """Raised when audit provenance is malformed, unauthorized, or unsigned."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class ProvenanceTrustPolicy:
    """Pinned identity policy for one repository's independent audit authority."""

    repository: str
    authority_type: str
    certificate_identity: str
    certificate_oidc_issuer: str
    allow_project_release_workflow: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.repository) is not str
            or REPOSITORY.fullmatch(self.repository) is None
            or type(self.authority_type) is not str
            or self.authority_type not in AUTHORITY_TYPES
            or not _is_canonical_text(self.certificate_identity, max_length=2_048)
            or not _is_canonical_text(self.certificate_oidc_issuer, max_length=2_048)
            or type(self.allow_project_release_workflow) is not bool
        ):
            raise ValueError("provenance trust policy is invalid")


AuditedSourceIdentityClaim: TypeAlias = (
    IdentifiedGitAuditedSource | IdentifiedArchiveAuditedSource
)


@dataclass(frozen=True, slots=True)
class VerifiedAuditedSourceProvenance:
    """A source/evidence binding signed by the explicitly trusted authority."""

    claim: AuditedSourceIdentityClaim
    attestation_sha256: str
    authority_identity: str
    issued_at: datetime


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    data: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class _DecodedAttestation:
    repository: str
    audit_artifacts: dict[str, str]
    source_identity: dict[str, str]
    file_evidence_manifest_digest: str
    authority_type: str
    authority_identity: str
    issued_at: datetime


def _is_canonical_text(value: object, *, max_length: int = 1_024) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and len(value) <= max_length
        and all(ord(character) >= 32 for character in value)
    )


def canonical_provenance_attestation_bytes(value: Mapping[str, object]) -> bytes:
    """Serialize an audit provenance attestation to its exact canonical JSON form."""

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
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
            "provenance attestation cannot be canonically serialized",
        ) from error


def _snapshot_regular_file(
    path: Path,
    *,
    max_bytes: int,
    code: str,
) -> _FileSnapshot:
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("provenance file byte budget must be positive")
    try:
        path_status = path.lstat()
    except OSError as error:
        raise AuditedSourceProvenanceError(
            code,
            "provenance input is unavailable",
        ) from error
    if not stat.S_ISREG(path_status.st_mode) or path_status.st_size > max_bytes:
        raise AuditedSourceProvenanceError(
            code,
            "provenance input must be a bounded regular file",
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AuditedSourceProvenanceError(
            code,
            "provenance input could not be opened safely",
        ) from error
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    observed_size = 0
    try:
        descriptor_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_status.st_mode)
            or (descriptor_status.st_dev, descriptor_status.st_ino)
            != (path_status.st_dev, path_status.st_ino)
            or descriptor_status.st_size != path_status.st_size
        ):
            raise AuditedSourceProvenanceError(
                code,
                "provenance input changed before verification",
            )
        while True:
            chunk = os.read(descriptor, READ_CHUNK_BYTES)
            if not chunk:
                break
            observed_size += len(chunk)
            if observed_size > max_bytes:
                raise AuditedSourceProvenanceError(
                    code,
                    "provenance input exceeds its byte budget",
                )
            digest.update(chunk)
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
            raise AuditedSourceProvenanceError(
                code,
                "provenance input changed during verification",
            )
    finally:
        os.close(descriptor)
    return _FileSnapshot(data=b"".join(chunks), sha256=digest.hexdigest())


def _decode_attestation(data: bytes) -> _DecodedAttestation:
    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AuditedSourceProvenanceError(
                    "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
                    "provenance attestation contains a duplicate key",
                )
            result[key] = value
        return result

    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicate_pairs)
    except AuditedSourceProvenanceError:
        raise
    except RecursionError as error:
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
            "provenance attestation exceeds its parser depth budget",
        ) from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
            "provenance attestation is not strict UTF-8 JSON",
        ) from error
    if type(raw) is not dict or data != canonical_provenance_attestation_bytes(raw):
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
            "provenance attestation must use canonical JSON",
        )
    if (
        set(raw)
        != {
            "attestationType",
            "schemaVersion",
            "repository",
            "auditArtifacts",
            "sourceIdentity",
            "fileEvidenceManifestDigest",
            "authority",
            "issuedAt",
        }
        or raw["attestationType"] != ATTESTATION_TYPE
        or raw["schemaVersion"] != 1
    ):
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
            "provenance attestation has an unsupported shape or version",
        )
    repository = raw["repository"]
    if type(repository) is not str or REPOSITORY.fullmatch(repository) is None:
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
            "provenance repository identity is invalid",
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
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
            "provenance audit artifact binding is invalid",
        )
    identity = raw["sourceIdentity"]
    if type(identity) is not dict:
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
            "provenance source identity is invalid",
        )
    if identity.get("kind") == "git":
        if (
            set(identity) != {"kind", "objectFormat", "gitRevision", "gitTree"}
            or identity["objectFormat"] != "sha1"
            or type(identity["gitRevision"]) is not str
            or SHA1_OBJECT_ID.fullmatch(identity["gitRevision"]) is None
            or type(identity["gitTree"]) is not str
            or SHA1_OBJECT_ID.fullmatch(identity["gitTree"]) is None
        ):
            raise AuditedSourceProvenanceError(
                "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
                "provenance Git source identity is invalid",
            )
    elif identity.get("kind") == "archive":
        if (
            set(identity) != {"kind", "archiveDigest"}
            or type(identity["archiveDigest"]) is not str
            or SHA256_DIGEST.fullmatch(identity["archiveDigest"]) is None
        ):
            raise AuditedSourceProvenanceError(
                "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
                "provenance archive source identity is invalid",
            )
    else:
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
            "provenance source identity kind is unsupported",
        )
    evidence_digest = raw["fileEvidenceManifestDigest"]
    if (
        type(evidence_digest) is not str
        or SHA256_DIGEST.fullmatch(evidence_digest) is None
    ):
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
            "provenance file-evidence manifest digest is invalid",
        )
    authority = raw["authority"]
    if (
        type(authority) is not dict
        or set(authority) != {"type", "identity"}
        or type(authority["type"]) is not str
        or authority["type"] not in AUTHORITY_TYPES
        or not _is_canonical_text(authority["identity"], max_length=2_048)
    ):
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_AUTHORITY",
            "provenance authority is invalid",
        )
    issued_at = raw["issuedAt"]
    if (
        type(issued_at) is not str
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
            issued_at,
        )
        is None
    ):
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
            "provenance issue time is not canonical UTC",
        )
    try:
        parsed_time = datetime.strptime(issued_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_ATTESTATION",
            "provenance issue time is invalid",
        ) from error
    return _DecodedAttestation(
        repository=repository,
        audit_artifacts=dict(artifacts),
        source_identity=dict(identity),
        file_evidence_manifest_digest=evidence_digest,
        authority_type=authority["type"],
        authority_identity=authority["identity"],
        issued_at=parsed_time,
    )


def _expected_source_identity(claim: AuditedSourceIdentityClaim) -> dict[str, str]:
    if isinstance(claim, IdentifiedGitAuditedSource):
        return {
            "kind": "git",
            "objectFormat": "sha1",
            "gitRevision": claim.git_revision,
            "gitTree": claim.git_tree,
        }
    return {"kind": "archive", "archiveDigest": claim.archive_digest}


def _validated_audit_artifacts(
    value: Mapping[str, str],
) -> dict[str, str]:
    if (
        type(value) is not dict
        or set(value) != AUDIT_ARTIFACT_FIELDS
        or any(
            type(digest) is not str or SHA256_DIGEST.fullmatch(digest) is None
            for digest in value.values()
        )
    ):
        raise ValueError("expected audit artifact digests are invalid")
    return dict(value)


def _tool_command(cosign: str | Sequence[str]) -> list[str]:
    command = [cosign] if isinstance(cosign, str) else list(cosign)
    if not command or any(type(item) is not str or not item for item in command):
        raise ValueError("cosign command must contain non-empty strings")
    return command


def verify_audited_source_provenance(
    claim: AuditedSourceIdentityClaim,
    *,
    attestation: Path,
    signature_bundle: Path,
    expected_audit_artifacts: Mapping[str, str],
    trust_policy: ProvenanceTrustPolicy,
    cosign: str | Sequence[str] = "cosign",
    max_attestation_bytes: int = DEFAULT_MAX_ATTESTATION_BYTES,
    max_signature_bundle_bytes: int = DEFAULT_MAX_SIGNATURE_BUNDLE_BYTES,
) -> VerifiedAuditedSourceProvenance:
    """Verify an independently authorized binding for an identified audit source."""

    if not isinstance(
        claim,
        (IdentifiedGitAuditedSource, IdentifiedArchiveAuditedSource),
    ):
        raise TypeError("provenance verification requires an identified source claim")
    artifact_binding = _validated_audit_artifacts(expected_audit_artifacts)
    attestation_snapshot = _snapshot_regular_file(
        attestation,
        max_bytes=max_attestation_bytes,
        code="AUDIT_SOURCE_PROVENANCE_ATTESTATION_FILE",
    )
    signature_snapshot = _snapshot_regular_file(
        signature_bundle,
        max_bytes=max_signature_bundle_bytes,
        code="AUDIT_SOURCE_PROVENANCE_SIGNATURE_FILE",
    )
    decoded = _decode_attestation(attestation_snapshot.data)
    expected_binding_kind = (
        "signed-attestation"
        if isinstance(claim, IdentifiedGitAuditedSource)
        else "recovered-audit-input"
    )
    if (
        claim.provenance_binding.kind != expected_binding_kind
        or claim.provenance_binding.digest != "sha256:" + attestation_snapshot.sha256
        or decoded.repository != trust_policy.repository
        or decoded.audit_artifacts != artifact_binding
        or decoded.source_identity != _expected_source_identity(claim)
        or decoded.file_evidence_manifest_digest != claim.file_evidence_manifest_digest
    ):
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_BINDING",
            "provenance attestation does not match the release evidence claim",
        )
    if (
        decoded.authority_type != trust_policy.authority_type
        or decoded.authority_identity != trust_policy.certificate_identity
    ):
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_AUTHORITY",
            "provenance authority is not trusted by policy",
        )
    project_workflow_prefix = (
        f"https://github.com/{trust_policy.repository}/.github/workflows/"
    )
    if (
        not trust_policy.allow_project_release_workflow
        and decoded.authority_identity.startswith(project_workflow_prefix)
    ):
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_AUTHORITY",
            "project workflows cannot establish historical audit provenance",
        )
    command = _tool_command(cosign)
    try:
        with tempfile.TemporaryDirectory(prefix="graphblocks-audit-provenance-") as raw:
            temporary = Path(raw)
            frozen_attestation = temporary / "attestation.json"
            frozen_bundle = temporary / "bundle.json"
            frozen_attestation.write_bytes(attestation_snapshot.data)
            frozen_bundle.write_bytes(signature_snapshot.data)
            subprocess.run(
                [
                    *command,
                    "verify-blob",
                    str(frozen_attestation),
                    "--bundle",
                    str(frozen_bundle),
                    "--certificate-identity",
                    trust_policy.certificate_identity,
                    "--certificate-oidc-issuer",
                    trust_policy.certificate_oidc_issuer,
                ],
                check=True,
                capture_output=True,
                text=False,
            )
    except (OSError, subprocess.CalledProcessError) as error:
        raise AuditedSourceProvenanceError(
            "AUDIT_SOURCE_PROVENANCE_SIGNATURE",
            "provenance signature verification failed",
        ) from error
    return VerifiedAuditedSourceProvenance(
        claim=claim,
        attestation_sha256=attestation_snapshot.sha256,
        authority_identity=decoded.authority_identity,
        issued_at=decoded.issued_at,
    )

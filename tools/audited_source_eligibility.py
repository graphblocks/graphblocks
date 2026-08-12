"""Closed composition of all proofs required for an eligible audited source."""

from __future__ import annotations

from dataclasses import dataclass

from tools.audited_source_archive_evidence import VerifiedArchiveFileEvidence
from tools.audited_source_claim import (
    IdentifiedArchiveAuditedSource,
    IdentifiedGitAuditedSource,
)
from tools.audited_source_evidence import VerifiedGitFileEvidence
from tools.audited_source_provenance import VerifiedAuditedSourceProvenance
from tools.audited_source_verification import (
    VerifiedArchiveSourceIdentity,
    VerifiedGitSourceIdentity,
)


class AuditedSourceEligibilityError(ValueError):
    """Raised when independently verified audit proofs do not form one closure."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class EligibleGitAuditedSource:
    """A Git audit input whose identity, files, and authority form one closure."""

    claim: IdentifiedGitAuditedSource
    source: VerifiedGitSourceIdentity
    file_evidence: VerifiedGitFileEvidence
    provenance: VerifiedAuditedSourceProvenance


@dataclass(frozen=True, slots=True)
class EligibleArchiveAuditedSource:
    """An archive audit input whose identity, files, and authority form one closure."""

    claim: IdentifiedArchiveAuditedSource
    source: VerifiedArchiveSourceIdentity
    file_evidence: VerifiedArchiveFileEvidence
    provenance: VerifiedAuditedSourceProvenance


def _require_common_closure(
    *,
    claim: IdentifiedGitAuditedSource | IdentifiedArchiveAuditedSource,
    manifest_sha256: str,
    evidence_artifacts: object,
    provenance: VerifiedAuditedSourceProvenance,
) -> None:
    if (
        provenance.claim != claim
        or provenance.file_evidence_manifest_digest
        != claim.file_evidence_manifest_digest
        or claim.file_evidence_manifest_digest != "sha256:" + manifest_sha256
        or provenance.audit_artifacts != evidence_artifacts
        or claim.provenance_binding.digest != "sha256:" + provenance.attestation_sha256
    ):
        raise AuditedSourceEligibilityError(
            "AUDIT_SOURCE_PROOF_CLOSURE",
            "source identity, file evidence, and authority provenance do not match",
        )


def qualify_verified_git_audited_source(
    *,
    source: VerifiedGitSourceIdentity,
    file_evidence: VerifiedGitFileEvidence,
    provenance: VerifiedAuditedSourceProvenance,
) -> EligibleGitAuditedSource:
    """Compose coherent Git audit proofs into a stable-eligibility value."""

    claim = source.claim
    if (
        source.commit != claim.git_revision
        or source.tree != claim.git_tree
        or file_evidence.source != source
    ):
        raise AuditedSourceEligibilityError(
            "AUDIT_SOURCE_GIT_PROOF",
            "Git source identity and file evidence do not match",
        )
    _require_common_closure(
        claim=claim,
        manifest_sha256=file_evidence.manifest_sha256,
        evidence_artifacts=file_evidence.audit_artifacts,
        provenance=provenance,
    )
    return EligibleGitAuditedSource(
        claim=claim,
        source=source,
        file_evidence=file_evidence,
        provenance=provenance,
    )


def qualify_verified_archive_audited_source(
    *,
    source: VerifiedArchiveSourceIdentity,
    file_evidence: VerifiedArchiveFileEvidence,
    provenance: VerifiedAuditedSourceProvenance,
) -> EligibleArchiveAuditedSource:
    """Compose coherent archive audit proofs into a stable-eligibility value."""

    claim = source.claim
    if (
        source.sha256 != claim.archive_digest.removeprefix("sha256:")
        or source.size < 0
        or file_evidence.source != source
    ):
        raise AuditedSourceEligibilityError(
            "AUDIT_SOURCE_ARCHIVE_PROOF",
            "archive source identity and file evidence do not match",
        )
    _require_common_closure(
        claim=claim,
        manifest_sha256=file_evidence.manifest_sha256,
        evidence_artifacts=file_evidence.audit_artifacts,
        provenance=provenance,
    )
    return EligibleArchiveAuditedSource(
        claim=claim,
        source=source,
        file_evidence=file_evidence,
        provenance=provenance,
    )

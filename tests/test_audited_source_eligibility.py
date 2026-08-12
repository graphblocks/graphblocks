from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tools.audited_source_archive_evidence import VerifiedArchiveFileEvidence
from tools.audited_source_claim import (
    IdentifiedArchiveAuditedSource,
    IdentifiedGitAuditedSource,
    ProvenanceBinding,
)
from tools.audited_source_eligibility import (
    AuditedSourceEligibilityError,
    qualify_verified_archive_audited_source,
    qualify_verified_git_audited_source,
)
from tools.audited_source_evidence import (
    AuditArtifactBinding,
    VerifiedGitFileEvidence,
)
from tools.audited_source_provenance import VerifiedAuditedSourceProvenance
from tools.audited_source_verification import (
    VerifiedArchiveSourceIdentity,
    VerifiedGitSourceIdentity,
)


ARTIFACTS = AuditArtifactBinding(
    report_digest="sha256:" + "a" * 64,
    inventory_digest="sha256:" + "b" * 64,
    evidence_bundle_digest="sha256:" + "c" * 64,
)
ISSUED_AT = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _git_proofs() -> tuple[
    VerifiedGitSourceIdentity,
    VerifiedGitFileEvidence,
    VerifiedAuditedSourceProvenance,
]:
    claim = IdentifiedGitAuditedSource(
        description="Original audit input",
        git_revision="1" * 40,
        git_tree="2" * 40,
        file_evidence_manifest_digest="sha256:" + "3" * 64,
        provenance_binding=ProvenanceBinding(
            kind="signed-attestation",
            digest="sha256:" + "4" * 64,
        ),
    )
    source = VerifiedGitSourceIdentity(
        claim=claim,
        commit=claim.git_revision,
        tree=claim.git_tree,
    )
    evidence = VerifiedGitFileEvidence(
        source=source,
        manifest_sha256="3" * 64,
        captured_files=13,
        reconstructed_files=5,
        audit_artifacts=ARTIFACTS,
    )
    provenance = VerifiedAuditedSourceProvenance(
        claim=claim,
        attestation_sha256="4" * 64,
        authority_identity="auditor@example.test",
        issued_at=ISSUED_AT,
        audit_artifacts=ARTIFACTS,
        file_evidence_manifest_digest=claim.file_evidence_manifest_digest,
    )
    return source, evidence, provenance


def _archive_proofs() -> tuple[
    VerifiedArchiveSourceIdentity,
    VerifiedArchiveFileEvidence,
    VerifiedAuditedSourceProvenance,
]:
    claim = IdentifiedArchiveAuditedSource(
        description="Recovered original audit archive",
        archive_digest="sha256:" + "1" * 64,
        file_evidence_manifest_digest="sha256:" + "3" * 64,
        provenance_binding=ProvenanceBinding(
            kind="recovered-audit-input",
            digest="sha256:" + "4" * 64,
        ),
    )
    source = VerifiedArchiveSourceIdentity(
        claim=claim,
        sha256="1" * 64,
        size=1_024,
    )
    evidence = VerifiedArchiveFileEvidence(
        source=source,
        manifest_sha256="3" * 64,
        captured_files=13,
        reconstructed_files=5,
        audit_artifacts=ARTIFACTS,
    )
    provenance = VerifiedAuditedSourceProvenance(
        claim=claim,
        attestation_sha256="4" * 64,
        authority_identity="custodian@example.test",
        issued_at=ISSUED_AT,
        audit_artifacts=ARTIFACTS,
        file_evidence_manifest_digest=claim.file_evidence_manifest_digest,
    )
    return source, evidence, provenance


def test_git_source_becomes_eligible_only_from_coherent_verified_proofs() -> None:
    source, evidence, provenance = _git_proofs()

    eligible = qualify_verified_git_audited_source(
        source=source,
        file_evidence=evidence,
        provenance=provenance,
    )

    assert eligible.claim == source.claim
    assert eligible.source == source
    assert eligible.file_evidence == evidence
    assert eligible.provenance == provenance


def test_archive_source_becomes_eligible_only_from_coherent_verified_proofs() -> None:
    source, evidence, provenance = _archive_proofs()

    eligible = qualify_verified_archive_audited_source(
        source=source,
        file_evidence=evidence,
        provenance=provenance,
    )

    assert eligible.claim == source.claim
    assert eligible.source == source
    assert eligible.file_evidence == evidence
    assert eligible.provenance == provenance


@pytest.mark.parametrize(
    "substitution",
    ("source", "evidence", "artifacts", "manifest", "attestation"),
)
def test_git_eligibility_rejects_cross_proof_substitution(substitution: str) -> None:
    source, evidence, provenance = _git_proofs()
    if substitution == "source":
        source = replace(source, tree="9" * 40)
    elif substitution == "evidence":
        evidence = replace(evidence, source=replace(source, commit="9" * 40))
    elif substitution == "artifacts":
        provenance = replace(
            provenance,
            audit_artifacts=replace(ARTIFACTS, report_digest="sha256:" + "9" * 64),
        )
    elif substitution == "manifest":
        provenance = replace(
            provenance,
            file_evidence_manifest_digest="sha256:" + "9" * 64,
        )
    else:
        provenance = replace(provenance, attestation_sha256="9" * 64)

    with pytest.raises(AuditedSourceEligibilityError):
        qualify_verified_git_audited_source(
            source=source,
            file_evidence=evidence,
            provenance=provenance,
        )


def test_archive_eligibility_rejects_cross_proof_substitution() -> None:
    source, evidence, provenance = _archive_proofs()
    evidence = replace(
        evidence,
        source=replace(
            source,
            claim=replace(source.claim, archive_digest="sha256:" + "9" * 64),
        ),
    )

    with pytest.raises(AuditedSourceEligibilityError):
        qualify_verified_archive_audited_source(
            source=source,
            file_evidence=evidence,
            provenance=provenance,
        )

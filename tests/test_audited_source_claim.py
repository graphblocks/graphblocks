from __future__ import annotations

import json

import pytest

from tools.audited_source_claim import (
    AuditedSourceClaimError,
    IdentifiedArchiveAuditedSource,
    IdentifiedGitAuditedSource,
    LegacyUnavailableAuditedSource,
    UnavailableAuditedSource,
    audited_source_report_claim,
    decode_audited_source,
    load_strict_yaml_document,
)


LEGACY_UNAVAILABLE = {
    "status": "unavailable",
    "description": "User-provided graphblocks-main.zip",
    "gitRevision": None,
    "archiveDigest": None,
    "limitation": "Original archive identity is unavailable",
}
V2_UNAVAILABLE = {
    "schemaVersion": 2,
    "state": "unavailable",
    "description": "Original audit input",
    "limitation": "Original archive identity is unavailable",
}
V2_GIT = {
    "schemaVersion": 2,
    "state": "identified",
    "description": "Original audit input",
    "identity": {
        "kind": "git",
        "objectFormat": "sha1",
        "gitRevision": "1" * 40,
        "gitTree": "2" * 40,
    },
    "fileEvidenceManifestDigest": "sha256:" + "3" * 64,
    "provenanceBinding": {
        "kind": "signed-attestation",
        "digest": "sha256:" + "4" * 64,
    },
}
V2_ARCHIVE = {
    "schemaVersion": 2,
    "state": "identified",
    "description": "Recovered original audit input",
    "identity": {
        "kind": "archive",
        "archiveDigest": "sha256:" + "5" * 64,
    },
    "fileEvidenceManifestDigest": "sha256:" + "6" * 64,
    "provenanceBinding": {
        "kind": "recovered-audit-input",
        "digest": "sha256:" + "7" * 64,
    },
}


def test_legacy_audited_source_decodes_only_for_manifest_v1() -> None:
    claim = decode_audited_source(LEGACY_UNAVAILABLE, manifest_format_version=1)

    assert isinstance(claim, LegacyUnavailableAuditedSource)
    assert audited_source_report_claim(claim) == "unavailable"
    with pytest.raises(AuditedSourceClaimError, match="AUDIT_SOURCE_MALFORMED"):
        decode_audited_source(LEGACY_UNAVAILABLE, manifest_format_version=2)


def test_v2_audited_source_uses_closed_discriminated_variants() -> None:
    unavailable = decode_audited_source(V2_UNAVAILABLE, manifest_format_version=2)
    git = decode_audited_source(V2_GIT, manifest_format_version=2)
    archive = decode_audited_source(V2_ARCHIVE, manifest_format_version=2)

    assert isinstance(unavailable, UnavailableAuditedSource)
    assert isinstance(git, IdentifiedGitAuditedSource)
    assert isinstance(archive, IdentifiedArchiveAuditedSource)
    assert audited_source_report_claim(unavailable) == V2_UNAVAILABLE
    assert audited_source_report_claim(git) == V2_GIT
    assert audited_source_report_claim(archive) == V2_ARCHIVE


@pytest.mark.parametrize(
    "mutate",
    [
        lambda claim: claim.update({"unknown": True}),
        lambda claim: claim["identity"].pop("gitTree"),
        lambda claim: claim["identity"].update({"archiveDigest": "sha256:" + "5" * 64}),
        lambda claim: claim["identity"].update({"gitRevision": "A" * 40}),
        lambda claim: claim.update(
            {"fileEvidenceManifestDigest": "sha256:" + "3" * 63}
        ),
        lambda claim: claim["provenanceBinding"].update(
            {"kind": "recovered-audit-input"}
        ),
    ],
)
def test_v2_git_audited_source_rejects_partial_ambiguous_or_noncanonical_shapes(
    mutate: object,
) -> None:
    claim = json.loads(json.dumps(V2_GIT))
    assert callable(mutate)
    mutate(claim)

    with pytest.raises(AuditedSourceClaimError, match="AUDIT_SOURCE_MALFORMED"):
        decode_audited_source(claim, manifest_format_version=2)


@pytest.mark.parametrize(
    "document",
    [
        "auditedSource:\n  state: unavailable\n  state: identified\n",
        "auditedSource: &source\n  state: unavailable\ncopy: *source\n",
    ],
)
def test_strict_yaml_loader_rejects_duplicate_keys_and_aliases(document: str) -> None:
    with pytest.raises(AuditedSourceClaimError, match="AUDIT_SOURCE_YAML_MALFORMED"):
        load_strict_yaml_document(document.encode("utf-8"))


def test_strict_yaml_loader_enforces_byte_and_depth_budgets() -> None:
    with pytest.raises(AuditedSourceClaimError, match="AUDIT_SOURCE_YAML_BUDGET"):
        load_strict_yaml_document(b"a: " + b"x" * 64, max_bytes=16)

    nested: object = "leaf"
    for _index in range(8):
        nested = {"next": nested}
    with pytest.raises(AuditedSourceClaimError, match="AUDIT_SOURCE_YAML_BUDGET"):
        load_strict_yaml_document(
            json.dumps(nested).encode("utf-8"),
            max_depth=4,
        )

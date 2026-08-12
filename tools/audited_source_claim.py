"""Strict, versioned audited-source claim contracts for release evidence."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TypeAlias

import yaml  # type: ignore[import-untyped]
from yaml.nodes import MappingNode  # type: ignore[import-untyped]
from yaml.tokens import AliasToken, AnchorToken  # type: ignore[import-untyped]


SHA1_OBJECT_ID = re.compile(r"[0-9a-f]{40}")
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
DEFAULT_MAX_YAML_BYTES = 256 * 1024
DEFAULT_MAX_YAML_DEPTH = 64
DEFAULT_MAX_YAML_NODES = 20_000


class AuditedSourceClaimError(ValueError):
    """Raised when an audited-source claim is malformed or exceeds its budget."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class LegacyUnavailableAuditedSource:
    description: str
    limitation: str


@dataclass(frozen=True, slots=True)
class UnavailableAuditedSource:
    description: str
    limitation: str
    schema_version: int = 2


@dataclass(frozen=True, slots=True)
class ProvenanceBinding:
    kind: str
    digest: str


@dataclass(frozen=True, slots=True)
class IdentifiedGitAuditedSource:
    description: str
    git_revision: str
    git_tree: str
    file_evidence_manifest_digest: str
    provenance_binding: ProvenanceBinding
    schema_version: int = 2
    identity_kind: str = "git"
    object_format: str = "sha1"


@dataclass(frozen=True, slots=True)
class IdentifiedArchiveAuditedSource:
    description: str
    archive_digest: str
    file_evidence_manifest_digest: str
    provenance_binding: ProvenanceBinding
    schema_version: int = 2
    identity_kind: str = "archive"


AuditedSourceClaim: TypeAlias = (
    LegacyUnavailableAuditedSource
    | UnavailableAuditedSource
    | IdentifiedGitAuditedSource
    | IdentifiedArchiveAuditedSource
)


class _StrictSafeLoader(yaml.SafeLoader):
    def construct_mapping(
        self,
        node: MappingNode,
        deep: bool = False,
    ) -> dict[object, object]:
        if not isinstance(node, MappingNode):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "expected a mapping node",
                node.start_mark,
            )
        mapping: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as error:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable mapping key",
                    key_node.start_mark,
                ) from error
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def load_strict_yaml_document(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_YAML_BYTES,
    max_depth: int = DEFAULT_MAX_YAML_DEPTH,
    max_nodes: int = DEFAULT_MAX_YAML_NODES,
) -> object:
    """Load bounded YAML while rejecting duplicate keys, anchors, and aliases."""

    if type(data) is not bytes or len(data) > max_bytes:
        raise AuditedSourceClaimError(
            "AUDIT_SOURCE_YAML_BUDGET",
            "audit reproduction manifest exceeds its byte budget",
        )
    if (
        type(max_depth) is not int
        or type(max_nodes) is not int
        or max_depth < 1
        or max_nodes < 1
    ):
        raise ValueError("YAML structural budgets must be positive integers")
    try:
        text = data.decode("utf-8")
        if any(
            isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(text)
        ):
            raise AuditedSourceClaimError(
                "AUDIT_SOURCE_YAML_MALFORMED",
                "audit reproduction manifest must not contain anchors or aliases",
            )
        value = yaml.load(text, Loader=_StrictSafeLoader)
    except AuditedSourceClaimError:
        raise
    except RecursionError as error:
        raise AuditedSourceClaimError(
            "AUDIT_SOURCE_YAML_BUDGET",
            "audit reproduction manifest exceeds its parser depth budget",
        ) from error
    except (UnicodeError, yaml.YAMLError) as error:
        raise AuditedSourceClaimError(
            "AUDIT_SOURCE_YAML_MALFORMED",
            "audit reproduction manifest is not strict UTF-8 YAML",
        ) from error
    observed_nodes = 0
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        observed_nodes += 1
        if observed_nodes > max_nodes or depth > max_depth:
            raise AuditedSourceClaimError(
                "AUDIT_SOURCE_YAML_BUDGET",
                "audit reproduction manifest exceeds its structural budget",
            )
        if type(current) is dict:
            for key, item in current.items():
                pending.append((key, depth + 1))
                pending.append((item, depth + 1))
        elif type(current) is list:
            pending.extend((item, depth + 1) for item in current)
    return value


def _exact_mapping(
    value: object,
    fields: set[str],
    *,
    owner: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise AuditedSourceClaimError(
            "AUDIT_SOURCE_MALFORMED",
            f"{owner} must contain exactly {sorted(fields)!r}",
        )
    return value


def _exact_text(value: object, *, owner: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 1_024
    ):
        raise AuditedSourceClaimError(
            "AUDIT_SOURCE_MALFORMED",
            f"{owner} must be canonical non-empty text",
        )
    return value


def _sha256_digest(value: object, *, owner: str) -> str:
    if type(value) is not str or SHA256_DIGEST.fullmatch(value) is None:
        raise AuditedSourceClaimError(
            "AUDIT_SOURCE_MALFORMED",
            f"{owner} must be a lowercase prefixed SHA-256 digest",
        )
    return value


def decode_audited_source(
    value: object,
    *,
    manifest_format_version: int,
) -> AuditedSourceClaim:
    """Decode the exact legacy or v2 audited-source variant for a manifest."""

    if manifest_format_version == 1:
        raw = _exact_mapping(
            value,
            {"status", "description", "gitRevision", "archiveDigest", "limitation"},
            owner="legacy audited-source claim",
        )
        if (
            raw["status"] != "unavailable"
            or raw["gitRevision"] is not None
            or raw["archiveDigest"] is not None
        ):
            raise AuditedSourceClaimError(
                "AUDIT_SOURCE_MALFORMED",
                "legacy audited-source identity must remain explicitly unavailable",
            )
        return LegacyUnavailableAuditedSource(
            description=_exact_text(
                raw["description"], owner="legacy audited-source description"
            ),
            limitation=_exact_text(
                raw["limitation"], owner="legacy audited-source limitation"
            ),
        )
    if manifest_format_version != 2:
        raise AuditedSourceClaimError(
            "AUDIT_SOURCE_MALFORMED",
            "audited-source manifest format version is unsupported",
        )
    raw = _exact_mapping(
        value,
        (
            {"schemaVersion", "state", "description", "limitation"}
            if isinstance(value, dict) and value.get("state") == "unavailable"
            else {
                "schemaVersion",
                "state",
                "description",
                "identity",
                "fileEvidenceManifestDigest",
                "provenanceBinding",
            }
        ),
        owner="v2 audited-source claim",
    )
    if raw["schemaVersion"] != 2:
        raise AuditedSourceClaimError(
            "AUDIT_SOURCE_MALFORMED",
            "audited-source schemaVersion must be 2",
        )
    if raw["state"] == "unavailable":
        return UnavailableAuditedSource(
            description=_exact_text(
                raw["description"], owner="audited-source description"
            ),
            limitation=_exact_text(
                raw["limitation"], owner="audited-source limitation"
            ),
        )
    if raw["state"] != "identified":
        raise AuditedSourceClaimError(
            "AUDIT_SOURCE_MALFORMED",
            "audited-source state is unsupported",
        )
    identity = _exact_mapping(
        raw["identity"],
        (
            {"kind", "objectFormat", "gitRevision", "gitTree"}
            if isinstance(raw["identity"], dict)
            and raw["identity"].get("kind") == "git"
            else {"kind", "archiveDigest"}
        ),
        owner="audited-source identity",
    )
    binding = _exact_mapping(
        raw["provenanceBinding"],
        {"kind", "digest"},
        owner="audited-source provenance binding",
    )
    description = _exact_text(raw["description"], owner="audited-source description")
    evidence_digest = _sha256_digest(
        raw["fileEvidenceManifestDigest"],
        owner="audited-source file-evidence manifest digest",
    )
    binding_digest = _sha256_digest(
        binding["digest"],
        owner="audited-source provenance binding digest",
    )
    if identity["kind"] == "git":
        revision = identity["gitRevision"]
        tree = identity["gitTree"]
        if (
            identity["objectFormat"] != "sha1"
            or type(revision) is not str
            or SHA1_OBJECT_ID.fullmatch(revision) is None
            or type(tree) is not str
            or SHA1_OBJECT_ID.fullmatch(tree) is None
            or binding["kind"] != "signed-attestation"
        ):
            raise AuditedSourceClaimError(
                "AUDIT_SOURCE_MALFORMED",
                "Git audited-source identity or provenance binding is invalid",
            )
        return IdentifiedGitAuditedSource(
            description=description,
            git_revision=revision,
            git_tree=tree,
            file_evidence_manifest_digest=evidence_digest,
            provenance_binding=ProvenanceBinding(
                kind="signed-attestation",
                digest=binding_digest,
            ),
        )
    if identity["kind"] == "archive":
        archive_digest = _sha256_digest(
            identity["archiveDigest"],
            owner="audited-source archive digest",
        )
        if binding["kind"] != "recovered-audit-input":
            raise AuditedSourceClaimError(
                "AUDIT_SOURCE_MALFORMED",
                "archive audited-source provenance binding is invalid",
            )
        return IdentifiedArchiveAuditedSource(
            description=description,
            archive_digest=archive_digest,
            file_evidence_manifest_digest=evidence_digest,
            provenance_binding=ProvenanceBinding(
                kind="recovered-audit-input",
                digest=binding_digest,
            ),
        )
    raise AuditedSourceClaimError(
        "AUDIT_SOURCE_MALFORMED",
        "audited-source identity kind is unsupported",
    )


def audited_source_report_claim(
    claim: AuditedSourceClaim,
) -> str | dict[str, object]:
    """Return the exact signed-report representation for a decoded claim."""

    if isinstance(claim, LegacyUnavailableAuditedSource):
        return "unavailable"
    if isinstance(claim, UnavailableAuditedSource):
        return {
            "schemaVersion": claim.schema_version,
            "state": "unavailable",
            "description": claim.description,
            "limitation": claim.limitation,
        }
    if isinstance(claim, IdentifiedGitAuditedSource):
        identity: dict[str, object] = {
            "kind": claim.identity_kind,
            "objectFormat": claim.object_format,
            "gitRevision": claim.git_revision,
            "gitTree": claim.git_tree,
        }
    else:
        identity = {
            "kind": claim.identity_kind,
            "archiveDigest": claim.archive_digest,
        }
    return {
        "schemaVersion": claim.schema_version,
        "state": "identified",
        "description": claim.description,
        "identity": identity,
        "fileEvidenceManifestDigest": claim.file_evidence_manifest_digest,
        "provenanceBinding": {
            "kind": claim.provenance_binding.kind,
            "digest": claim.provenance_binding.digest,
        },
    }

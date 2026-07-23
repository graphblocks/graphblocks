from __future__ import annotations

from typing import Any

GRAPH_API_VERSION = "graphblocks.ai/v1"
PLUGIN_API_VERSION = "graphblocks.ai/v1"
LEGACY_GRAPH_API_VERSIONS = {
    "graphblocks.ai/v1alpha1",
    "graphblocks.ai/v1alpha2",
    "graphblocks.ai/v1alpha3",
}
LEGACY_PLUGIN_API_VERSIONS = {"graphblocks.ai/v1alpha1"}


class MigrationError(ValueError):
    """Raised when a versioned resource has no explicit migration path."""

    def __init__(self, code: str, message: str, *, path: str = "$.apiVersion") -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(f"{code} {path}: {message}")


def _record_source_version(document: dict[str, Any], previous: str) -> None:
    metadata = document.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        return
    annotations = metadata.setdefault("annotations", {})
    if not isinstance(annotations, dict):
        return
    annotations["graphblocks.ai/migratedFrom"] = previous


def _complete_legacy_plugin_blocks(document: dict[str, Any]) -> None:
    spec = document.get("spec")
    if not isinstance(spec, dict):
        return
    blocks = spec.get("blocks")
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block.setdefault("capabilities", [])
        block.setdefault("configSchema", {"type": "object"})


def _require_valid_migration_target(
    document: dict[str, Any],
    *,
    code: str,
    resource_name: str,
) -> None:
    # Imported lazily because schema loading depends on the canonical module,
    # which in turn exposes this migration API.
    from .schema import resource_schema_errors

    violations = resource_schema_errors(document)
    if not violations:
        return
    violation = violations[0]
    raise MigrationError(
        code,
        (
            f"legacy {resource_name} cannot be represented by the stable wire schema: "
            f"{violation.message}"
        ),
        path=violation.path,
    )


def _migrate_document(
    document: dict[str, Any],
    *,
    require_valid_target: bool,
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise TypeError("migration document must be a mapping")
    # Import lazily to avoid the canonical/migration module cycle. The
    # round-trip gives all later migration steps one bounded, trusted snapshot
    # instead of retaining caller-owned or stateful containers.
    from .canonical import canonical_dumps, canonical_loads

    try:
        migrated = canonical_loads(canonical_dumps(document))
    except (TypeError, ValueError) as error:
        raise ValueError("migration document must contain canonical JSON values") from error
    kind = migrated.get("kind")
    api_version = migrated.get("apiVersion")
    if kind == "Graph":
        if api_version == GRAPH_API_VERSION:
            if require_valid_target:
                _require_valid_migration_target(
                    migrated,
                    code="GB0002",
                    resource_name="Graph",
                )
            return migrated
        if (
            not isinstance(api_version, str)
            or api_version not in LEGACY_GRAPH_API_VERSIONS
        ):
            raise MigrationError(
                "GB0002",
                f"Graph apiVersion {api_version!r} has no migration to {GRAPH_API_VERSION}",
            )
        previous = str(api_version)
        migrated["apiVersion"] = GRAPH_API_VERSION
        _record_source_version(migrated, previous)
        if require_valid_target:
            _require_valid_migration_target(
                migrated,
                code="GB0002",
                resource_name="Graph",
            )
        return migrated

    if kind == "PluginManifest":
        if api_version == PLUGIN_API_VERSION:
            if require_valid_target:
                _require_valid_migration_target(
                    migrated,
                    code="GB2018",
                    resource_name="PluginManifest",
                )
            return migrated
        if (
            not isinstance(api_version, str)
            or api_version not in LEGACY_PLUGIN_API_VERSIONS
        ):
            raise MigrationError(
                "GB2002",
                f"PluginManifest apiVersion {api_version!r} has no migration to {PLUGIN_API_VERSION}",
            )
        previous = str(api_version)
        migrated["apiVersion"] = PLUGIN_API_VERSION
        _record_source_version(migrated, previous)
        _complete_legacy_plugin_blocks(migrated)
        if require_valid_target:
            _require_valid_migration_target(
                migrated,
                code="GB2018",
                resource_name="PluginManifest",
            )
    return migrated


def _migrate_document_unchecked(document: dict[str, Any]) -> dict[str, Any]:
    """Migrate for a validator that will report the complete target errors."""

    return _migrate_document(document, require_valid_target=False)


def migrate_document(document: dict[str, Any]) -> dict[str, Any]:
    """Return a valid stable-wire copy through an explicit migration.

    Unknown resource kinds are preserved because this function is shared by
    multi-document tooling. Graph and PluginManifest are stable resources, so
    their unknown, malformed, or non-representable versions fail instead of
    being relabelled.
    """

    return _migrate_document(document, require_valid_target=True)

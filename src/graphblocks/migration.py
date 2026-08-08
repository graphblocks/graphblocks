from __future__ import annotations

from collections.abc import Mapping
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


class NativeMigrationUnavailableError(RuntimeError):
    """Raised when the normative native migration boundary is unavailable."""


class NativeMigrationContractError(RuntimeError):
    """Raised when the native migration boundary returns an invalid result."""


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
    from .schema import resource_schema_errors_reference as resource_schema_errors

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
    if not isinstance(document, Mapping):
        raise TypeError("migration document must be a mapping")
    # Import lazily to avoid the canonical/migration module cycle. The
    # round-trip gives all later migration steps one bounded, trusted snapshot
    # instead of retaining caller-owned or stateful containers.
    from ._canonical_reference import canonical_dumps, canonical_loads

    try:
        migrated = canonical_loads(canonical_dumps(document))
    except (TypeError, ValueError, RuntimeError, LookupError) as error:
        raise ValueError(
            "migration document must contain canonical JSON values"
        ) from error
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


def migrate_document_reference(document: dict[str, Any]) -> dict[str, Any]:
    """Return the explicit Python reference migration result.

    Unknown resource kinds are preserved because this function is shared by
    multi-document tooling. Graph and PluginManifest are stable resources, so
    their unknown, malformed, or non-representable versions fail instead of
    being relabelled.
    """

    return _migrate_document(document, require_valid_target=True)


def migrate_document(document: dict[str, Any]) -> dict[str, Any]:
    """Return a valid stable-wire copy through the normative native migration."""

    if not isinstance(document, Mapping):
        raise TypeError("migration document must be a mapping")
    from ._canonical_reference import canonical_dumps, canonical_loads

    try:
        snapshot = canonical_loads(canonical_dumps(document))
    except (TypeError, ValueError, RuntimeError, LookupError) as error:
        cause = error.__cause__ if isinstance(error.__cause__, Exception) else error
        raise ValueError(
            "migration document must contain canonical JSON values"
        ) from cause

    unavailable_message = (
        "native GraphBlocks resource migration is unavailable; install "
        "graphblocks[runtime] or call migrate_document_reference explicitly"
    )
    try:
        import graphblocks_runtime
    except ImportError as error:
        raise NativeMigrationUnavailableError(
            f"{unavailable_message}: {error}"
        ) from error

    native_extension_available = getattr(
        graphblocks_runtime,
        "native_extension_available",
        None,
    )
    if callable(native_extension_available):
        try:
            available = native_extension_available()
        except Exception as error:
            raise NativeMigrationUnavailableError(
                f"{unavailable_message}: native extension availability check failed"
            ) from error
    else:
        available = True
    if available is not True:
        detail = None
        native_extension_status = getattr(
            graphblocks_runtime,
            "native_extension_status",
            None,
        )
        if callable(native_extension_status):
            try:
                status = native_extension_status()
            except Exception as error:
                raise NativeMigrationUnavailableError(
                    f"{unavailable_message}: native extension status check failed"
                ) from error
            if isinstance(status, Mapping):
                detail = status.get("error")
        if detail is not None and str(detail).strip():
            unavailable_message = f"{unavailable_message}: {detail}"
        raise NativeMigrationUnavailableError(unavailable_message)

    native_migration = getattr(graphblocks_runtime, "migrate_resource", None)
    if not callable(native_migration):
        raise NativeMigrationUnavailableError(
            f"{unavailable_message}: graphblocks_runtime does not expose migrate_resource"
        )
    try:
        payload = native_migration(snapshot)
    except Exception as error:
        raise NativeMigrationContractError(
            "native resource migration rejected a reference-valid document"
        ) from error
    if type(payload) is not dict or type(payload.get("ok")) is not bool:
        raise NativeMigrationContractError(
            "native resource migration result must be a closed result object"
        )
    if payload["ok"]:
        if set(payload) != {"document", "ok"} or type(payload["document"]) is not dict:
            raise NativeMigrationContractError(
                "native resource migration success must contain one document"
            )
        try:
            migrated = canonical_loads(canonical_dumps(payload["document"]))
        except (TypeError, ValueError, RuntimeError, LookupError) as error:
            raise NativeMigrationContractError(
                "native resource migration document must contain canonical JSON values"
            ) from error
        if type(migrated) is not dict:
            raise NativeMigrationContractError(
                "native resource migration success must contain one document"
            )
        return migrated

    if set(payload) != {"error", "ok"}:
        raise NativeMigrationContractError(
            "native resource migration failure must contain one error"
        )
    native_error = payload["error"]
    if (
        type(native_error) is not dict
        or set(native_error) != {"code", "message", "path"}
        or any(type(native_error[field]) is not str for field in native_error)
        or any(not native_error[field] for field in native_error)
    ):
        raise NativeMigrationContractError(
            "native resource migration error must be closed"
        )
    raise MigrationError(
        native_error["code"],
        native_error["message"],
        path=native_error["path"],
    )

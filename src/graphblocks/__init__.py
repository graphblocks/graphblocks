"""GraphBlocks candidate-stable package facade.

Only the C0/C1 surface declared in ``__all__`` is discoverable from the
package root. Historical preview aliases are resolved lazily for runtime
compatibility; new code must import preview capabilities from their defining
feature modules.
"""

from __future__ import annotations as _annotations

from typing import TYPE_CHECKING as _TYPE_CHECKING

from ._lazy_exports import resolve_lazy_export as _resolve_lazy_export
from ._root_compat import _ROOT_COMPAT_EXPORTS
from ._version import __version__ as __version__


if _TYPE_CHECKING:
    from .canonical import (
        canonical_loads as canonical_loads,
        canonical_dumps as canonical_dumps,
        canonical_hash as canonical_hash,
        normalize_graph as normalize_graph,
    )
    from .diagnostics import (
        Severity as Severity,
        Diagnostic as Diagnostic,
        DiagnosticSet as DiagnosticSet,
    )
    from .schema import (
        SchemaId as SchemaId,
        ResourceSchemaViolation as ResourceSchemaViolation,
        ResourceValidationError as ResourceValidationError,
        resource_schema_errors as resource_schema_errors,
        validate_resource as validate_resource,
    )
    from .migration import (
        migrate_document as migrate_document,
    )
    from .plugins import (
        validate_plugin_manifest as validate_plugin_manifest,
        PluginManifest as PluginManifest,
        OutputRequirednessPredicate as OutputRequirednessPredicate,
        PortDescriptor as PortDescriptor,
        ResourceSlotDescriptor as ResourceSlotDescriptor,
        BlockDescriptor as BlockDescriptor,
        BlockCatalog as BlockCatalog,
    )
    from .compiler import (
        Plan as Plan,
        NativeCompilerUnavailableError as NativeCompilerUnavailableError,
        compile_graph as compile_graph,
    )
    from .runtime import (
        BlockCallable as BlockCallable,
        LocalJournalKind as LocalJournalKind,
        LocalTerminalJournalKind as LocalTerminalJournalKind,
        CancellationToken as CancellationToken,
        LocalJournalRecord as LocalJournalRecord,
        LocalExecutionJournal as LocalExecutionJournal,
        RuntimeRegistry as RuntimeRegistry,
        LocalRunResult as LocalRunResult,
        LocalRuntime as LocalRuntime,
        core_stdlib_registry as core_stdlib_registry,
    )


def __getattr__(name: str) -> object:
    return _resolve_lazy_export(
        module_name=__name__,
        package_name=__package__,
        namespace=globals(),
        export_modules=_ROOT_COMPAT_EXPORTS,
        name=name,
    )


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})


__all__ = [
    "canonical_loads",
    "canonical_dumps",
    "canonical_hash",
    "normalize_graph",
    "Severity",
    "Diagnostic",
    "DiagnosticSet",
    "SchemaId",
    "ResourceSchemaViolation",
    "ResourceValidationError",
    "resource_schema_errors",
    "validate_resource",
    "migrate_document",
    "validate_plugin_manifest",
    "PluginManifest",
    "OutputRequirednessPredicate",
    "PortDescriptor",
    "ResourceSlotDescriptor",
    "BlockDescriptor",
    "BlockCatalog",
    "Plan",
    "NativeCompilerUnavailableError",
    "compile_graph",
    "BlockCallable",
    "LocalJournalKind",
    "LocalTerminalJournalKind",
    "CancellationToken",
    "LocalJournalRecord",
    "LocalExecutionJournal",
    "RuntimeRegistry",
    "LocalRunResult",
    "LocalRuntime",
    "core_stdlib_registry",
]

"""GraphBlocks Phase 0 contract toolkit."""

from __future__ import annotations

from .canonical import canonical_dumps, canonical_hash, normalize_graph
from .compiler import Plan, compile_graph
from .diagnostics import Diagnostic, DiagnosticSet
from .loader import load_documents
from .leases import InMemoryLeasePool, Lease, LeaseUnavailableError
from .migration import migrate_document
from .packages import load_package_catalog, package_rows
from .plugins import PluginManifest, PluginRegistry, discover_plugins, load_plugin_manifest, validate_plugin_manifest
from .runtime import (
    CancellationToken,
    ExecutionJournal,
    InProcessRuntime,
    JournalStateError,
    RunResult,
    RuntimeRegistry,
    SQLiteExecutionJournal,
    stdlib_registry,
)
from .run_store import InMemoryRunStore, RunRecord, SQLiteRunStore, StateConflictError

__version__ = "0.1.0"

__all__ = [
    "Diagnostic",
    "DiagnosticSet",
    "Plan",
    "PluginManifest",
    "PluginRegistry",
    "CancellationToken",
    "ExecutionJournal",
    "InProcessRuntime",
    "JournalStateError",
    "__version__",
    "InMemoryRunStore",
    "InMemoryLeasePool",
    "RunResult",
    "RunRecord",
    "RuntimeRegistry",
    "SQLiteRunStore",
    "SQLiteExecutionJournal",
    "Lease",
    "LeaseUnavailableError",
    "StateConflictError",
    "canonical_dumps",
    "canonical_hash",
    "compile_graph",
    "discover_plugins",
    "load_package_catalog",
    "load_documents",
    "load_plugin_manifest",
    "migrate_document",
    "normalize_graph",
    "package_rows",
    "stdlib_registry",
    "validate_plugin_manifest",
]

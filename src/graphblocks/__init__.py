"""GraphBlocks Phase 0 contract toolkit."""

from __future__ import annotations

from .canonical import canonical_dumps, canonical_hash, normalize_graph
from .compiler import Plan, compile_graph
from .diagnostics import Diagnostic, DiagnosticSet
from .documents import (
    ArtifactRef,
    AssetRevision,
    DocumentChunk,
    DocumentElement,
    DocumentSpan,
    ParsedDocument,
    SourceAsset,
    SourceLocation,
    SourceRef,
    chunk_document_by_lines,
    create_local_text_revision,
    parse_plain_text_document,
)
from .loader import load_documents
from .leases import InMemoryLeasePool, Lease, LeaseUnavailableError
from .migration import migrate_document
from .packages import load_package_catalog, package_rows
from .plugins import (
    BlockCatalog,
    BlockDescriptor,
    PluginManifest,
    PluginRegistry,
    PortDescriptor,
    ResourceSlotDescriptor,
    discover_plugins,
    load_plugin_manifest,
    validate_plugin_manifest,
)
from .rag import InMemoryChunkRetriever, KnowledgeItemRef, SearchHit, knowledge_item_from_chunk
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
    "BlockCatalog",
    "BlockDescriptor",
    "ArtifactRef",
    "AssetRevision",
    "DocumentChunk",
    "DocumentElement",
    "DocumentSpan",
    "PluginManifest",
    "PluginRegistry",
    "PortDescriptor",
    "ParsedDocument",
    "ResourceSlotDescriptor",
    "SourceAsset",
    "SourceLocation",
    "SourceRef",
    "CancellationToken",
    "ExecutionJournal",
    "InProcessRuntime",
    "JournalStateError",
    "__version__",
    "InMemoryRunStore",
    "InMemoryLeasePool",
    "InMemoryChunkRetriever",
    "KnowledgeItemRef",
    "RunResult",
    "RunRecord",
    "RuntimeRegistry",
    "SQLiteRunStore",
    "SQLiteExecutionJournal",
    "SearchHit",
    "Lease",
    "LeaseUnavailableError",
    "StateConflictError",
    "canonical_dumps",
    "canonical_hash",
    "compile_graph",
    "chunk_document_by_lines",
    "create_local_text_revision",
    "discover_plugins",
    "load_package_catalog",
    "load_documents",
    "load_plugin_manifest",
    "knowledge_item_from_chunk",
    "migrate_document",
    "normalize_graph",
    "package_rows",
    "parse_plain_text_document",
    "stdlib_registry",
    "validate_plugin_manifest",
]

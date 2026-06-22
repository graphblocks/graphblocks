"""GraphBlocks Phase 0 contract toolkit."""

from __future__ import annotations

from .canonical import canonical_dumps, canonical_hash, normalize_graph
from .compiler import Plan, compile_graph
from .diagnostics import Diagnostic, DiagnosticSet
from .loader import load_documents
from .migration import migrate_document

__version__ = "0.1.0"

__all__ = [
    "Diagnostic",
    "DiagnosticSet",
    "Plan",
    "__version__",
    "canonical_dumps",
    "canonical_hash",
    "compile_graph",
    "load_documents",
    "migrate_document",
    "normalize_graph",
]

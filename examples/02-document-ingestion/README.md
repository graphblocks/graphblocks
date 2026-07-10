# Document Ingestion

This durable job snapshots a source, processes changed assets with per-item
checkpoints and ordered parser fallback, propagates ACL revisions through
documents, chunks, and the staging index, and reports deletion outcomes.

```bash
python examples/02-document-ingestion/run.py
```

The runner validates the outer and embedded worker graphs, then executes
parser-fallback and ACL-lineage gates with in-memory parser and index fakes.

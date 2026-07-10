# Document Ingestion

This durable job snapshots a source, processes changed assets with per-item
checkpoints and ordered parser fallback, propagates ACL revisions through
documents, chunks, and the staging index, and reports deletion outcomes.

```bash
python examples/02-document-ingestion/run.py
```

The runner validates both the outer ingestion graph and the per-asset graph.
The acceptance application separately executes parser-lock and ACL-lineage
checks.

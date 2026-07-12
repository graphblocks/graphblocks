# Marker Document Ingestion

This durable job snapshots a source, processes changed assets with per-item
checkpoints, parses PDFs with Marker, falls back to the lightweight PDF text
adapter when Marker does not pass the quality gate, propagates ACL revisions
through documents, chunks, and the staging index, and reports deletion
outcomes.

Install Marker separately before using the real parser:

```bash
python -m pip install 'marker-pdf>=1.10,<1.11'
```

Register the Marker descriptor in the worker that implements
`document.convert@1`:

```python
from graphblocks.document_parsers import DocumentParserRegistry
from graphblocks.integrations.pdf import marker_pdf_parser_descriptor

parsers = DocumentParserRegistry()
parsers.register(marker_pdf_parser_descriptor())
```

The descriptor lazily creates one Marker model set, disables image extraction
and external LLM use, requests chunk output, and maps Marker pages, block types,
section ancestry, and bounding boxes into GraphBlocks document lineage. The
first real parse can download model weights and may require substantial CPU,
GPU, memory, and disk resources; pre-warm the model cache for offline workers.

```bash
python examples/02-document-ingestion/run.py
```

The runner validates the outer and embedded worker graphs, then executes
the real GraphBlocks Marker adapter with an injected deterministic converter,
exercises Marker-to-`pdf-text` fallback, and verifies ACL lineage without
downloading models or making network requests.

Marker code is GPL-3.0-or-later and its model weights have separate terms.
Review the upstream [code license](https://github.com/datalab-to/marker/blob/v1.10.2/LICENSE)
and [model license](https://github.com/datalab-to/marker/blob/v1.10.2/MODEL_LICENSE)
before deployment. Marker is intentionally not a default GraphBlocks
dependency.

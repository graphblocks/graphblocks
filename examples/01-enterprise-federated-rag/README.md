# Enterprise Federated RAG

This example combines dense and keyword retrieval, fusion, reranking, a bounded
context builder, grounded answer generation, citation validation, and
abstention. Its binding document shows how provider and index choices remain
outside the portable graph.

Validate the resources and execute the graph with recording retriever/reranker
fakes and a scripted LLM, without contacting the named providers:

```bash
python examples/01-enterprise-federated-rag/run.py
```

The enterprise RAG acceptance application additionally executes citation and
abstention semantic gates.

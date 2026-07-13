# LLM-as-an-Interviewer: RAG vs No RAG

This example is an executable, offline benchmark that compares the same answer
model with and without retrieval. A scripted interviewer reads a reference
snapshot, creates three factual questions, and scores both candidates against
the hidden reference answer.

The comparison controls the main sources of avoidable bias:

- Both candidates receive the exact same question IDs and text.
- Both use the same scripted answer model; only the RAG candidate receives a
  rendered `ContextPack` from `InMemoryChunkRetriever`.
- Candidate identities are hidden behind `A` and `B`, and their order alternates
  by question before the interviewer scores them.
- Scores use `Decimal`, record provider usage, and produce a canonical evidence
  digest. `MetricObservation`, `evaluate_gate`, and `TrialResult` carry the
  aggregate comparison.

The authoring graph is physically split along those responsibilities:

```text
example.yaml
binding.yaml
fragments/
  interview-setup.yaml
  answer-variants.yaml
  blind-evaluation.yaml
```

`example.yaml` declares three typed slots and connects their placeholder nodes.
The local model and retriever resources stay in the imported `binding.yaml`.
Composition expands the fragment nodes with their placeholder prefix (for
example, `variants__ragAnswer`) before validation, compilation, or execution.

Run it from the repository root after the development install:

```bash
python examples/13-llm-interviewer-rag-benchmark/run.py
```

To inspect the ordinary materialized `Graph` and imported `Binding` directly:

```bash
graphblocks compose examples/13-llm-interviewer-rag-benchmark/example.yaml
```

The final JSON includes each interview turn, retrieved item IDs, blinded order,
RAG and no-RAG scores, means, score delta, win rate, gate decision, usage, and an
evidence digest. The checked-in fixture produces a RAG mean of `1`, a no-RAG
mean of `0.2`, and a delta of `0.8`.

These scripted scores demonstrate the benchmark contract; they are not a claim
about general model quality. For a real benchmark, replace the scripted
providers while keeping the answer model and decoding settings identical,
snapshot the interviewer prompt/model and dataset, run multiple repetitions,
and set promotion thresholds from observed variance.

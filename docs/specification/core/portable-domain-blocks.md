# Portable Domain Blocks

The standard Python and native Rust runtimes MUST resolve the block identities
in this document. Provider-neutral blocks execute their existing GraphBlocks
domain semantics. Operations that normally depend on an external model,
retriever, check runner, or reviewer accept already-resolved data at the block
boundary; adapters and tests MUST NOT fabricate a successful external result.

## Model and RAG

`model.structured_generate@1` validates that `config.outputSchema` identifies
the expected contract and projects JSON supplied through `inputs.response` or
a deterministic offline `config.response`. It returns `value`, `response`,
optional `items`, `schemaId`, the compatibility alias `schemaRef`, and
`contentDigest`. A missing response or a non-JSON object/array fails closed.

`retrieve.execute_plan@1` accepts a query and resolved retrieval source records
through `inputs.sources` or `config.sources`. It applies the configured minimum
successful-source policy and returns `result` plus normalized `sources`.
Network and database clients remain resource adapters rather than stdlib
dependencies.

`retrieve.fuse@1` deterministically fuses source hit lists.
`rank.documents@1` deterministically reranks supplied hits.
`context.build@1` creates a bounded context pack from supplied evidence.
`answer.validate_grounding@1` validates a typed answer against that context and
returns `candidate`, `response`, `result`, and `validation`. When configured to
abstain, insufficient evidence returns a graph-compatible abstention candidate
instead of an unsupported answer.

## Evaluation, review, and results

`check.run_suite@1` normalizes configured or injected check outcomes. A check
without an implementation or outcome becomes `inconclusive`; it MUST NOT pass
implicitly. The block returns `results`, `checks`, `diagnostics`, `passed`, and
`hardGatePassed`.

`gate.evaluate@1` applies required-check and metric constraints using the typed
gate evaluator. It returns `result`, `decision`, `passed`, and `violations`.

`review.request@1` always creates a subject-bound review request. Without a
review response it returns a pending application work item and never fabricates
approval. An injected review must match the subject digest and requested scope;
when `requiredCredential` is configured, its reference must be present. The
block returns `request`, `record`, `accepted`/`approved`, `status`, and
`waitMode`. Durable notification and wait/resume remain application/runtime
responsibilities.

`result.bundle@1` creates a canonical result bundle from outputs and optional
evidence, checks, metrics, artifacts, diagnostics, reviews, and gate data. It
returns `result`, the `bundle` alias, and `contentDigest`. Persisting or
publishing the bundle is a separate effect.

## Conformance

Each identity MUST have:

1. a descriptor in the builtin plugin catalog;
2. a callable in the stock Python registry;
3. native Rust dispatch when the native stdlib profile is claimed;
4. deterministic negative behavior for missing external implementations; and
5. graph-level tests that execute the real block rather than a fixture block.

External APIs are mocked by supplying their resolved response contracts. Tests
MUST still exercise GraphBlocks-owned fusion, ranking, context, grounding,
gate, review, and bundle logic directly.

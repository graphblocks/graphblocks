# Part IV. Retrieval, RAG, Context, Citation

## 100. 공개 추상화

GraphBlocks의 공개 검색 추상화는 `Retriever`다.

```rust
#[async_trait]
pub trait Retriever: Send + Sync {
    async fn retrieve(
        &self,
        request: SearchRequest,
        ctx: &ExecutionContext,
    ) -> Result<RetrievalResult, RetrievalError>;
}
```

VectorStore는 implementation detail일 수 있다. 다음 모두가 Retriever가 될 수 있다.

- BM25/keyword search
- dense vector search
- hybrid search
- hosted file search
- web search
- SQL/full-text search
- federated enterprise search
- graph search
- custom service

## 101. KnowledgeIndex와 Retriever 분리

```text
KnowledgeIndex
- document/chunk write
- delete
- metadata/ACL update
- revision publish
- health/capabilities

Retriever
- query execution
- filter
- top-k
- search result semantics
```

하나의 backend가 두 interface를 모두 구현할 수 있지만 GraphSpec port와 테스트는 분리한다.

`DocumentStore`라는 이름은 일반 RecordStore와 retrieval knowledge store를 혼동하므로 사용하지 않는다.

Knowledge index records, write reports, publish results, capabilities, and health summaries MUST
validate their construction-time wire shape. Indexed chunks remain typed `DocumentChunk` records,
record status is limited to active or tombstoned, write report affected counts match the chunk ID
set, publish identities are non-empty, capability flags are booleans, and health counters are
non-negative with active plus tombstoned chunks not exceeding total indexed chunks.

Ingestion manifest와 index record boundary는 non-empty manifest/asset/revision/processor/index identity,
valid ingestion lifecycle status, processor reference records, object-shaped metadata, typed artifact references,
and index record asset/revision consistency를 검증해야 한다. Failed manifest는 non-empty error를 가져야 하며
non-failed manifest는 stale failure error를 보존해서는 안 된다.

## 102. RetrievalResult

```python
class RetrievalResult(BaseModel):
    retrieval_id: str
    request: SearchRequest
    hits: list[SearchHit]
    total_candidates: int | None = None
    latency_ms: float | None = None
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

RAG request, retrieval result, hit, knowledge item reference, federated source, and context pack records
MUST validate their wire shape at construction boundaries. Identity fields are non-empty strings, scores
and latency are finite numeric values, ranks are positive, token counts are non-negative and must not
exceed declared context budgets, metadata is object-shaped with string keys, and nested references such
as `SourceRef`, `KnowledgeItemRef`, `SearchHit`, and `SearchRequest` remain typed records.

모든 hit는 rank, score semantics, source chunk, retriever ID를 가진다.

## 103. Retrieval strategy

표준 semantic block:

```text
retrieve.keyword
retrieve.dense
retrieve.hybrid
retrieve.hosted
retrieve.federated
retrieve.web
```

### Dense retrieval

Query embedder와 document embedding model compatibility를 검증한다.

```text
model family
revision
dimension
normalization
distance metric
```

### Keyword retrieval

Analyzer, language, stemming, stop-word config를 provenance에 기록한다.

### Hybrid retrieval

```yaml
nodes:
  retrieve:
    block: retrieve.hybrid@1
    connection: knowledge
    config:
      keywordWeight: 0.35
      denseWeight: 0.65
      candidateK: 80
      topK: 20
      fusion: reciprocal_rank
```

Raw score 선형 합산은 각 score scale이 호환된다는 근거가 있을 때만 허용한다.

### Hosted retrieval

Provider-managed file/vector search는 canonical `SearchHit`로 변환한다. Provider가 page/char span을 제공하지 않으면 citation precision 제한을 warning으로 기록한다.

## 104. Query processing

표준 block:

```text
query.normalize
query.rewrite
query.expand
query.decompose
query.translate
query.embed
query.route
```

Query rewrite는 원문 query를 보존하고 다음을 반환한다.

```python
class QueryPlan(BaseModel):
    original: str
    rewritten: list[str]
    subqueries: list[str] = Field(default_factory=list)
    filters: FilterExpr | None = None
    rationale_summary: str | None = None
```

내부 reasoning chain을 저장하지 않고, operationally useful한 rewrite provenance만 기록한다.

## 105. Retrieval filter와 authorization

Protected corpus에서 `AuthContext` 없는 retrieve 호출은 compile 또는 runtime policy error여야 한다.

```python
class AuthContext(BaseModel):
    tenant_id: str
    principal_id: str
    groups: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
```

ACL enforcement:

```text
source ACL
→ revision ACL
→ document/chunk ACL
→ index payload ACL
→ retrieval filter
→ post-retrieval authorization verification
→ citation authorization verification
```

Post-filter만으로 보안을 구현하지 않는다. Unauthorized hit가 top-k 후보를 차지하면 결과 품질과 정보 노출 위험이 생긴다.

## 106. Federated retrieval

```text
HR index
+ policy index
+ ticket search
+ web search
→ canonical hits
→ normalize/dedupe/fuse
```

각 source는 timeout, quota, trust, cost, freshness를 가진다.

```yaml
nodes:
  federated:
    block: retrieve.federated@1
    config:
      sources:
        - retriever: hr
          weight: 1.0
          timeout: 800ms
        - retriever: policy
          weight: 0.8
          timeout: 800ms
        - retriever: web
          weight: 0.3
          timeout: 1500ms
      failureMode: partial
```

## 107. Fusion

표준 block:

```text
retrieve.fuse
```

지원 전략:

```text
concatenate
reciprocal_rank_fusion
weighted_rank
normalized_score
interleave
```

Fusion output은 원래 source rank와 fusion score를 모두 보존한다.

## 108. Deduplication

검색 결과 중복 판단 기준:

```text
same chunk_id
same source span
same canonical asset
near-duplicate text
parent-child overlap
```

중복 제거 시 citation 가능한 source를 임의로 하나만 버리지 않는다. 대표 hit에 alternate sources를 연결할 수 있다.

## 109. Reranking

표준 block:

```text
rank.cross_encoder
rank.model
rank.rule
rank.diversity
rank.recency
```

```python
class RankedHit(BaseModel):
    hit: SearchHit
    rerank_score: float | None = None
    reranker: str | None = None
    explanation: str | None = None
```

Reranker input limit과 truncation 정책을 기록한다.
`RankedHit`와 `RerankResult` records는 typed `SearchHit`/`RankedHit` entries, finite rerank scores,
non-empty reranker IDs when present, non-negative input/evaluated counts, and typed truncation IDs를
검증해야 한다. Evaluated count는 input count를 초과할 수 없고 ranked hit 수는 evaluated count를 초과할 수 없다.

## 110. Diversity와 coverage

단순 top score만 선택하면 같은 section의 유사 chunk로 context가 채워질 수 있다.

```text
MMR
per-document cap
per-section cap
source diversity
recency quota
required source coverage
```

Context selection policy가 이러한 제약을 표현해야 한다.

## 111. ContextBuilder

```yaml
nodes:
  context:
    block: context.build@1
    config:
      tokenBudget: 48000
      reserveOutputTokens: 3000
      priorities:
        instructions: 100
        currentMessage: 100
        toolResults: 90
        retrievedContext: 80
        recentHistory: 70
        memory: 50
      overflow:
        strategy: summarize_then_truncate
      retrieval:
        perDocumentMaxChunks: 4
        deduplicate: true
```

Context build 단계:

```text
collect candidates
→ authorization verify
→ trust label
→ deduplicate
→ score/priority combine
→ token estimate
→ select
→ optional compress/summarize
→ final token count
→ ContextPack
```

## 112. Trust boundary

Retrieved document content는 `retrieved_untrusted`다.

```text
trusted system/developer instructions
> application context
> user content
> retrieved untrusted content
> tool result, by declared trust
```

문서 안의 지시문이 application tool permission이나 system policy를 변경해서는 안 된다.

Context renderer는 source를 명확한 delimiter와 metadata로 구분해야 한다.

## 113. Context compression

```text
context.select
context.compress.extractive
context.compress.model
context.summarize
context.order
```

Compression 결과는 source span mapping을 유지해야 한다. Model summary가 새로운 claim을 만들 수 있으므로 `derived_from` source IDs와 model provenance를 기록한다.

## 114. Prompt assembly

RAG prompt는 다음 입력을 분리한다.

```text
instructions
conversation
retrieved context
current question
output contract
```

Prompt template가 retrieval raw hit object를 직접 serialize하지 않고 `ContextPack`을 받도록 권장한다.

## 115. Answer assembly

Model output을 그대로 final API response로 취급하지 않는다.

```text
ModelResponse
+ query
+ ContextPack
+ source documents
+ provider metadata
→ Answer
```

표준 block:

```text
answer.build
answer.attach_citations
answer.validate_citations
answer.validate_grounding
answer.abstain
```

Answer, claim, citation, abstention, citation validation issue/result, citation source trace,
and RAG result payload records MUST validate typed nested records and object-shaped metadata at
construction boundaries. Citation IDs, claim IDs, answer IDs, abstention reasons, issue codes, and
trace identities are non-empty strings; optional quoted citation text and claim links are non-empty
when present; citation confidence is finite and between 0 and 1. Semantic citation validity, such as
missing citation references or sources outside the current context, remains the responsibility of
`answer.validate_citations` and related validation blocks.

## 116. Citation production mode

지원 방식:

```text
model_inline_marker
structured_citation_output
posthoc_alignment
provider_native_annotation
```

### Inline marker

예: `[S1]`은 rendering format일 뿐 source of truth가 아니다. `S1`은 `SourceRef`와 해당 locator로 resolve되어야 한다.

### Structured output

Model이 claim과 source IDs를 구조화 반환하도록 할 수 있다.

### Posthoc alignment

Generated answer span과 context source를 별도 aligner가 연결한다. Alignment uncertainty를 보존해야 한다.

## 117. Citation validation

검사:

- citation ID가 존재하는가
- source가 current context에 포함되었는가
- principal이 source를 볼 권한이 있는가
- cited text가 source span과 일치하는가
- claim이 cited source로 지지되는가
- 페이지/cell/slide reference가 유효한가

Validation 실패 policy:

```text
warn
repair
remove_invalid
abstain
fail
```

## 118. Abstention

다음 조건에서 답변 보류를 지원한다.

- relevant hit 없음
- ACL로 모두 제거됨
- context 부족
- citation validation 실패
- conflicting sources
- requested freshness 보장 불가

```python
class Abstention(BaseModel):
    reason: str
    user_message: str
    diagnostics: dict[str, JsonValue] = Field(default_factory=dict)
```

## 119. Freshness와 source quality

SearchHit metadata에 다음을 둘 수 있다.

```text
source_modified_at
indexed_at
valid_from/valid_to
authority level
review status
```

Context selection은 사용자 요구와 domain policy에 따라 freshness 또는 authority를 고려한다.
Freshness 비교에 사용하는 `source_modified_at`, `indexed_at`, `valid_from`, `valid_to`,
`minimum_source_modified_at` 값은 ISO 8601 datetime으로 해석한다. Runtime과
평가 도구는 문자열 정렬이 아니라 timezone-normalized instant 비교를 사용해야 하며,
offset 표기가 다른 동일 시각을 동일하게 처리해야 한다.
공유 RAG TCK는 offset 표기가 섞인 freshness filtering과 freshness satisfaction 계산을
검증해야 한다.

## 120. RAG graph 예

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: grounded-rag-answer
  version: 1.0.0

spec:
  profile: request_response

  inputs:
    question:
      type: string
    conversation:
      type: graphblocks.ai/ConversationView@1
    auth:
      type: graphblocks.ai/AuthContext@1

  outputs:
    answer:
      type: graphblocks.ai/Answer@1
    deltas:
      type: graphblocks.ai/GenerationChunk@1
      mode: incremental

  connections:
    model: answer-model
    knowledge: company-knowledge

  nodes:
    rewrite:
      block: query.rewrite@1
      connection: model

    retrieve:
      block: retrieve.hybrid@1
      connection: knowledge
      config:
        candidateK: 80
        topK: 20

    rerank:
      block: rank.cross_encoder@1
      config:
        topK: 10

    context:
      block: context.build@1
      config:
        tokenBudget: 32000
        perDocumentMaxChunks: 4

    prompt:
      block: prompt.registry@1
      config:
        ref: company/rag-answer@production

    render:
      block: prompt.render@1

    generate:
      block: model.chat@1
      connection: model
      flow:
        retry: model-read

    answer:
      block: answer.build@1

    validate:
      block: answer.validate_grounding@1
      config:
        citationRequired: true
        onInsufficientContext: abstain

  edges:
    - from: $input.question
      to: rewrite.query
    - from: rewrite.plan
      to: retrieve.query
    - from: $input.auth
      to: retrieve.auth
    - from: retrieve.result
      to: rerank.hits
    - from: rerank.hits
      to: context.retrieval
    - from: $input.conversation
      to: context.conversation
    - from: prompt.template
      to: render.template
    - from: context.context
      to: render.variables.context
    - from: $input.question
      to: render.variables.question
    - from: render.messages
      to: generate.messages
    - from: generate.deltas
      to: $output.deltas
    - from: generate.response
      to: answer.response
    - from: context.context
      to: answer.context
    - from: answer.answer
      to: validate.answer
    - from: validate.answer
      to: $output.answer
```

## 121. Hosted file search adapter

Hosted retrieval adapter는 다음을 canonicalize한다.

```text
provider file/store IDs
provider annotations
provider ranking options
provider citation metadata
usage/cost
```

GraphBlocks는 provider store를 자체 `KnowledgeIndex`로 가장하지 않고 capability를 명확히 선언한다.

```yaml
capabilities:
  write: provider_managed
  delete: true
  keyword_search: unknown
  dense_search: true
  filter: limited
  source_span: file_level
```

## 122. RAG evaluation

### Retrieval metrics

```text
Recall@K
Precision@K
MRR
MAP
NDCG
coverage
ACL precision
freshness satisfaction
```

### Context metrics

```text
context relevance
context precision
source diversity
token efficiency
lost-in-the-middle sensitivity
```

### Answer metrics

```text
answer relevance
faithfulness
citation precision
citation recall
citation source accuracy
abstention precision/recall
unsupported claim rate
```

Evaluation pipeline은 production RAG 실행과 분리할 수 있어야 한다.

```text
immutable ResultBundle
→ retrieval evaluators
→ answer evaluators
→ policy evaluators
```

## 123. RAG result bundle profile

RAG 결과는 별도 source-of-truth hierarchy를 만들지 않고 generic `ResultBundle`에 typed payload를 추가한다.

```python
class RagResultPayload(BaseModel):
    query_plan: QueryPlan
    retrievals: list[RetrievalResult]
    context: ContextPack
    model_response: ModelResponse
    answer: Answer

class RagResultBundle(BaseModel):
    base: ResultBundle
    profile: Literal["rag"] = "rag"
    payload: RagResultPayload
```

이 bundle을 저장하면 evaluator를 바꿔도 원래 provider 호출을 재실행하지 않고 평가할 수 있다.

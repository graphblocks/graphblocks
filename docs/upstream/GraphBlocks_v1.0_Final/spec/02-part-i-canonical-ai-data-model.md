# Part I. 제품 범위와 Canonical AI Data Model

## 10. 제품 포지셔닝

GraphBlocks는 다음 제품군을 우선 지원한다.

1. 파일 업로드와 직접 분석
2. 문서 ingestion, 변환, OCR, chunking, indexing
3. 검색과 근거 기반 답변(RAG)
4. multi-turn chatbot과 attachment conversation
5. structured extraction, classification, translation, summarization
6. tool-using agent와 승인 기반 effect workflow
7. retrieval 및 generation evaluation
8. 장시간 실행되는 문서 생성 및 batch job

Voice와 realtime media는 위의 `Conversation`, `Message`, `ToolCall`, `ModelResponse`를 재사용하는 확장이다. Voice extension이 일반 conversation model을 정의해서는 안 된다.

## 11. 대표 사용자 시나리오

### 직접 파일 분석

```text
사용자 메시지 + PDF/DOCX/XLSX attachment
→ 파일 입력 전략 선택
→ 필요 시 parsing/OCR
→ context budget 구성
→ model 또는 tool 실행
→ answer + citation + generated artifact
```

### 영구 지식베이스 ingestion

```text
source discover
→ fingerprint/revision detect
→ convert/OCR
→ canonical document
→ normalize/enrich/split
→ embedding/index write
→ manifest commit
→ index publish
```

### RAG chatbot

```text
conversation + current message
→ query rewrite
→ ACL-aware retrieval
→ fusion/reranking
→ context selection
→ prompt render
→ model incremental output
→ answer/citation validation
→ conversation append
```

### Structured extraction

```text
file(s)
→ canonical document
→ relevant section selection
→ structured generation
→ JSON Schema validation
→ repair or reject
→ record/artifact write
```

### Agent workflow

```text
conversation state
→ model requests tool
→ policy and approval
→ tool execution
→ tool result append
→ model continues
→ final answer or explicit stop condition
```

## 12. 실행 특성 모델

`invocation`, `realtime_session`, `durable_dataflow`를 동일한 단일 enum으로 강제하지 않는다. 실행 특성은 직교 축으로 표현한다.

```yaml
execution:
  lifetime: conversation       # invocation | conversation | job | session
  input_mode: value            # value | bounded_sequence | unbounded_stream | duplex
  output_mode: incremental     # value | incremental | bounded_sequence | stream
  durability: checkpointed     # ephemeral | checkpointed | durable
  delivery: at_most_once       # best_effort | at_most_once | at_least_once
```

### 권장 preset

| Preset | lifetime | input | output | durability | 대표 용도 |
|---|---|---|---|---|---|
| `request_response` | invocation | value | value/incremental | ephemeral | 요약, RAG API |
| `conversation` | conversation | value | incremental | checkpointed | chatbot, agent |
| `ingestion_job` | job | bounded sequence | progress/value | durable | 문서 처리 |
| `realtime_voice` | session | duplex | duplex | ephemeral/checkpointed | voice agent |

Preset은 편의 기능이며 최종 컴파일된 IR에는 명시적 축이 기록되어야 한다.

## 13. Canonical schema 공통 규칙

모든 공개 데이터 타입은 다음 원칙을 따른다.

- schema ID와 version을 가진다.
- 직렬화 가능한 값만 포함한다.
- provider SDK 객체를 포함하지 않는다.
- 식별자, lineage, 보안 label, metadata를 명시적으로 둔다.
- 확장 필드는 namespaced metadata 또는 versioned union variant로 추가한다.
- unknown 필드 처리 정책을 schema별로 선언한다.

공통 envelope 예시:

```python
class CanonicalValue(BaseModel):
    schema_id: str
    schema_version: int
    value_id: str | None = None
    created_at: datetime | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    sensitivity: Literal["public", "internal", "confidential", "restricted"] | None = None
```

Python 예시는 Pydantic v2 스타일의 pseudocode다. 실제 generated model은 mutable field에 `default_factory`를 사용하고 validation/serialization mode를 명시해야 한다.

`metadata`는 핵심 의미를 숨기는 용도로 사용해서는 안 된다. 검색 score, citation span, model usage, ACL처럼 상호운용에 필요한 필드는 정식 schema 필드여야 한다.


### Schema authority와 code generation

Canonical schema의 source of truth는 특정 Python 또는 Rust class가 아니라 versioned schema repository다.

```text
specs/schemas/
  graphspec/
  canonical/
  protocol/
  plugin/
```

규칙:

- GraphSpec, connection, policy, manifest schema는 JSON Schema로 배포한다.
- Rust `serde` type과 Python model/type stub은 동일 schema definition에서 생성하거나 conformance test로 동등성을 검증한다.
- Python class identity, Rust crate path, provider SDK type은 schema identity가 아니다.
- schema 변경은 compatibility classification과 migration adapter를 가진다.
- canonical schema package는 provider integration보다 먼저 release되어야 한다.

### TypedValue와 wire encoding

Runtime 및 remote protocol은 typed envelope를 사용한다.

```rust
pub struct TypedValue {
    pub schema_id: String,
    pub schema_version: u32,
    pub encoding: ValueEncoding,
    pub payload: Bytes,
}
```

표준 encoding:

```text
json          # 규범적 상호운용/debug encoding
message_pack  # 동일 logical schema의 compact encoding, optional
arrow_ipc     # large tabular/batch value, optional
raw_bytes     # declared binary/artifact content only
artifact_ref  # payload를 복사하지 않는 외부 object reference
```

- JSON encoding은 conformance 기준이며 다른 encoding은 JSON logical model과 동등해야 한다.
- 큰 파일, 이미지, 오디오, embedding batch를 JSON/base64로 기본 전달하지 않는다.
- encoding negotiation은 plan compile 또는 protocol handshake에서 완료되어야 한다.
- unknown encoding이나 지원되지 않는 schema version은 실행 전에 실패해야 한다.
- artifact reference의 대상 무결성은 checksum, size, media type으로 검증한다.

## 14. Message와 ContentPart

텍스트 하나만을 message로 취급하지 않는다.

```text
ContentPart =
  TextPart
  | ImagePart
  | FilePart
  | AudioPart
  | TablePart
  | JsonPart
  | ToolCallPart
  | ToolResultPart
  | CitationPart
  | RefusalPart
  | ArtifactPart
```

```python
class Message(BaseModel):
    message_id: str
    conversation_id: str | None = None
    turn_id: str | None = None
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: list[ContentPart]
    parent_message_id: str | None = None
    revision: int = 1
    status: Literal["draft", "completed", "cancelled", "superseded"] = "completed"
    created_at: datetime
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

### TextPart

```python
class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str
    language: str | None = None
    annotations: list[TextAnnotation] = Field(default_factory=list)
```

### FilePart

```python
class FilePart(BaseModel):
    type: Literal["file"] = "file"
    attachment_id: str
    asset: ArtifactRef
    purpose: Literal[
        "direct_input",
        "retrieval",
        "code_analysis",
        "reference",
        "output"
    ]
```

### JsonPart

`JsonPart`는 JSON-compatible value와 optional schema ID를 가진다. 구조화 출력의 source of truth를 문자열 JSON으로 두지 않는다.

## 15. Prompt model

Prompt는 LLM block 내부의 단순 문자열 config가 아니다.

```python
class PromptRef(BaseModel):
    name: str
    version: str | None = None
    label: str | None = None
    content_hash: str | None = None
    registry: str | None = None
```

```python
class PromptTemplate(BaseModel):
    prompt_id: str
    kind: Literal["text", "chat"]
    template: str | list[MessageTemplate]
    variables_schema: JsonSchemaRef | None = None
    output_schema: JsonSchemaRef | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

Prompt 관련 표준 block:

```text
prompt.const
prompt.file
prompt.registry
prompt.compose
prompt.render
prompt.freeze
```

Prompt render 결과는 `list[Message]` 또는 `TextPart`이며, 사용한 prompt ref/version/hash를 provenance에 기록해야 한다.

## 16. Model request, response, incremental output

```python
class ModelRequest(BaseModel):
    request_id: str
    messages: list[Message]
    tools: list[ToolDefinition] = Field(default_factory=list)
    response_schema: JsonSchemaRef | None = None
    generation: GenerationParameters = GenerationParameters()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

```python
class ModelResponse(BaseModel):
    response_id: str
    messages: list[Message]
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str | None = None
    provider_metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

### GenerationChunk

`TokenDelta{text, index}`는 충분하지 않다.

```text
GenerationChunk =
  TextDelta
  | ReasoningDelta
  | ToolCallDelta
  | CitationDelta
  | UsageDelta
  | FinishDelta
  | ProviderEvent
```

공통 필드:

```python
class ChunkBase(BaseModel):
    response_id: str
    message_id: str | None = None
    choice_index: int = 0
    content_index: int | None = None
    sequence: int
    occurred_at: datetime | None = None
```

규칙:

- chunk sequence는 response 내에서 단조 증가해야 한다.
- `FinishDelta`는 response finalization을 의미하며 transport stream close와 동일하지 않다.
- tool argument delta는 임의 문자열 append만 제공할 수 있지만 final tool call은 schema-valid JSON이어야 한다.
- reasoning content는 provider policy와 capture policy에 따라 저장 또는 노출하지 않을 수 있다.

## 17. Tool model

```python
class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: JsonSchemaRef
    output_schema: JsonSchemaRef | None = None
    effects: set[Literal[
        "none", "external_read", "external_write",
        "filesystem", "process", "network", "destructive"
    ]]
    approval: Literal["never", "policy", "always"] = "policy"
    idempotency: Literal["not_applicable", "optional", "required"] = "optional"
```

```python
class ToolCall(BaseModel):
    tool_call_id: str
    name: str
    arguments: JsonValue
    status: Literal["requested", "approved", "running", "completed", "failed", "denied"]
```

```python
class ToolResult(BaseModel):
    tool_call_id: str
    output: list[ContentPart]
    is_error: bool = False
    error: BlockError | None = None
```

Tool은 block, graph, remote service, MCP tool에서 생성할 수 있다.

## 18. Artifact와 FileAttachment

```python
class ArtifactRef(BaseModel):
    artifact_id: str
    uri: str
    media_type: str | None = None
    size_bytes: int | None = None
    checksum: str | None = None
    etag: str | None = None
    version: str | None = None
    filename: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
```

```python
class FileAttachment(BaseModel):
    attachment_id: str
    asset: ArtifactRef
    scope: Literal["message", "conversation", "user", "project", "tenant"]
    purpose: Literal["direct_input", "retrieval", "code_analysis", "reference", "output"]
    ingestion_status: Literal[
        "pending", "processing", "ready", "failed", "expired", "deleted"
    ]
    retention_policy: str | None = None
```

Attachment scope가 `conversation`이라고 해서 영구 knowledge index에 자동 저장되어서는 안 된다.

## 19. SourceAsset와 AssetRevision

```python
class SourceAsset(BaseModel):
    asset_id: str
    source_uri: str
    source_kind: Literal[
        "upload", "local", "http", "s3", "gcs", "sharepoint",
        "drive", "email", "record_store", "generated"
    ]
    tenant_id: str | None = None
    current_revision_id: str | None = None
```

```python
class AssetRevision(BaseModel):
    revision_id: str
    asset_id: str
    content_hash: str
    observed_at: datetime
    modified_at: datetime | None = None
    artifact: ArtifactRef
    source_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    acl: AccessPolicy | None = None
```

`AssetRevision`이 동일하면 deterministic processing cache를 재사용할 수 있다.

## 20. ParsedDocument와 DocumentElement

문서를 Markdown 문자열 하나로 축소하지 않는다.

```python
class ParsedDocument(BaseModel):
    document_id: str
    asset_id: str
    revision_id: str
    elements: list[DocumentElement]
    plain_text: str | None = None
    language: str | None = None
    title: str | None = None
    parser: ProcessorRef
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

```text
DocumentElement =
  Heading
  | Paragraph
  | ListElement
  | TableElement
  | ImageElement
  | Caption
  | CodeBlock
  | Formula
  | Footnote
  | HeaderFooter
  | PageBreak
  | SheetRegion
  | SlideRegion
```

공통 위치 정보:

```python
class SourceLocation(BaseModel):
    page: int | None = None
    bbox: BoundingBox | None = None
    char_start: int | None = None
    char_end: int | None = None
    section_path: list[str] = Field(default_factory=list)
    sheet: str | None = None
    cell_range: str | None = None
    slide: int | None = None
```

각 element는 `element_id`, `parent_id`, `order`, `location`, `content`, `metadata`를 가져야 한다.

## 21. DocumentChunk와 lineage

```python
class DocumentChunk(BaseModel):
    chunk_id: str
    document_id: str
    asset_id: str
    revision_id: str
    text: str
    element_ids: list[str]
    source_refs: list[SourceRef]
    token_count: int | None = None
    chunker: ProcessorRef
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    acl: AccessPolicy | None = None
```

Lineage 최소 경로:

```text
SourceAsset
  → AssetRevision
    → ParsedDocument
      → DocumentElement
        → DocumentChunk
          → EmbeddingRecord / IndexRecord
            → KnowledgeItemRef / SearchHit
              → ContextItem
                → Claim / Citation / EvidenceRef
```

각 단계가 이전 단계의 ID, revision, digest를 잃으면 안 된다.

## 22. 범용 source, locator, snapshot

문서 page 위치만으로는 웹, structured record, code, dataset, experiment artifact를 표현할 수 없다. Core source model은 identity와 위치를 분리한다.

```python
class ResourceSnapshotRef(BaseModel):
    resource_id: str
    resource_kind: str
    revision: str
    digest: str
    captured_at: datetime
    schema_ref: SchemaRef | None = None
    artifact_ref: ArtifactRef | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

`ResourceSnapshotRef.resource_id`와 `digest`는 비어 있지 않은 문자열이어야 한다. `resource_kind`, `uri` 같은 선택 identity/context field가 제공되면 역시 비어 있지 않은 문자열이어야 하며, metadata는 생성 시 복사되어 caller-side mutation이 snapshot identity record에 소급 반영되지 않아야 한다.

```text
SourceLocator =
    DocumentSpan
  | TextFileRange
  | StructuredRecordLocator
  | WebResourceLocator
  | DatasetLocator
  | CodeArtifactLocator
  | TraceLocator
  | ArtifactLocator
```

```python
class DocumentSpan(BaseModel):
    asset_id: str
    revision_id: str
    document_id: str
    element_id: str | None = None
    chunk_id: str | None = None
    page: int | None = None
    bbox: BoundingBox | None = None
    char_start: int | None = None
    char_end: int | None = None
    sheet: str | None = None
    cell_range: str | None = None
    slide: int | None = None
```

```python
class TextFileRange(BaseModel):
    file_uri: str
    revision: str
    start_line: int | None = None
    start_column: int | None = None
    end_line: int | None = None
    end_column: int | None = None
```

```python
class SourceRef(BaseModel):
    source_id: str
    source_kind: str
    revision: str | None = None
    digest: str | None = None
    locator: SourceLocator | None = None
    observed_at: datetime | None = None
    relevant_as_of: datetime | None = None
    trust: Literal[
        "authoritative", "verified", "application", "user_supplied",
        "retrieved_untrusted", "generated", "unknown"
    ] = "unknown"
    access_policy: AccessPolicy | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

기존 `SourceSpan` 입력은 migration layer에서 `SourceRef(locator=DocumentSpan(...))`로 변환한다.

## 23. Knowledge item과 Search model

검색 대상은 반드시 `DocumentChunk`일 필요가 없다. Structured record, hosted search item, web result, code symbol도 knowledge item이 될 수 있다.

```python
class KnowledgeItemRef(BaseModel):
    item_id: str
    item_kind: str
    source: SourceRef
    schema_ref: SchemaRef | None = None
    payload_ref: ArtifactRef | None = None
    preview: list[ContentPart] = Field(default_factory=list)
    acl: AccessPolicy | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

```python
class SearchRequest(BaseModel):
    query_text: str | None = None
    query_embedding: list[float] | None = None
    filters: FilterExpr | None = None
    top_k: int = 10
    candidate_k: int | None = None
    namespaces: list[str] = Field(default_factory=list)
    auth_context: AuthContext | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

```python
class SearchHit(BaseModel):
    hit_id: str
    item: KnowledgeItemRef
    rank: int
    raw_score: float | None = None
    normalized_score: float | None = None
    score_kind: str | None = None
    highlights: list[SourceRef] = Field(default_factory=list)
    retriever: str
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

서로 다른 retriever의 `raw_score`를 직접 비교해서는 안 된다. Fusion 이전에 score normalization 또는 rank 기반 알고리즘을 사용해야 한다.

## 24. Filter expression

Provider별 query DSL을 GraphSpec에 직접 노출하지 않는다.

```text
FilterExpr =
  Eq(field, value)
  | Ne(field, value)
  | In(field, values)
  | Range(field, gte, gt, lte, lt)
  | Exists(field)
  | And(expressions)
  | Or(expressions)
  | Not(expression)
```

Connector는 지원하지 않는 filter를 compile 또는 bind 시점에 명확히 거부해야 한다. 조용히 client-side filtering으로 바꾸면 ACL과 top-k 의미가 달라질 수 있다.

## 25. ContextPack

```python
class ContextItem(BaseModel):
    item_id: str
    kind: Literal[
        "instruction", "message", "summary", "memory",
        "retrieved_item", "attachment", "tool_result", "evidence"
    ]
    content: list[ContentPart]
    priority: int
    token_count: int | None = None
    sources: list[SourceRef] = Field(default_factory=list)
    trust: Literal[
        "trusted", "application", "user", "retrieved_untrusted", "tool"
    ]
    inclusion_reason: str | None = None
```

```python
class ContextPack(BaseModel):
    items: list[ContextItem]
    token_budget: int
    used_tokens: int
    excluded: list[ContextExclusion] = Field(default_factory=list)
    builder: ProcessorRef
    budget_reservation_id: str | None = None
```

Context builder는 무엇을 제외했는지와 이유, token estimator, policy adaptation을 기록해야 한다.

## 26. Claim, Evidence, Citation, Answer

```python
class EvidenceRef(BaseModel):
    evidence_id: str
    source: SourceRef
    relation: Literal[
        "supports", "contradicts", "qualifies", "diagnoses", "reproduces"
    ]
    excerpt: str | None = None
    artifact_ref: ArtifactRef | None = None
    captured_by: ProcessorRef | None = None
    captured_at: datetime | None = None
```

```python
class Claim(BaseModel):
    claim_id: str
    statement: str
    status: Literal[
        "asserted", "supported", "disputed", "contradicted",
        "unverified", "retracted"
    ] = "asserted"
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    derived_from_claim_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

```python
class Citation(BaseModel):
    citation_id: str
    source: SourceRef
    cited_text: str | None = None
    claim_ids: list[str] = Field(default_factory=list)
    verified: bool | None = None
```

```python
class Answer(BaseModel):
    answer_id: str
    message: Message
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    finish_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    usage: Usage | None = None
```

Grounding policy 예:

```yaml
grounding:
  required: true
  citationRequired: true
  allowUncitedClaims: false
  onInsufficientContext: abstain
```

## 27. Diagnostic

```python
class Diagnostic(BaseModel):
    diagnostic_id: str
    severity: Literal["info", "warning", "error", "fatal"]
    code: str | None = None
    message: str
    sources: list[SourceRef] = Field(default_factory=list)
    tool: ProcessorRef | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    structured_data: JsonValue | None = None
```

`Diagnostic`는 parser warning, structured output validation, retrieval warning, compiler error, test failure, policy violation을 동일 envelope로 표현한다. Diagnostic 자체가 check 또는 policy decision을 대신하지 않는다.

## 28. ChangeSet과 mutation scope

```python
class ChangeSet(BaseModel):
    change_set_id: str
    base: ResourceSnapshotRef
    operations_ref: ArtifactRef
    digest: str
    affected_resources: list[ResourceRef]
    generated_by: ProcessorRef
    mutation_scope: Literal["ephemeral_trial", "draft", "durable"]
    integrity_policy_ref: str | None = None
```

구현이 `operations_ref` 대신 lightweight inline operation list를 제공하는 경우에도, `ChangeSet` 생성 시 operation mapping을 복사하고 immutable sequence로 고정해야 한다. Review, gate, CAS commit은 생성 이후 caller-side mutation으로 operation 내용이 바뀌지 않는다는 전제에 의존한다.

표준 lifecycle:

```text
snapshot
→ fork
→ apply ChangeSet
→ check/gate
→ propose
→ review
→ compare-and-swap commit
→ cleanup
```

Source, test oracle, acceptance policy처럼 신뢰된 입력은 mutation policy에서 read-only로 선언할 수 있어야 한다.

## 29. Check, Metric, Gate, Trial

```python
class CheckResult(BaseModel):
    check_id: str
    subject: ResourceSnapshotRef
    status: Literal[
        "passed", "failed", "error", "timeout", "inconclusive", "skipped"
    ]
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    tool: ProcessorRef
    environment: ResourceSnapshotRef | None = None
```

```python
class MetricObservation(BaseModel):
    name: str
    value: Decimal | bool | str | None
    unit: str | None = None
    direction: Literal["minimize", "maximize", "target", "informational"]
    baseline_value: Decimal | None = None
    subject: ResourceSnapshotRef | None = None
    evaluator: ProcessorRef | None = None
```

```python
class GateResult(BaseModel):
    gate_id: str
    subject: ResourceSnapshotRef
    decision: Literal["pass", "fail", "inconclusive"]
    check_ids: list[str] = Field(default_factory=list)
    violated_constraints: list[str] = Field(default_factory=list)
    metrics: list[MetricObservation] = Field(default_factory=list)
    policy_ref: str | None = None
```

```python
class TrialResult(BaseModel):
    trial_id: str
    base: ResourceSnapshotRef
    candidate: ResourceSnapshotRef
    change_set: ChangeSet | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    metrics: list[MetricObservation] = Field(default_factory=list)
    gate: GateResult | None = None
    usage: list[UsageRecordRef] = Field(default_factory=list)
    outcome: str
```

`Check`는 검증 결과, `Metric`은 측정값, `Gate`는 수용 결정이다. Model-based judge와 deterministic check는 provenance와 신뢰 수준을 구분한다.

## 30. Approval과 Review 분리

```text
Approval
- effect 또는 privileged action을 실행할 권한 결정

Review
- 특정 immutable subject digest의 내용/품질 검토
```

```python
class ReviewRecord(BaseModel):
    review_id: str
    subject: ResourceSnapshotRef
    subject_digest: str
    scope: str
    reviewer: PrincipalRef
    decision: Literal["accept", "accept_with_conditions", "revise", "reject"]
    comments: list[str] = Field(default_factory=list)
    credential_refs: list[str] = Field(default_factory=list)
    created_at: datetime
    invalidated_at: datetime | None = None
```

Review 후 subject digest가 바뀌면 기존 review는 자동 무효다. Review가 effect permission을 자동 부여하지는 않는다.

## 31. ResultBundle

```python
class ResultBundle(BaseModel):
    bundle_id: str
    run_id: str
    release_id: str
    deployment_revision_id: str | None = None
    inputs: list[ResourceSnapshotRef]
    outputs: list[TypedValueRef]
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    metrics: list[MetricObservation] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    reviews: list[ReviewRecord] = Field(default_factory=list)
    usage_records: list[UsageRecordRef] = Field(default_factory=list)
    policy_decision_refs: list[str] = Field(default_factory=list)
    provenance: RunProvenance
```

RAG, ingestion, conversation turn, research, trial은 `ResultBundle`의 typed profile 또는 payload다. Immutable evaluation은 bundle을 입력으로 사용한다.

## 32. Conversation과 Turn

```python
class Conversation(BaseModel):
    conversation_id: str
    tenant_id: str | None = None
    user_id: str | None = None
    revision: int
    status: Literal["active", "archived", "deleted"]
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

```python
class Turn(BaseModel):
    turn_id: str
    conversation_id: str
    user_message_id: str
    assistant_message_ids: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)
    retrieval_ids: list[str] = Field(default_factory=list)
    status: Literal[
        "running", "completed", "cancelled", "failed",
        "budget_constrained", "budget_exhausted", "paused_for_entitlement",
        "completed_with_overdraft"
    ]
    started_at: datetime
    ended_at: datetime | None = None
```

Conversation은 edit, regenerate, branch를 지원하기 위해 message parent와 revision을 보존해야 한다.

## 33. Memory model

Memory를 대화 history와 동일시하지 않는다.

```text
MemoryRecord =
  conversation_summary
  | user_preference
  | episodic_memory
  | semantic_memory
  | task_state
```

Memory write는 외부 effect다. 자동 장기 기억은 policy, consent, TTL, deletion contract를 가져야 한다.

## 34. Structured output

```python
class StructuredResult(BaseModel):
    schema_ref: JsonSchemaRef
    value: JsonValue | None
    raw_response: ModelResponse | None = None
    validation_errors: list[ValidationIssue] = Field(default_factory=list)
    repair_attempts: int = 0
    status: Literal["valid", "invalid", "repaired", "rejected"]
```

Schema validation은 provider native structured output 사용 여부와 최종 local validation 결과를 모두 기록해야 한다.

## 35. Evaluation case

```python
class DatasetCase(BaseModel):
    case_id: str
    inputs: dict[str, TypedValueRef]
    expected: dict[str, TypedValueRef] = Field(default_factory=dict)
    fixtures: list[ArtifactRef] = Field(default_factory=list)
    assertions: list[AssertionSpec] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
```

```python
class MetricResult(BaseModel):
    metric: str
    value: float | bool | str | None
    target_id: str
    target_kind: Literal[
        "run", "turn", "generation", "retrieval", "citation", "document",
        "dataset_case", "task", "trial", "check", "gate", "result_bundle"
    ]
    explanation: str | None = None
    evaluator: ProcessorRef | None = None
```

Conversation/RAG convenience schema는 profile-specific wrapper로 제공한다. Evaluation은 deterministic verification, model-based quality evaluation, policy compliance를 구분해야 한다.

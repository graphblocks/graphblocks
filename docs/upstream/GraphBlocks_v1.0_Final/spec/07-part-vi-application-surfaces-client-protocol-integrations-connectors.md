# Part VI. Application Surfaces, Client Protocol, Integrations, Connectors

## 160. ApplicationSpec의 역할

ApplicationSpec은 GraphSpec을 사용자에게 노출하는 표면을 정의한다. 계산 node, provider credential, worker replica 수를 소유하지 않는다.

```yaml
apiVersion: graphblocks.ai/v1alpha1
kind: Application
metadata:
  name: workspace-assistant

spec:
  graphs:
    chat: graphs/workspace-agent.yaml
    ingest: graphs/knowledge-ingestion.yaml

  surfaces:
    default:
      kind: tui
      implementation: textual
      clientMode: local
      protocol: graphblocks.app.v1

  routes:
    - id: chat-sse
      kind: http_sse
      path: /v1/chat
      graph: chat

    - id: ingest-job
      kind: job_http
      path: /v1/ingest
      graph: ingest

  capabilities:
    - assistant_drafts
    - approval
    - run_cancellation
    - artifact_preview
    - breakpoint_resume
```

ApplicationSpec에는 `workers`, replica 수, node selector, image, autoscaling을 넣지 않는다. 이는 GraphDeployment가 소유한다.

## 161. Application command/event protocol

TUI, CLI, web UI, IDE extension은 동일한 command/event protocol을 사용할 수 있다.

### Client command

```text
InvokeGraph
CancelRun
SubmitInput
ApproveEffect
DenyEffect
SubmitReview
RequestBudgetExtension
ApplyPolicyOverride
ResumeInterrupt
SelectCandidate
OpenArtifact
SetBreakpoint
RequestSnapshot
```

### Application event

```text
RunStarted
TurnStarted
ContextReady
AssistantDraftStarted
AssistantDraftDelta
AssistantCommitted
AssistantRetracted
ToolStarted
ToolCompleted
ApprovalRequested
ReviewRequested
BudgetConstrained
BudgetExhausted
BudgetExtensionRequested
BudgetExtensionGranted
PolicyDecisionRequired
ExecutionDegraded
FilePatchPreview
JobProgress
ArtifactReady
StateSnapshot
RunCompleted
RunFailed
RunCancelled
```

공통 envelope:

```python
class ApplicationProtocolEvent(BaseModel):
    event_id: str
    protocol_version: str
    run_id: str
    turn_id: str | None = None
    sequence: int
    cursor: str | None = None
    occurred_at: datetime
    type: str
    payload: JsonValue
```

Protocol은 cursor replay, duplicate suppression, event coalescing, capability negotiation을 정의해야 한다. 느린 client가 runtime scheduler를 block해서는 안 된다.

## 162. TUI와 workspace architecture

TUI는 graph node가 아니라 client다.

```text
Textual/Ratatui/other TUI client
        ↓ graphblocks.app.v1
GraphBlocks client/server adapter
        ↓
Graph runtime + conversation graph + workspace tools
```

Workspace domain package는 다음 canonical contract를 재사용한다.

```text
WorkspaceRef
ResourceSnapshotRef / WorkspaceSnapshot
SourceRef(TextFileRange)
ChangeSet
MutationPolicy
Diagnostic
CheckResult / GateResult / TrialResult
ReviewRecord
CommandSpec / ProcessResult
```

표준 tool:

```text
workspace.snapshot
workspace.fork
workspace.search
workspace.read
workspace.propose_changeset
workspace.apply_changeset
workspace.compare_and_swap_commit
workspace.cleanup
process.execute
git.diff
test.run
```

Workspace lifecycle:

```text
snapshot
→ fork isolated workspace
→ apply ephemeral ChangeSet
→ checks/gate
→ proposal artifact
→ review
→ approval, if external write is required
→ CAS commit
→ cleanup
```

`apply_changeset`, `process.execute`, external write는 approval, sandbox, audit, idempotency, budget, integrity policy를 가진다. Trusted tests, golden files, policy, acceptance gate는 mutation policy로 보호할 수 있어야 한다.

`WorkspaceSnapshot` 안의 `ResourceSnapshotRef.resource_id`는 snapshot-local unique key다. 중복 resource id를 가진 snapshot이나 CAS commit candidate는 거부해야 하며, mutation policy와 read-only protection은 이 unique key를 기준으로 비교한다. Snapshot과 commit record는 workspace/snapshot/commit/change-set identity, positive revision, typed `ResourceSnapshotRef`, typed `PrincipalRef`, metadata mapping key를 검증해야 한다. `WorkspaceCommit.snapshot`은 construction 시 복사되어 원본 snapshot metadata mutation이 audit record를 바꾸지 않아야 한다.

TUI/IDE는 BudgetConstrained, BudgetExhausted, ReviewRequested, TrialProgress event를 표시하고 top-up/override/resume command를 capability에 따라 제공할 수 있다.


## 163. Route와 transport

```text
HTTP request/response
HTTP SSE incremental output
WebSocket chat
job submit/status/cancel
OpenAI-compatible compatibility surface
local in-process client
remote worker/client protocol
```

Route는 ApplicationSpec에, authentication implementation과 ingress/gateway는 server/deployment profile에 둔다. Transport event와 canonical `GenerationChunk`/`Message`를 동일 타입으로 취급하지 않는다.

## 164. Application test contract

Application test는 두 층으로 나눈다.

```text
protocol test
- command → expected event sequence
- reconnect/cursor replay
- cancellation/approval

surface integration test
- keyboard/http/client input → rendered/client state
```

UI framework test는 해당 optional package가 소유한다. Core runtime TCK는 특정 UI toolkit에 의존하지 않는다.

## 165. Integration 원칙

GraphBlocks integration은 core semantic contract를 provider 또는 외부 framework에 연결한다.

```text
semantic block
→ provider-neutral SPI
→ integration adapter
→ provider SDK/service
```

Integration이 core canonical schema를 provider 객체로 대체해서는 안 된다.

## 166. SPI 분류

```text
ModelProvider
EmbeddingProvider
DocumentConverter
OcrProvider
Retriever
KnowledgeIndex
BlobStore
RecordStore
ConversationStore
StateStore
CoordinationBackend
MessageBus
SecretProvider
PromptRegistry
PolicyEvaluator
EntitlementProvider
UsageLedger
BudgetLedger
LeasePool
TelemetryExporter
EvaluationSink
FrameworkBridge
RealtimeProvider, extension
```

하나의 `Connector` interface로 모든 저장소를 합치지 않는다.

## 167. ConnectionSpec

```yaml
connections:
  answer-model:
    kind: model
    provider: openai
    config:
      model: ${MODEL_ID}
      baseUrl: ${OPENAI_BASE_URL:-https://api.openai.com/v1}
    credentials: secret://env/OPENAI_API_KEY

  company-knowledge:
    kind: knowledge_index
    provider: qdrant
    config:
      url: ${QDRANT_URL}
      collection: company_docs
    credentials: secret://vault/qdrant-production

  artifacts:
    kind: blob
    provider: s3
    config:
      bucket: company-ai-artifacts
      prefix: graphblocks/
      region: ap-northeast-2
    credentials: secret://aws/artifacts-role
```

Connection은 secret 값을 직렬화하지 않는다.

## 168. Connector lifecycle

```rust
#[async_trait]
pub trait Connector: Send + Sync {
    async fn initialize(&self, ctx: &ConnectorContext) -> Result<()>;
    async fn healthcheck(&self) -> HealthStatus;
    async fn capabilities(&self) -> CapabilitySet;
    async fn close(&self) -> Result<()>;
}
```

공통 요구사항:

- connection pool
- timeout
- retry classification
- credential refresh
- tracing
- readiness/liveness
- graceful close
- rate limit handling
- tenant boundary

## 169. ModelProvider SPI

```rust
#[async_trait]
pub trait ModelProvider: Send + Sync {
    async fn generate(
        &self,
        request: ModelRequest,
        emitter: &dyn IncrementalEmitter,
        ctx: &ExecutionContext,
    ) -> Result<ModelResponse, ModelError>;

    fn capabilities(&self) -> ModelCapabilities;
}
```

Capability 예:

```text
chat
text
vision
file_input
structured_output
tool_calling
parallel_tool_calls
reasoning
streaming
usage
prompt_cache
hosted_retrieval
```

Provider-specific option은 namespaced extension config로 제공하되 canonical behavior와 충돌하지 않아야 한다.

## 170. EmbeddingProvider SPI

```rust
#[async_trait]
pub trait EmbeddingProvider: Send + Sync {
    async fn embed_texts(&self, texts: Vec<String>, ctx: &ExecutionContext)
        -> Result<Vec<EmbeddingRecord>, EmbeddingError>;
}
```

Batch size, token limit, dimension, normalization, retry semantics과 preflight/final usage report를 capability로 공개한다.

## 171. DocumentConverter SPI

```rust
#[async_trait]
pub trait DocumentConverter: Send + Sync {
    async fn convert(
        &self,
        revision: AssetRevision,
        options: ConvertOptions,
        ctx: &ExecutionContext,
    ) -> Result<ParsedDocument, ConversionError>;

    fn capabilities(&self) -> ConverterCapabilities;
}
```

Heavy parser dependency는 converter integration package가 소유한다.

## 172. BlobStore

대상:

```text
local filesystem
memory
S3/MinIO
GCS
Azure Blob
HTTP read-only
```

```rust
#[async_trait]
pub trait BlobStore: Send + Sync {
    async fn get(&self, key: &BlobKey, range: Option<ByteRange>) -> Result<BlobReader>;
    async fn put(
        &self,
        key: &BlobKey,
        body: BlobReader,
        options: PutOptions,
    ) -> Result<ArtifactRef>;
    async fn head(&self, key: &BlobKey) -> Result<BlobMetadata>;
    async fn delete(&self, key: &BlobKey) -> Result<()>;
    async fn list(&self, prefix: &str, cursor: Option<String>) -> Result<ListPage>;
}
```

`list` prefix는 blob key와 같은 namespace segment rule을 따라야 하며 absolute path, backslash, empty/path traversal segment를 허용하지 않는다. Local filesystem과 S3-compatible 구현은 같은 invalid prefix를 거부해야 한다.

Capability:

```text
range_read
streaming_write
multipart_write
conditional_put
etag
versioning
presigned_url
atomic_rename
watch
```

MinIO는 S3-compatible provider profile로 처리할 수 있다.

## 173. RecordStore

Firestore, MongoDB, DynamoDB, Postgres JSONB와 같은 structured record storage다.

```rust
#[async_trait]
pub trait RecordStore: Send + Sync {
    async fn get(&self, collection: &str, key: &str) -> Result<Option<Record>>;
    async fn put(&self, collection: &str, record: Record, options: WriteOptions) -> Result<()>;
    async fn query(&self, request: RecordQuery) -> Result<RecordPage>;
    async fn delete(&self, collection: &str, key: &str, options: DeleteOptions) -> Result<()>;
}
```

Capability:

```text
transaction
compare_and_swap
query
watch
ttl
bulk_write
secondary_index
```

## 174. KnowledgeIndex

Qdrant, pgvector, OpenSearch, Elasticsearch, Pinecone, Weaviate, Milvus 또는 hosted file store를 연결한다.

```rust
#[async_trait]
pub trait KnowledgeIndex: Send + Sync {
    async fn upsert(&self, records: Vec<KnowledgeRecord>, options: UpsertOptions) -> Result<WriteReport>;
    async fn delete(&self, request: KnowledgeDelete) -> Result<WriteReport>;
    async fn update_metadata(&self, request: MetadataUpdate) -> Result<WriteReport>;
    async fn publish(&self, request: PublishRequest) -> Result<PublishResult>;
}
```

Retrieval은 별도 `Retriever` SPI가 담당한다.

## 175. StateStore와 ConversationStore

StateStore는 checkpoint, agent state, run state 같은 key/value 또는 versioned state에 사용한다.

```text
memory
SQLite
Postgres
Redis/Valkey
Firestore
```

ConversationStore는 message/branch/revision semantics를 가진 domain-specific SPI다. 일반 StateStore 위에 구현할 수 있지만 공개 계약은 분리한다.

## 176. CoordinationBackend

```text
InMemory
Redis/Valkey
Postgres
Etcd, future
```

제공 기능:

- lease semaphore
- fencing mutex
- distributed rate limit
- barrier
- generic lease pool reservation/renewal/release
- leader/run-ownership lease, optional

## 177. MessageBus

Durable extension에서 사용한다.

```text
Kafka
NATS JetStream
SQS
Google Pub/Sub
Redis Streams
```

Core chatbot/RAG 실행이 MessageBus 설치를 요구해서는 안 된다.

## 178. SecretProvider

```text
env
file
AWS Secrets Manager
GCP Secret Manager
Azure Key Vault
HashiCorp Vault
Kubernetes Secret
```

```python
class SecretRef(BaseModel):
    uri: str
    version: str | None = None
```

GraphSpec, plan, lockfile, trace에는 resolved secret을 기록하지 않는다.

## 179. Provider-neutral block naming

권장:

```text
model.chat
embedding.document
blob.put
record.upsert
knowledge.upsert
retrieve.hybrid
```

비권장:

```text
llm.openai_chat
vector.qdrant_upsert
object_store.minio_put
firestore.document_write
```

Provider는 `connection.provider`, `implementation`, 또는 binding에서 선택한다.

## 180. Capability negotiation

Block requirement 예:

```yaml
nodes:
  publish:
    block: knowledge.publish@1
    requires:
      connectionCapabilities:
        - atomic_alias_swap
```

Bind 단계에서 capability 부족을 발견하면 실행 전에 실패해야 한다.

```text
CapabilityError:
  connection company-knowledge does not provide atomic_alias_swap
  supported: generation_namespace, non_atomic_publish
```

## 181. Observability integration boundary

Part IX가 execution journal, audit, usage ledger, application event, OTel telemetry의 규범 계약을 정의한다. Integration part는 exporter와 registry SPI만 정의한다.

```text
TelemetryExporter
PromptRegistry
EvaluationSink
DatasetProvider
AuditSink
UsageSink
```

한 vendor adapter가 모든 SPI를 구현할 수 있지만 각 기능은 독립 설정, 독립 failure mode, 독립 package dependency를 가져야 한다.

## 182. Langfuse integration decomposition

```text
LangfuseTelemetryExporter
LangfusePromptRegistry
LangfuseEvaluationSink
LangfuseDatasetProvider
```

권장 mapping:

| GraphBlocks | Langfuse |
|---|---|
| Conversation | Session |
| Turn/graph invocation | Trace |
| Node | Observation/span |
| Model call | Generation |
| Retrieval/tool/agent | typed observation |
| PromptRef | prompt version link |
| MetricResult | score |
| Dataset case/run | dataset item/experiment |

Langfuse는 run recovery, exact billing, quota/budget enforcement, required audit, checkpoint store가 아니다. PolicyDecision, BudgetLedger, UsageLedger는 별도 durable path를 사용한다.

## 183. Instrumentation ownership

```yaml
observability:
  instrumentation:
    owner: graphblocks       # graphblocks | provider | framework | auto
    nestedProviderSpans: infrastructure
```

한 model call이 GraphBlocks, provider SDK, framework callback, Langfuse SDK에 의해 중복 generation으로 기록되지 않게 한다. Provider request ID와 span link를 dedup key로 사용할 수 있다.

## 184. PromptRegistry

```rust
#[async_trait]
pub trait PromptRegistry: Send + Sync {
    async fn resolve(&self, reference: PromptRef) -> Result<PromptTemplate>;
    async fn list_versions(&self, name: &str) -> Result<Vec<PromptVersion>>;
}
```

Implementations:

```text
file/git
Langfuse
custom HTTP registry
in-memory test registry
```

Production release는 mutable prompt label을 release build 시 immutable version/hash로 resolve해야 한다.

## 185. EvaluationSink와 DatasetProvider

```rust
#[async_trait]
pub trait EvaluationSink: Send + Sync {
    async fn write_result(&self, result: MetricResult) -> Result<()>;
    async fn write_run(&self, run: EvaluationRun) -> Result<()>;
}
```

Evaluation record가 rollout gate나 compliance requirement이면 durable evaluation store/outbox를 사용한다. Best-effort telemetry exporter에만 의존하지 않는다.

## 186. Framework integration levels

| Level | 의미 |
|---|---|
| L0 | trace/context propagation |
| L1 | component/runnable을 단일 block으로 호출 |
| L2 | pipeline/subgraph를 composite block으로 호출 |
| L3 | canonical data type mapping |
| L4 | 제한적 GraphSpec import/export |

L4는 실행 의미론이 손실될 수 있으므로 loss report를 생성해야 한다.

## 187. Haystack bridge

권장 mapping:

| Haystack | GraphBlocks |
|---|---|
| `ByteStream` | `ArtifactRef` 또는 bounded binary input |
| `Document` | `ParsedDocument`/`DocumentChunk` adapter |
| `ChatMessage` | `Message` |
| Component | `InvocationBlock` |
| AsyncPipeline | Composite graph block |
| DocumentStore | KnowledgeIndex + Retriever adapter |
| Retriever | Retriever block |
| Tool/PipelineTool | ToolDefinition |
| streaming callback | GenerationChunk emitter |
| AnswerBuilder output | Answer adapter |

Integration 형태:

```text
graphblocks-haystack
  - HaystackComponentBlock
  - HaystackPipelineBlock
  - type adapters
  - trace bridge
  - package capability manifest
```

Haystack component의 input/output socket을 GraphBlocks port로 정적으로 추출하지 못하면 명시적 descriptor가 필요하다.

## 188. LangGraph bridge

```text
LangGraphSubgraphBlock
- one turn/subgraph invocation
- checkpoint context bridge
- interrupt/resume adapter
- event projection mapping
```

LangGraph가 raw media/backpressure runtime을 소유한다고 가정하지 않는다. GraphBlocks의 full backend로 자동 번역하지 않는다.

## 189. LangChain bridge

```text
RunnableBlock
ToolAdapter
MessageAdapter
Callback/OTel bridge
```

Runnable의 dynamic input/output이 `Any`이면 production graph에서 explicit schema wrapper를 요구한다.

## 190. LlamaIndex bridge

```text
Retriever adapter
QueryEngine block
Tool adapter
Document/Node conversion
trace bridge
```

## 191. Integration maturity

```text
built_in
official
partner
community
experimental
deprecated
```

Registry와 CLI는 maturity, maintainer, support range, security status를 표시해야 한다.

## 192. Connector catalog 초기 범위

### Core/lightweight

```text
local BlobStore
memory BlobStore
memory RecordStore
memory KnowledgeIndex/Retriever
file PromptRegistry
in-memory State/Conversation store
```

### Official priority

```text
S3/MinIO
GCS
Qdrant
pgvector/Postgres
OpenSearch
Firestore
Redis/Valkey
Langfuse
OpenAI
Anthropic
Google GenAI
```

### Document converters

```text
PyPDF
Docling
MarkItDown
Tika
HWP/HWPX
```

각 provider는 별도 distribution이어야 한다.

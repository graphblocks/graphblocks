# GraphBlocks Specification

## Version 1.0 — Final Architecture and Implementation Baseline

- Project: GraphBlocks
- Tagline: **Typed blocks and a Rust-native runtime for document AI, RAG, conversations, agents, and production AI applications**
- Document status: Final architecture baseline and normative implementation contract
- API maturity: public object APIs remain alpha until their TCK profiles pass; document finalization does not imply API GA
- Document date: 2026-06-22
- Supersedes: Draft v0.3 through Draft v0.8
- Primary scope: natural language, files, document processing, retrieval, chat, agents, policy, usage governance, evaluation, deployment, and operations
- Optional extensions: realtime voice, durable unbounded dataflow
- Intended readers: runtime and SDK engineers, block and integration authors, application developers, platform engineers, SRE, security, and evaluation teams

---

## 문서 구성

이 명세는 하나의 거대한 설치 패키지나 하나의 구현 process를 전제하지 않는다. 규범적 계약은 Part로 분리되고, Python distribution, Rust crate, container image, worker pool도 같은 경계에 맞춰 나뉜다.

| 문서 Part | 핵심 내용 | 대표 구현 패키지 |
|---|---|---|
| Part I | 제품 범위와 canonical AI data model | `graphblocks-core` |
| Part II | Graph IR, block contract, Rust runtime | `graphblocks-core`, `graphblocks-runtime` |
| Part III | 파일과 문서 처리 | `graphblocks-documents` |
| Part IV | retrieval, RAG, context, citation | `graphblocks-rag` |
| Part V | conversation, memory, agent, tools | `graphblocks-conversation`, `graphblocks-agents` |
| Part VI | ApplicationSpec, client protocol, integrations, connectors | `graphblocks-client`, integration packages |
| Part VII | packaging, plugin discovery, distribution | 모든 distribution에 공통 |
| Part VIII | immutable release, placement, Kubernetes, Terraform, rollout | deployment packages |
| Part IX | execution records, OpenTelemetry, Langfuse, SLO, operations | observability packages |
| Part X | policy, quota, budget, entitlement, resource governance | `graphblocks-policy`, `graphblocks-budget`, `graphblocks-usage` |
| Part XI | security, testing, diagnostics, roadmap | `graphblocks-testing`, tooling packages |
| Extension A | realtime voice와 duplex session | `graphblocks-voice` 계열 |
| Extension B | durable unbounded dataflow | `graphblocks-durable` 계열 |

## 최종 확정 상태와 적합성 프로필

이 문서는 GraphBlocks의 **구현 기준선**을 확정한다. 이후 변경은 단순한 문구 수정이 아니라 object API, canonical schema, runtime protocol 또는 conformance profile의 version 변경으로 관리한다. 문서가 Version 1.0이라는 사실과 개별 API가 `v1alpha*`라는 사실은 모순되지 않는다. 전자는 아키텍처 기준선의 확정이고, 후자는 구현 및 TCK가 완료되기 전의 API 성숙도다.

### 안정성 등급

| 등급 | 범위 | 의미 |
|---|---|---|
| Normative Core | Part I, Part II, Part VII의 package/plugin contract, Part X의 policy/budget semantics, Part XI의 TCK | 호환 구현이 반드시 따라야 하는 계약 |
| Normative Profile | Documents, RAG, Conversation, Application Protocol, Release/Deployment/Observability object | 해당 profile을 구현한다고 주장할 때 필수 |
| Provisional Extension | TaskPlan orchestration, workspace trial/review, Kubernetes operator | 공개 contract는 유지하되 구현 피드백으로 minor revision 가능 |
| Experimental Extension | Realtime Voice, Durable Unbounded Dataflow | 기본 설치 및 Core conformance에 포함되지 않음 |

### 적합성 프로필

| ID | 구현이 제공해야 하는 범위 |
|---|---|
| `GB-C0-SCHEMA` | canonical schema, GraphSpec parse/normalize/hash, plugin manifest validation |
| `GB-C1-LOCAL-RUNTIME` | Rust scheduler, typed ports, `Outcome<T>`, cancellation, journal, local flow, Python binding |
| `GB-C2-AI-APPLICATION` | Document/RAG/Conversation profile과 provider-neutral acceptance applications |
| `GB-C3-GOVERNED-RUNTIME` | Policy PEP, UsageLedger, BudgetLedger, permit, exhaustion boundary, approval/review/gate |
| `GB-C4-PRODUCTION` | immutable release, worker protocol, placement, drain, deployment revision, audit/SLO/telemetry |
| `GB-X1-ORCHESTRATION` | bounded TaskPlan/TaskPlanPatch, worker/model pool, task budget delegation |
| `GB-X2-VOICE` | duplex session, VAD authority, interruption, playback ledger |
| `GB-X3-DURABLE-STREAM` | unbounded source offset, watermark, checkpoint and sink commit semantics |

구현과 package는 지원하는 profile만 주장해야 한다. 예를 들어 local Python SDK가 `GB-C1`을 통과했다고 해서 Kubernetes deployment나 durable stream 적합성을 주장해서는 안 된다.

### 아키텍처 동결 규칙

- 도메인별 업무 객체는 core package에 추가하지 않고 `SourceRef`, `EvidenceRef`, `ResourceSnapshotRef`, `ChangeSet`, `Check/Gate/Review`, `TaskPlan` 조합으로 표현한다.
- 모델이 normalized Graph IR 또는 production GraphSpec을 직접 수정하지 않는다. 동적 작업은 bounded `TaskPlan`으로 제한한다.
- Kubernetes/Terraform 세부 필드는 GraphSpec에 들어가지 않는다. Graph는 요구 조건을, Deployment는 placement를, 플랫폼 adapter는 실제 resource를 정의한다.
- telemetry backend, Langfuse 또는 Prometheus를 correctness, quota, billing, audit의 source of truth로 사용하지 않는다.
- block마다 별도 Pod를 만들지 않는다. remote boundary는 ExecutionGroup과 target 단위로 설계한다.
- 하나의 거대한 Python wheel 또는 container image를 공식 배포 단위로 만들지 않는다.

## 1. 규범 키워드

이 문서의 `MUST`, `MUST NOT`, `SHOULD`, `SHOULD NOT`, `MAY`는 각각 필수, 금지, 권고, 비권고, 선택 요구사항을 뜻한다.

## 2. 공개 객체와 API version

Version 1.0의 규범 객체는 다음과 같다.

```text
GraphSpec               graphblocks.ai/v1alpha3
CompositeBlockSpec      graphblocks.ai/v1alpha3
ApplicationSpec         graphblocks.ai/v1alpha1
BindingSpec             graphblocks.ai/v1alpha1
GraphRelease            graphblocks.ai/v1alpha1
GraphDeployment         graphblocks.ai/v1alpha1
ObservabilityProfile    graphblocks.ai/v1alpha1
EvaluationSpec          graphblocks.ai/v1alpha1
PolicyBundle            graphblocks.ai/v1alpha1
PolicyProfile           graphblocks.ai/v1alpha1
PolicySnapshot          graphblocks.policy/PolicySnapshot@1
```

`v1alpha1` GraphSpec의 단일 `connection`과 generic `control.parallel/control.join`은 migration reader에서 허용할 수 있지만, compiler는 이를 v1alpha3 normalized IR의 named resource binding과 구체적 control primitive로 변환해야 한다. `v1alpha2`의 document-only `SourceSpan`, chat-centric `DatasetCase`, untyped dynamic plan은 migration adapter가 범용 source/evidence, typed case, TaskPlan contract로 변환한다.

## 3. 공개 호환성 단위

GraphBlocks의 공개 호환성은 다음 단위로 관리한다.

1. object API version과 normalized IR format version
2. canonical schema ID와 schema version
3. BlockDescriptor, typed port, resource slot contract
4. runtime 및 worker protocol version
5. plugin API와 static manifest version
6. connector/provider SPI version
7. release bundle와 physical plan format version
8. package compatibility range와 package lock
9. checkpoint, conversation, manifest store schema version
10. telemetry mapping profile version

Rust 내부 타입 레이아웃, Tokio task 구조, PyO3 함수 배치, Kubernetes renderer의 내부 구현은 공개 ABI가 아니다.

## 4. 객체 계층

```text
Authoring DSL/YAML
        ↓
GraphSpec                     논리적 계산과 상태 전이
PolicyBundle / PolicyProfile  권한, quota, budget, lifecycle obligation
ApplicationSpec               사용자 표면, route, command/event protocol
BindingSpec                   model/store/retriever/prompt/secret reference
        ↓
Normalized Graph IR           모든 port, adapter, policy가 명시된 언어 중립 IR
        ↓
GraphRelease                  graph, app, package, prompt, policy bundle의 불변 릴리스
GraphDeployment               환경별 desired state와 placement/rollout
        ↓
DeploymentRevision            binding과 target을 해석한 불변 revision
PhysicalExecutionPlan         node/group/target/transport/implementation 계획
        ↓
Rust runtime, worker pools, Kubernetes workloads, external services
```

Terraform은 GraphBlocks runtime 객체가 아니다. Terraform은 cluster, node pool, network, storage, IAM과 GraphBlocks Helm/operator 배포를 관리하며, GraphBlocks는 infrastructure requirement와 module input/output bridge를 제공한다.

## 5. 핵심 설계 원칙

1. **자연어와 파일이 코어다.** 음성 및 범용 stream은 일반 `Message`, `Document`, `ToolCall`, `Answer` 모델을 확장한다.
2. **Rust가 실행을 소유한다.** Python은 첫 authoring SDK와 provider/custom block 계층이다.
3. **Graph IR은 언어 중립적이다.** Python 임의 객체나 provider SDK 객체는 공개 port와 remote wire contract에 들어가지 않는다.
4. **Graph, application, binding, deployment를 분리한다.** 계산, 사용자 표면, 외부 자원, 물리적 위치를 한 YAML에 혼합하지 않는다.
5. **의미와 구현을 분리한다.** `model.chat`, `document.convert`, `retrieve.hybrid`는 의미 block이며 provider는 binding/implementation으로 선택한다.
6. **출처와 증거를 잃지 않는다.** source asset에서 chunk, retrieval, claim, citation, check 결과까지 lineage와 `SourceRef`/`EvidenceRef`를 보존한다.
7. **검색의 공개 추상화는 Retriever다.** vector database는 여러 구현 수단 중 하나다.
8. **상태 변경은 명시적이다.** 외부 write, tool, delete, publish는 effect, idempotency, approval, audit 계약을 가진다.
9. **부재는 null이 아니다.** branch skip, cancellation, failure, 값 `null`을 `Outcome<T>`로 구분한다.
10. **독립 node는 자동 병렬 실행한다.** 명시적 task group은 취소, deadline, quorum, partial failure 정책이 있을 때만 사용한다.
11. **incremental output과 commit을 구분한다.** UI draft delta는 durable final message가 아니며 commit/retract 의미론을 가진다.
12. **release는 불변이다.** graph, prompt, index revision, package lock, image digest, policy를 pin한 release 없이 production run을 시작하지 않는다.
13. **관측성과 correctness 기록을 분리한다.** execution journal, audit, usage ledger는 durable하며 OTel telemetry는 진단 plane이다.
14. **OpenTelemetry가 vendor-neutral base다.** Langfuse는 LLM observability, prompt, evaluation, dataset integration이다.
15. **기본 설치는 제품 중심이되 가볍다.** 문서/RAG/conversation 계약은 포함하지만 provider SDK, parser, DB/cloud client, server, voice는 선택 설치다.
16. **plugin은 지연 로드한다.** installed distribution 탐색만으로 provider SDK를 import하지 않는다.
17. **명세는 TCK와 acceptance application으로 검증한다.** 예시 YAML만으로 의미론을 정의하지 않는다.
18. **정책은 prompt가 아니라 runtime 계약이다.** 권한, quota, budget, tool/effect, data capture, review/gate는 typed decision과 enforcement point를 가진다.
19. **사용량과 예산을 분리한다.** UsageLedger는 실제 사용량, BudgetLedger는 allocation/reservation/settlement를 소유한다.
20. **초과 시 종료 경계를 명시한다.** 현재 turn/task/item을 완료할지, checkpoint할지, 즉시 취소할지 policy가 atomic unit과 overdraft를 정의한다.
21. **도메인 객체를 core에 축적하지 않는다.** Snapshot, ChangeSet, Evidence, Check, Gate, Review, TaskPlan 같은 공통 work contract로 일반화한다.

## 6. 핵심 문장

> **GraphBlocks는 자연어와 파일이 graph를 통과하는 동안 타입, 출처, 권한, 예산, 실행 의미론, 검증, release identity, 관찰 가능성을 보존하는 Rust-native AI application runtime이다.**

## 7. 비목표

초기 코어는 다음을 목표로 하지 않는다.

- 범용 Apache Beam/Flink 대체
- 모든 provider 기능의 완전한 최소공통분모화
- Python 임의 객체의 자동 직렬화
- 미신뢰 native plugin의 in-process 실행
- block마다 Kubernetes Pod 하나를 만드는 실행 모델
- Terraform state나 Kubernetes CRD를 run/event store로 사용하는 것
- 하나의 `graphblocks-all` 패키지/이미지에 parser, provider, DB, media stack을 모두 포함하는 것
- LangGraph, Haystack, LangChain 내부 scheduler의 완전한 복제

## 8. 권장 읽기 순서

일반 애플리케이션 개발자는 Part I, III, IV, V, VI, VII을 먼저 읽는다. Runtime/SDK 구현자는 Part II를 추가로 읽는다. Production 운영자는 Part VIII, IX, X, XI를 읽는다. Voice 또는 범용 unbounded stream이 필요할 때만 Extension A/B를 적용한다.

## 9. v0.8 대비 최종 확정 변경

- GraphSpec API가 `v1alpha3`로 상승한다.
- document-only `SourceSpan`은 `SourceRef + SourceLocator`로 일반화되고 기존 값은 `DocumentSpan` variant로 migration한다.
- `SearchHit.document`는 `KnowledgeItemRef`로 변경한다.
- `Claim`, `Diagnostic`, `EvidenceRef`, `ResourceSnapshotRef`, `ChangeSet`, `ReviewRecord`, `Check/Gate/Trial`, `ResultBundle`을 canonical contract로 정의한다.
- `DatasetCase`는 chat 전용 field 집합에서 typed input/expected/assertion 구조로 변경한다.
- `Approval`과 substantive `Review`를 분리한다.
- model이 graph topology를 직접 수정하지 않고 optional `TaskPlan/TaskPlanPatch` executor를 사용한다.
- `Outcome<T>`에 `Denied`, `BudgetExhausted`, `Paused`를 추가한다.
- UsageLedger의 quota 책임을 분리해 `BudgetLedger`가 allocation/reservation/settlement를 소유한다.
- PolicyBundle/PolicyProfile, typed obligation, exhaustion boundary를 공개 계약으로 추가한다.
- `graphblocks-policy`, `graphblocks-budget`, `graphblocks-orchestration` package 경계를 추가한다.

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

ToolResult diagnostics MUST be mapping records with non-empty string codes and messages; malformed
diagnostic entries must fail as ToolResult validation errors before result delivery or persistence.
ToolResult `artifacts` and `diagnostics` MUST be list-like collections; scalar strings,
single mapping records, or non-iterable values MUST fail before entry normalization.
When present, `ToolResult.error` MUST be a BlockError mapping with non-empty string `code` and
`message` fields.
Before tool output validation, policy processing, redaction, capture, or model return, the runtime
MUST validate that the boundary records are typed `ToolCall`, `ToolResult`, `ResolvedTool`, and
schema registry instances. Malformed boundary records MUST fail as ToolResult validation errors
before field dereference, schema lookup, or content-policy evaluation.
Model-visible trust designation, prompt-injection label, and content-classification labels applied
during tool-output preparation MUST be non-empty after trimming. Empty labels MUST fail before the
tool result is returned to the model.
When capture metadata is applied during tool-output preparation, the capture policy MUST be a typed
mapping. `mode` MUST be a recognized string literal, `retention_policy` MUST be a non-empty string,
and a supplied `consent_ref` MUST be a non-empty string. Malformed capture policy MUST fail before
capture metadata is attached or returned to the model.
Tool-output byte limits applied before model return MUST be non-negative integers; booleans and
non-integer values MUST fail as ToolResult validation errors before size comparison or delivery.

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


# Part II. Graph IR과 Rust Runtime

## 36. 런타임 소유권

GraphBlocks의 규범적 실행 엔진은 `NativeRustRuntime`이다.

```text
Python SDK / Rust API / CLI
          ↓
GraphSpec parser and compiler frontend
          ↓
Language-neutral Graph IR
          ↓
NativeRustRuntime
  - planner
  - scheduler
  - executor
  - flow controller
  - sequence runtime
  - state/run store adapters
  - cancellation/resource scope
          ↓
Blocks / Connectors / Provider Adapters
```

`LangGraph`, `Haystack`, `LangChain`은 core backend가 아니라 bridge 또는 subgraph implementation이다. `Eject`는 실행 backend가 아니라 배포 산출물 생성 target이다.

### Rust가 소유하는 영역

- dependency resolution과 executable node 계산
- task scheduling과 concurrency
- timeout, cancellation, resource scope
- bounded channel과 backpressure
- retry arbitration
- terminal state 결정
- flow semaphore, rate limit, bulkhead
- run lifecycle와 checkpoint coordinator
- Python worker와 remote block protocol

### Python이 소유하는 영역

- graph authoring API
- custom block authoring
- Python provider SDK adapter
- Pydantic type facade와 IDE typing
- application callback
- notebook 및 test ergonomics

Python event loop가 runtime source of truth가 되어서는 안 된다.

## 37. 권장 Rust workspace

```text
crates/
  graphblocks-schema/          # GraphSpec/IR wire schema
  graphblocks-types/           # canonical value schema
  graphblocks-compiler/        # validation and executable plan
  graphblocks-runtime-core/    # scheduler, lifecycle, cancellation
  graphblocks-runtime-seq/     # bounded sequence and channels
  graphblocks-runtime-durable/ # optional checkpoint/replay
  graphblocks-flow/            # semaphore/rate limit/bulkhead
  graphblocks-telemetry/       # OTel events and metrics
  graphblocks-protocol/        # Python worker/remote protocol
  graphblocks-python/          # PyO3 binding only
  graphblocks-cli-native/      # optional native CLI helpers
  graphblocksd/                # standalone server
```

규칙:

- PyO3 dependency는 `graphblocks-python` 밖으로 전파되어서는 안 된다.
- runtime core의 공개 값은 Rust-owned schema 또는 bytes여야 한다.
- Python callback을 호출할 때만 GIL/Python runtime 경계로 진입한다.
- Cargo feature는 provider integration catalog를 표현하는 데 사용하지 않는다.
- provider와 parser는 별도 package/crate로 배포한다.

## 38. Authoring layer와 normalized IR

GraphBlocks는 사람이 작성하는 DSL과 runtime이 실행하는 IR을 구분한다.

```text
Authoring GraphSpec
- shorthand 허용
- composite block 허용
- 단일 connection shorthand 제한적 허용
- 사람이 읽을 수 있는 expression

Normalized Graph IR
- 모든 port와 edge 명시
- 모든 resource slot binding 명시
- wrapper/flow/policy obligation 정규화
- branch outcome과 optionality 명시
- implementation/package requirement 명시
- secret 값 제외
```

Compiler와 CLI는 다음을 제공해야 한다.

```bash
graphblocks plan graph.yaml --expand
graphblocks plan graph.yaml --show-bindings
graphblocks plan graph.yaml --show-packages
graphblocks plan graph.yaml --target standalone-rust
```

## 39. GraphSpec 기본 구조

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: intranet-rag-turn
  version: 2.0.0

spec:
  profile: conversation

  interface:
    inputs:
      turn:
        type: graphblocks.ai/ConversationTurnInput@1
      auth:
        type: graphblocks.ai/AuthContext@1
    outputs:
      result:
        type: graphblocks.ai/TurnCandidate@1
    events:
      - graphblocks.ai/AssistantDraftDelta@1
      - graphblocks.ai/RetrievalProgress@1
    interrupts:
      - graphblocks.ai/ApprovalRequested@1

  state:
    schema: company.ai/IntranetChatState@3

  policies:
    bundle: company-ai-policy@sha256:...
    profile: production-interactive
    attachments:
      - retry: default-read
      - security: intranet
      - capture: production
      - usage: interactive-graceful

  nodes: {}
  edges: []
```

GraphSpec에는 HTTP path, TUI widget, replica 수, Kubernetes node selector, cloud IAM resource를 넣지 않는다.

## 40. 단일 wiring source of truth

Port 연결은 `edges`만이 source of truth다. Node 안에 `inputs.from`, `config.input_from`, 별도 `edges`를 동시에 기록하지 않는다.

```yaml
nodes:
  retrieve:
    block: retrieve.hybrid@1
    bindings:
      retriever: company_knowledge

  build_context:
    block: context.build@1

edges:
  - from: $input.turn.message
    to: retrieve.query
  - from: $input.auth
    to: retrieve.auth
  - from: retrieve.result
    to: build_context.retrieval
  - from: build_context.context
    to: $output.context
```

`$input`, `$output`, `$state`, `$context`는 compiler가 제공하는 pseudo node다.

## 41. BindingSpec

BindingSpec은 logical resource name을 environment-specific resource configuration에 연결한다. Secret 값 자체는 포함하지 않고 `SecretRef`만 포함한다.

```yaml
apiVersion: graphblocks.ai/v1alpha1
kind: Binding
metadata:
  name: company-ai-production

spec:
  resources:
    answer_model:
      kind: ChatModel
      provider: openai
      implementation: openai.responses
      config:
        model: chat-model-production
      credentials:
        secretRef: secret://openai/production

    company_knowledge:
      kind: Retriever
      implementation: qdrant.hybrid
      config:
        collection: intranet_docs_v17
        endpoint: https://qdrant.internal
      credentials:
        secretRef: secret://qdrant/production

    conversations:
      kind: ConversationStore
      implementation: postgres
      config:
        dsnRef: secret://postgres/conversations-dsn
```

Binding은 다음 시점 중 하나에 resolve할 수 있다.

```text
compile time    schema/capability만 확인
release time    package/implementation/prompt/index revision 고정
deployment time endpoint/secret reference/region 해석
run time        short-lived credential와 dynamic lease 획득
```

Run provenance에는 secret 값이 아니라 resolved binding hash, resource revision, credential reference ID를 기록한다.

## 42. Resource slot

BlockDescriptor는 외부 자원을 typed resource slot으로 선언한다.

```rust
pub struct ResourceSlotDescriptor {
    pub name: String,
    pub resource_type: TypeRef,
    pub cardinality: Cardinality,
    pub required_capabilities: CapabilitySet,
    pub optional: bool,
}
```

예:

```yaml
block:
  id: retrieve.execute_plan
  policyAttachments:
    - usage: interactive-default
    - review: required-for-publish

  resourceSlots:
    retrievers:
      type: graphblocks.ai/Retriever@1
      cardinality: many
    embedding:
      type: graphblocks.ai/EmbeddingModel@1
      optional: true
    reranker:
      type: graphblocks.ai/Reranker@1
      optional: true
```

Node는 named binding을 사용한다.

```yaml
nodes:
  execute_retrieval:
    block: retrieve.execute_plan@1
    bindings:
      retrievers:
        dense: qdrant_dense
        keyword: opensearch_keyword
        tickets: ticket_search
      embedding: query_embedding
```

`connection: x`는 BlockDescriptor에 required resource slot이 정확히 하나이고 cardinality가 one인 경우에만 `bindings: {<slot>: x}`의 shorthand로 허용한다.

## 43. NodeSpec

```yaml
nodes:
  generate:
    block: model.chat@1
    implementation: openai.responses
    bindings:
      model: answer_model
      promptRegistry: prompts
    config:
      temperature: 0.1
      maxOutputTokens: 1600
    execution:
      class: remote_model_call
      requires:
        capabilities:
          - network.egress.model
    flow:
      timeout: 45s
      retry: model-read
      rateLimit: llm-production
    policies:
      capture: model-redacted
```

| 필드 | 의미 |
|---|---|
| `block` | provider-neutral semantic block type과 major version |
| `implementation` | 선택 implementation ID; binding으로 추론 가능하면 생략 |
| `bindings` | BlockDescriptor resource slot과 logical resource 연결 |
| `config` | compile-time 또는 bind-time configuration |
| `execution` | portable capability/resource/isolation requirement 또는 hint |
| `flow` | scheduler 정책 |
| `policies` | 보안, capture, approval, audit 등 |

Graph node의 `execution`에는 portable requirement만 둔다. Kubernetes selector와 target 이름은 GraphDeployment에서 정한다.

## 44. BlockDescriptor

```rust
pub struct BlockDescriptor {
    pub type_id: String,
    pub version: u32,
    pub role: BlockRole,
    pub lifecycle: LifecycleKind,
    pub input_mode: InputMode,
    pub output_mode: OutputMode,
    pub inputs: Vec<PortDescriptor>,
    pub outputs: Vec<PortDescriptor>,
    pub resource_slots: Vec<ResourceSlotDescriptor>,
    pub effects: EffectSet,
    pub capabilities: CapabilitySet,
    pub execution_requirements: ExecutionRequirements,
    pub policy_requirements: PolicyRequirementSet,
    pub usage_capabilities: UsageCapabilitySet,
    pub cancellation_guarantee: CancellationGuarantee,
    pub config_schema: SchemaRef,
    pub state_schema: Option<SchemaRef>,
}
```

### Role

```text
source
value
transform
model
embedder
retriever
ranker
builder
router
validator
tool
effect
control
composite
```

`surface`는 GraphSpec role이 아니다. TUI, CLI, HTTP, IDE는 ApplicationSpec과 client/server adapter가 소유한다.

### Lifecycle

```text
finite
session
service
```

대부분의 자연어 및 파일 block은 `finite`다. 긴 ingestion job도 여러 finite block과 durable control operator로 구성될 수 있다.

### Input/output mode

```text
InputMode:
  value
  bounded_sequence
  unbounded_stream
  duplex

OutputMode:
  value
  incremental
  bounded_sequence
  stream
  duplex
```

LLM 호출은 일반적으로 `finite + value input + incremental projection + final value`다. 이를 raw audio stream과 같은 단일 StreamBlock 분류로 축소하지 않는다.

### Effects

```text
pure
external_read
external_write
filesystem_read
filesystem_write
network
process
user_visible
security_sensitive
destructive
```

`external_write`, `destructive`, `process`를 포함하는 block은 policy, audit, idempotency, rollback/cancellation capability 또는 approval 요구를 선언해야 한다. Model/tool/compute block은 preflight estimate, final usage report, streaming usage, cancellation 지원 여부를 capability로 공개한다.

### Execution requirement

```yaml
executionRequirements:
  capabilities:
    - document.parse.pdf
    - python.worker
  isolation: process       # in_process | process | sandbox | remote
  resources:
    class: cpu_heavy
    memoryHint: 4Gi
  locality:
    prefers:
      - artifact_store
  placementPortability: equivalent
```

정확한 VM, Pod, node pool은 requirement가 아니라 DeploymentSpec concern이다.

## 45. PortDescriptor

```rust
pub struct PortDescriptor {
    pub name: String,
    pub type_ref: TypeRef,
    pub cardinality: Cardinality,
    pub required: bool,
    pub mode: PortMode,
    pub variadic: bool,
    pub sensitivity: Option<Sensitivity>,
    pub absence_policy: AbsencePolicy,
}
```

```text
Cardinality: one | optional | many
PortMode: value | incremental | bounded_sequence | stream | duplex
AbsencePolicy: reject | skip_node | use_default | accept_outcome
```

Compiler는 port 존재, type, cardinality, mode, variadic 연결 수, required input, sensitivity, absence policy, backend capability를 검증한다.

## 46. TypeRef와 schema compatibility

```text
TypeRef =
  Primitive
  | List<TypeRef>
  | Map<String, TypeRef>
  | Optional<TypeRef>
  | Outcome<TypeRef>
  | Union<TypeRef...>
  | NamedSchema(schema_id, version)
  | Artifact
```

Compatibility 규칙:

- exact schema version match가 기본이다.
- registry에 backward-compatible migration adapter가 있으면 허용한다.
- narrowing union은 명시적 router/validator가 필요하다.
- `Any`는 GraphSpec 공개 port에서 금지한다.
- Python class identity는 schema identity가 아니다.
- remote edge를 통과하는 값은 wire encoding이 정의되어야 한다.

## 47. Compile pipeline

```text
parse authoring spec
→ normalize shorthand
→ resolve block descriptors and resource slots
→ resolve schemas
→ validate ports, edges, absence/readiness
→ insert explicit adapters
→ resolve bindings and capability requirements
→ validate effects, approval, idempotency, rollback class
→ validate execution profile and target compatibility
→ dead-node/reachability/loop analysis
→ compute normalized plan and package closure
→ generate plan hash
→ optional release lock resolution
```

대표 compile diagnostic:

```text
GB1001 DeadNode
GB1003 RequiredInputNeverProduced
GB1004 OptionalBranchFeedsRequiredInput
GB1005 BranchOutputTypesDoNotUnify
GB1006 AmbiguousResourceBinding
GB1008 UnboundedLoopWithoutLimit
GB1011 EffectMissingIdempotencyPolicy
GB1012 ProtectedRetrievalMissingAuthContext
```

## 48. Invocation interface

```rust
#[async_trait]
pub trait InvocationBlock: Send + Sync {
    async fn run(
        &self,
        inputs: ValueMap,
        emitter: &dyn IncrementalEmitter,
        ctx: &ExecutionContext,
    ) -> Result<ValueMap, BlockError>;
}
```

`IncrementalEmitter`는 optional projection이며 final output을 대체하지 않는다.

```rust
#[async_trait]
pub trait IncrementalEmitter: Send + Sync {
    async fn emit(&self, port: &str, value: TypedValue) -> Result<EmitOutcome, EmitError>;
    fn is_cancelled(&self) -> bool;
}
```

규칙:

- emitter가 없는 실행에서도 block은 final output을 생성해야 한다.
- partial output 이후 retry 정책이 별도로 정의되어야 한다.
- delta는 durable state patch로 자동 승격되지 않는다.

## 49. Bounded sequence interface

대용량 파일 page/chunk 처리에는 bounded sequence가 유용하지만, 이를 무한 stream으로 취급하지 않는다.

```rust
#[async_trait]
pub trait SequenceBlock: Send + Sync {
    async fn run_sequence(
        &self,
        inputs: InputPorts,
        outputs: OutputPorts,
        ctx: &ExecutionContext,
    ) -> Result<ValueMap, BlockError>;
}
```

Sequence runtime이 channel을 소유한다. Block은 raw channel 구현을 생성하지 않는다.

```text
OPEN → COMPLETED | FAILED | CANCELLED
```

Terminal signal은 정확히 한 번만 발생해야 한다.

## 50. Python block adapter

Python block은 다음 execution kind를 지원한다.

| kind | 용도 | 기본 격리 |
|---|---|---|
| `python_inproc` | 짧고 신뢰된 callback | 동일 process/GIL |
| `python_worker` | CPU, parser, provider SDK | subprocess/worker pool |
| `remote` | 언어 독립 service | gRPC/HTTP protocol |
| `rust_builtin` | hot path와 core operator | in-process Rust |
| `wasm` | 미래 portable sandbox | optional extension |

권장 전환 경로:

```text
python_inproc → python_worker → rust_builtin 또는 remote
```

BlockDescriptor와 TCK가 동일하면 graph를 변경하지 않고 implementation만 교체할 수 있다.

## 51. FFI boundary

- Python 임의 객체를 runtime queue에 저장하지 않는다.
- frame/item마다 GIL을 왕복하지 않는다.
- 대량 document element는 batch 또는 serialized buffer로 넘긴다.
- cancellation token은 Rust가 소유하고 Python에는 read-only handle을 제공한다.
- Python exception은 canonical `BlockError`로 변환한다.
- Rust panic은 process abort가 아니라 boundary에서 error로 격리해야 한다. 단, memory safety 위반 가능 상태는 fail-fast할 수 있다.

## 52. Run lifecycle

```text
CREATED
→ VALIDATING
→ ADMISSION_PENDING
→ QUEUED
→ RUNNING
→ PAUSED | INTERRUPTED
→ COMPLETED | FAILED | CANCELLED | POLICY_STOPPED
```

Node lifecycle:

```text
PENDING
→ READY
→ WAITING_BUDGET | WAITING_LEASE | WAITING_APPROVAL
→ RUNNING
→ COMPLETED | FAILED | CANCELLED | SKIPPED | PAUSED | POLICY_STOPPED
```

Invariant:

1. terminal 상태는 정확히 한 번만 기록한다.
2. terminal 이후 output이나 state patch를 수락하지 않는다.
3. cancel은 idempotent하다.
4. `COMPLETED` 전에 required output validation, usage settlement, policy finalization이 끝나야 한다.
5. `FAILED`는 canonical error를 가진다.
6. effect commit 상태와 node terminal 상태의 순서를 명시한다.
7. scheduler readiness는 값 부재와 terminal outcome을 구분한다.
8. admission되지 않은 run은 provider/tool/effect를 시작하지 않는다.
9. paused run은 checkpoint와 resume precondition을 가진다.
10. policy exhaustion은 user cancel이나 provider failure와 다른 terminal/paused reason을 가진다.

## 53. Outcome과 absence semantics

분기와 부분 실행에서 다음은 서로 다른 상태다.

```text
Value(null)       node가 실행되어 정상적으로 null을 반환
Absent            해당 경로에서 값이 생성되지 않음
Skipped           조건에 의해 node가 실행되지 않음
Denied            policy가 실행을 거부
BudgetExhausted   budget/quota boundary에서 중단
Paused            resumable checkpoint에서 일시 정지
Failed            실행했으나 실패
Cancelled         외부/상위 취소
```

```rust
pub enum Outcome<T> {
    Value(T),
    Absent,
    Skipped(SkipReason),
    Denied(PolicyDecisionRef),
    BudgetExhausted(BudgetExhaustion),
    Paused(PauseReason),
    Failed(BlockError),
    Cancelled(CancelReason),
}
```

규칙:

- 일반 `T` input은 `Outcome<T>`를 암묵적으로 받지 않는다.
- optional branch output을 required input에 연결하면 compile error다.
- `control.select`, `control.fallback`, `outcome.require`, `outcome.collect`이 명시적으로 outcome을 해석한다.
- `null`은 schema가 허용한 정상 값이며 branch absence의 sentinel로 사용하지 않는다.
- `Denied`, `BudgetExhausted`, `Paused`를 일반 `Failed`로 축소하지 않는다.

## 54. Structured cancellation

```python
class CancelReason(BaseModel):
    code: Literal[
        "client_disconnect", "user_cancel", "timeout", "superseded",
        "policy_denied", "budget_exhausted", "provider_quota_exhausted",
        "dependency_failed", "shutdown", "barge_in",
        "rollout_drain", "lease_lost", "entitlement_revoked"
    ]
    message: str | None = None
    requested_by: str | None = None
    policy_decision_ref: str | None = None
```

Cancellation scope:

```text
provider_call
node
branch
task_group
agent_step
turn
map_item
task
trial
run
job
session
```

Parent cancellation은 기본적으로 child에 전파된다. Child failure가 siblings 또는 parent를 취소하는지는 task group/loop/map policy가 결정한다.

Cancellation guarantee:

```text
immediate_local
cooperative
best_effort_remote
non_cancellable_atomic_section
```

Runtime은 `cancel_immediately` policy라도 provider/effect capability보다 강한 보장을 주장하지 않는다.

## 55. Error model

```python
class BlockError(BaseModel):
    code: str
    category: Literal[
        "validation", "configuration", "authentication", "authorization",
        "not_found", "rate_limit", "quota", "budget", "capacity",
        "timeout", "transient", "permanent", "provider", "policy",
        "cancelled", "conflict", "internal"
    ]
    message: str
    retryable: bool
    details: dict[str, JsonValue] = Field(default_factory=dict)
    cause_chain: list[str] = Field(default_factory=list)
```

Error는 item error, batch partial error, node fatal, run fatal, connector unavailable, internal quota exhaustion, provider quota exhaustion, policy denial, lease loss를 구분한다.

## 56. Retry

```yaml
retryPolicies:
  model-read:
    maxAttempts: 3
    backoff:
      kind: exponential
      initial: 250ms
      max: 8s
      jitter: full
    retryOn: [rate_limit, timeout, transient]
    allowedUntil: first_output
    onPartialOutput: fail
    reserveBudgetPerAttempt: true
```

핵심 규칙:

- partial output 이후 전체 LLM 호출 재시도는 기본 금지다.
- provider resume cursor 또는 dedup contract가 있을 때만 부분 재개한다.
- effect retry에는 idempotency key가 필요하다.
- validation/policy/internal budget error는 기본 retry 대상이 아니다.
- provider quota는 `Retry-After` 또는 reset 정보를 존중하고 무한 retry하지 않는다.
- 각 retry attempt는 budget reservation과 실제 usage accounting을 가진다.
- retry attempt와 provider request ID를 execution journal/telemetry에 기록한다.

## 57. Idempotency, effect journal, rollback class

```text
policy.precheck
→ budget/lease reserve
→ approval, if required
→ idempotency.lookup
→ effect.prepare
→ effect.execute
→ effect.commit
→ usage/budget settlement
→ execution journal/audit outbox
→ node.completed
```

BlockDescriptor는 effect rollback capability와 cancellation safety를 선언한다.

```text
rollback:
  none
  idempotent_replay
  compensatable
  reversible

cancellation:
  before_prepare_only
  cancel_if_safe
  finish_atomic_commit
  non_cancellable
```

외부 시스템이 transaction을 제공하지 않으면 journal은 결과 exactly-once를 보장하지 않는다. GraphBlocks는 invocation delivery와 external outcome guarantee를 분리해서 표시한다.

## 58. State model

GraphBlocks의 기본 데이터 흐름은 immutable value edge다. Global mutable dictionary를 암묵적 통신 수단으로 사용하지 않는다.

```python
class StatePatch(BaseModel):
    operations: list[PatchOperation]
    expected_revision: int | None = None
```

```text
PatchOperation = set | append | merge | remove | increment
```

State schema는 reducer와 conflict policy를 정의한다.

```yaml
state:
  schema: company.ai/ChatState@3
  reducers:
    messages: append_unique_by_id
    memory: merge_by_key
    usage: sum
  conflict: compare_and_swap
```

Budget balance는 일반 graph state reducer로 관리하지 않는다. Distributed BudgetLedger의 atomic reservation/settlement를 사용한다.

## 59. Execution record 책임

실행 correctness와 운영 분석을 하나의 EventStore에 혼합하지 않는다.

| 구성요소 | 책임 | 손실 허용 |
|---|---|---|
| RunStore | 현재 run/node snapshot, output/checkpoint pointer | workload 정책에 따름 |
| ExecutionJournal | terminal, effect commit, checkpoint, lease epoch | durable workload에서 불가 |
| AuditLog | actor/action/resource/policy/approval/review/delete | 불가 |
| UsageLedger | 실제 token/audio/compute/storage/cost | 불가 |
| BudgetLedger | allocation/reservation/commit/release/quota balance | hard policy에서 불가 |
| ApplicationEventStream | UI draft/progress/approval/policy event | 정책에 따라 coalesce/drop/replay |
| Telemetry | OTel trace/metric/log/profile | sampling/drop 가능 |

Part IX와 Part X가 각 record의 schema, delivery, retention, enforcement를 정의한다. Langfuse나 Prometheus는 execution, billing, quota source of truth가 아니다.


## 60. Automatic concurrency와 control primitive

입력이 준비된 독립 node는 scheduler가 자동으로 병렬 실행한다. 단순 병렬성을 표현하기 위해 wrapper node를 추가하지 않는다.

표준 control primitive:

```text
control.branch          조건에 따라 한 경로를 선택
control.switch          tagged decision에 따라 route
control.select          여러 Outcome 중 정책에 맞는 값을 선택
control.task_group      deadline, cancellation, quorum, failure policy가 있는 child group
control.map             bounded item-level subgraph invocation
control.loop            구조화 반복과 termination contract
control.try             error/outcome handling
control.fallback        ordered alternatives
control.subgraph        graph invocation
sequence.collect        여러 값을 sequence로 수집
value.merge             구조화 값 merge
stream.merge            stream extension의 stream merge
retrieve.fuse           retrieval semantics 기반 fusion
control.await           완료 동기화가 data로 필요한 제한적 primitive
```

Generic `control.parallel`과 `control.join`은 public v1alpha2 authoring API에서 권장하지 않는다. Migration compiler는 다음처럼 변환한다.

```text
parallel(children, failure policy) → control.task_group
join(all values)                  → sequence.collect
join(wait only)                   → implicit readiness 또는 control.await
join(search hits)                 → retrieve.fuse
join(objects)                     → value.merge
```

`flow.barrier`는 분산 parties의 rendezvous가 필요한 경우에만 사용한다.

## 61. control.map contract

```yaml
nodes:
  process_assets:
    block: control.map@2
    config:
      graph: graphs/process-single-asset.yaml
      mapping:
        itemInput: asset
        resultOutput: outcome
      itemKey: $.revision_id
      concurrency: 16
      preserveOrder: true
      stateIsolation: item
      checkpoint: per_item
      onError: collect
      retryFailedItems: true
      budgetReservation: per_item
      onBudgetExhaustion: checkpoint_and_pause
```

Map body graph는 typed interface를 가져야 한다.

```text
input item type
output result type
item state scope
item key and idempotency scope
checkpoint granularity
ordering
partial failure policy
item budget and exhaustion boundary
```

Map output은 `list[ItemOutcome<T>]` 또는 명시된 aggregation type이다. 성공 값만 반환해 실패 item을 숨겨서는 안 된다.

## 62. control.task_group

```yaml
nodes:
  retrieve_sources:
    block: control.task_group@1
    config:
      children: [dense_search, keyword_search, ticket_search]
      deadline: 3s
      failure: collect
      minimumSuccesses: 1
      cancellation: cancel_siblings_on_fatal
```

Task group은 실제 node dependency를 대체하지 않는다. 정책 범위를 부여할 때만 사용한다.

## 63. Structured loop

Loop는 최대 step 또는 termination proof를 가져야 한다.

```yaml
nodes:
  agent:
    block: control.loop@1
    config:
      body: graphs/agent-step.yaml
      maxIterations: 20
      until: $.state.exit_reason != null
      checkpoint: each_iteration
```

동적 임의 graph mutation 대신 typed loop state와 명시적 exit condition을 사용한다.

## 64. Flow policy와 resource lease

Semaphore, timeout, retry, rate limit, budget reservation은 대부분 data node가 아니라 scheduler policy다.

```yaml
flow:
  semaphores:
    document-convert:
      scope: worker
      limit: 2

  rateLimits:
    embedding-api:
      scope: distributed
      limit: 600
      per: minute
      coordination: redis-main

  leasePools:
    licensed-tool:
      scope: distributed
      resourceClass: commercial_tool_license
      capacityUnits: 8
      coordination: postgres-main
      ttl: 120s
      renewal: 30s

nodes:
  convert:
    block: document.convert@1
    flow:
      semaphore: document-convert
      timeout: 120s
```

Distributed primitive는 lease와 fencing token을 지원해야 한다.

`Semaphore`는 동일한 unit의 동시성에 적합하다. 속성 선택, 용량 단위, heartbeat, cleanup, usage accounting이 필요한 scarce resource는 `LeasePool`을 사용한다.

```python
class LeaseRequest(BaseModel):
    pool_id: str
    units: Decimal
    attribute_selector: dict[str, JsonValue] = Field(default_factory=dict)
    owner: ResourceRef
    deadline: datetime | None = None
    budget_reservation_id: str | None = None
```

```python
class ResourceLease(BaseModel):
    lease_id: str
    pool_id: str
    resource_identity: str | None
    units: Decimal
    attributes: dict[str, JsonValue]
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime
```

Lease loss는 stale worker의 commit을 막아야 한다. Resource usage는 Budget/UsageLedger와 연결할 수 있다.


## 65. Bounded channel과 batch

파일 page/chunk sequence와 incremental projection은 bounded buffer를 사용한다.

```yaml
buffer:
  maxItems: 256
  maxBytes: 16777216
  highWatermark: 0.8
  lowWatermark: 0.5
  onFull: block
```

Batch는 item count만으로 정의하지 않는다.

```yaml
batch:
  maxItems: 64
  maxBytes: 4194304
  maxWait: 500ms
  flushFinal: true
  onOversizedItem: emit_single
  onItemError: collect
```

`drop_silence`와 `compress`는 domain transform이며 일반 backpressure policy가 아니다.

## 66. Ordering과 concurrency

- `control.map(preserveOrder=true)`는 input order로 결과를 재정렬한다.
- `preserveOrder=false`는 completion order를 허용한다.
- per-key ordering은 partition key를 선언한다.
- merge는 기본적으로 ordering을 보장하지 않는다.
- effect parallelism은 idempotency, quota, transaction boundary와 함께 검증한다.

## 67. Incremental output, draft, commit, retract

Incremental output은 final state가 아니라 application projection이다.

```text
AssistantDraftStarted
AssistantDraftDelta
AssistantDraftCompleted
AssistantCommitted
AssistantRetracted
AssistantCorrected
```

Chat turn 권장 transaction:

```text
conversation.begin_turn
→ context.build
→ model.generate + draft events
→ answer.validate/finalize
→ conversation.commit_turn

실패/취소:
→ conversation.abort_turn
→ AssistantRetracted
```

규칙:

- durable source of truth는 committed `Message`와 `TurnResult`다.
- delta persist 기본값은 false다.
- reconnect/replay가 필요하면 ApplicationEventStream cursor를 사용한다.
- partial output 이후 retry와 correction policy를 명시한다.
- budget/policy stop 시 retract, incomplete commit, current-unit completion 중 하나를 명시한다.
- `AssistantDraftDelta`가 전송된 뒤 hard-stop이면 `AssistantRetracted` 또는 `AssistantIncomplete`를 반드시 보낸다.
- Output-policy application event builders MUST validate typed `GenerationChunk`,
  `OutputPolicyDecision`, `OutputCutoff`, and digest inputs before constructing events.
- Output-policy redaction instructions MUST be typed mapping records; malformed redaction entries
  MUST fail before event construction, delivery-gate mutation, or client delivery.
- Output-delivery `flush_boundaries` MUST be a collection of recognized boundary names; scalar
  strings or non-iterable values MUST fail before output delivery policy construction completes.

## 68. Checkpoint

```python
class CheckpointManifest(BaseModel):
    checkpoint_id: str
    run_id: str
    release_id: str
    deployment_revision_id: str
    plan_hash: str
    checkpoint_schema: SchemaRef
    state_revision: int
    completed_nodes: list[str]
    pending_nodes: list[str]
    effect_journal_ref: ArtifactRef | None = None
    source_cursors: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime
```

Checkpoint가 provider connection object나 Python object를 직렬화해서는 안 된다. Release upgrade 시 checkpoint compatibility/migration 정책을 검증한다.

## 69. Composite block

재사용 graph를 안정적 facade로 노출한다.

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: CompositeBlock
metadata:
  name: rag.federated_answer

spec:
  graph: graphs/federated-rag.yaml

  interface:
    inputs:
      query: graphblocks.ai/TextQuery@1
      history:
        type: list[graphblocks.ai/Message@1]
        optional: true
      auth: graphblocks.ai/AuthContext@1
    outputs:
      answer: graphblocks.ai/Answer@1
      retrieval: graphblocks.ai/FederatedRetrievalResult@1
    events:
      - graphblocks.ai/RetrievalProgress@1
      - graphblocks.ai/AssistantDraftDelta@1
    interrupts:
      - graphblocks.ai/ClarificationRequired@1

  resourceSlots:
    model:
      type: graphblocks.ai/ChatModel@1
    retrievers:
      type: graphblocks.ai/Retriever@1
      cardinality: many
    reranker:
      type: graphblocks.ai/Reranker@1
      optional: true

  exposeState:
    - retrieval.summary
    - context.token_usage
```

Composite block은 내부 node ID를 공개 API로 누출하지 않는다. event, interrupt, resource slot, state exposure를 명시한다.

## 70. Backend 및 bridge 분류

### NativeRustRuntime

모든 규범적 의미론을 지원한다.

### InProcessTestRuntime

- deterministic clock/ID
- mock connection
- no external durable store required
- controlled scheduler
- trace capture

### RemoteRuntime

Compiled plan 또는 graph invocation을 standalone `graphblocksd`로 위임한다.

### Framework bridge

```text
HaystackComponentBlock
HaystackPipelineBlock
LangGraphSubgraphBlock
LangChainRunnableBlock
LlamaIndexQueryEngineBlock
```

Bridge는 외부 framework의 내부 scheduler 의미론을 GraphBlocks 전체 backend로 위장해서는 안 된다.

### Eject target

```text
rust-server
python-fastapi
worker
container
```

Eject는 생성 시점의 package lock과 runtime protocol을 기록하는 code generation target이다.

## 71. Plan artifact

Compiler는 선택적으로 실행 plan을 생성한다.

```text
header
- graph schema version
- plan format version
- compiler version
- plan hash
- required runtime protocol

body
- normalized nodes/ports/edges
- resolved descriptor hashes
- policy bindings
- required plugin IDs and versions
- resource binding capability requirements
```

Plan에는 secret 값이 들어가서는 안 된다.

# Part III. 파일 및 문서 처리 Profile

## 72. 범위

Document profile은 파일을 읽는 기능만이 아니라 다음 lifecycle 전체를 정의한다.

```text
acquire
→ identify revision
→ validate and classify
→ convert/OCR
→ canonicalize
→ normalize/enrich
→ split
→ index/write
→ publish
→ update/delete
```

문서 처리 결과는 RAG뿐 아니라 요약, 번역, 분류, extraction, diff, artifact generation에 재사용되어야 한다.

## 73. 파일 사용 유형

| 유형 | 예 | 기본 수명 | 기본 저장 |
|---|---|---|---|
| direct analysis | “이 PDF 요약” | turn/conversation | 임시 |
| temporary corpus | 여러 파일을 올린 프로젝트 대화 | conversation/project | TTL |
| permanent knowledge | 사내 규정/매뉴얼 | project/tenant | durable |
| batch transformation | 번역본/보고서 생성 | job | output policy |
| generated artifact | PDF/DOCX/XLSX 산출물 | explicit | artifact store |

GraphSpec은 file attachment의 목적과 수명을 명시해야 한다.

## 74. Source acquisition

표준 source block:

```text
asset.from_upload
asset.from_local
asset.from_http
asset.from_blob
asset.from_record
asset.discover
asset.watch
```

Source는 `SourceAsset`과 `AssetRevision`을 반환해야 한다. 단순 path string만 반환하면 revision, checksum, ACL lineage를 잃는다.

### Remote fetch policy

HTTP 및 cloud fetch는 다음을 지원해야 한다.

- size limit
- content-type allowlist
- redirect limit
- timeout
- checksum validation
- SSRF protection
- egress policy
- credential scope
- range read capability

## 75. File fingerprint와 revision

Fingerprint는 최소한 content hash를 포함한다.

```python
class FileFingerprint(BaseModel):
    algorithm: Literal["sha256", "blake3"]
    digest: str
    size_bytes: int
    normalized_source_uri: str | None = None
```

Metadata-only 변경과 content 변경을 구분해야 한다.

```text
content revision
metadata revision
ACL revision
processing revision
```

재처리 여부는 위 revision과 processor config hash를 함께 사용해 결정한다.

## 76. MIME/type detection

확장자만 신뢰하지 않는다.

```text
filename extension
+ declared media type
+ magic bytes
+ archive/container inspection
→ DetectionResult
```

```python
class DetectionResult(BaseModel):
    media_type: str
    confidence: float
    container_type: str | None = None
    warnings: list[str] = Field(default_factory=list)
```

암호화 PDF, macro-enabled Office file, archive bomb, executable 포함 문서는 별도 policy로 처리한다.

## 77. Archive와 container 처리

ZIP, email, Office container, HWPX 등은 nested asset을 만들 수 있다.

```text
parent asset
  ├─ embedded image
  ├─ attachment
  ├─ worksheet
  └─ nested document
```

규칙:

- traversal path(`../`)를 거부한다.
- depth, file count, expanded size를 제한한다.
- child asset은 parent lineage를 가진다.
- embedded asset마다 독립 retention/ACL을 적용할 수 있다.

## 78. Conversion strategy

`document.convert`는 provider-neutral semantic block이다.

```yaml
nodes:
  convert:
    block: document.convert@1
    config:
      strategy: auto
      preferredImplementations:
        - docling
        - pypdf
      fallback: provider_native
```

Conversion output은 `ParsedDocument` 또는 conversion failure다.

### Converter capability

```text
supported_media_types
text_extraction
layout
page_images
tables
formulas
ocr
embedded_assets
password_protected
streaming_pages
```

Compiler 또는 binder는 요구 capability와 converter capability를 비교해야 한다.

## 79. FileInputStrategy

모든 파일을 먼저 Markdown으로 변환할 필요는 없다.

```text
provider_native
parsed_full_text
parsed_multimodal
retrieve_from_index
code_analysis
```

선택 기준:

| 전략 | 적합한 경우 | 주의점 |
|---|---|---|
| `provider_native` | 일회성 PDF/이미지 분석 | provider 종속, 재현성 |
| `parsed_full_text` | 짧은 문서, 정형 텍스트 | layout 손실 가능 |
| `parsed_multimodal` | 표/그림/페이지 layout 중요 | 비용과 payload 크기 |
| `retrieve_from_index` | 대규모/반복 질문 | indexing latency |
| `code_analysis` | spreadsheet, archive, programmatic 분석 | sandbox 필요 |

`file.input.select_strategy` block이 정책과 capability를 바탕으로 선택할 수 있다.

## 80. OCR

OCR은 converter의 숨겨진 옵션이 아니라 독립 processor로 모델링할 수 있다.

```text
document.ocr
image.ocr
pdf.ocr_overlay
```

OCR 결과에는 다음 provenance가 필요하다.

```text
engine/model/version
language hints
page/region
confidence
rotation/deskew
preprocessing config hash
```

OCR text는 원본 text layer를 덮어쓰지 않고 source variant로 보존해야 한다.

## 81. Canonical document element

Parser가 반환한 provider-specific tree를 canonical `DocumentElement`로 변환한다.

### TableElement

```python
class TableElement(BaseModel):
    element_id: str
    rows: list[TableRow]
    caption: str | None = None
    header_rows: int = 0
    location: SourceLocation
```

Cell에는 row/column span과 원본 위치를 보존해야 한다.

### ImageElement

```python
class ImageElement(BaseModel):
    element_id: str
    artifact: ArtifactRef
    alt_text: str | None = None
    caption: str | None = None
    location: SourceLocation
```

Image description을 생성한 경우 model/version과 생성 여부를 metadata에 기록한다.

### Spreadsheet

Spreadsheet는 단일 텍스트 문서로 평탄화하지 않는다.

```text
Workbook
→ Sheet
→ SheetRegion / Table
→ Cell values and formulas
```

Cell range를 citation에 사용할 수 있어야 한다.

### Presentation

Slide 번호, shape order, speaker note, image와 text 관계를 보존한다.

## 82. Normalization

표준 block:

```text
document.normalize_unicode
document.remove_repeated_header_footer
document.normalize_whitespace
document.repair_hyphenation
document.normalize_lists
document.normalize_tables
document.detect_language
```

Normalization은 원본 element를 파괴하지 않고 transformed document와 processor lineage를 생성해야 한다.

## 83. Cleaning과 redaction

```text
document.clean
document.redact_pii
document.remove_boilerplate
document.policy_filter
```

Redaction 결과는 다음을 기록한다.

- redaction rule/model
- 원본 span reference
- replacement token
- reversible 여부
- audit reference

원본 restricted artifact와 redacted derivative는 별도 ACL을 가질 수 있다.

## 84. Enrichment

```text
document.title_extract
document.metadata_enrich
document.classify
document.entity_extract
document.keyword_extract
document.summary
document.language_detect
document.security_label
```

Enrichment는 `DocumentElement` 또는 `ParsedDocument`를 mutate하지 않고 새 revision 또는 annotation을 생성한다.

## 85. Splitter/Chunker

표준 전략:

```text
fixed_tokens
sentence
paragraph
section_aware
page_aware
layout_aware
table_aware
semantic
parent_child
```

```yaml
nodes:
  split:
    block: document.split@1
    config:
      strategy: section_aware
      targetTokens: 600
      maxTokens: 850
      overlapTokens: 80
      preserveTables: true
      parentChild:
        enabled: true
        parentTokens: 2200
```

필수 output:

- chunk ID
- source element IDs
- source spans
- chunker version/config hash
- token count와 tokenizer ref
- ACL/security labels

## 86. Parent-child와 hierarchical retrieval

큰 section과 작은 retrieval chunk를 함께 사용할 수 있다.

```text
Parent chunk: 문맥 보존
Child chunk: 검색 정밀도
```

SearchHit이 child를 반환한 뒤 context selector가 parent를 확장할 수 있다. Parent 확장은 ACL과 token budget을 다시 검증해야 한다.

## 87. Deduplication

중복은 여러 단계에서 처리한다.

```text
asset exact duplicate
near-duplicate document
repeated template/boilerplate
near-duplicate chunk
```

Dedup 결과는 삭제가 아니라 canonical reference와 duplicate relationship으로 기록하는 것이 기본이다.

```python
class DuplicateRelation(BaseModel):
    source_id: str
    canonical_id: str
    method: str
    score: float | None = None
```

## 88. Embedding

```text
embedding.document
embedding.text
embedding.multimodal
```

```python
class EmbeddingRecord(BaseModel):
    embedding_id: str
    source_id: str
    vector: list[float] | None = None
    dimension: int
    model: str
    model_revision: str | None = None
    config_hash: str
    created_at: datetime
```

Vector를 event log나 telemetry에 넣지 않는다. 저장 위치 reference만 기록한다.

## 89. Ingestion manifest

```python
class IngestionManifest(BaseModel):
    manifest_id: str
    asset_id: str
    revision_id: str
    source_uri: str
    content_hash: str

    parser: ProcessorRef
    ocr: ProcessorRef | None = None
    normalizers: list[ProcessorRef] = Field(default_factory=list)
    chunker: ProcessorRef
    embedding: ProcessorRef | None = None

    parsed_document_ref: ArtifactRef | None = None
    chunk_set_ref: ArtifactRef | None = None
    index_records: list[IndexRecordRef] = Field(default_factory=list)

    acl_revision: str | None = None
    pipeline_hash: str
    status: Literal[
        "discovered", "processing", "ready", "failed", "superseded", "deleted"
    ]
    error: BlockError | None = None
    created_at: datetime
    updated_at: datetime
```

Manifest는 dedupe, retry, rollback, deletion, reindex, audit, lineage의 source of truth다.

## 90. Processing cache

Cache key:

```text
content_hash
+ block type/version
+ implementation version
+ config hash
+ relevant policy hash
+ schema version
```

Secret 값은 cache key에 직접 넣지 않는다. 결과가 tenant/ACL에 의존하면 scope를 key에 포함한다.

## 91. Ingestion transaction

일반적인 commit sequence:

```text
1. create processing manifest
2. write derived artifacts
3. write chunks/embeddings
4. upsert index records to staging namespace
5. validate counts and ACL payload
6. commit manifest
7. publish index revision/alias
8. mark previous revision superseded
```

중간 실패가 current index를 부분 변경하지 않도록 staging 또는 generation ID를 권장한다.

## 92. Index version과 publish

```text
knowledge/hr-v1
knowledge/hr-v2
alias: knowledge/hr-current → hr-v2
```

```yaml
nodes:
  publish:
    block: knowledge.publish@1
    connection: knowledge
    config:
      alias: hr-current
      targetRevision: ${state.index_revision}
      strategy: atomic_alias_swap
```

Connector가 atomic alias를 지원하지 않으면 capability error 또는 명시적 non-atomic policy가 필요하다.

## 93. Update와 change propagation

변경 종류별 동작:

| 변경 | 기본 동작 |
|---|---|
| content 변경 | parse부터 재처리 |
| parser/chunker 변경 | 해당 단계 이후 재처리 |
| embedding model 변경 | embedding/index 재생성 |
| metadata 변경 | metadata/index payload update |
| ACL 변경 | chunk/index ACL 즉시 갱신 |
| source delete | tombstone 후 retention policy 적용 |

ACL 변경은 content re-embedding을 요구하지 않아야 하지만 retrieval filter에는 즉시 반영되어야 한다.

## 94. Delete와 tombstone

```yaml
nodes:
  load_manifest:
    block: manifest.get@1

  remove_index:
    block: knowledge.delete@1
    connection: knowledge

  tombstone:
    block: manifest.tombstone@1

  schedule_artifact_delete:
    block: blob.delete@1
    connection: artifacts
```

Deletion 요구사항:

- permission
- audit
- idempotency
- index record removal
- derived artifact policy
- cache invalidation
- citation dead-link policy
- legal hold 예외

## 95. Generated artifacts

Output file도 first-class data다.

```python
class GeneratedArtifact(BaseModel):
    artifact: ArtifactRef
    kind: Literal[
        "report", "translation", "extraction", "spreadsheet", "presentation", "archive"
    ]
    source_ids: list[str]
    generator: ProcessorRef
    provenance: dict[str, JsonValue]
```

표준 block:

```text
artifact.render_pdf
artifact.render_docx
artifact.render_pptx
artifact.render_xlsx
artifact.write_json
artifact.bundle
```

## 96. 표준 document block catalog

```text
asset.fetch
asset.discover
asset.fingerprint
asset.detect_type
asset.unpack

file.input.select_strategy

document.convert
document.ocr
document.normalize
document.clean
document.redact
document.enrich
document.classify
document.extract
document.split
document.deduplicate
document.diff
document.write

embedding.document
knowledge.upsert
knowledge.delete
knowledge.publish
manifest.get
manifest.commit
manifest.tombstone
```

Provider 이름은 semantic block ID에 포함하지 않는다.

## 97. Single document ingestion 예

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: ingest-one-document
  version: 1.0.0

spec:
  profile: ingestion_job

  inputs:
    asset:
      type: graphblocks.ai/AssetRevision@1

  outputs:
    manifest:
      type: graphblocks.ai/IngestionManifest@1

  connections:
    artifacts: s3-artifacts
    knowledge: qdrant-knowledge
    manifests: postgres-manifests
    embedding: openai-embedding

  nodes:
    detect:
      block: asset.detect_type@1

    store_raw:
      block: blob.put@1
      connection: artifacts
      flow:
        retry: idempotent-write

    convert:
      block: document.convert@1
      config:
        strategy: auto
      flow:
        semaphore: document-convert
        timeout: 120s

    normalize:
      block: document.normalize@1

    split:
      block: document.split@1
      config:
        strategy: section_aware
        targetTokens: 600
        overlapTokens: 80

    embed:
      block: embedding.document@1
      connection: embedding

    upsert:
      block: knowledge.upsert@1
      connection: knowledge
      flow:
        retry: idempotent-write

    commit:
      block: manifest.commit@1
      connection: manifests

  edges:
    - from: $input.asset
      to: detect.asset
    - from: $input.asset
      to: store_raw.asset
    - from: $input.asset
      to: convert.asset
    - from: detect.result
      to: convert.detection
    - from: convert.document
      to: normalize.document
    - from: normalize.document
      to: split.document
    - from: split.chunks
      to: embed.documents
    - from: embed.documents
      to: upsert.documents
    - from: upsert.records
      to: commit.index_records
    - from: commit.manifest
      to: $output.manifest
```

## 98. Direct file analysis 예

```yaml
nodes:
  select_strategy:
    block: file.input.select_strategy@1
    config:
      maxNativeBytes: 20000000
      preferRetrievalAbovePages: 80

  analyze:
    block: model.chat@1
    connection: model

edges:
  - from: $input.message
    to: select_strategy.message
  - from: $input.attachments
    to: select_strategy.attachments
  - from: select_strategy.context
    to: analyze.context
```

Direct analysis 결과도 가능한 경우 `Citation`을 source page/cell에 연결한다.

## 99. Document processing quality metrics

```text
conversion_success_rate
text_coverage
layout_element_recall
table_structure_accuracy
ocr_character_error_rate
heading_preservation
chunk_size_distribution
chunk_source_span_coverage
duplicate_rate
index_write_success_rate
acl_payload_accuracy
delete_propagation_latency
```

Metric은 processor version과 fixture revision에 연결되어야 한다.

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

# Part V. Conversation, Chatbot, Memory, Agent

## 124. Conversation profile

Conversation profile은 multi-turn 상태를 가지지만 각 turn은 finite invocation이다.

```text
Conversation lifetime
  ├─ Turn 1: finite run + incremental output
  ├─ Turn 2: finite run + tools
  └─ Turn 3: finite run + attachment retrieval
```

Raw transport session과 conversation identity를 동일시하지 않는다. HTTP 요청, SSE reconnect, WebSocket connection이 바뀌어도 같은 conversation을 이어갈 수 있다.

## 125. Conversation store contract

```rust
#[async_trait]
pub trait ConversationStore: Send + Sync {
    async fn create(&self, conversation: Conversation) -> Result<()>;
    async fn get(&self, id: &str) -> Result<ConversationSnapshot>;
    async fn append_messages(
        &self,
        id: &str,
        expected_revision: u64,
        messages: Vec<Message>,
    ) -> Result<u64>;
    async fn branch(&self, request: BranchRequest) -> Result<Conversation>;
    async fn archive(&self, id: &str) -> Result<()>;
    async fn delete(&self, id: &str, policy: DeletePolicy) -> Result<()>;
}
```

Optimistic concurrency를 기본으로 한다. 동일 conversation에 두 turn이 동시에 들어올 때 정책을 명시한다.

```text
reject
queue
cancel_previous
allow_branch
```

## 126. Turn lifecycle

```text
CREATED
→ CONTEXT_BUILDING
→ MODEL_RUNNING
→ TOOL_WAITING / APPROVAL_WAITING
→ MODEL_RUNNING
→ FINALIZING
→ COMPLETED | FAILED | CANCELLED
```

Turn은 retrieval, tool, model response를 여러 번 포함할 수 있다.

## 127. Message edit, regenerate, branch

### Edit

User message edit는 원본 message를 overwrite하지 않고 새 revision을 생성한다.

### Regenerate

Assistant regenerate는 기존 assistant message를 `superseded`로 표시하고 같은 parent user message에서 새 branch를 만든다.

### Branch

```python
class BranchRequest(BaseModel):
    conversation_id: str
    from_message_id: str
    new_conversation_id: str | None = None
    include_attachments: bool = True
    include_memory: bool = False
```

Branch lineage를 보존해야 평가와 audit가 가능하다.

## 128. Chat input model

```python
class ChatTurnInput(BaseModel):
    conversation_id: str
    message: Message
    attachments: list[FileAttachment] = Field(default_factory=list)
    auth: AuthContext
    locale: str | None = None
    client_capabilities: ClientCapabilities | None = None
```

Client capability 예:

```text
incremental_text
structured_events
tool_status
citation_preview
artifact_download
```

## 129. Context assembly

Conversation context는 다음 후보에서 만든다.

```text
system/developer instruction
recent messages
conversation summary
long-term memory
current message
message/conversation attachment
retrieved document
active tool result
task state
```

Context policy는 token budget, priority, freshness, trust, privacy를 함께 고려한다.

## 130. History compaction

```text
truncate_oldest
summary_memory
semantic_memory
provider_compaction
hybrid
```

Compaction은 다음을 기록한다.

```python
class CompactionRecord(BaseModel):
    compaction_id: str
    source_message_ids: list[str]
    output_message_id: str
    method: str
    model: str | None = None
    token_before: int
    token_after: int
```

Summary가 source message를 삭제하는 것은 아니다. Retention policy가 별도로 삭제할 수 있다.

## 131. Attachment processing in chat

Attachment 처리 정책:

```yaml
attachments:
  directInput:
    maxFiles: 10
    maxTotalBytes: 50000000
  temporaryIndex:
    enabled: true
    ttl: 24h
  permanentPromotion:
    requiresApproval: true
```

한 attachment가 여러 turn에서 재사용될 때 parse/index 결과를 cache할 수 있다.

## 132. Incremental chat events

Transport-independent event:

```text
turn.started
context.ready
retrieval.started
retrieval.completed
model.response.started
assistant.text.delta
assistant.tool_call.started
assistant.tool_call.arguments_delta
assistant.tool_call.completed
tool.started
tool.completed
assistant.message.completed
turn.completed
```

UI-specific event format은 router adapter가 변환한다.

## 133. Finalization

Incremental delta가 끝났다고 conversation에 즉시 append하지 않는다.

```text
provider finish
→ final ModelResponse validate
→ Answer/citation assemble
→ policy/guardrail
→ ConversationStore append CAS
→ turn.completed
```

Conversation append가 실패하면 client에게 이미 보낸 output과 store state가 다를 수 있다. Runtime은 reconciliation 상태와 retry policy를 제공해야 한다.

## 134. Chatbot standard blocks

```text
conversation.load
conversation.append
conversation.branch
conversation.compact
conversation.feedback

attachment.resolve
attachment.index_temp

context.build
context.compact

model.chat
answer.build
answer.validate_grounding

router.chat_http
router.chat_sse
router.chat_websocket
```

## 135. Feedback

```python
class Feedback(BaseModel):
    feedback_id: str
    target_id: str
    target_kind: Literal["message", "turn", "answer", "citation", "tool_call"]
    value: Literal["positive", "negative"] | float | str
    reason: str | None = None
    created_at: datetime
```

Feedback는 evaluation dataset 후보로 전환할 수 있다.

## 136. Memory write policy

Memory extraction은 별도 graph로 실행할 수 있다.

```text
turn completed
→ candidate memory extraction
→ privacy/policy filter
→ dedupe/conflict resolution
→ optional user confirmation
→ memory write
```

Memory에 다음을 저장하지 않는 기본 policy를 권장한다.

- secret
- raw credential
- highly sensitive health/financial detail
- temporary instruction
- retrieved document content 전체

## 137. Agent model

Agent는 voice extension 아래가 아니라 일반 conversation profile의 first-class 기능이다.

```python
class AgentSpec(BaseModel):
    model_pool: str
    tools: list[str]
    state_schema: JsonSchemaRef | None = None
    max_steps: int = 12
    exit_conditions: list[str] = ["final_message"]
    tool_failure: Literal["return_to_model", "fail", "fallback"] = "return_to_model"
    parallel_tool_calls: bool = True
    budget_policy_ref: str | None = None
    completion_reserve_ref: str | None = None
```

단일 model connection은 shorthand일 뿐이다. Production agent는 capability, cost, sensitivity, residency에 따라 선택 가능한 `ModelPool`을 사용할 수 있다.

## 138. Agent loop

```text
admission and budget reservation
→ assemble messages/state
→ model
→ final response? finalize
→ tool calls?
→ validate tool calls
→ policy/approval
→ execute tools
→ account usage and release reservation
→ append tool results
→ repeat until exit/max steps/budget boundary
```

Agent loop 자체는 `agent.run` composite block으로 제공하며 내부 step를 trace할 수 있어야 한다. Remaining free budget가 completion reserve 이하이면 새 tool/subtask를 시작하지 않고 finalization path로 전환한다.

## 139. Tool resolution

```text
Tool.from_block
Tool.from_graph
Tool.from_remote
Tool.from_mcp
Tool.from_openapi
```

Tool schema는 static descriptor로 resolve하고, runtime에 임의 Python callable을 삽입하는 방식은 production GraphSpec에서 금지한다.

## 140. Tool permission

```yaml
agent:
  tools:
    allow:
      - knowledge.search
      - ticket.read
      - ticket.create
    deny:
      - shell.*
  approval:
    requiredFor:
      - external_write
      - destructive
      - process
```

Tool permission은 model이 아니라 application policy가 결정한다. Budget이 남아 있어도 permission이 없는 tool은 실행할 수 없다.

## 141. Approval

```python
class ApprovalRequest(BaseModel):
    approval_id: str
    run_id: str
    subject: ResourceSnapshotRef
    action: str
    arguments_digest: str
    risk: str
    summary: str
    expires_at: datetime | None = None
```

Approval 상태:

```text
requested
approved
denied
expired
cancelled
invalidated
```

승인 후 arguments 또는 subject digest 변경을 허용하지 않는다. 변경되면 새 approval을 요청해야 한다. 내용 검토는 `ReviewRecord`를 사용한다.
Approval request builders MUST validate tool-call arguments as mapping records before computing
`arguments_digest`; scalar, sequence, or non-iterable argument inputs MUST fail at the approval
boundary and MUST NOT produce an approval request.
Approval request and record metadata MUST be mapping records with non-empty string keys before
metadata is captured in provenance, audit, policy, or UI approval events.
Tool approval APIs MUST validate typed resolved-tool, tool-call, and approval-request records before
binding or checking approvals, so malformed approval inputs fail at the approval boundary.

## 142. Tool execution

Tool은 다음을 가져야 한다.

- validated input
- execution timeout
- budget reservation
- idempotency key if needed
- audit record
- output size limit
- redaction policy
- egress policy
- sandbox policy
- cancellation capability
- rollback/compensation capability

Runtime tool admission MUST validate typed `ToolCall`, `ResolvedTool`, schema registry, and
`PolicyDecision` records before comparing digests, evaluating policy outcomes, or admitting effects.

Tool result가 너무 크면 ArtifactRef로 저장하고 summary/reference를 message에 넣는다.

## 143. Parallel tool calls

Parallel tool call은 독립성이 명시된 경우만 병렬 실행한다.

```python
class ToolDependency(BaseModel):
    tool_call_id: str
    depends_on: list[str] = Field(default_factory=list)
    budget_reservation_id: str | None = None
```

같은 resource를 write하는 tool은 keyed mutex 또는 transaction policy가 필요할 수 있다. 각 parallel call은 parent budget에서 atomic reservation을 가져야 한다.

`toolExecution.maximumParallelism` MUST be a positive integer, `toolExecution.parallelToolCalls`
MUST be a boolean, and `toolExecution.effectSerialization.keyTemplate` MUST be a non-empty
string when supplied. The compiler MUST report malformed tool execution settings instead of
silently treating them as disabled defaults.

## 144. Tool error semantics

```text
validation_error
permission_denied
approval_denied
budget_denied
timeout
transient_provider_error
provider_quota_exceeded
permanent_error
partial_success
```

`return_to_model`일 때도 error detail 전체를 model에 노출하지 않는다. 안전한 user/tool-facing error projection을 사용한다.

## 145. Agent state

Agent state는 message list와 별도다.

```python
class AgentState(BaseModel):
    revision: int
    values: dict[str, JsonValue]
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    pending_approvals: list[str] = Field(default_factory=list)
    pending_reviews: list[str] = Field(default_factory=list)
    budget_id: str | None = None
    active_task_plan_id: str | None = None
```

State schema와 reducer를 선언하고 tool이 임의 key를 생성하지 못하게 한다.

## 146. ModelPool과 WorkerProfile

```python
class ModelProfile(BaseModel):
    profile_id: str
    connection: str
    capabilities: set[str]
    quality_tier: str
    cost_class: str
    latency_class: str
    allowed_sensitivity: set[str]
    regions: set[str]
    supports_cancellation: bool
    supports_usage_report: bool
```

```python
class ModelPool(BaseModel):
    pool_id: str
    models: list[ModelProfile]
    selection_policy_ref: str
```

```python
class WorkerProfile(BaseModel):
    profile_id: str
    required_capabilities: set[str]
    allowed_tools: set[str]
    model_pool_ref: str | None = None
    sensitivity_ceiling: str | None = None
    default_budget_ref: str | None = None
```

Model/worker selection은 prompt가 아니라 policy와 physical plan의 제약을 함께 적용한다.

## 147. 정적 GraphSpec과 runtime TaskPlan

Model이 normalized Graph IR을 직접 생성하거나 수정해서는 안 된다.

```text
Static GraphSpec
- release, policy, resource, outer lifecycle, allowed executor

Runtime TaskPlan
- bounded task dependency, worker requirement, context access, output schema, budget
```

```python
class TaskPlan(BaseModel):
    plan_id: str
    revision: int
    objective: str
    tasks: list[TaskSpec]
    final_task_ids: list[str]
    limits: PlanLimits
    budget_id: str
    policy_ref: str
    created_by: ProcessorRef
```

```python
class TaskSpec(BaseModel):
    task_id: str
    role: str
    instruction: str
    depends_on: list[str] = Field(default_factory=list)
    context_from: list[str] = Field(default_factory=list)
    output_schema: SchemaRef
    required_capabilities: list[str] = Field(default_factory=list)
    worker_profile_ref: str | None = None
    priority: Literal["required", "high", "normal", "optional"] = "normal"
    budget: TaskBudgetEnvelope
    retry_policy_ref: str | None = None
    verification_policy_ref: str | None = None
    sensitivity: str | None = None
```

```python
class TaskPlanPatch(BaseModel):
    plan_id: str
    expected_revision: int
    add_tasks: list[TaskSpec] = Field(default_factory=list)
    cancel_tasks: list[str] = Field(default_factory=list)
    replace_tasks: list[TaskSpec] = Field(default_factory=list)
    reason: str
```

## 148. TaskPlan validation

Executor는 최소 다음을 검증한다.

```text
acyclic dependency
maximum tasks and depth
bounded recursion
allowed task/worker/output schema
explicit context access
parent budget and completion reserve
provider/tool eligibility
sensitivity and residency
required verification path
plan revision CAS
```

TaskPlan은 GraphSpec 대체물이 아니다. `orchestration.execute_task_plan`이라는 predeclared executor가 typed task를 실행한다.

## 149. Task context access

Task는 모든 이전 result를 자동으로 보지 않는다.

```text
context_from
- explicit task output IDs
- shared source/evidence collection
- approved summary
- immutable input snapshot
```

이 규칙은 context pollution, data leakage, 비용 폭증을 막고 provenance를 보존한다. Task result가 큰 경우 ArtifactRef와 typed summary를 사용한다.

## 150. TaskPlan execution과 patch

```text
plan validated
→ ready task 계산
→ budget reserve
→ worker/model select
→ execute and checkpoint
→ result/gate/accounting
→ release unused reservation
→ optional plan patch
```

Running task와 plan patch가 경쟁할 때 `expected_revision` CAS를 사용한다. 이미 시작된 task를 취소할 때 exhaustion/cancellation policy를 적용한다.

## 151. Candidate, trial, verification pattern

Research, code 수정, structured transformation은 다음 일반 구조를 사용할 수 있다.

```text
input ResourceSnapshot
→ candidate ChangeSet(s)
→ isolated Trial(s)
→ CheckResult + MetricObservation
→ GateResult
→ candidate selection
→ ReviewRecord
→ commit/publish effect
→ ResultBundle
```

Core는 domain-specific candidate를 정의하지 않는다. Trial executor와 typed result contract만 제공한다.

## 152. Human-in-the-loop

HITL 유형:

```text
approve effect
provide missing input
select candidate
review generated artifact
resolve ambiguity
increase budget or entitlement
resume paused run
```

Interrupt/resume는 checkpointed conversation 또는 job profile에서 지원한다.

```python
class Interrupt(BaseModel):
    interrupt_id: str
    kind: str
    payload: JsonValue
    resume_schema: JsonSchemaRef
    expires_at: datetime | None = None
    policy_decision_ref: str | None = None
```

Budget top-up 또는 override로 resume할 때 entitlement snapshot과 policy를 다시 평가한다.


## 153. MCP integration

MCP는 tool/resource/prompt discovery bridge다.

```text
MCP server connection
→ discover capabilities
→ policy filter
→ canonical ToolDefinition/ResourceDescriptor
→ invoke through adapter
```

규칙:

- 발견된 tool을 자동 allow하지 않는다.
- server identity와 tool schema hash를 lockfile/trace에 기록한다.
- remote content를 untrusted로 취급한다.
- destructive capability에 approval을 적용한다.

## 154. Agent observability

각 step에 다음을 기록한다.

```text
agent step index
model response ID
tool call IDs
selected tool
approval latency
tool latency
state revision
exit condition
step token/cost
```

Internal chain-of-thought 전체를 요구하거나 저장하지 않는다. 운영에 필요한 action/state summary만 기록한다.

## 155. Chat graph 예

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: company-assistant
  version: 1.0.0

spec:
  profile: conversation

  inputs:
    turn:
      type: graphblocks.ai/ChatTurnInput@1

  outputs:
    answer:
      type: graphblocks.ai/Answer@1
    events:
      type: graphblocks.ai/ConversationEvent@1
      mode: incremental

  connections:
    conversations: postgres-conversations
    knowledge: qdrant-company
    model: openai-assistant

  nodes:
    load:
      block: conversation.load@1
      connection: conversations

    attachments:
      block: attachment.resolve@1

    retrieve:
      block: rag.answer@1
      connection: knowledge

    agent:
      block: agent.run@1
      connection: model
      config:
        maxSteps: 10
        tools:
          - knowledge.search
          - ticket.create

    finalize:
      block: answer.build@1

    append:
      block: conversation.append@1
      connection: conversations
      flow:
        retry: optimistic-cas

  edges:
    - from: $input.turn.conversation_id
      to: load.conversation_id
    - from: $input.turn.attachments
      to: attachments.attachments
    - from: load.snapshot
      to: agent.conversation
    - from: $input.turn.message
      to: agent.message
    - from: attachments.context
      to: agent.attachments
    - from: agent.events
      to: $output.events
    - from: agent.response
      to: finalize.response
    - from: finalize.answer
      to: append.answer
    - from: load.snapshot.revision
      to: append.expected_revision
    - from: append.answer
      to: $output.answer
```

## 156. API router semantics

### HTTP request/response

Final answer만 필요할 때 사용한다.

### SSE

Finite turn incremental events를 전송한다. Reconnect cursor와 completed event를 지원할 수 있다.

### WebSocket chat

여러 turn, client event, cancellation을 하나의 connection에서 처리할 수 있지만 conversation identity를 socket identity에 묶지 않는다.

### OpenAI-compatible surface

Compatibility router는 external API shape를 canonical Message/GenerationChunk로 변환한다. Provider-specific field를 core schema에 강제로 추가하지 않는다.

## 157. Conversation retention

```yaml
retention:
  messages: 365d
  attachments:
    messageScope: 7d
    conversationScope: 30d
  partialDeltas: 0d
  toolArtifacts: 30d
  feedback: 730d
```

Delete는 conversation, attachment, temporary index, memory, telemetry link에 전파되어야 한다.

## 158. Conversation evaluation

```text
multi-turn consistency
instruction adherence
memory precision/recall
context carryover
citation correctness
tool selection correctness
tool argument validity
unnecessary tool rate
approval policy compliance
conversation branch correctness
regeneration determinism envelope
```

Dataset case는 history와 attachment를 포함할 수 있어야 한다.

## 159. Agent safety limits

```yaml
limits:
  maxSteps: 12
  maxToolCalls: 20
  maxWallTime: 120s
  maxInputTokens: 100000
  maxOutputTokens: 12000
  maxCostUsd: 2.0
  maxArtifactBytes: 50000000
```

Limit 초과는 canonical finish reason과 terminal state로 처리한다.

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

# Part VII. Packaging, Plugin Discovery, Distribution

## 193. Packaging goals

GraphBlocks packaging은 다음 목표를 만족해야 한다.

1. `pip install graphblocks`가 모든 provider, cloud SDK, parser, DB client를 설치하지 않는다.
2. Graph authoring/validation은 native runtime 없이도 가능하다.
3. 실행 runtime은 provider integration과 독립적으로 업그레이드할 수 있다.
4. 하나의 integration 설치/삭제가 core package 파일을 덮어쓰거나 제거하지 않는다.
5. plugin 탐색은 heavy SDK import 없이 가능하다.
6. missing dependency 오류가 필요한 distribution 이름과 설치 명령을 알려 준다.
7. Python과 standalone Rust deployment가 같은 GraphSpec/plan을 실행한다.
8. official/community integration을 독립 release할 수 있다.
9. package compatibility가 lockfile과 TCK로 검증 가능하다.
10. `graphblocks-all` 같은 비대한 공식 bundle을 제공하지 않는다.

## 194. 배포물 계층

```text
Layer 0: Schema and authoring
Layer 1: Native runtime
Layer 2: Provider-neutral domain packs
Layer 3: Tooling and surfaces
Layer 4: Provider/framework integrations
Layer 5: Optional runtime extensions
```


### Package 분리 기준

패키지는 namespace 수가 아니라 dependency와 운영 경계로 나눈다. 다음 중 하나 이상이면 별도 distribution을 SHOULD 사용한다.

- 무거운 provider/cloud/DB/parser dependency를 추가한다.
- native wheel 또는 system binary가 필요하다.
- core와 다른 release cadence 또는 보안 대응 주기를 가진다.
- runtime process 격리 또는 별도 credential boundary가 필요하다.
- 선택적 product profile 또는 transport를 제공한다.
- 독립 maintainer/support tier가 필요하다.

다음 이유만으로는 패키지를 분리하지 않는다.

- block namespace가 다르다.
- 동일 SDK로 여러 SPI를 구현한다.
- 파일 수가 많다.
- 문서상 chapter가 다르다.

예를 들어 하나의 `graphblocks-postgres` integration은 RecordStore, StateStore, ConversationStore, CoordinationBackend를 함께 제공할 수 있다. `graphblocks-pgvector`는 vector-specific dependency와 capability가 독립적일 때만 별도로 둔다.

### Dependency 방향 원칙

```text
core ← domain contracts ← provider integrations
  ↑          ↑
  └─ tooling/runtime/extension은 필요한 방향으로만 의존
```

- domain package는 `graphblocks-core`에 의존하고 `graphblocks-runtime`에는 의존하지 않는다.
- `graphblocks-policy`는 core schema에만 의존하며 external PDP adapter를 기본 dependency로 포함하지 않는다.
- `graphblocks-budget`은 `graphblocks-usage`와 분리하며, distributed ledger backend는 integration package로 제공한다.
- provider package는 core와 필요한 domain contract에만 의존한다.
- server/worker/runtime package가 provider integration을 역으로 dependency에 포함하지 않는다.
- application package가 최종 provider 조합과 version range를 소유한다.
- dependency cycle은 build 및 release gate에서 실패한다.

## 195. Base distributions

### `graphblocks-core`

**역할:** 가장 작은 순수 Python authoring/validation package.

제공:

- import package `graphblocks`
- canonical AI types
- GraphSpec/ApplicationSpec/BindingSpec/Release/Deployment schema
- BlockDescriptor SDK
- compiler frontend와 static validation
- plugin manifest reader
- generated type stubs

금지 dependency:

- PyO3 native runtime
- web server/UI framework
- provider/cloud/DB SDK
- Langfuse SDK
- PDF/OCR parser
- Kubernetes/Terraform SDK

### `graphblocks-runtime`

**역할:** Native Rust runtime Python binding.

제공:

- `graphblocks_runtime`
- native extension `graphblocks_runtime._native`
- scheduler, cancellation, bounded sequence, flow control
- Python block adapter와 worker protocol client

특정 provider, DB/cloud connector, parser, web server, voice/media package에 의존하지 않는다.

### `graphblocks-stdlib`

Provider/domain에 독립적인 lightweight block만 포함한다.

```text
value.*
schema.*
control.*
sequence.*
text.*
json.*
prompt.const/file/compose/render
memory/local test connector
```

다음은 stdlib에 넣지 않는다.

```text
document.*
query/retrieve/rank/context/answer.*
conversation.*
agent/tool.*
provider/cloud/db/parser integration
```

### `graphblocks` standard metapackage

`pip install graphblocks`는 GraphBlocks의 주력인 문서/RAG/대화 graph를 provider-neutral하게 작성하고 local 실행할 수 있어야 한다.

```text
graphblocks-core
graphblocks-runtime
graphblocks-stdlib
graphblocks-documents
graphblocks-rag
graphblocks-conversation
graphblocks-policy
graphblocks-budget
graphblocks-usage
graphblocks-cli
```

이 package들은 pure Python 또는 GraphBlocks native runtime wheel만 포함하고, 특정 LLM SDK, vector DB client, cloud SDK, PDF/OCR engine, server framework를 기본 dependency로 가져오지 않는다. `graphblocks-budget`와 `graphblocks-usage`의 기본 설치는 in-memory/SQLite 개발 구현과 SPI만 제공하며 production distributed backend는 별도 integration으로 설치한다.

가장 작은 설치는 metapackage가 아니라 필요한 distribution을 직접 선택한다.

```bash
pip install graphblocks-core
pip install graphblocks-core graphblocks-runtime graphblocks-stdlib
```

## 196. Domain feature distributions

| Distribution | 기능 | 기본 metapackage |
|---|---|---|
| `graphblocks-documents` | document profile, lineage, manifest, orchestration | 포함 |
| `graphblocks-rag` | Retriever, fusion/rerank, context, answer/citation | 포함 |
| `graphblocks-conversation` | conversation/turn transaction, compaction | 포함 |
| `graphblocks-agents` | tool loop, approval, agent state | 선택 |
| `graphblocks-evaluation` | generic check/metric/gate/trial/result bundle | 선택 |
| `graphblocks-policy` | policy composition, typed obligation, default evaluator | 포함 |
| `graphblocks-orchestration` | TaskPlan/TaskPlanPatch, model/worker pool | 선택 |
| `graphblocks-review` | review workflow와 credential verifier SPI | 선택 |

Domain package는 provider SDK나 parser engine을 포함하지 않는다. Canonical foundational schema는 core가 소유하고 profile-specific block/config는 domain package가 소유한다.

## 197. Application and tooling distributions

| Distribution | 책임 |
|---|---|
| `graphblocks-cli` | validate, plan, run, lock, doctor, release/deploy 명령 |
| `graphblocks-server` | HTTP/SSE/WebSocket, auth hooks, health endpoints |
| `graphblocks-client` | local/remote client와 app command/event protocol |
| `graphblocks-tui` | Textual 기반 reference TUI; client에만 의존 |
| `graphblocks-workspace` | snapshot/fork/ChangeSet/check/review/CAS commit과 file/git/test/process tool |
| `graphblocks-worker` | isolated Python worker process/pool |
| `graphblocks-testing` | deterministic runtime, test DSL, TCK clients |
| `graphblocks-devtools` | graph visualization, migration, profiling, codegen |

`graphblocks-tui`가 parser, vector DB, provider SDK, native runtime을 직접 의존해서는 안 된다.

## 198. Deployment and operations distributions

| Distribution | 책임 |
|---|---|
| `graphblocks-deployment` | GraphRelease, GraphDeployment, DeploymentRevision, physical planner |
| `graphblocks-oci` | release bundle push/pull, digest, signature/provenance helpers |
| `graphblocks-kubernetes` | Kubernetes/Helm renderer, cluster capability inspection |
| `graphblocks-terraform` | infrastructure requirement와 module input/output bridge |
| `graphblocks-gitops` | Argo CD/Flux-compatible release manifest adapter |
| `graphblocks-operator` | 별도 controller image/Helm chart; standard pip install에 미포함 |
| `graphblocks-telemetry` | canonical observation/capture/redaction policy |
| `graphblocks-otel` | OTLP exporter와 Collector templates |
| `graphblocks-prometheus` | metric exporter, dashboards/rules |
| `graphblocks-langfuse` | telemetry/prompt/eval/dataset adapters |
| `graphblocks-audit` | durable audit sink SPI/implementations |
| `graphblocks-usage` | durable actual usage ledger, provider reconciliation, immutable usage facts |
| `graphblocks-budget` | budget/quota allocation, atomic reservation/settlement, entitlement adapter |
| `graphblocks-policy-opa` | OPA/Rego policy decision adapter |
| `graphblocks-policy-cedar` | Cedar authorization decision adapter |
| `graphblocks-dashboards` | generated dashboards, alerts, runbooks |

Kubernetes, Terraform, Langfuse, Prometheus, OPA, Cedar SDK는 base runtime dependency가 아니다.

## 199. Provider integration distributions

Naming convention:

```text
graphblocks-<technology>
```

Import package는 충돌을 피하기 위해 고유 top-level 이름을 사용한다.

```text
Distribution: graphblocks-openai
Import:       graphblocks_openai

Distribution: graphblocks-qdrant
Import:       graphblocks_qdrant
```

**중요:** integration distribution은 `graphblocks/` 디렉터리에 파일을 추가하지 않는다. `graphblocks-core`만 public `graphblocks` import package를 소유한다.

### Model providers

```text
graphblocks-openai
graphblocks-anthropic
graphblocks-google-genai
graphblocks-azure-openai
graphblocks-bedrock
graphblocks-huggingface
graphblocks-ollama
graphblocks-vllm
```

### Document converters

```text
graphblocks-pypdf
graphblocks-docling
graphblocks-markitdown
graphblocks-tika
graphblocks-unstructured
graphblocks-hwp
```

### Knowledge and storage

```text
graphblocks-qdrant
graphblocks-pgvector
graphblocks-opensearch
graphblocks-elasticsearch
graphblocks-pinecone
graphblocks-weaviate
graphblocks-milvus

graphblocks-s3
graphblocks-gcs
graphblocks-azure-blob

graphblocks-firestore
graphblocks-mongodb
graphblocks-postgres
graphblocks-redis
```

### Observability and framework

```text
graphblocks-langfuse
graphblocks-haystack
graphblocks-langgraph
graphblocks-langchain
graphblocks-llamaindex
graphblocks-mcp
```

## 200. Extension distributions

```text
graphblocks-voice
graphblocks-webrtc
graphblocks-websocket-media
graphblocks-openai-realtime
graphblocks-silero-vad

graphblocks-durable
graphblocks-kafka
graphblocks-nats
graphblocks-sqs
graphblocks-pubsub
```

Voice나 durable stream package는 default `graphblocks` dependency가 아니다.

## 201. Dependency graph

```text
Application package
  ├─ graphblocks (meta)
  │    ├─ graphblocks-core
  │    ├─ graphblocks-runtime
  │    └─ graphblocks-stdlib
  ├─ selected domain packages ───────────────→ graphblocks-core
  ├─ selected provider integrations ─────────→ core + required domain contract
  ├─ selected tooling ───────────────────────→ core; runtime only when needed
  └─ selected extensions ────────────────────→ core/runtime/domain as declared
```

규칙:

- provider integration은 `graphblocks-core`와 필요한 domain contract에만 의존한다.
- integration이 `graphblocks` metapackage에 의존해서 불필요한 runtime/stdlib을 끌어오지 않도록 한다.
- runtime은 integration package에 의존하지 않는다.
- circular dependency를 금지한다.
- framework bridge는 해당 외부 framework와 core에 의존하되 다른 bridge에 의존하지 않는다.

## 202. 설치 프로파일

### Authoring/validation only

```bash
pip install graphblocks-core
```

용도:

- CI schema validation
- editor/IDE
- graph migration
- package manifest inspection

### Provider-neutral local runtime

```bash
pip install graphblocks
```

### Document ingestion

```bash
pip install \
  graphblocks \
  graphblocks-documents \
  graphblocks-pypdf \
  graphblocks-s3 \
  graphblocks-qdrant \
  graphblocks-openai
```

### RAG chatbot server

```bash
pip install \
  graphblocks \
  graphblocks-rag \
  graphblocks-conversation \
  graphblocks-server \
  graphblocks-openai \
  graphblocks-qdrant \
  graphblocks-postgres \
  graphblocks-langfuse
```

### Haystack interoperability

```bash
pip install graphblocks graphblocks-haystack
```

### Voice extension

```bash
pip install \
  graphblocks \
  graphblocks-conversation \
  graphblocks-voice \
  graphblocks-webrtc \
  graphblocks-openai-realtime
```

### Application dependency groups

Application repository는 development/test/documentation 도구에 standardized dependency groups를 사용할 수 있다.

```toml
[dependency-groups]
test = ["graphblocks-testing~=1.0", "pytest>=8"]
dev = ["graphblocks-cli~=1.0", "graphblocks-devtools~=1.0"]
docs = ["mkdocs-material"]
```

Dependency group은 배포 runtime dependency를 대신하지 않는다. Production image에는 application의 main dependencies와 선택한 runtime/provider package만 설치한다.

### Profile template은 distribution이 아니다

`rag-chat`, `document-ingestion`, `voice` 같은 profile은 project template 또는 generated dependency set으로 제공한다. 이를 `graphblocks-all`, `graphblocks-rag-all` 같은 장기 유지 bundle distribution으로 만들지 않는다.

```bash
graphblocks init --profile rag-chat
# pyproject.toml, graphblocks.lock template, sample connections 생성
```

## 203. Extras policy

Python extras는 소수의 convenience feature에만 사용한다.

```toml
[project.optional-dependencies]
cli = ["graphblocks-cli~=1.0"]
server = ["graphblocks-server~=1.0"]
testing = ["graphblocks-testing~=1.0"]
dev = ["graphblocks-cli~=1.0", "graphblocks-testing~=1.0", "graphblocks-devtools~=1.0"]
```

다음은 extras로 제공하지 않는다.

- 모든 model provider 목록
- 모든 DB/cloud connector
- 모든 parser
- voice와 durable stack 전체
- `all`

이유는 dependency resolution, security surface, wheel 크기, provider version 충돌을 통제하기 위해서다.

## 204. Namespace policy

공식 정책:

- `graphblocks-core`만 `graphblocks` import namespace를 소유한다.
- 다른 distribution은 `graphblocks_<integration>` 이름을 사용한다.
- PEP 420 namespace package로 여러 wheel이 같은 `graphblocks/` tree를 나눠 갖는 방식을 공식 기본으로 사용하지 않는다.
- 사용자는 integration module을 직접 import할 필요 없이 plugin registry를 통해 사용할 수 있다.

이 정책은 wheel uninstall 시 shared files가 손상되는 문제와 package ownership 불명확성을 줄인다.

## 205. Plugin discovery

Python package metadata entry point를 사용한다.

```toml
[project.entry-points."graphblocks.plugins"]
openai = "graphblocks_openai.plugin:load_plugin"
```

세부 group을 선택적으로 둘 수 있다.

```text
graphblocks.plugins
graphblocks.blocks
graphblocks.connectors
graphblocks.telemetry
graphblocks.prompt_registries
graphblocks.evaluators
graphblocks.framework_bridges
```

Registry는 heavy plugin module을 eager import하지 않는다.

## 206. Static plugin manifest

각 integration wheel은 static manifest를 포함해야 한다.

```json
{
  "manifest_version": 1,
  "plugin_id": "io.graphblocks.openai",
  "distribution": "graphblocks-openai",
  "plugin_version": "1.0.0",
  "maturity": "official",
  "requires_core": ">=1.0,<2.0",
  "requires_runtime_protocol": ">=1,<2",
  "plugin_api": ">=1,<2",
  "provides": [
    "model.provider:openai",
    "embedding.provider:openai"
  ],
  "blocks": [
    "model.chat@1",
    "embedding.text@1"
  ],
  "connections": ["model", "embedding"],
  "entry_point": "graphblocks_openai.plugin:load_plugin",
  "licenses": ["Apache-2.0"],
}
```

Manifest는 wheel의 dist-info에 `graphblocks-plugin.json` 이름으로 포함한다. Entry point metadata는 manifest locator와 lazy factory를 가리킨다. CLI가 manifest를 읽는 것만으로 provider SDK를 import해서는 안 된다.

Registry cache는 설치 distribution의 name/version, manifest hash, environment fingerprint로 무효화한다. Cache가 없거나 손상되어도 manifest 재탐색만 수행하고 integration SDK를 eager import하지 않는다.

## 207. Lazy loading

```text
scan installed distributions
→ read static manifests
→ build registry index
→ resolve graph requirements
→ import only selected plugin factory
→ instantiate only selected connection/block
```

Import 규칙:

- import 시 network connection을 열지 않는다.
- import 시 credential을 resolve하지 않는다.
- import 시 global event loop/task를 생성하지 않는다.
- optional SDK 누락 오류는 plugin load 단계에서 명확히 발생한다.

## 208. Plugin descriptor

```python
class PluginDescriptor(BaseModel):
    plugin_id: str
    version: str
    blocks: list[BlockDescriptor]
    connector_factories: list[ConnectorFactoryDescriptor]
    adapters: list[TypeAdapterDescriptor]
    capabilities: set[str]
    maturity: str
```

Plugin factory는 descriptor와 lazy factory를 반환한다.

## 209. Block registration conflict

동일 semantic block은 여러 implementation을 가질 수 있다.

```text
block: model.chat@1
implementations:
- openai
- anthropic
- google_genai
- local_openai_compatible
```

Conflict resolution:

1. GraphSpec `implementation`
2. connection provider
3. application binding
4. 유일한 implementation일 때만 자동 선택

동일 plugin ID/version 충돌이나 동일 implementation ID 중복은 startup error다.

## 210. Plugin trust policy

```yaml
plugins:
  allow:
    - io.graphblocks.*
    - com.company.*
  deny:
    - io.unknown.experimental
  maturity:
    minimum: official
  signatures:
    required: false
```

Production에서는 allowlist를 권장한다. 미신뢰 Python/native plugin은 in-process로 실행하지 않고 worker/remote 격리를 사용한다.

## 211. Package manifest validation

Official integration은 다음을 가져야 한다.

- static plugin manifest
- pyproject metadata
- README와 minimal usage example
- supported core/runtime range
- block/connector TCK 결과
- unit/integration tests
- security contact
- changelog
- license
- dependency upper/lower bound policy
- deprecation metadata, 해당 시

## 212. Compatibility dimensions

독립 version:

```text
GraphSpec API version
canonical schema version
block type version
runtime protocol version
plugin API version
Python distribution version
Rust crate version
provider adapter version
```

모든 것을 하나의 package SemVer로 암묵적으로 추론하지 않는다.

## 213. Foundation release train

다음 package만 coordinated minor release train을 따른다.

```text
graphblocks-core
graphblocks-runtime
graphblocks-stdlib
graphblocks-documents
graphblocks-rag
graphblocks-conversation
graphblocks-policy
graphblocks-budget
graphblocks-usage
graphblocks-testing
```

규칙:

- foundation package의 major.minor는 동일하게 유지한다.
- patch는 독립 배포할 수 있다.
- `graphblocks` metapackage는 검증된 foundation patch set과 선택한 CLI version을 pin한다.
- core/runtime protocol mismatch는 import 또는 runtime initialization에서 즉시 실패한다.

다음 first-party extension은 독립 SemVer를 사용하고 `requires_core`, `requires_runtime_protocol`, `plugin_api`, `schema_api` 범위로 호환성을 선언한다.

```text
graphblocks-agents
graphblocks-evaluation
graphblocks-orchestration
graphblocks-review
graphblocks-workspace
graphblocks-client
graphblocks-tui
graphblocks-cli
graphblocks-server
graphblocks-worker
graphblocks-deployment
graphblocks-telemetry
graphblocks-devtools
```

이 분리는 wheel을 작게 만드는 것뿐 아니라 optional feature 하나 때문에 foundation 전체를 다시 배포하는 일을 방지한다.

## 214. Integration release policy

Provider integration은 독립 SemVer를 사용한다.

예:

```toml
[project]
name = "graphblocks-qdrant"
version = "0.4.2"
dependencies = [
  "graphblocks-core>=1.0,<2.0",
  "graphblocks-rag>=1.0,<2.0",
  "qdrant-client>=1,<2"
]
```

Integration package version이 core version과 같을 필요는 없다.

## 215. Runtime protocol check

Python binding initialization:

```text
core expected runtime protocol
vs
native extension provided protocol
```

Mismatch error 예:

```text
RuntimeProtocolMismatch:
  graphblocks-core 1.0.2 requires protocol 1.x
  graphblocks-runtime 2.0.0 provides protocol 2.x
  install a compatible runtime: pip install "graphblocks-runtime>=1.0,<2.0"
```

## 216. Graph lockfile

```bash
graphblocks lock graph.yaml --output graphblocks.lock
```

Lockfile 내용:

```yaml
lockVersion: 1
graph:
  id: company-assistant
  graphHash: sha256:...
  schemaVersion: graphblocks.ai/v1alpha3

runtime:
  protocol: 1
  distribution: graphblocks-runtime
  version: 1.0.0

packages:
  - name: graphblocks-core
    version: 1.0.0
    hash: sha256:...
  - name: graphblocks-openai
    version: 0.3.1
    hash: sha256:...

plugins:
  - id: io.graphblocks.openai
    version: 0.3.1
    descriptorHash: sha256:...

blocks:
  model.chat@1:
    implementation: openai
    descriptorHash: sha256:...

prompts:
  - ref: company/rag-answer@12
    contentHash: sha256:...
```

Lockfile은 secret, access token, raw prompt variable을 포함하지 않는다.


### Environment lock과의 구분

`graphblocks.lock`은 Python dependency resolver의 environment lock을 대체하지 않는다.

| Lock | 책임 |
|---|---|
| `pylock.toml`, `uv.lock`, 또는 동등한 environment lock | Python wheel/sdist와 transitive dependency pin |
| `Cargo.lock` | standalone Rust build dependency pin |
| `graphblocks.lock` | graph/plan, block descriptor, plugin, prompt, schema, runtime protocol의 의미적 pin |
| container digest/SBOM | 배포 image와 system package pin |

Production reproducibility는 위 계층을 함께 사용한다. `graphblocks lock verify`는 environment에 설치된 distribution이 semantic lock과 일치하는지 검사하지만 package resolver 역할을 수행하지 않는다.

## 217. Lock modes

```text
strict
- exact package/plugin/descriptor hashes required

compatible
- declared version range 내 resolve 허용

unlocked
- development only
```

Production deploy는 strict 또는 approved compatible mode를 사용해야 한다.

## 218. Python wheel strategy

### Core

`graphblocks-core`는 pure Python universal wheel이다.

### Runtime

`graphblocks-runtime`은 Maturin/PyO3로 platform wheel을 배포한다.

지원 target 예:

```text
manylinux x86_64/aarch64
musllinux x86_64/aarch64
macOS x86_64/arm64
Windows x86_64/arm64, supported when toolchain permits
```

### Unsupported platform behavior

Native wheel을 제공하지 않는 platform에서도 `graphblocks-core`는 설치 및 validation이 가능해야 한다. 실행 시에는 다음 중 하나를 명시적으로 선택한다.

```text
build graphblocks-runtime from source
use RemoteRuntime/graphblocksd
use InProcessTestRuntime for tests only
```

Native extension import 실패를 silent pure-Python production runtime으로 자동 fallback하지 않는다.

### Stable ABI

CPython `abi3` 사용은 required PyO3 API와 성능 요구를 만족할 때 선택한다. 초기에는 Python minor별 wheel을 허용한다. 내부 runtime crate가 PyO3에 의존하지 않기 때문에 binding 전략을 바꿔도 core를 재설계할 필요가 없어야 한다.

## 219. Mixed Rust/Python project layout

```text
packages/graphblocks-runtime/
  Cargo.toml
  pyproject.toml
  python/
    graphblocks_runtime/
      __init__.py
      _typing.pyi
  src/
    lib.rs
```

Native module은 private 이름을 사용한다.

```toml
[tool.maturin]
python-source = "python"
module-name = "graphblocks_runtime._native"
```

Public Python API는 `graphblocks_runtime` wrapper를 통해 제공한다.

## 220. Rust crate packaging

Cargo workspace는 공통 lockfile과 build output을 공유한다. Publishable crate와 internal crate를 구분한다.

```toml
[workspace]
resolver = "3"
members = ["crates/*"]
default-members = [
  "crates/graphblocks-schema",
  "crates/graphblocks-runtime-core",
  "crates/graphblocks-python"
]
```

Internal crate에는 `publish = false`를 사용한다. Public Rust embedding API가 안정화되기 전에는 최소 crate만 crates.io에 공개한다.

## 221. Cargo feature policy

Cargo feature는 다음에 사용할 수 있다.

- platform allocator
- TLS backend
- optional telemetry exporter
- debug diagnostics
- compile-time performance option

다음에는 사용하지 않는다.

- 모든 model provider catalog
- 모든 document parser
- 모든 database connector
- user-facing plugin registry

Provider integration을 feature로 묶으면 Cargo feature unification 때문에 실제 dependency closure와 binary size가 불투명해질 수 있다.

## 222. Repository strategy

### Core monorepo

```text
graphblocks/
  crates/
  packages/
    graphblocks-core/
    graphblocks-runtime/
    graphblocks-stdlib/
    graphblocks-documents/
    graphblocks-rag/
    graphblocks-conversation/
    graphblocks-agents/
    graphblocks-evaluation/
    graphblocks-cli/
    graphblocks-server/
    graphblocks-worker/
    graphblocks-testing/
    graphblocks-devtools/
  specs/
  tck/
  examples/
```

### Official integrations monorepo

```text
graphblocks-integrations/
  integrations/
    openai/
    qdrant/
    s3/
    firestore/
    langfuse/
    haystack/
    ...
```

각 integration 디렉터리는 독립 `pyproject.toml`, tests, README, changelog를 가진다.

### Community integrations

외부 repository에서 독립 배포할 수 있다. Official registry 등록 전에 manifest validation과 TCK를 통과해야 한다.

## 223. Package naming rules

- PyPI distribution: lowercase kebab case, `graphblocks-<name>`
- Python import: lowercase snake case, `graphblocks_<name>`
- plugin ID: reverse DNS 또는 globally unique namespace
- semantic block ID: provider-neutral dotted name
- connection provider ID: short stable identifier

예:

```text
PyPI: graphblocks-google-genai
Import: graphblocks_google_genai
Plugin: io.graphblocks.google_genai
Provider: google_genai
```

## 224. Dependency policy

### Core direct dependency budget

`graphblocks-core`는 최소 dependency를 유지한다. 새로운 direct dependency는 다음을 검토한다.

- import time
- wheel size
- transitive dependency count
- license
- security history
- Python support range
- optionality

### No import-time side effects

모든 package는 import 시 다음을 금지한다.

- network call
- credential read
- background thread/task
- filesystem scan beyond package metadata
- telemetry exporter start
- logging global configuration overwrite

### Optional system dependencies

Tika server, LibreOffice, OCR engine, ffmpeg 같은 system dependency는 integration README와 capability doctor에서 명시한다. Core install 과정에서 자동 설치하지 않는다.

## 225. Dependency error ergonomics

```python
try:
    import qdrant_client
except ImportError as exc:
    raise MissingOptionalDependency(
        distribution="graphblocks-qdrant",
        dependency="qdrant-client",
        install="pip install graphblocks-qdrant",
    ) from exc
```

Generic `ModuleNotFoundError`를 그대로 사용자에게 노출하지 않는다.

## 226. Package size and startup targets

Normative requirement는 dependency boundary이며, 다음은 release target이다.

- `graphblocks-core` compressed wheel은 작고 pure Python이어야 한다.
- `graphblocks-runtime` wheel은 provider SDK와 parser asset을 포함하지 않는다.
- plugin registry scan은 integration SDK import 없이 완료되어야 한다.
- `import graphblocks`는 network/connector 초기화를 하지 않는다.
- 사용하지 않는 integration은 process memory에 load되지 않아야 한다.

Release CI는 wheel size와 import time regression을 기록한다.

## 227. CLI package commands

```bash
graphblocks packages list
graphblocks plugins list
graphblocks plugins inspect io.graphblocks.openai
graphblocks plugins validate dist/*.whl
graphblocks doctor graph.yaml
graphblocks lock graph.yaml
graphblocks lock verify graphblocks.lock
graphblocks env export --format requirements
graphblocks env sbom --format cyclonedx
```

## 228. Missing package diagnosis

`graphblocks doctor`는 다음을 검사한다.

- GraphSpec schema
- required plugin 설치
- core/runtime protocol
- connection capability
- system binary/service requirement
- credentials reference 존재 여부, 값은 출력하지 않음
- model/provider configuration
- package conflict
- deprecated integration

## 229. Integration TCK gate

Official integration release 전 필수:

```text
manifest TCK
block descriptor TCK
canonical serialization TCK
error mapping TCK
cancellation/timeout TCK
telemetry propagation TCK
connector-specific TCK
secret redaction TCK
```

Provider live tests는 nightly/credentialed job으로 분리하고 PR 기본 테스트는 deterministic mock을 사용한다.

## 230. Test extras

Optional dependency test는 설치되지 않은 환경에서 skip 또는 marker로 분리한다.

```text
unit
integration_mock
integration_live
contract
tck
benchmark
```

Core test suite가 모든 provider SDK 설치를 요구해서는 안 된다.

## 231. Release artifacts

각 release는 가능한 경우 다음을 생성한다.

- sdist
- wheel
- changelog
- SBOM(SPDX 또는 CycloneDX)
- checksums
- provenance/attestation
- TCK report
- supported platform matrix

Trusted publishing과 package signing은 release maturity에 따라 적용한다.

## 232. Deprecation

Plugin manifest:

```json
{
  "status": "deprecated",
  "deprecated_since": "0.9.0",
  "removal_after": "1.2.0",
  "replacement": "graphblocks-google-genai"
}
```

CLI와 compiler는 deprecated block/package 사용을 경고한다. Security issue가 있으면 normal deprecation window 없이 block할 수 있다.

## 233. Version pinning guidance

Application production lock:

```text
- core/runtime minor pin
- integration compatible range 또는 exact pin
- provider SDK transitive lock
- graphblocks.lock descriptor hash
- container/image digest
```

Library author는 지나치게 exact pin하지 않고 compatibility range를 선언한다.

## 234. Distribution support tier

| Tier | 소유자 | TCK | Release SLA | Registry 표시 |
|---|---|---|---|---|
| built-in | core team | mandatory | coordinated | built-in |
| official | core/integration team | mandatory | maintained | official |
| partner | named partner | mandatory | declared | partner |
| community | community | recommended | best effort | community |
| experimental | any | partial | none | experimental |

## 235. No mega package rule

공식 `graphblocks-all` distribution은 만들지 않는다.

이유:

- cloud SDK와 DB client 충돌
- 매우 큰 wheel/environment
- 보안 취약점 surface 증가
- platform-specific parser 설치 실패
- 사용하지 않는 native dependency load
- release cadence 결합

문서와 examples는 목적별 explicit install set을 제공한다. 조직 내부에서 curated constraints/bundle을 만들 수 있지만 core release artifact와 분리한다.

## 236. Application package 예

```toml
[project]
name = "company-knowledge-assistant"
version = "1.0.0"
dependencies = [
  "graphblocks>=1.0,<2.0",
  "graphblocks-rag>=1.0,<2.0",
  "graphblocks-conversation>=1.0,<2.0",
  "graphblocks-server>=1.0,<2.0",
  "graphblocks-openai>=0.3,<0.4",
  "graphblocks-qdrant>=0.4,<0.5",
  "graphblocks-postgres>=0.2,<0.3",
  "graphblocks-langfuse>=0.3,<0.4",
]

[dependency-groups]
test = [
  "graphblocks-testing>=1.0,<2.0",
  "pytest>=8",
]
docs = ["mkdocs-material"]
```

Application package가 실제 provider 조합을 소유한다.

## 237. Container image strategy

공식 image는 최소 계층으로 나눈다.

```text
graphblocks/runtime:<version>
- graphblocksd only

graphblocks/python-runtime:<version>
- Python + core/runtime/stdlib

graphblocks/dev:<version>
- CLI/testing/devtools
```

Provider별 모든 integration을 넣은 universal image를 기본 제공하지 않는다. Application image가 필요한 integration만 설치한다.

## 238. Standalone Rust distribution

```text
graphblocksd
- run compiled plans
- load remote/Python worker plugins
- expose HTTP/gRPC control plane
- no embedded provider SDK by default
```

Native Rust provider plugin은 정적 링크 또는 versioned process protocol을 우선한다. Rust dynamic library ABI를 public stable plugin contract로 간주하지 않는다.

## 239. Remote plugin protocol

언어/프로세스 격리가 필요한 integration은 remote protocol을 구현한다.

```text
DescribePlugin
DescribeBlock
InitializeConnection
Invoke
InvokeIncremental
Cancel
Health
Close
```

Protocol은 schema ID/version과 runtime protocol을 handshake한다.

## 240. Packaging acceptance criteria

1. `pip install graphblocks-core`는 provider SDK와 native wheel 없이 성공한다.
2. `pip install graphblocks`는 model/cloud/DB/parser SDK를 설치하지 않는다.
3. `graphblocks plugins list`는 provider SDK를 import하지 않는다.
4. integration uninstall이 `graphblocks` import package 파일을 삭제하지 않는다.
5. core/runtime protocol mismatch가 startup 전에 감지된다.
6. missing integration 오류에 distribution과 install command가 포함된다.
7. provider package는 독립적으로 release하고 TCK를 실행할 수 있다.
8. lockfile로 descriptor/package hash를 검증할 수 있다.
9. application은 필요한 provider만 explicit dependency로 선언할 수 있다.
10. voice/durable packages가 기본 설치에 포함되지 않는다.
11. wheel/platform matrix가 자동 CI에서 검증된다.
12. 모든 official wheel에 manifest와 license가 포함된다.
13. `graphblocks-stdlib`은 domain/provider package를 암묵적으로 설치하지 않는다.
14. environment lock과 `graphblocks.lock`의 불일치를 배포 전 검출한다.
15. unsupported native platform에서도 core validation과 RemoteRuntime 안내가 동작한다.

# Part VIII. Immutable Release, Placement, Deployment, and Infrastructure

## 241. 운영 plane

```text
Management Plane
- compile, lock, release, sign, GitOps, Terraform/Kubernetes reconciliation

Control Plane
- admission, scheduling, worker registry, leases, ownership, cancellation, checkpoint orchestration

Data Plane
- Rust runtime service, Python/Rust worker pools, provider/connectors, parser/OCR/sandbox

Observation Plane
- telemetry, audit, usage, evaluation, SLO, release analysis
```

초기 구현이 한 process여도 책임과 protocol은 분리해야 한다.

## 242. Release object hierarchy

```text
GraphSpec + ApplicationSpec + Binding template + package/environment locks
        ↓
GraphRelease / ReleaseBundle (immutable)
        ↓
GraphDeployment (desired state)
        ↓
DeploymentRevision (resolved immutable revision)
        ↓
PhysicalExecutionPlan
        ↓
RuntimeInstance / WorkerPool / Kubernetes workload
```

## 243. GraphRelease와 ReleaseBundle

`GraphRelease`는 production에 배포할 불변 artifact 집합이다. `.gbr` archive 또는 OCI artifact로 저장할 수 있다.

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: GraphRelease
metadata:
  name: enterprise-rag
  version: 2026.06.22.1

spec:
  bundle:
    digest: sha256:...
    mediaType: application/vnd.graphblocks.release.v1

  application:
    hash: sha256:...

  graphs:
    chat:
      graphHash: sha256:...
      normalizedPlanHash: sha256:...
    ingest:
      graphHash: sha256:...
      normalizedPlanHash: sha256:...

  locks:
    semantic: graphblocks.lock
    python: pylock.toml
    rust: Cargo.lock
    prompts: prompts.lock
    policies: policies.lock

  images:
    control: registry.example.com/gb/control@sha256:...
    docCpu: registry.example.com/gb/doc-cpu@sha256:...
    ocrGpu: registry.example.com/gb/ocr-gpu@sha256:...

  knowledge:
    indexRevision: intranet_docs_v17
    embeddingProfile: company-embedding-v4

  schemas:
    checkpoint: company.ai/Checkpoint@4
    conversation: company.ai/Conversation@3
    manifest: company.ai/IngestionManifest@2

  supplyChain:
    sbomRef: oci://registry/.../sbom@sha256:...
    provenanceRef: oci://registry/.../provenance@sha256:...
    signaturePolicy: production-publishers
```

Production release는 `latest`, Git branch, mutable prompt label, mutable image tag, unpinned index revision을 포함해서는 안 된다.

## 244. GraphDeployment

GraphDeployment는 environment의 desired state다.

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: GraphDeployment
metadata:
  name: enterprise-rag-production

spec:
  releaseRef:
    digest: sha256:...

  profile: production
  bindingRef: bindings/company-ai-production.yaml
  observabilityProfileRef: observability/rag-production.yaml

  coordinator:
    target: control

  targets: {}
  executionGroups: {}
  placements: []
  rollout: {}
  upgrades: {}
  recovery: {}
```

GraphDeployment에는 secret 값이 아니라 reference만 포함한다.

## 245. DeploymentRevision과 run pinning

Deployment controller/compiler는 GraphDeployment와 binding/cluster capability를 resolve해 불변 revision을 만든다.

```python
class DeploymentRevision(BaseModel):
    revision_id: str
    release_digest: str
    deployment_spec_hash: str
    physical_plan_hash: str
    resolved_binding_hash: str
    target_capability_hash: str
    created_at: datetime
```

권장 pin scope:

| workload | 기본 pin 범위 |
|---|---|
| HTTP request | run |
| chat | turn |
| sticky conversation | conversation |
| realtime voice | session |
| ingestion | job |
| map item | parent job revision 상속 |

실행 중 revision이 자동으로 바뀌면 안 된다.

## 246. PhysicalExecutionPlan

```yaml
apiVersion: graphblocks.ai/physical-plan/v1alpha1
plan:
  releaseDigest: sha256:...
  deploymentRevisionId: rev_...
  graphHash: sha256:...
  packageLockHash: sha256:...

  groups:
    chat-turn:
      target: control
      locality: same_process
      implementations:
        load_context: rust_builtin
        rewrite: python_inproc
        generate: python_inproc

    document-transform:
      target: doc-cpu
      locality: same_worker_per_invocation

    gpu-ocr:
      target: ocr-gpu
      locality: any_worker

  remoteEdges:
    - from: document-transform.convert
      to: gpu-ocr.ocr
      schema: graphblocks.ai/ArtifactRef@1
      transport: gb-worker-rpc
      delivery: at_least_once
```

Plan hash를 run, trace, manifest, checkpoint에 기록한다.

## 247. ExecutionTarget

```yaml
targets:
  control:
    kind: service
    executionHost: rust
    image: registry.example.com/gb/control@sha256:...
    packageLock: locks/control.lock
    accepts:
      capabilities:
        - graph.coordinator
        - model.remote_call
        - retrieval.remote_call

  doc-cpu:
    kind: workerPool
    executionHost: python_worker
    image: registry.example.com/gb/doc-cpu@sha256:...
    packageLock: locks/doc-cpu.lock
    accepts:
      capabilities:
        - document.parse.pdf
        - document.parse.office
        - document.normalize
        - document.split

  ocr-gpu:
    kind: workerPool
    executionHost: python_worker
    image: registry.example.com/gb/ocr-gpu@sha256:...
    accepts:
      capabilities:
        - document.ocr
        - accelerator.cuda

  sandbox:
    kind: sandboxPool
    executionHost: python_worker
    accepts:
      effects:
        - process_execution
        - workspace_write
```

Target는 정확한 Pod/Node가 아니라 logical worker pool이다.

## 248. ExecutionGroup과 locality

블록마다 Pod 하나를 생성하지 않는다. Remote boundary를 줄이기 위해 group을 사용한다.

```yaml
executionGroups:
  chat-turn:
    nodes: [load_context, classify, rewrite, build_context, generate, validate, commit]
    target: control
    locality: same_process

  per-document:
    subgraph: graphs/process-single-asset.yaml
    target: doc-cpu
    locality: same_worker_per_invocation
    dispatch: per_map_item

  gpu-ocr:
    nodes: [ocr]
    target: ocr-gpu
    locality: any_worker
```

Locality:

```text
same_process
same_worker_per_invocation
same_node_preferred
same_zone_required
any_worker
external
```

## 249. Placement rule

```yaml
placements:
  - select:
      nodes: [generate, build_context]
    target: control

  - select:
      capabilities: [document.parse.*]
    target: doc-cpu

  - select:
      blocks: [document.ocr]
    target: ocr-gpu

  - select:
      effects: [process_execution, workspace_write]
    target: sandbox
```

우선순위:

```text
node ID > execution group/subgraph > block ID > capability > execution class > default
```

동일 우선순위 충돌은 compile error다. Block requirement와 deployment overlay가 모두 만족되어야 한다.

## 250. Cross-target edge

Remote edge는 다음을 정의한다.

```text
wire schema/version
inline vs artifact_ref
payload limit/compression/checksum
delivery/retry/idempotency
cancellation/trace propagation
authentication/authorization/backpressure
```

대용량 file/document는 target 간 inline 복사보다 `ArtifactRef`를 사용한다.

```yaml
remoteEdges:
  - from: convert.document
    to: ocr.document
    transport:
      mode: artifact_ref
      binding: artifacts
      compression: zstd
      checksum: sha256
      delivery: at_least_once
```

## 251. Kubernetes mapping

| Target kind | Kubernetes workload |
|---|---|
| `service` | Deployment + Service |
| `workerPool` | Deployment |
| `jobPool` | Job/Indexed Job |
| `sandboxPool` | isolated Deployment 또는 invocation Job |
| `statefulService` | StatefulSet |
| `external` | 생성하지 않음 |

Portable fields가 기본이며 Kubernetes-specific overlay는 escape hatch다.

```yaml
targets:
  ocr-gpu:
    resources:
      requests:
        cpu: "4"
        memory: 16Gi
        accelerator:
          nvidia.com/gpu: 1

    platform:
      kubernetes:
        namespace: graphblocks-workers
        serviceAccountName: graphblocks-ocr
        nodeSelector:
          workload.graphblocks.ai/class: gpu
        tolerations:
          - key: nvidia.com/gpu
            operator: Exists
            effect: NoSchedule
        topologySpread:
          topologyKey: topology.kubernetes.io/zone
          maxSkew: 1
```

Gateway API를 신규 route exposure 기본으로 사용하고 Ingress는 compatibility option으로 둔다.

## 252. Sandbox와 network boundary

```yaml
targets:
  sandbox:
    kind: sandboxPool
    security:
      trustLevel: untrusted
      filesystem: ephemeral
      rootFilesystem: read_only
      privilegeEscalation: denied
      egressPolicy: restricted
    platform:
      kubernetes:
        runtimeClassName: gvisor
        serviceAccountName: graphblocks-sandbox
```

Deployment renderer는 NetworkPolicy, service account, pod security profile, secret mount 정책을 생성하거나 요구사항으로 출력할 수 있다.

## 253. Worker lifecycle와 draining

Worker state:

```text
STARTING → WARMING → READY ↔ SATURATED
READY/SATURATED → DRAINING → TERMINATED
READY → DEGRADED | UNHEALTHY
```

Probe 의미:

```text
startup   package/plugin/schema/model warmup 완료
readiness 새 task를 받을 수 있고 registry/queue capacity가 유효
liveness  runtime loop/heartbeat가 살아 있고 deadlock이 없음
```

외부 provider 장애만으로 liveness를 실패시켜 Pod를 재시작하지 않는다.

Drain sequence:

```text
readiness false
→ worker registry DRAINING
→ new lease 거부
→ active task 완료 또는 checkpoint
→ incremental output 종료
→ required outbox flush
→ telemetry bounded flush
→ lease 반환
→ exit
```

```yaml
lifecycle:
  drain:
    onlineRequestTimeout: 30s
    durableTaskTimeout: 5m
    realtimeSessionTimeout: 10m
    onDeadline:
      onlineRequest: cancel
      durableTask: checkpoint
      realtimeSession: disconnect_with_resume_token
```

## 254. Autoscaling, admission, load shedding

```yaml
targets:
  control:
    scaling:
      kind: request
      minReplicas: 3
      maxReplicas: 20
      targetConcurrencyPerReplica: 32

  doc-cpu:
    scaling:
      kind: queue
      minReplicas: 0
      maxReplicas: 40
      targetQueueDepthPerReplica: 4

admission:
  maxConcurrentRuns: 500
  maxQueueWait: 2s
  overload:
    strategy: reject
    retryAfter: 2s
```

Scaling signal은 workload별로 다르다.

```text
online: concurrency, queue wait, TTFT
batch: queue depth, oldest item age, throughput
GPU: active model slots, memory, queue age
realtime: active sessions; scale-to-zero 금지 가능
```

## 255. Workload-aware rollout

공통 전략:

```text
validate → shadow → canary/blue-green → promote 또는 abort
```

```yaml
rollout:
  strategy: canary
  affinity: conversation_id
  steps:
    - traffic: 1
      minimumSamples: 200
    - traffic: 10
      minimumDuration: 30m
    - traffic: 50
      minimumDuration: 1h
  analysisProfile: rag-production-rollout
```

Workload별 규칙:

- Chat: 한 turn 중 revision 변경 금지; conversation sticky policy 명시.
- Ingestion: fixture regression → production sample shadow → staging index dual-write → alias publish.
- Effectful agent: shadow에서 effect suppress/sandbox; 비가역 effect는 자동 rollback 대상이 아니다.
- Realtime session: 기존 session drain, 신규 session만 새 revision.

RAG release는 graph, prompt, embedding profile, index revision을 하나의 cohort로 rollout한다.

## 256. Upgrade, migration, rollback

```yaml
upgrades:
  existingRequests: finish_on_old
  conversations: keep_affinity
  durableJobs: checkpoint_and_migrate
  realtimeSessions: drain_on_old
```

Compatibility matrix:

```text
runtime protocol
plan format
checkpoint schema
RunStore/ConversationStore/Manifest schema
worker package lock
canonical schema migrations
```

Rollback class:

```text
runtime/image rollback
prompt/graph rollback
index alias rollback
state migration rollback
compensation graph for effects
non-reversible effect
```

자동 rollback이 non-reversible effect를 되돌린다고 가정해서는 안 된다.

## 257. Control plane HA와 fencing

```python
class RunOwnershipLease(BaseModel):
    run_id: str
    owner_instance_id: str
    lease_epoch: int
    expires_at: datetime
    last_checkpoint: str | None = None
```

규칙:

- 한 run에는 하나의 active owner만 존재한다.
- ownership acquire는 fencing epoch를 발급한다.
- stale owner의 state/effect result write를 거부한다.
- worker result는 lease epoch와 node attempt ID를 포함한다.
- owner 장애 시 compatible checkpoint 이후부터 재개한다.

Worker advertisement:

```python
class WorkerAdvertisement(BaseModel):
    worker_id: str
    target_id: str
    protocol_versions: list[str]
    package_lock_hash: str
    image_digest: str
    capabilities: set[str]
    state: str
    heartbeat_at: datetime
```

## 258. Multi-tenancy, residency, recovery

지원 isolation profile:

```text
shared_runtime
dedicated_worker_pool
namespace_isolated
cluster_isolated
region_isolated
```

```yaml
tenancy:
  mode: dedicated_worker_pool
  policyProfileRef: tenant-standard
  quotaDefaults:
    maxConcurrentRuns: 100
    modelInputTokensPerDay: 10000000
    artifactStorage: 100Gi
  network:
    defaultEgress: deny
```

Recovery profile은 RPO/RTO, backup source, restore compatibility, failover ownership을 정의한다.

```yaml
recovery:
  service:
    rto: 15m
    rpo: 5m
  durableJobs:
    rto: 1h
    rpo: checkpoint
  knowledgeIndex:
    rebuildableFrom: [source_assets, manifests, release_bundle]
  regionalFailover:
    mode: active_passive
```

정기 restore test는 production acceptance criterion이다.

## 259. Terraform와 GitOps 경계

Terraform 책임:

```text
cluster/node pool/network/IAM
object store/database/queue/search service
workload identity/DNS/certificate
GraphBlocks operator/Helm release
```

GraphBlocks 책임:

```text
portable infrastructure requirement
module input/tfvars generation
Terraform output → BindingSpec import
release/deployment manifest
runtime scheduling/retry/cancellation
```

GraphBlocks가 임의 HCL 전체를 source of truth로 생성하지 않는다.

```bash
graphblocks infra requirements deployment.yaml \
  --format terraform-vars \
  --out graphblocks.auto.tfvars.json

graphblocks bindings import \
  --from terraform-output.json \
  --template bindings/production.template.yaml
```

Secret 값은 Terraform output이나 generated BindingSpec에 기록하지 않고 SecretRef만 연결한다.

GitOps repository에는 mutable source가 아니라 release digest와 GraphDeployment desired state를 기록한다.

## 260. Software supply chain

Production release는 다음을 지원해야 한다.

```text
image and bundle digest
SBOM
build provenance
signature verification
plugin allowlist
vulnerability/license scan
package lock verification
admission policy
```

미검증 plugin/native image는 production target에 배치하지 않는다.

## 261. Deployment status

```python
class DeploymentStatus(BaseModel):
    observed_revision: str
    desired_release: str
    stable_revision: str | None
    canary_revision: str | None
    phase: str
    conditions: list[Condition]
    target_status: dict[str, TargetStatus]
    rollout_status: RolloutStatus | None
    migration_status: MigrationStatus | None
```

Condition 예:

```text
ReleaseVerified
BindingsResolved
PackagesAvailable
WorkersCompatible
MigrationsReady
RolloutHealthy
SLOWithinBudget
RecoveryTestCurrent
```

## 262. Deployment diagnostics와 CLI

```text
GB3001 NoCompatibleTarget
GB3002 AmbiguousPlacement
GB3003 MissingPackage
GB3004 UnsupportedExecutionHost
GB3005 AcceleratorUnavailable
GB3006 NonSerializableRemoteEdge
GB3007 OversizedInlineTransfer
GB3008 NonIdempotentRemoteEffect
GB3009 DataResidencyViolation
GB3010 LocalStorageViolation
GB3011 IsolationViolation
GB3012 RealtimeScaleToZero
GB3013 CyclicLocalityConstraint
GB4001 MutableReleaseReference
GB4002 UnverifiedArtifact
GB4003 IncompatibleCheckpointSchema
GB4004 UnsafeInFlightUpgrade
GB4005 MissingDrainPolicy
GB4006 RolloutWithoutQualityGate
GB4007 NonReversibleEffectRollback
```

```bash
graphblocks release build release.yaml --out dist/company-ai.gbr
graphblocks release verify dist/company-ai.gbr
graphblocks deploy plan deployment.yaml
graphblocks placement explain deployment.yaml --node ocr
graphblocks deploy render deployment.yaml --target kubernetes
graphblocks deploy render deployment.yaml --target helm
graphblocks deploy diff deployment.yaml --cluster production
graphblocks deploy doctor deployment.yaml
graphblocks images build deployment.yaml
graphblocks packages closure deployment.yaml
```

# Part IX. Execution Records, Observability, SLO, and Operations

## 263. 기록 계층

```text
RunStore
- 현재 실행 snapshot와 pointer

ExecutionJournal
- correctness/recovery를 위한 append-only record

AuditLog
- 보안/승인/삭제/배포/권한 결정

UsageLedger
- actual token/audio/embedding/compute/storage/cost

BudgetLedger
- allocation/reservation/settlement/quota balance

ApplicationEventStream
- UI progress/draft/approval protocol

Telemetry
- OpenTelemetry trace/metric/log/profile

EvaluationStore
- dataset case, metric, release quality gate
```

Delivery/retention이 다른 기록을 하나의 `EventEnvelope`로 통합하지 않는다.

## 264. ExecutionJournal

```python
class ExecutionRecord(BaseModel):
    record_id: str
    run_id: str
    run_sequence: int
    release_id: str
    deployment_revision_id: str
    type: str
    causation_id: str | None = None
    node_id: str | None = None
    attempt_id: str | None = None
    lease_epoch: int | None = None
    payload: JsonValue | None = None
    payload_ref: ArtifactRef | None = None
    occurred_at: datetime
```

최소 durable record:

```text
run admitted/terminal
node terminal
checkpoint committed
effect prepared/committed/compensation state
ownership/lease transition
required store migration
```

Ephemeral request는 full journal을 생략할 수 있지만 effect와 required audit는 별도 정책을 따른다.

## 265. AuditLog

```python
class AuditRecord(BaseModel):
    audit_id: str
    actor: PrincipalRef
    action: str
    resource: ResourceRef
    decision: str
    policy_ref: str | None
    approval_ref: str | None
    release_id: str
    deployment_revision_id: str
    occurred_at: datetime
    integrity: AuditIntegrity
```

Audit는 sampling/drop 가능한 OTel exporter에만 기록해서는 안 된다. Required audit는 transaction/outbox 또는 동등한 durable path를 사용한다.

## 266. UsageLedger와 BudgetLedger

`UsageLedger`는 실제 사용량의 immutable source이고, `BudgetLedger`는 quota/budget의 allocation과 reservation source다.

```python
class UsageRecord(BaseModel):
    usage_id: str
    tenant_id: str | None
    principal_id: str | None
    application_id: str | None
    conversation_id: str | None
    run_id: str
    turn_id: str | None
    task_id: str | None
    trial_id: str | None
    node_id: str
    attempt_id: str
    provider: str | None
    provider_response_id: str | None
    model: str | None
    measurement: UsageMeasurement
    budget_id: str | None
    reservation_id: str | None
    idempotency_key: str
    occurred_at: datetime
```

필수 invariant:

```text
usage record는 append-only
provider response/attempt id로 deduplicate
provisional과 reconciled record 연결
retry/cancel/failure의 실제 사용량 포함
pricing revision과 currency 보존
usage를 삭제하더라도 billing/audit retention policy 준수
```

Budget ledger event:

```python
class BudgetLedgerRecord(BaseModel):
    record_id: str
    budget_id: str
    sequence: int
    type: Literal[
        "allocated", "reserved", "committed", "released", "expired",
        "adjusted", "overdrafted", "exhausted", "closed"
    ]
    reservation_id: str | None = None
    amounts: list[UsageAmount]
    owner: ResourceRef | None = None
    fencing_token: int | None = None
    policy_ref: str
    occurred_at: datetime
```

Hard quota와 병렬 reservation은 atomic compare-and-update 또는 동등한 serializable contract를 사용해야 한다. Quota, billing, chargeback은 UsageLedger/BudgetLedger를 사용한다. Langfuse cost, OTel metric, Prometheus counter를 exact source로 사용하지 않는다.

Provider invoice와 GraphBlocks estimated cost가 다를 수 있으므로 다음을 구분한다.

```text
provider_reported_usage
runtime_estimated_usage
provider_billed_cost
internal_chargeback_cost
reconciled_cost
```


## 267. OpenTelemetry architecture

```text
Rust runtime / Python workers / server adapters
        ↓ OTel SDK and context propagation
OTLP pipeline or direct exporter
        ├─ Langfuse
        ├─ Tempo/Jaeger/APM
        ├─ Prometheus-compatible metrics
        └─ internal observability platform
```

Telemetry exporter 장애가 graph correctness를 결정해서는 안 된다. Required audit/usage/effect record는 별도 durable path를 사용한다.

## 268. Trace topology

권장 root unit:

```text
chat:        Conversation = session, Turn = trace, graph invocation = root span
HTTP:        request/run = trace
ingestion:   job summary trace + per-document linked traces
agent:       user turn/top task = trace, step/tool/model = child span
queue task:  producer span link → consumer task trace
```

수십만 map item을 하나의 거대한 trace에 child span으로 넣지 않는다. Summary span과 linked item trace를 사용한다.

## 269. Span timing

Node/model/tool span은 가능하면 다음 시점을 구분한다.

```text
scheduled_at
admitted_at
started_at
first_output_at
completed_at
```

이를 통해 queue wait, semaphore wait, provider latency, execution, streaming 시간을 분리한다.

Token delta마다 span/log 하나를 만들지 않는다. Generation span 하나에 chunk count, first chunk, last chunk, usage, finish reason을 집계한다.

## 270. Semantic convention versioning

GraphBlocks canonical observation model과 OTel mapping을 분리한다.

```text
GraphBlocksObservation@1
        ↓ versioned adapter
OpenTelemetry core semantic conventions
OpenTelemetry GenAI profile revision
```

```yaml
semanticConventions:
  graphblocks: "1.0"
  opentelemetry: "1.42"
  genaiProfile: "2026-06"
```

GraphBlocks custom namespace:

```text
graphblocks.release.id
graphblocks.deployment.revision
graphblocks.graph.id
graphblocks.graph.hash
graphblocks.plan.hash
graphblocks.node.id
graphblocks.block.type
graphblocks.target.id
graphblocks.execution_group.id
graphblocks.outcome
```

OTel GenAI convention이 진화해도 GraphBlocks public schema가 특정 experimental attribute에 직접 결합되지 않게 한다.

## 271. Langfuse export projection

지원 mode:

```text
direct     GraphBlocks → Langfuse SDK/exporter
collector  GraphBlocks → OTel Collector → Langfuse
dual       OTel APM pipeline + Langfuse projection
```

Langfuse가 잘 담당하는 영역:

```text
LLM trace inspection
session/turn grouping
prompt linkage
usage/cost analytics
score/dataset/experiment
production/offline evaluation
```

담당하지 않는 영역:

```text
run recovery
checkpoint/effect journal
exact billing/quota
required audit source of truth
```

## 272. Provenance attributes

AI observation과 release analysis에는 가능한 경우 다음을 기록한다.

```text
release_id, release_channel, rollout_id/step/cohort
graph/version/hash, physical_plan_hash
binding/package/prompt/policy bundle/profile hash
block type/version, implementation/version
target/execution group/image digest
provider, requested/actual model, provider response ID
prompt ref/version/hash
parser/chunker/embedding/index revision
conversation/turn/item IDs, trace에서만
```

고유 ID는 metric label이 아니라 trace/log field로 기록한다.

## 273. Metrics와 cardinality budget

필수 metric family 예:

```text
graphblocks_run_total
graphblocks_run_duration_seconds
graphblocks_node_duration_seconds
graphblocks_queue_wait_seconds
graphblocks_flow_wait_seconds
graphblocks_model_first_output_seconds
graphblocks_retrieval_duration_seconds
graphblocks_context_tokens
graphblocks_usage_units_total
graphblocks_budget_consumed_units_total
graphblocks_budget_overdraft_total
graphblocks_policy_decisions_total
graphblocks_quota_exhaustions_total
graphblocks_worker_tasks_active
graphblocks_worker_queue_depth
graphblocks_telemetry_records_dropped_total
```

허용 label:

```text
environment
release_channel
graph_id
block_type
target_id
provider
model_family
outcome
error_class
```

금지 label:

```text
run_id
trace_id
conversation_id
turn_id
user_id
document_id
chunk_id
provider_response_id
unbounded tenant/model string
```

## 274. Sampling과 content capture 분리

```yaml
sampling:
  traces:
    normal: 0.05
    errors: 1.0
    slow: 1.0
    canary: 1.0

  content:
    normal: 0
    consentedDebugSession: 1.0

  evaluations:
    productionRandom: 0.01
    canary: 0.20
    highRiskEffect: 1.0
```

Trace 보존, prompt/document 본문 capture, evaluation 실행 비율은 서로 다른 결정이다.

## 275. Capture와 redaction

```python
class CaptureDecision(BaseModel):
    mode: Literal[
        "none", "hash_only", "reference_only", "redacted_preview", "full"
    ]
    retention_policy: str
    consent_ref: str | None = None
```

권장 production default:

```yaml
contentCapture:
  messages: redacted
  documentContent: reference_only
  toolArguments: schema_only
  toolResults: metadata
  embeddings: none
  rawFiles: none
```

Redaction은 exporter별 hook에만 의존하지 않고 canonical record/telemetry 생성 지점과 durable storage 이전에 적용한다. `fail_closed`가 필요한 data class를 명시한다.

## 276. Telemetry pipeline backpressure

```yaml
telemetry:
  queue:
    maxItems: 10000
    onFull: drop_low_priority
  shutdown:
    flushTimeout: 3s
```

Drop 가능:

```text
debug span
반복 progress
per-item low-priority trace
chunk/token debug
```

별도 durable path 필요:

```text
audit
usage ledger
effect/checkpoint terminal
durable run terminal
required evaluation result
```

Telemetry pipeline 자체를 관측한다.

```text
queue size/capacity
enqueue failure/drop count
export failure/retry/latency
flush time
redaction failure
collector health
```

## 277. SLO

Chat/RAG SLI:

```text
admission/commit success
TTFD(time to first draft)
time to committed answer
retrieval no-hit
citation resolution/validation
grounding/abstention
context truncation
tool success
cost per successful turn
budget exhaustion/retraction rate
```

Ingestion SLI:

```text
source-to-index freshness
oldest backlog age
document success/quarantine
parser fallback/OCR rate
index publish/delete/ACL propagation
```

```yaml
slos:
  - id: chat-availability
    indicator: successful_committed_turns / admitted_turns
    objective: 0.995
    window: 30d

  - id: first-draft
    indicator: p95(turn_first_draft_ms)
    objective:
      max: 1500ms

  - id: citation-validity
    indicator: validated_citations / returned_citations
    objective: 0.99
```

## 278. Rollout quality gate

Rollout analysis는 infra health뿐 아니라 semantic quality와 cost를 비교한다.

```yaml
qualityGates:
  - metric: turn_success_rate
    min: 0.995
  - metric: p95_time_to_first_draft_ms
    maxRegression: 0.15
  - metric: citation_validation_rate
    min: 0.98
  - metric: average_cost_per_successful_turn
    maxRegression: 0.10
  - metric: critical_effect_failure_rate
    max: 0
```

Quality evaluator는 production result bundle을 비동기로 평가할 수 있다. Gate가 release promotion을 결정할 경우 result는 durable EvaluationStore에 기록한다.

## 279. Deployment observability

Deployment event:

```text
deployment.started
release.verified
revision.created
rollout.step.started
rollout.gate.passed/failed
release.promoted/aborted
rollback.started/completed
worker.draining
migration.started/completed
```

모든 telemetry에는 release/revision/cohort context가 있어 stable vs canary 비교가 가능해야 한다.

## 280. Run Explorer

GraphBlocks-specific explorer는 다음을 연결해 보여 준다.

```text
Logical Graph
→ Physical Plan
→ actual timeline
```

필수 표시:

```text
node readiness/queue/flow wait/execution
remote transfer
retry/fallback/cancellation
checkpoint/effect commit
usage/cost
critical path
release/target/image provenance
```

## 281. Diagnostic bundle와 replay

Diagnostic bundle은 기본적으로 content-free 또는 redacted다.

```text
release/revision identity
normalized graph/physical plan
package/plugin/image inventory
run/node terminal summary
selected trace/log/metric excerpts
worker status
configuration hashes
redaction report
```

Replay mode:

```text
logic replay      stored inputs/references + mocked effects
provider replay   recorded provider outputs, where permitted
checkpoint resume compatible release에서 재개
full production replay는 effect/secret/privacy 정책으로 제한
```

## 282. Observability diagnostics와 CLI

```text
GB4008 AuditUsesLossyTelemetry
GB4009 BillingUsesTelemetry
GB4010 HighCardinalityMetricLabel
GB4011 UnredactedMultiExporter
GB4012 UnpinnedIndexRevision
GB4013 MissingReleaseAffinity
GB4014 MissingRpoRto
GB4015 TailSamplingTopologyUnsafe
```

```bash
graphblocks observe run run_123
graphblocks observe critical-path run_123
graphblocks observe compare --stable rev_a --canary rev_b
graphblocks observe diagnostic-bundle run_123 --redacted
graphblocks slo report deployment.yaml
graphblocks telemetry doctor observability.yaml
```

# Part X. Policy, Quota, Budget, and Resource Governance

## 283. Policy plane의 범위

GraphBlocks policy plane은 authorization만을 뜻하지 않는다. 다음 결정을 하나의 versioned contract로 다루되, 기록과 enforcement 특성은 구분한다.

```text
authorization
- 누가 어떤 application, graph, tool, artifact, corpus를 사용할 수 있는가

resource governance
- token, cost, request, concurrency, CPU/GPU, storage, licensed resource를 얼마나 사용할 수 있는가

execution safety
- 어떤 tool/effect/network/process를 어떤 isolation과 승인 아래 실행할 수 있는가

content and data governance
- 어떤 데이터를 model, connector, telemetry, memory, artifact에 보낼 수 있는가

quality governance
- 어떤 check, gate, review를 통과해야 commit 또는 publish할 수 있는가

lifecycle governance
- limit 초과, provider quota, policy 변경, shutdown 시 현재 작업을 어디까지 완료하는가
```

Policy는 model prompt에만 포함해서는 안 된다. Rust runtime, server adapter, worker, connector, effect commit path가 typed decision과 obligation을 강제해야 한다.

Mandatory policy enforcement는 일반 graph node가 아니다. `policy.evaluate` block은 decision을 application data로 사용하거나 설명 UI를 만들 때 MAY 제공할 수 있지만, 해당 block을 생략·우회해도 scheduler/provider/effect PEP가 동작해야 한다.

## 284. Policy 객체와 해석 계층

공개 객체:

```text
PolicyBundle          graphblocks.ai/v1alpha1
PolicyProfile         graphblocks.ai/v1alpha1
PolicySnapshot        graphblocks.policy/PolicySnapshot@1
PolicyDecision        graphblocks.policy/PolicyDecision@1
EntitlementSnapshot   graphblocks.policy/EntitlementSnapshot@1
```

역할:

```text
PolicyBundle
- 정적 rule, schema, obligation type, evaluator metadata
- release에 digest로 pin

PolicyProfile
- 환경/application/tenant plan의 quota, budget, exhaustion, capture 기본값
- deployment revision 또는 entitlement system에서 resolve

EntitlementSnapshot
- 특정 principal/tenant가 한 run을 시작할 때 보유한 plan, credit, override의 불변 snapshot

PolicyDecision
- 한 enforcement point의 allow/deny/defer와 typed obligation
```

```python
class PolicyRule(BaseModel):
    rule_id: str
    effect: Literal["allow", "deny", "obligate"]
    actions: list[str]
    resource_selectors: list[str]
    principal_selectors: list[str] = Field(default_factory=list)
    condition: PolicyPredicate | None = None
    obligations: list[PolicyObligation] = Field(default_factory=list)
    priority: int = 0

class PolicyBundleSpec(BaseModel):
    bundle_id: str
    version: str
    rule_language: str
    rules: list[PolicyRule] = Field(default_factory=list)
    external_evaluator: ResourceRef | None = None
    obligation_schema_versions: list[str] = Field(default_factory=list)
    default_fail_modes: dict[str, str] = Field(default_factory=dict)
    digest: str
    signature_ref: str | None = None

class PolicyProfileSpec(BaseModel):
    profile_id: str
    bundle_refs: list[str]
    scope_selectors: list[str]
    quota_accounts: dict[str, JsonValue] = Field(default_factory=dict)
    budgets: dict[str, JsonValue] = Field(default_factory=dict)
    thresholds: list[JsonValue] = Field(default_factory=list)
    exhaustion: ExhaustionPolicy | None = None
    affinity: dict[str, str] = Field(default_factory=dict)
    capture: dict[str, JsonValue] = Field(default_factory=dict)
    required_reviews: list[str] = Field(default_factory=list)
    required_gates: list[str] = Field(default_factory=list)
```

Default declarative rule language는 versioned, deterministic, side-effect-free여야 한다. Arbitrary Python, Jinja evaluation, network lookup을 rule expression으로 실행해서는 안 된다. External facts는 PIP에서 typed attribute로 제공한다.

```python
class BudgetGrant(BaseModel):
    grant_id: str
    budget_id: str
    scope: ResourceRef
    limits: list[BudgetLimit]
    valid_from: datetime
    valid_until: datetime | None = None
    source_ref: str
```

GraphBlocks는 기본 declarative evaluator를 제공할 수 있으며, OPA/Rego, Cedar 또는 조직 내부 PDP는 adapter로 연결한다. 외부 policy engine의 표현식을 GraphSpec public API로 삼지 않는다.


### Policy control-plane 역할

GraphBlocks는 policy authoring과 enforcement를 다음 역할로 분리한다.

```text
PAP — Policy Administration Point
- PolicyBundle/Profile 작성, 검증, 서명, release pin

PIP — Policy Information Point
- principal/tenant entitlement, data label, usage balance, resource state 제공

PDP — Policy Decision Point
- canonical PolicyRequest를 평가해 PolicyDecision 반환

PEP — Policy Enforcement Point
- scheduler, provider adapter, worker, connector, effect commit, publish path에서 obligation 강제
```

PDP는 graph state나 external effect를 직접 변경해서는 안 된다. PEP는 `allow`를 받았다는 이유만으로 obligation을 무시해서는 안 된다. Policy decision과 enforcement result는 서로 다른 durable record로 남긴다.

```python
class PolicyRequest(BaseModel):
    request_id: str
    enforcement_point: Literal[
        "compile", "release", "admission", "before_node",
        "before_provider_call", "on_usage_delta", "before_tool_or_effect",
        "before_commit", "before_publish", "on_resume"
    ]
    action: str
    principal: PrincipalRef | None = None
    tenant: ResourceRef | None = None
    resource: ResourceRef
    release_id: str | None = None
    deployment_revision_id: str | None = None
    run_id: str | None = None
    atomic_unit: ResourceRef | None = None
    data_labels: list[str] = Field(default_factory=list)
    requested_usage: list[UsageAmount] = Field(default_factory=list)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    policy_snapshot_id: str | None = None
    input_digest: str
    occurred_at: datetime
```

`PolicyRequest.attributes`는 schema-registered allowlist여야 한다. Prompt, 문서 본문, tool result 같은 민감한 payload는 기본적으로 포함하지 않고 digest, `SourceRef`, `ArtifactRef`, classification만 전달한다.
Builders for `before_tool_or_effect` policy requests MUST validate typed `ToolCall`, `ResolvedTool`,
`PrincipalRef`, and output-policy state mapping inputs before constructing the canonical request.

```python
class EntitlementSnapshot(BaseModel):
    snapshot_id: str
    subject: PrincipalRef
    scopes: list[ResourceRef]
    plan_id: str | None = None
    policy_profile_refs: list[str] = Field(default_factory=list)
    grants: list[str] = Field(default_factory=list)
    budget_grants: list[BudgetGrant] = Field(default_factory=list)
    overrides: list[PolicyOverride] = Field(default_factory=list)
    source_revision: str
    resolved_at: datetime
    valid_until: datetime | None = None
    digest: str

class PolicySnapshot(BaseModel):
    snapshot_id: str
    effective_policy_digest: str
    policy_bundle_refs: list[str]
    profile_ref: str
    entitlement_snapshot_ref: str | None = None
    pricing_revision: str | None = None
    quota_window_ids: list[str] = Field(default_factory=list)
    affinity: Literal["pinned", "boundary_refresh", "live"]
    issued_at: datetime
    valid_until: datetime | None = None
```

`PolicySnapshot`은 effective rule을 재현하는 불변 identity다. Secret, raw content, mutable provider object를 포함하지 않는다. Distributed worker에는 필요한 최소 decision/permit과 snapshot digest만 전달한다.

## 285. Policy attachment와 effective policy

Policy는 다음 scope에 attach할 수 있다.

```text
platform
organization
Tenant
project/workspace
application
release/graph
principal plan
conversation/session
run/turn/task/node
resource or data classification
```

권장 merge 규칙:

```text
explicit deny                → 항상 우선
hard maximum                 → 적용 가능한 값 중 최소
required obligation          → 합집합
allow list                   → 교집합
 deny list                   → 합집합
retention/capture restriction→ 더 제한적인 값
budget                       → parent allocation을 초과할 수 없음
```

하위 scope가 상위 scope를 완화하려면 별도의 `PolicyOverride` capability, 만료 시간, 승인자, 사유, audit record가 필요하다. 단순히 더 구체적인 policy라는 이유만으로 상위 deny 또는 hard limit를 덮어쓰지 않는다.

```python
class PolicyOverride(BaseModel):
    override_id: str
    scope: ResourceRef
    granted_by: PrincipalRef
    capability: str
    constraints: dict[str, JsonValue]
    reason: str
    expires_at: datetime
    max_uses: int | None = None
```

## 286. Policy decision point와 enforcement point

필수 decision/enforcement point:

```text
compile
- block/effect/policy capability 검증

release
- policy bundle, prompt, package, pricing reference pin

admission
- principal authorization, quota, concurrency, budget reservation

before_node
- node, target, data sensitivity, remaining budget 검증

before_provider_call
- model/provider eligibility, context/output cap, reservation

on_usage_delta
- streaming 또는 provider-reported usage에 따른 threshold와 exhaustion 처리

before_tool_or_effect
- permission, approval, egress, sandbox, idempotency

before_commit
- effect atomicity, review/check/gate, budget settlement

before_publish
- final data/content policy, citation/review, retention

on_resume
- entitlement, policy revision, checkpoint compatibility 재검증
```

Policy engine은 결정을 내리고, enforcement point는 결정을 실제 실행에 적용한다. Observer나 prompt guardrail만으로 enforcement를 대체할 수 없다.

## 287. PolicyDecision과 typed obligation

```python
class PolicyDecision(BaseModel):
    decision_id: str
    effect: Literal["allow", "deny", "allow_with_obligations", "defer"]
    reason_codes: list[str]
    policy_refs: list[str]
    obligations: list[PolicyObligation] = Field(default_factory=list)
    advice: list[PolicyAdvice] = Field(default_factory=list)
    evaluated_at: datetime
    valid_until: datetime | None = None
    input_digest: str
```

표준 obligation:

```text
require_approval
require_review
force_sandbox
restrict_egress
redact_fields
set_capture_mode
cap_model_input
cap_model_output
force_model_class
reserve_budget
reserve_completion_budget
reduce_parallelism
require_checkpoint
require_audit
set_retention
preserve_release_affinity
```

Policy adapter가 임의 code/config mutation을 반환하게 하지 않는다. Compiler와 runtime이 이해하는 versioned obligation만 허용한다.

### Policy, entitlement, usage, budget SPI

```rust
#[async_trait]
pub trait PolicyEvaluator: Send + Sync {
    async fn evaluate(
        &self,
        request: PolicyRequest,
        ctx: &PolicyContext,
    ) -> Result<PolicyDecision, PolicyError>;

    fn capabilities(&self) -> PolicyCapabilities;
}

#[async_trait]
pub trait EntitlementProvider: Send + Sync {
    async fn resolve(
        &self,
        subject: PrincipalRef,
        scope: ResourceRef,
        at: DateTime<Utc>,
    ) -> Result<EntitlementSnapshot, EntitlementError>;
}

#[async_trait]
pub trait UsageLedger: Send + Sync {
    async fn append(&self, record: UsageRecord) -> Result<LedgerOffset, UsageError>;
    async fn reconcile(&self, record: UsageReconciliation) -> Result<LedgerOffset, UsageError>;
}

#[async_trait]
pub trait BudgetLedger: Send + Sync {
    async fn allocate(&self, request: BudgetAllocationRequest) -> Result<BudgetAccount, BudgetError>;
    async fn reserve(&self, request: BudgetReservationRequest) -> Result<BudgetReservation, BudgetError>;
    async fn commit(&self, request: BudgetCommitRequest) -> Result<BudgetSettlement, BudgetError>;
    async fn release(&self, request: BudgetReleaseRequest) -> Result<BudgetSettlement, BudgetError>;
    async fn balance(&self, budget: BudgetRef) -> Result<BudgetBalance, BudgetError>;
}
```

필수 성질:

- `PolicyDecision.input_digest`는 평가 input의 canonical encoding에 기반한다.
- hard quota의 `reserve/commit/release`는 atomic하고 fencing-aware해야 한다.
- ledger write는 retry-safe idempotency key를 가진다.
- entitlement snapshot은 run/turn 경계와 policy affinity에 따라 pin 또는 refresh한다.
- external PDP가 unavailable일 때 각 decision class의 fail mode를 적용한다.
- telemetry exporter는 이 SPI들의 source of truth가 될 수 없다.

## 288. Fail mode와 policy availability

```text
fail_closed
- authorization, secret, ACL, destructive effect, hard quota, residency

fail_open_with_audit
- 선택적 최적화, non-critical telemetry enrichment

use_cached_decision
- policy가 cache-safe라고 선언하고 TTL/input key가 일치할 때만

defer
- 사람, 상위 PDP, entitlement refresh가 필요
```

Policy evaluator 장애를 model/provider 장애와 동일한 retry로 처리하지 않는다. `policy_unavailable`, `policy_denied`, `entitlement_stale`를 구분한다.

## 289. Limit, quota, budget, rate, capacity 구분

```text
system limit
- 구현 또는 provider의 절대 한계. override 불가

quota
- scope와 window에 할당된 누적 사용량

budget
- 특정 run/turn/task/project에 계획적으로 할당한 다중 단위 envelope

rate limit
- 시간당 요청 또는 사용량 속도

concurrency limit
- 동시 실행/lease 수

capacity
- queue, worker, GPU, storage의 현재 수용 가능량

safety limit
- agent step, loop iteration, task depth, tool call 수
```

이들을 `maxTokens` 하나로 합치지 않는다. 하나의 실행은 여러 hard/soft limit를 동시에 가진다.

Policy는 **허용 범위와 effective limit**를 결정하고, FlowRuntime은 semaphore/rate-limit/queue/backpressure 같은 **집행 mechanism**을 제공한다. Graph config와 policy가 모두 limit를 정의하면 더 제한적인 값을 적용한다. Process-local counter나 Prometheus metric을 distributed hard quota의 source로 사용해서는 안 된다.

## 290. Usage unit taxonomy

```python
class UsageAmount(BaseModel):
    kind: Literal[
        "model_input_tokens", "model_cached_input_tokens",
        "model_output_tokens", "model_reasoning_tokens",
        "embedding_input_tokens", "image_input_units", "image_output_units",
        "audio_input_ms", "audio_output_ms",
        "provider_requests", "tool_invocations", "web_searches",
        "cpu_seconds", "gpu_seconds", "memory_byte_seconds",
        "licensed_resource_seconds", "wall_time_ms",
        "artifact_bytes", "storage_byte_seconds", "egress_bytes",
        "product_credits", "currency"
    ]
    quantity: Decimal
    unit: str
    dimensions: dict[str, str] = Field(default_factory=dict)
```

Token은 provider/model/tokenizer에 따라 의미가 다르므로 ledger는 model과 tokenizer/pricing revision을 보존한다. Monetary cost와 token quota는 별도 unit이며, 둘 중 하나만 초과해도 hard policy가 동작할 수 있다.

```python
class BudgetLimit(BaseModel):
    limit_id: str
    usage_selector: str
    amount: Decimal
    unit: str
    mode: Literal["soft", "hard"]
    window: QuotaWindow
    dimensions: dict[str, str] = Field(default_factory=dict)
    warning_thresholds: list[Decimal] = Field(default_factory=list)
```

서로 다른 tokenizer/model의 raw token을 무조건 하나의 숫자로 합산해서는 안 된다. 정책은 model family/tokenizer dimension별 raw token limit를 두거나, versioned conversion rule을 가진 별도 `product_credits`/`currency` unit으로 변환해야 한다.

`usage_selector`는 raw `UsageAmount.kind` 또는 release에 pin된 derived selector를 가리킨다. 표준 derived selector 예시는 다음과 같다.

```text
model_total_tokens
= model_input_tokens + model_cached_input_tokens
  + model_output_tokens + model_reasoning_tokens

model_generated_tokens
= model_output_tokens + model_reasoning_tokens

model_billable_cost
= pinned UsageRateCard가 계산한 currency 또는 product_credits
```

Derived selector의 포함 항목과 coefficient는 policy/rate-card revision에 포함되어야 하며, 이름만 같다는 이유로 provider별 billing 의미를 추정해서는 안 된다. Authoring shorthand의 `kind: model_total_tokens`는 normalized IR에서 `usageSelector: graphblocks.usage/model_total_tokens@1`로 확장한다.

```python
class UsagePricingRule(BaseModel):
    match: dict[str, str]
    source_kind: str
    source_unit: str
    target_kind: Literal["product_credits", "currency"]
    target_unit: str
    multiplier: Decimal
    minimum_charge: Decimal | None = None

class UsageRateCard(BaseModel):
    rate_card_id: str
    revision: str
    valid_from: datetime
    valid_until: datetime | None = None
    rules: list[UsagePricingRule]
    currency: str | None = None
```

Rate card는 raw usage를 product credit 또는 monetary cost로 변환하는 versioned 함수다. Production release는 pricing/rate-card revision을 pin해야 하며 과거 UsageRecord를 최신 가격으로 소급 변경하지 않는다.

```python
class BudgetBalance(BaseModel):
    budget_id: str
    allocated: list[UsageAmount]
    reserved: list[UsageAmount]
    committed: list[UsageAmount]
    available: list[UsageAmount]
    overdraft: list[UsageAmount]
    revision: int
    observed_at: datetime
```

## 291. Usage measurement와 reconciliation

```python
class UsageMeasurement(BaseModel):
    source: Literal[
        "provider_reported", "runtime_measured", "tokenizer_estimated",
        "pricing_estimated", "reconciled"
    ]
    confidence: Literal["exact", "provider_exact", "estimated", "unknown"]
    amounts: list[UsageAmount]
    pricing_ref: str | None = None
    provider_response_id: str | None = None
```

규칙:

- Provider가 최종 usage를 늦게 반환하면 provisional record를 쓴 뒤 reconciled record로 정산한다.
- 실패, timeout, cancel된 provider call도 실제 소비가 보고되면 usage에 포함한다.
- Retry attempt마다 실제 사용량을 별도로 기록한다.
- Provider request ID와 attempt ID를 이용해 중복 기록을 제거한다.
- Quota enforcement는 가능한 경우 strong ledger를 사용하고, eventual telemetry counter에 의존하지 않는다.
- 예상치보다 실제 사용량이 큰 경우 overdraft 또는 policy violation을 명시적으로 기록한다.

### Usage aggregation과 roll-up

Raw UsageRecord는 다음 key를 잃지 않는다.

```text
organization / tenant / project / principal
application / release / graph
conversation / run / turn / task / trial / node / attempt
provider / model / tokenizer / tool / target
usage kind / pricing revision / quota window
```

집계 규칙:

- 동일 token 숫자라도 model/tokenizer dimension이 다른 원시 단위를 무조건 동일 비용으로 간주하지 않는다.
- Product credit와 monetary cost는 pinned `UsageRateCard`를 통해 파생한다.
- Dashboard roll-up은 eventual consistency여도 되지만 hard enforcement balance는 BudgetLedger의 atomic state를 사용한다.
- Late provider usage는 원래 quota window와 실행 scope에 귀속하고 reconciliation 시점과 구분한다.
- Tenant/user별 조회 dimension은 ledger query에 사용하되 Prometheus label로 사용하지 않는다.
- Retention 때문에 상세 record를 compact할 때도 audit/billing에 필요한 signed aggregate와 reconciliation lineage를 보존한다.

## 292. UsageLedger와 BudgetLedger 분리

```text
UsageLedger
- 실제로 발생했거나 provider가 보고한 immutable usage

BudgetLedger
- allocation, reservation, commitment, release, overdraft, balance
```

```python
class BudgetAccount(BaseModel):
    budget_id: str
    parent_budget_id: str | None
    scope: ResourceRef
    limits: list[BudgetLimit]
    status: Literal["active", "exhausted", "paused", "closed"]
    policy_ref: str
```

```python
class BudgetReservation(BaseModel):
    reservation_id: str
    budget_id: str
    owner: ResourceRef
    amounts: list[UsageAmount]
    purpose: Literal[
        "provider_call", "task", "trial", "tool", "finalization", "cleanup"
    ]
    expires_at: datetime
    fencing_token: int
    status: Literal["reserved", "committed", "released", "expired"]
```

병렬 task는 다음 protocol을 사용한다.

```text
estimate
→ atomic reserve
→ execute
→ commit actual usage
→ release unused reservation
→ reconcile delayed provider usage
```

Budget reservation이 없는 병렬 worker가 각각 전체 잔액을 보고 실행해서는 안 된다.


### BudgetPermit과 distributed enforcement

Reservation은 ledger의 재무적/정책적 hold이고, `BudgetPermit`은 특정 worker attempt가 실행할 수 있는 bounded 권한이다.

```python
class BudgetPermit(BaseModel):
    permit_id: str
    reservation_refs: list[str]
    owner: ResourceRef
    atomic_unit: ResourceRef
    admission_epoch: int
    authorized_amounts: list[UsageAmount]
    low_watermark: list[UsageAmount] = Field(default_factory=list)
    continuation_profile: str
    policy_snapshot_digest: str
    expires_at: datetime
    fencing_tokens: dict[str, int]
```

Distributed 실행 protocol:

```text
reserve applicable budget chain atomically
→ issue bounded permit
→ worker executes only within permit
→ worker emits measured/provisional usage deltas
→ renew or extend before low watermark
→ commit actual usage and release remainder
→ reconcile delayed provider report
```

Network partition 시 worker는 이미 발급된 permit 범위만 사용할 수 있다. Hard quota에서 permit을 자동으로 무한 연장하거나 stale balance를 기준으로 새 provider call을 시작해서는 안 된다. Permit보다 늦게 보고되는 unavoidable provider usage는 overdraft로 정산하고 incident/audit policy를 적용한다.

## 293. Budget scope와 window

Quota/budget scope:

```text
platform/organization/tenant/project
application/release/graph
principal/team
conversation/session
run/turn/task/trial/node
provider/model/tool/resource pool
```

Window:

```text
per_invocation
per_turn
per_run
fixed_window
rolling_window
calendar_day/week/month
lifetime_credit
subscription_period
```

```python
class QuotaWindow(BaseModel):
    kind: Literal[
        "per_invocation", "per_turn", "per_run", "fixed", "rolling",
        "calendar", "lifetime", "subscription"
    ]
    duration: timedelta | None = None
    timezone: str | None = None
    reset_at: datetime | None = None
```

Run admission 시 선택된 entitlement와 window ID를 snapshot으로 보존한다. 실행 도중 plan이 변경되어도 이미 시작된 atomic unit의 semantics가 임의로 바뀌지 않아야 한다.

### Hierarchical budget와 multi-account reservation

한 작업에는 tenant, principal, application, conversation, run, provider budget가 동시에 적용될 수 있다. 모든 applicable hard account에 대한 reservation이 성공해야 work를 admission한다.

```text
resolve applicable accounts in deterministic order
→ validate child allocation ≤ parent available allocation
→ atomic multi-account reserve or fail all
→ issue one attempt-scoped BudgetPermit
→ settle every account from the same usage fact
```

단일 transaction domain이 아닌 여러 ledger를 묶을 때는 escrow/allocation partition 또는 durable coordinator와 fencing을 사용한다. 부분 reservation 후 실행을 시작해서는 안 된다. Parent/child account에 같은 실제 usage를 기록할 수 있지만, billing report에서 이중 청구하지 않도록 aggregation semantics를 명시한다.

### Policy affinity와 refresh boundary

```text
pinned
- release/run 시작 시의 policy와 entitlement를 종료까지 유지

boundary_refresh
- turn, task, map item, checkpoint 같은 선언된 경계에서만 새 snapshot 적용

live
- 매 enforcement point에서 재평가; authorization revoke 등 제한적 용도
```

```yaml
policyAffinity:
  authorization: live
  dataResidency: pinned
  usageEntitlement: boundary_refresh
  exhaustionSemantics: pinned
  refreshBoundary: turn
```

이미 시작된 atomic effect나 current-unit completion grace의 의미를 live policy refresh로 소급 변경해서는 안 된다. 긴 session에는 최대 snapshot age와 강제 reauthorization 경계를 둘 수 있다.

Atomic unit이 quota reset 경계를 넘어 계속되더라도 reservation과 usage는 기본적으로 admission 시 pin된 window ID에 귀속한다. 새 window 또는 top-up을 사용해 계속하려면 기존 unit을 pause/checkpoint하고 새 entitlement snapshot과 extension permit을 명시적으로 발급해야 한다.

## 294. Threshold와 exhaustion lifecycle

Budget 상태:

```text
healthy
warning
constrained
degraded
exhausted
overdraft
reconciling
```

Threshold는 action을 가질 수 있다.

```yaml
thresholds:
  - at: 0.70
    actions: [notify]
  - at: 0.90
    actions: [reduce_parallelism, prefer_economy_model]
  - at: 1.00
    actions: [apply_exhaustion_policy]
```

Notification은 enforcement가 아니다. Threshold event와 actual policy action을 구분한다.

## 295. ExhaustionPolicy

```python
class ContinuationEnvelope(BaseModel):
    allowed_work: set[Literal[
        "current_provider_call", "already_admitted_child_work",
        "declared_finalization", "checkpoint", "cleanup", "read_only_tool"
    ]] = Field(default_factory=set)
    forbidden_work: set[Literal[
        "new_turn", "plan_expansion", "optional_task", "new_trial",
        "state_changing_effect", "unreserved_provider_call"
    ]] = Field(default_factory=set)
    max_additional_usage: list[UsageAmount] = Field(default_factory=list)
    max_additional_steps: int | None = None
    deadline: timedelta | None = None

class PartialOutputPolicy(BaseModel):
    client_delivery: Literal[
        "stop_immediately", "continue_to_boundary", "buffer_until_commit"
    ] = "stop_immediately"
    durable_result: Literal[
        "none", "retract", "mark_incomplete", "commit_partial",
        "commit_with_exhaustion_notice"
    ] = "mark_incomplete"

class ExhaustionPolicy(BaseModel):
    preset: Literal[
        "finish_current_turn", "finish_current_call", "finish_current_step",
        "checkpoint_and_pause", "hard_stop", "degrade_then_finalize",
        "request_extension"
    ] | None = None
    deny_new_work: bool = True
    in_flight: Literal[
        "finish_current_unit", "checkpoint_then_pause",
        "degrade_and_continue", "request_topup_or_approval",
        "cancel_immediately"
    ]
    unit: Literal[
        "provider_call", "node", "agent_step", "turn",
        "map_item", "task", "trial", "run"
    ]
    continuation: ContinuationEnvelope | None = None
    max_overdraft: list[UsageAmount] = Field(default_factory=list)
    deadline: timedelta | None = None
    output: PartialOutputPolicy = Field(default_factory=PartialOutputPolicy)
    effects: Literal[
        "preserve_atomicity", "cancel_if_safe", "finish_committing_effect",
        "compensate_if_committed"
    ] = "preserve_atomicity"
    after_unit: Literal["reject", "pause", "fallback", "close"] = "reject"
```

Preset은 authoring shorthand이며 compiler가 위의 explicit contract로 확장한다. Explicit override는 preset보다 더 엄격하게 만들 수 있다. 완화에는 `PolicyOverride` capability가 필요하다.

When an output policy is supplied, the compiler MUST validate the shape of the policy contract
before applying defaults. `outputPolicy`, `delivery`, `evaluation`, and `onViolation` MUST be
mappings, and `evaluation.enforcementPoints` MUST be a list of enforcement point names. Malformed
policy structure MUST produce explicit diagnostics rather than being treated as an omitted policy.

| Preset | Crossing 후 의미 | 기본 output | 기본 금지 |
|---|---|---|---|
| `finish_current_turn` | 현재 turn에 이미 admission된 work와 finalization만 bounded envelope 안에서 완료 | boundary까지 delivery, 완료 또는 exhaustion notice commit | 새 turn, plan 확장, optional trial, 새 state-changing effect |
| `finish_current_call` | 현재 provider/tool call만 완료하고 다음 node/call 금지 | call 결과를 incomplete result로 사용할 수 있음 | 후속 call/effect |
| `finish_current_step` | 현재 agent step을 완료하고 checkpoint | step boundary까지 delivery | 다음 step/tool expansion |
| `checkpoint_and_pause` | 현재 item/task를 일관된 checkpoint/rollback boundary로 이동 후 pause | 진행 event만 commit | 새 item/task |
| `hard_stop` | 새 admission과 client delivery를 즉시 멈추고 cooperative cancellation 요청 | retract 또는 incomplete | 모든 새 work |
| `degrade_then_finalize` | soft threshold에서만 저비용 경로로 전환하고 finalization reserve를 보존 | policy에 따라 계속 | 필수 safety check 생략 |
| `request_extension` | checkpoint 후 entitlement/top-up/사람 결정을 기다림 | paused 상태 | 승인 전 새 work |

`finish_current_turn`은 무제한 grace가 아니다. Turn 시작 시 reserve된 completion envelope 또는 명시적 `max_additional_usage/deadline/steps`가 없으면 production compiler는 이를 거부해야 한다. Turn 내부에서 새 provider/tool call이 필요한 경우 `already_admitted_child_work` 또는 `declared_finalization`으로 미리 분류되고 permit을 가져야 한다. Destructive effect와 plan expansion은 기본 금지다.

`hard_stop`은 **논리적 즉시 중단**을 뜻한다. Runtime은 새 node/task/call admission과 추가 client delivery를 즉시 차단하고 cancellation을 요청하지만, remote provider가 이미 계산한 사용량까지 물리적으로 되돌린다는 뜻은 아니다. Provider/worker가 취소를 지원하지 않으면 `cancel_requested_but_in_flight`와 최대 노출 permit을 기록하고 실제 usage를 사후 정산한다.

Vendor 제품명은 비규범적 설명에만 사용할 수 있다. GraphBlocks의 호환성 단위는 위 preset과 expanded contract이며 특정 제품의 현재 UX가 아니다.

### Atomic unit membership와 admission epoch

Runtime은 turn/task/trial 등 각 atomic unit에 `atomic_unit_id`와 단조 증가하는 `admission_epoch`를 부여한다. Exhaustion 시 continuation work는 다음 중 하나여야 한다.

```text
- exhaustion 이전 epoch에서 이미 admission된 child work
- release/policy에 미리 선언된 finalization/checkpoint/cleanup work
- 유효한 continuation BudgetPermit을 받은 work
```

동적 TaskPlan patch, retry, fallback이 기존 atomic unit ID를 재사용해 새 work를 숨겨서는 안 된다. Retry도 새 attempt reservation을 요구한다.

### Output cutoff와 client consistency

```python
class OutputCutoff(BaseModel):
    stream_id: str
    last_accepted_sequence: int
    terminal_reason: Literal["budget_exhausted", "policy_denied", "cancelled"]
    durable_result: str
```

`hard_stop`에서 server는 local PEP가 수락한 마지막 sequence를 terminal event에 포함한다. Client는 그 이후 지연 도착 frame/delta를 표시 또는 commit해서는 안 된다. 이미 렌더링된 draft를 되돌릴 수 없는 surface는 `AssistantIncomplete` 또는 `AssistantRetracted` 상태를 명확히 표시한다.

Runtime cutoff checks and output-gate policy application MUST validate typed sequence and decision
inputs before comparing or applying them, so malformed caller input fails as a protocol boundary
error rather than an incidental attribute or comparison error.

### Safe point와 강제 종료 단계

```text
request_stop
- provider, worker, tool에 cooperative cancellation 요청

stop_admission
- 새 node, task, provider call, effect prepare를 금지

safe_point
- chunk boundary, tool-call boundary, node boundary, checkpoint, effect transaction boundary

force_terminate
- sandbox/process/container를 종료; trusted in-process block에는 사용 금지
```

Policy는 `cancel_immediately`와 `force_terminate`를 동일시하지 않는다. 강제 종료는 isolated worker/sandbox, 명시적 cleanup/rollback policy, 사용량 상한 추정이 있을 때만 허용한다.


### Exhaustion state machine

Graceful turn:

```text
quota threshold crossed
→ budget.exhausted recorded once
→ stop admission for next atomic unit
→ activate continuation permit
→ execute only envelope-allowed work
→ commit final/incomplete result
→ settle actual usage and overdraft
→ close or reject subsequent unit
```

Hard stop:

```text
quota threshold crossed
→ stop admission
→ stop client delivery at local safe point
→ request provider/worker cancellation
→ prevent partial tool-call execution
→ preserve effect transaction/cleanup invariant
→ emit retract/incomplete outcome
→ reconcile late usage
```

A structured-output parser that receives a truncated stream must report `BudgetExhausted` as the primary terminal cause; schema validation failure may be attached as a diagnostic but must not hide policy termination.

## 296. Turn과 incremental output의 quota semantics

대화 turn은 다음 상태를 추가한다.

```text
budget_constrained
budget_exhausted
paused_for_entitlement
completed_with_overdraft
```

Finish-current-turn 예:

```yaml
usagePolicy:
  scope: principal
  quota:
    window: {kind: rolling, duration: 5h}
    limits:
      - {kind: model_input_tokens, hard: 200000}
      - {kind: model_output_tokens, hard: 40000}
  exhaustion:
    preset: finish_current_turn
    denyNewWork: true
    inFlight: finish_current_unit
    unit: turn
    continuation:
      allowedWork:
        - already_admitted_child_work
        - declared_finalization
        - checkpoint
        - cleanup
      forbiddenWork:
        - new_turn
        - plan_expansion
        - optional_task
        - state_changing_effect
      maxAdditionalUsage:
        - {kind: model_output_tokens, quantity: 4000, unit: token}
        - {kind: wall_time_ms, quantity: 600000, unit: ms}
      maxAdditionalSteps: 2
      deadline: 10m
    maxOverdraft:
      - {kind: model_output_tokens, quantity: 4000, unit: token}
      - {kind: wall_time_ms, quantity: 600000, unit: ms}
    output:
      clientDelivery: continue_to_boundary
      durableResult: commit_with_exhaustion_notice
    effects: preserve_atomicity
    afterUnit: reject
```

Hard-stop 예:

```yaml
usagePolicy:
  exhaustion:
    preset: hard_stop
    denyNewWork: true
    inFlight: cancel_immediately
    unit: provider_call
    continuation:
      allowedWork: [cleanup]
      forbiddenWork:
        - new_turn
        - plan_expansion
        - unreserved_provider_call
        - state_changing_effect
    maxOverdraft: []
    output:
      clientDelivery: stop_immediately
      durableResult: retract
    effects: preserve_atomicity
    afterUnit: reject
```

Draft를 이미 client에 보낸 경우 `AssistantRetracted` 또는 `AssistantIncomplete` event를 반드시 보낸다. Durable `Message`는 commit되지 않은 draft와 구분한다.

표준 policy finish reason:

```text
quota_rejected_before_start
budget_exhausted_cancelled
budget_exhausted_at_safe_point
completed_with_bounded_overdraft
paused_for_budget_extension
provider_quota_exceeded
entitlement_revoked
policy_denied
```

Client는 `finish_reason`, committed/draft 상태, resume 가능 여부를 함께 받아야 한다.

## 297. Completion reserve

```python
class CompletionReserve(BaseModel):
    reserve_id: str
    budget_id: str
    purpose: Literal["finalization", "checkpoint", "cleanup", "compensation"]
    amounts: list[UsageAmount]
    spendable_by: set[str]
    expires_at: datetime | None = None
```

Agent, research, trial workflow는 planning과 exploration이 모든 예산을 소모해 final response, checkpoint, cleanup을 수행하지 못하는 상황을 방지해야 한다.

```yaml
budget:
  limits:
    model_total_tokens: 100000
    currency_usd: 20
  reserves:
    finalization:
      model_output_tokens: 3000
      wall_time: 60s
    cleanup:
      cpu_seconds: 30
```

Completion reserve는 일반 task가 사용할 수 없다. Remaining free budget가 reserve 수준에 도달하면 planner는 새 task를 만들지 않고 finalize/abort path로 전환한다.

## 298. Degradation과 fallback

Budget pressure에서 허용 가능한 adaptation:

```text
더 저렴한 compatible model로 전환
reasoning/quality tier 낮춤
max output tokens 축소
context compression
retrieval top-k/branch 수 축소
subagent/trial concurrency 축소
optional verifier/check 생략
cached result 재사용
```

Adaptation은 다음 제약을 통과해야 한다.

```text
required model capability
sensitivity/data residency
quality gate minimum
provider allowlist
user-visible contract
release compatibility
```

정확성이나 안전에 필수인 check를 비용 절감 목적으로 생략해서는 안 된다. 적용한 adaptation은 ResultBundle, trace, UsageLedger에 기록한다.

## 299. Provider quota와 GraphBlocks budget 구분

```text
GraphBlocksQuotaExceeded
- 내부 entitlement/budget policy가 거부

ProviderQuotaExceeded
- 외부 provider가 429/limit/credit exhaustion을 반환

CapacityUnavailable
- worker/queue/resource pool이 현재 수용 불가
```

Provider quota 처리:

```text
retry_after 준수
compatible provider/model fallback
queue 또는 pause
사용자 top-up/credential 전환 요청
run failure
```

Provider fallback은 policy, residency, data classification, capability를 다시 평가해야 한다.

### Retry, cache, speculative execution accounting

- Retry는 새 attempt이며 별도 reservation과 usage record를 가진다.
- Provider가 처리했으나 client가 응답을 잃은 경우 provider request ID로 reconciliation한다.
- Cache hit는 실제로 발생한 provider/compute usage만 기록하되, 제품 credit 정책이 별도 charge unit을 사용하면 해당 unit을 명시한다.
- Hedged/speculative request는 승자뿐 아니라 실제 실행된 모든 branch 사용량을 계상한다.
- Shadow/canary 실행은 사용자 quota에서 제외할지 별도 platform budget에 부과할지 PolicyProfile이 정한다.
- 사용자가 취소해도 이미 발생한 provider usage는 UsageLedger에서 제거하지 않는다.

## 300. ModelProvider usage capability

Provider adapter는 다음 capability를 선언한다.

```text
preflight_token_count
max_input_tokens
max_output_tokens
streaming_usage_delta
final_usage_report
request_cancellation
provider_side_budget
idempotency_key
retry_after
```

Preflight estimate가 정확하지 않은 provider는 reservation confidence와 safety margin을 설정한다. Runtime은 사용량을 정확히 알 수 없는 provider에서 exact hard token cutoff를 보장한다고 주장해서는 안 된다.

## 301. TaskPlan budget delegation

`TaskPlan`의 각 task는 parent budget에서 envelope를 받아야 한다.

```python
class TaskBudgetEnvelope(BaseModel):
    budget_id: str
    priority: Literal["required", "high", "normal", "optional"]
    limits: list[BudgetLimit]
    completion_reserve: list[UsageAmount] = Field(default_factory=list)
    exhaustion: ExhaustionPolicy
```

Planner는 다음을 초과하는 plan을 만들 수 없다.

```text
maximum tasks/depth
parent available budget
provider/model eligibility
concurrency and lease capacity
required verification reserve
```

Plan patch는 running reservation과 CAS revision을 확인해야 한다. 취소된 task의 unused reservation은 반환하고, 이미 소비된 usage는 반환하지 않는다.

## 302. Trial, verification, ingestion의 exhaustion boundary

권장 기본값:

```text
chat turn
- finish current turn 또는 hard-stop 중 product policy가 선택

research task
- 현재 task를 finish/checkpoint하고 새 task를 금지

RTL trial
- current check의 cancellation safety에 따라 finish 또는 cancel
- 새 candidate/trial은 금지
- final cleanup과 artifact sealing reserve 유지

ingestion job
- current item을 commit/rollback한 뒤 checkpoint and pause

external effect
- prepare 전이면 deny
- commit 중이면 effect atomicity policy를 따름
```

`unit`을 명시하지 않은 exhaustion policy는 compile warning 또는 production error다.

## 303. LeasePool과 scarce resource policy

```python
class LeasePoolDescriptor(BaseModel):
    pool_id: str
    resource_class: str
    capacity_units: Decimal
    attributes: dict[str, JsonValue]
    lease_ttl: timedelta
    renewal_interval: timedelta
    cleanup_policy: str
```

사용 예:

```text
GPU slice
commercial tool license
FPGA board
browser session
sandbox slot
laboratory instrument
```

Lease acquire에는 policy, budget reservation, attribute selector, TTL, fencing token이 필요하다. Lease usage는 `licensed_resource_seconds` 또는 domain-specific unit으로 UsageLedger에 기록할 수 있다.

```rust
#[async_trait]
pub trait LeasePool: Send + Sync {
    async fn acquire(&self, request: LeaseRequest) -> Result<ResourceLease, LeaseError>;
    async fn renew(&self, lease_id: String, fencing_token: u64) -> Result<ResourceLease, LeaseError>;
    async fn release(&self, lease_id: String, fencing_token: u64) -> Result<(), LeaseError>;
    async fn inspect(&self, pool_id: String) -> Result<JsonValue, LeaseError>;
}
```

Lease 만료 후 stale holder가 artifact/effect를 commit하지 못하도록 fencing token을 commit path에서 검사한다.

## 304. Policy와 review/check/gate

```text
Approval
- effect 실행 권한

Review
- 특정 immutable subject digest에 대한 내용 검토

Check
- deterministic 또는 declared verifier 결과

Gate
- check와 metric을 조합한 acceptance decision

Policy
- 위 결과가 어떤 commit/publish/effect에 필요한지 결정
```

Review 후 subject digest가 변경되면 review는 무효다. Gate가 통과한 artifact와 실제 commit 대상 digest가 다르면 commit을 거부한다.

## 305. Policy events와 durable records

Application/diagnostic event:

```text
UsageSnapshotUpdated
PolicyWarning
BudgetWarning
BudgetConstrained
BudgetExhausted
BudgetContinuationStarted
BudgetContinuationEnded
BudgetTopUpRequested
BudgetExtensionResolved
ExecutionDegraded
RunPausedByPolicy
AssistantIncomplete
DraftRetractedByPolicy
TurnCompletedWithOverdraft
```

`BudgetExhausted`는 동일 atomic unit과 limit에 대해 idempotent하게 한 번 발생해야 한다. Client-facing event에는 최소한 reason code, affected unit, selected continuation preset, reset/top-up 가능 여부, remaining/overdraft의 측정 신뢰도를 포함한다. 내부 ledger ID나 다른 tenant 정보는 노출하지 않는다.

User-facing usage snapshot은 `remaining`, `reset_at`, `measurement_confidence`, `pending_reconciliation`, `selected_exhaustion_profile`을 MAY 포함한다. 이는 UI 표시용 snapshot이며 BudgetLedger의 compare-and-reserve를 대체하지 않는다.

Durable journal/ledger record:

```text
policy.evaluated
policy.override.applied
budget.allocated
budget.reserved
budget.committed
budget.released
budget.overdrafted
usage.provisional
usage.reconciled
quota.threshold_crossed
quota.exhausted
execution.adaptation_applied
```

Policy decision 전체 input content를 audit에 복사하지 않는다. Input digest, 필요한 attribute, decision, policy ref, obligation, actor를 기록한다.

## 306. Policy observability와 cardinality

Metric 예:

```text
graphblocks_policy_decisions_total
graphblocks_policy_denials_total
graphblocks_budget_reserved_units
graphblocks_budget_consumed_units_total
graphblocks_budget_overdraft_total
graphblocks_quota_exhaustions_total
graphblocks_policy_adaptations_total
graphblocks_policy_evaluation_seconds
```

허용 label:

```text
policy_class
decision
reason_code
resource_kind
usage_kind
exhaustion_mode
environment
```

principal, tenant, budget ID, run ID는 일반 metric label로 사용하지 않는다. Ledger/query dimension으로만 사용한다.

## 307. Policy test와 rollout

필수 test:

```text
policy schema and type test
allow/deny scenario test
merge/precedence test
quota boundary test
parallel and hierarchical reservation race test
finish-current-turn continuation envelope test
hard-stop logical cutoff and client retraction test
non-cancellable provider late-usage reconciliation test
completion reserve isolation test
TaskPlan/trial child-budget delegation test
partial structured-output terminal-cause test
provider usage reconciliation test
override expiry test
review/gate invalidation test
policy and ledger outage test
```

Policy 변경도 release change다. Production에서는 다음 mode를 지원한다.

```text
dry_run
shadow_decision
canary
active
```

Shadow decision은 실제 enforcement를 바꾸지 않고 stable policy와 diff를 기록한다. Authorization deny와 destructive effect policy는 명시적 승인 없이 shadow-only로 완화하지 않는다.

## 308. Policy CLI와 diagnostics

```bash
graphblocks policy validate policies/production.yaml
graphblocks policy test policies/production.yaml --cases policy-cases/
graphblocks policy evaluate --profile prod --input decision.json
graphblocks policy explain --decision decision_123
graphblocks policy diff stable.yaml candidate.yaml --dataset cases/
graphblocks budget status --scope conversation:conv_123
graphblocks usage report --scope tenant:tenant_a --window 30d
graphblocks quota reconcile --provider openai --since 1h
```

Diagnostics:

```text
GB5001 MissingExhaustionBoundary
GB5002 UnsafeImmediateCancellation
GB5003 BudgetReservationRequired
GB5004 CompletionReserveMissing
GB5005 QuotaUsesLossyTelemetry
GB5006 PolicyOverrideUnaudited
GB5007 ProviderUsageCapabilityInsufficient
GB5008 FallbackViolatesPolicy
GB5009 ReviewSubjectDigestMismatch
GB5010 GateSubjectDigestMismatch
GB5011 BudgetHierarchyExceeded
GB5012 PolicyMergeConflict
GB5013 EntitlementSnapshotExpired
GB5014 ExactCutoffNotEnforceable
GB5015 NonAtomicBudgetLedger
GB5016 UnboundedContinuationEnvelope
GB5017 NewEffectAllowedAfterExhaustion
GB5018 PartialOutputPolicyMissing
GB5019 BudgetPermitExpired
GB5020 CrossAccountReservationPartial
GB5021 ClientDeliveryContinuesAfterHardStop
GB5022 StructuredOutputHidesBudgetTermination
```

## 309. Policy package boundary

```text
graphblocks-core
- PolicyDecision, obligation, entitlement, budget/usage schema

graphblocks-policy
- policy composition, default evaluator, merge, PEP middleware, test DSL

graphblocks-usage
- durable UsageLedger and reconciliation

graphblocks-budget
- BudgetLedger, reservation, quota windows, entitlement adapter SPI

graphblocks-policy-opa
- OPA/Rego adapter

graphblocks-policy-cedar
- Cedar authorization adapter

optional durable backends
- graphblocks-budget-postgres
- graphblocks-usage-postgres
- graphblocks-budget-redis, only where transactional guarantees are sufficient
```

`graphblocks-policy`, `graphblocks-budget`, `graphblocks-usage`의 provider-neutral in-memory/SQLite 개발 구현은 standard metapackage에 포함한다. Production distributed ledger와 external PDP adapter는 선택 설치다.

# Part XI. Security, Testing, Diagnostics, and Roadmap

## 310. Security model

GraphBlocks security는 네 경계를 구분한다.

```text
package/plugin trust
runtime/process trust
content/instruction trust
user/data authorization
```

하나의 `trusted=true` flag로 합치지 않는다.

## 311. Content trust labels

```text
system_trusted
application_trusted
user_supplied
retrieved_untrusted
tool_untrusted
generated_untrusted
```

Prompt/context renderer는 label을 유지하고, retrieved/tool content가 system/developer instruction으로 승격되지 않도록 한다.

## 312. Prompt injection 방어 계약

- retrieval content는 instruction이 아니라 data로 delimit한다.
- tool permission은 model output과 독립된 policy engine이 결정한다.
- secret과 credential을 model context에 주입하지 않는다.
- document가 요청한 외부 URL fetch를 자동 실행하지 않는다.
- data exfiltration 가능 tool은 egress policy를 적용한다.
- citation source가 answer instruction을 정당화하지 않는다.

Guardrail은 block, policy middleware, output validator로 구성할 수 있다.

## 313. ACL propagation

```text
SourceAsset ACL
→ AssetRevision ACL
→ ParsedDocument ACL
→ Chunk ACL
→ Index payload
→ SearchRequest filter
→ SearchHit verification
→ ContextItem
→ Citation authorization
```

어느 단계에서 ACL이 누락되면 compile 또는 ingestion validation을 실패시켜야 한다.

## 314. Tenant isolation

- 모든 durable key는 tenant scope를 가진다.
- connection pool은 tenant credential boundary를 존중한다.
- cache key에 tenant/security scope가 필요할 수 있다.
- cross-tenant artifact reference를 기본 거부한다.
- telemetry에 raw tenant secret을 넣지 않는다.

## 315. Secret handling

Secret은 `SecretRef`로만 GraphSpec에 나타난다.

```yaml
credentials: secret://vault/prod/openai
```

금지:

- serialized plan의 API key
- trace attribute의 credential
- exception string에 full connection URI
- lockfile의 resolved secret
- generated code의 plaintext secret

## 316. Tool and effect governance

Effect 위험 수준:

```text
read_only
low_risk_write
external_communication
financial_or_privileged
destructive
process_execution
```

Policy는 principal, environment, tool, arguments, target resource, risk를 평가한다.

## 317. File security

- archive traversal 방지
- expanded size/depth/file-count 제한
- MIME spoofing 검증
- malware scanning hook
- macro/executable policy
- parser sandbox/worker isolation
- encrypted file policy
- resource exhaustion timeout
- generated artifact content policy

미신뢰 parser는 `python_worker` 또는 remote sandbox에서 실행하는 것을 권장한다.

## 318. Network egress

```yaml
egress:
  default: deny
  allow:
    - host: api.openai.com
      ports: [443]
    - host: company-qdrant.internal
      ports: [6333]
```

Remote URL이나 tool argument가 egress allowlist를 우회할 수 없어야 한다.

## 319. Data capture and privacy

Default:

```text
raw file: not copied to telemetry
raw document text: reference only
partial model delta: not persisted
final answer: configurable/redacted
embedding vector: never telemetry
secret/tool credential: never
voice raw audio: extension default false
```

Masking은 durable storage와 exporter 이전에 적용해야 한다.

## 320. Retention and deletion

Deletion graph는 다음을 다룬다.

```text
conversation/messages
attachments
source/derived artifacts
chunks/index records
memory
run/event records
telemetry linkage
cache
backup/legal hold exception
```

Connector capability가 hard delete를 지원하지 않으면 tombstone과 retention SLA를 명시한다.

## 321. Audit

Audit 대상:

- permission decision
- approval and review decision
- policy override and entitlement change
- budget overdraft/top-up/reconciliation
- destructive tool/effect
- document ACL change
- index publish/delete
- secret provider access metadata
- plugin load and version
- production graph deployment

Audit event는 immutable sink 또는 별도 retention policy를 사용할 수 있다.

## 322. Testing layers

```text
schema test
block unit test
graph compile test
runtime contract test
connector contract test
integration mock test
integration live test
scenario/e2e test
evaluation test
benchmark
security test
policy/quota race test
review/gate integrity test
```

## 323. Deterministic test runtime

`InProcessTestRuntime` 제공 기능:

- deterministic clock
- deterministic ID
- seeded scheduler
- virtual sleep/timeouts
- fake connector
- scripted provider
- trace capture
- fault injection
- cancellation injection

## 324. Block TCK

검사:

- descriptor/schema consistency
- required/optional port
- serialization round trip
- timeout/cancel response
- error mapping
- no output after terminal
- no secret leakage
- telemetry context propagation

## 325. Runtime TCK

```text
single terminal invariant
cancel idempotency
branch cancellation
retry boundary
partial output retry rule
flow/resource lease release
budget reservation race and fencing
policy exhaustion boundary
state CAS conflict
checkpoint resume
resource cleanup
shutdown behavior
```

## 326. Sequence TCK

```text
bounded buffer never exceeds configured limit
ordering contract
backpressure policy
subscriber cancellation
producer failure propagation
final batch flush
item error collection
```

## 327. Connector TCK

공통:

```text
initialize/close idempotency
health semantics
timeout and retry classification
credential redaction
trace propagation
capability declaration accuracy
```

BlobStore:

```text
range read
conditional write
etag/version
streaming put/get
delete/list pagination
```

RecordStore:

```text
CAS/transaction, if declared
query/filter
TTL, if declared
```

KnowledgeIndex/Retriever:

```text
upsert/delete
filter semantics
score metadata
ACL enforcement
pagination/top-k
publish capability
```

## 328. Package TCK

```text
wheel installs in clean environment
manifest is readable without plugin import
entry point resolves
no import-time side effect
supported core range check
uninstall isolation
license/SBOM metadata
```

## 329. Document fixture suite

초기 fixture:

```text
text PDF
scanned PDF
multi-column PDF
table-heavy PDF
DOCX with headings/tables/images
PPTX with notes
XLSX with formulas/merged cells
HTML
Markdown
HWP/HWPX
encrypted/corrupt files
large archive
```

Expectations:

- canonical elements
- source spans
- text/layout coverage
- table preservation
- chunk lineage
- ACL propagation

## 330. RAG test DSL

```yaml
cases:
  - id: hr-carryover-policy
    input:
      conversation:
        - role: user
          text: 연차 이월 규정을 알려줘
    expect:
      answer:
        mustInclude: ["이월"]
        citationCount:
          min: 1
        unsupportedClaimRate:
          max: 0.0
      retrieval:
        relevantSourceIds:
          recallAt10:
            min: 1.0
      security:
        forbiddenSourceIds: []
```

## 331. Conversation/agent test DSL

```yaml
cases:
  - id: create-ticket-requires-approval
    input:
      message: 장애 티켓을 만들어 줘
    script:
      approvals:
        ticket.create: deny
    expect:
      toolCalls:
        requested: [ticket.create]
        completed: []
      events:
        mustInclude: [approval.requested, approval.denied]
      answer:
        mustExplainDenial: true
```

## 332. Graph patch experiment

```yaml
experiment:
  id: chunker-model-matrix
  baseline:
    graph: graphs/company-rag.yaml
    lock: graphblocks.lock
  matrix:
    chunker:
      path: nodes.split.config.strategy
      values: [section_aware, semantic]
    model:
      path: connections.model.config.model
      values: [model-a, model-b]
```

Experiment result는 graph/package/prompt/model hashes를 포함한다.

## 333. Evaluation separation

```text
production graph execution
→ immutable result bundle
→ one or more evaluator graphs
→ EvaluationSink
```

Evaluator 변경 때문에 provider call을 다시 수행할 필요가 없어야 한다.

## 334. Benchmark

### Runtime

```text
node scheduling overhead
Python↔Rust boundary overhead
bounded sequence throughput
cancellation latency
memory per run
cold start
```

### Document

```text
files/minute
pages/minute
conversion p50/p95/p99
peak memory
cache hit rate
index commit latency
cost/file
```

### RAG/chat

```text
time to first delta
time to final answer
retrieval/rerank/context latency
tokens and cost/turn
concurrent conversations
error rate
```

Average만 보고하지 않고 p50/p90/p95/p99와 saturation point를 포함한다.

## 335. CLI

```bash
# schema, compile, plan
graphblocks validate graph.yaml
graphblocks plan graph.yaml --expand --show-bindings --show-packages
graphblocks migrate graph-v1alpha1.yaml --to v1alpha3

# packages and plugins
graphblocks plugins list
graphblocks packages doctor
graphblocks lock resolve app.yaml

# execution
graphblocks run graph.yaml --input input.json
graphblocks serve application.yaml
graphblocks job resume run_123

# application protocol
graphblocks app invoke application.yaml --graph chat
graphblocks app events run_123 --cursor latest

# tests/evaluation
graphblocks test tests/rag.yaml
graphblocks compare experiments/model_prompt.yaml
graphblocks tck runtime
graphblocks tck package graphblocks-qdrant

# release/deployment
graphblocks release build release.yaml --out dist/app.gbr
graphblocks release verify dist/app.gbr
graphblocks deploy plan deployment.yaml
graphblocks deploy render deployment.yaml --target kubernetes
graphblocks deploy diff deployment.yaml --cluster production

# policy, quota, budget
graphblocks policy validate policies/production.yaml
graphblocks policy test policies/production.yaml --cases policy-cases/
graphblocks policy explain --decision decision_123
graphblocks budget status --scope conversation:conv_123
graphblocks usage report --scope tenant:tenant_a --window 30d

# observability/diagnostics
graphblocks observe run run_123
graphblocks observe diagnostic-bundle run_123 --redacted
graphblocks slo report deployment.yaml
graphblocks doctor --target standalone-rust
```

## 336. Production readiness checklist

```text
strict semantic/environment locks
immutable signed release bundle
plugin and image allowlist
secret references only
ACL/prompt-injection tests
retention/delete graph
capture/redaction policy
provider timeout/retry
idempotency and rollback class for effects
runtime/package/connector TCK report
SBOM/vulnerability/license scan
load and quality benchmark
rollout/rollback/drain plan
index publish and revision pin
conversation CAS/release affinity
RPO/RTO and restore test
metric cardinality and telemetry budget
policy bundle and entitlement source pinned
quota/budget ledger atomicity and reconciliation
explicit exhaustion boundary and completion reserve
bounded continuation envelope and partial-output policy
atomic hierarchical reservation and worker BudgetPermit
late provider usage reconciliation
review/gate subject digest enforcement
```

## 337. Roadmap principles

- 자연어/파일/RAG/chat이 voice와 범용 stream보다 앞선다.
- provider breadth보다 canonical contract, compiler diagnostic, TCK를 먼저 완성한다.
- Policy, usage, budget은 production add-on이 아니라 runtime contract로 설계하되 외부 engine/backend는 선택 package로 둔다.
- Static GraphSpec을 유지하고 adaptive work는 bounded TaskPlan executor로 제한한다.
- ApplicationSpec과 deployment object는 runtime core와 독립 version으로 발전시킨다.
- Kubernetes operator는 renderer와 deployment revision이 안정된 뒤 구현한다.
- durable ingestion은 문서 lifecycle에 필요한 item checkpoint/idempotency부터 구현한다.

## 338. Implementation Phase 0 — Canonical Contracts and Policy Foundation

```text
GraphSpec v1alpha3
SourceRef/SourceLocator and KnowledgeItemRef
Claim/Evidence/Diagnostic
ResourceSnapshot/ChangeSet
Check/Metric/Gate/Trial/Review/ResultBundle
PolicyBundle/PolicyProfile/typed obligation
Outcome Denied/BudgetExhausted/Paused
UsageLedger and BudgetLedger split
finish-current-unit and hard-stop exhaustion TCK
```

## 339. Implementation Phase 1 — Documents, RAG, Conversation, Usage Governance

```text
canonical document/element/chunk lineage
Retriever/federated retrieval/fusion/rerank
ContextPack/citation/evidence
conversation transaction, attachment, memory
turn budget reservation/completion reserve
provider usage reconciliation
enterprise RAG/chat acceptance apps
```

## 340. Implementation Phase 2 — Adaptive Orchestration and Verification

```text
TaskPlan/TaskPlanPatch executor
ModelPool/WorkerProfile
per-task budget delegation
workspace snapshot/fork/ChangeSet/CAS commit
Check/Gate/Trial and Review workflow
LeasePool and scarce-resource accounting
research and RTL stress-test acceptance apps
```

## 341. Implementation Phase 3 — Release, Deployment, Observability, Policy Operations

```text
GraphRelease/GraphDeployment/PhysicalExecutionPlan
Kubernetes/Helm renderer and worker draining
Policy rollout/shadow/canary
OTel/Langfuse integration
SLO and semantic rollout gates
DR/RPO/RTO and diagnostic bundle
stable runtime/worker/policy protocol
```

Optional extensions can mature independently:

```text
Realtime Voice Extension
Durable Unbounded Dataflow Extension
WASM/sandbox plugin extension
multi-cluster placement extension
```

## 342. Core release acceptance applications

1. Direct PDF/DOCX/PPTX/XLSX/HWP analysis with generalized source references and generated artifact.
2. Incremental document ingestion with parser fallback, per-item budget/checkpoint, staging index, publish, delete, ACL propagation.
3. Federated enterprise RAG with dense/keyword/hosted sources, quorum, fusion, rerank, context budget, abstention, citation validation.
4. Conventional multi-turn chatbot with attachment, regenerate/branch, CAS, draft/retract/commit, finish-current-turn and hard-stop quota profiles.
5. Tool-using agent with typed state, approval, sandboxed effect, completion reserve, compensation class.
6. Bounded research orchestrator using TaskPlan, task budget reservation, evidence, independent verification, ResultBundle.
7. Isolated candidate/trial application using snapshot, ChangeSet, Check/Gate, Review, LeasePool, CAS commit; Verilog is one acceptance fixture, not a core domain package.
8. TUI workspace assistant using ApplicationProtocol rather than a surface graph node.
9. GraphRelease build, signed bundle verification, Kubernetes execution groups, canary quality/policy gate, rollback/drain.
10. OTel + Langfuse projection while audit/usage/budget/recovery remain correct when telemetry is unavailable.

## 343. 최종 아키텍처 요약

```text
Canonical AI Schemas
        ↓
GraphSpec v1alpha3 + ApplicationSpec + BindingSpec + PolicyBundle/Profile
        ↓
Normalized IR + Package Closure
        ↓
GraphRelease (immutable)
        ↓
GraphDeployment + DeploymentRevision
        ↓
PhysicalExecutionPlan
        ↓
Rust Runtime / Worker Pools / External Services
        ↓
RunStore + ExecutionJournal + AuditLog + UsageLedger + BudgetLedger
        ↓
ApplicationEventStream + OTel/Langfuse + Evaluation/SLO
```

핵심 경계:

> **Graph는 계산과 상태 전이를 표현하고, Application은 사용자 표면과 protocol을 표현하며, Binding은 외부 자원을 연결하고, Deployment는 실행 위치와 release lifecycle을 정의한다.**

> **독립 node의 병렬성은 scheduler가 결정하고, 명시적 control primitive는 취소·실패·반복·부분 성공 같은 정책이 있을 때만 사용한다.**

> **관측성 backend는 실행 source of truth가 아니며, durable correctness/audit/usage/budget 기록은 별도 plane에 둔다.**

> **Quota 초과 동작은 제품별 암묵적 UX가 아니라, atomic unit·overdraft·partial output·effect safety를 포함한 ExhaustionPolicy로 정의한다.**

# Extension A. Realtime Voice와 Duplex Session

## A.1 위치

Voice는 `graphblocks-voice` 선택 extension이다. Core의 `Conversation`, `Message`, `ToolCall`, `ModelResponse`, `Answer`를 재사용하고 다음만 추가한다.

```text
audio track
transport
VAD/turn detection
playout
interruption
duplex provider session
```

## A.2 패키지

```text
graphblocks-voice             # canonical media/session contract
graphblocks-webrtc            # transport
graphblocks-websocket-media   # transport
graphblocks-silero-vad        # local acoustic VAD
graphblocks-openai-realtime   # provider adapter
```

기본 `graphblocks` install에 포함되지 않는다.

## A.3 Pipeline profile

```text
cascade
- audio → VAD → STT → text agent → TTS → audio

realtime
- audio ⇄ native realtime provider ⇄ audio
          ⇅ tools/control

hybrid
- 일부 modality/provider만 realtime
```

## A.4 Duplex session contract

```rust
#[async_trait]
pub trait RealtimeSession: Send {
    async fn send(&self, command: RealtimeCommand) -> Result<()>;
    fn events(&mut self) -> Pin<Box<dyn Stream<Item = Result<RealtimeEvent>> + Send + '_>>;
    async fn close(&self, reason: CloseReason) -> Result<()>;
}
```

Control lane은 audio data lane보다 우선순위가 높아야 한다.

```text
CancelResponse
ClearOutput
CommitInput
CreateResponse
ToolResult
TruncateConversation
CloseSession
```

## A.5 AudioFrame

```python
class AudioFrame(BaseModel):
    track_id: str
    data: bytes
    codec: Literal["pcm16", "opus", "mulaw", "alaw"]
    sample_rate: int
    channels: int
    timestamp_ms: int
    sequence: int
    duration_ms: int | None = None
```

AEC, noise suppression, resampling, jitter buffering은 VAD와 분리한다.

## A.6 VoiceSession

```python
class VoiceSession(BaseModel):
    voice_session_id: str
    conversation_id: str
    transport: str
    pipeline_kind: Literal["cascade", "realtime", "hybrid"]
    provider_session_id: str | None = None
    status: Literal["connecting", "active", "closing", "closed", "failed"]
```

User turn과 assistant response를 분리한다.

## A.7 VAD 계층

```text
Acoustic VAD
- 음성 존재 확률과 speech start/stop

Endpoint detector
- 물리적 silence와 max utterance

Semantic turn detector
- 의미상 발화 완료

Interruption classifier
- true interruption/backchannel/echo/noise/background speaker
```

## A.8 Authority

```yaml
turnDetection:
  authority: provider       # provider | graphblocks | client
  mode: semantic

localVad:
  enabled: true
  role: metrics_and_early_duck
```

하나의 turn authority만 응답 생성/commit 권한을 가져야 한다.

## A.9 Interruption

```yaml
interruption:
  policy: adaptive
  minSpeechMs: 180
  ignoreBackchannels: true
  onPossible: duck
  onConfirmed:
    - clear_playout
    - cancel_response
    - truncate_conversation
  onFalse:
    - resume_playout
```

## A.10 PlaybackLedger

사용자가 실제로 들은 위치를 추적한다.

```python
class PlaybackCursor(BaseModel):
    response_id: str
    item_id: str
    content_index: int
    generated_ms: int
    enqueued_ms: int
    played_ms: int
    acknowledged_ms: int
```

WebSocket transport에서는 client playout acknowledgement를 받아 conversation truncation을 계산해야 한다.

## A.11 RealtimeEvent

```text
SessionCreated
InputSpeechStarted
InputSpeechStopped
InputTranscriptDelta
InputTranscriptFinal
ResponseCreated
OutputTextDelta
OutputAudioDelta
OutputTranscriptDelta
ToolCallStarted
ToolCallArgumentsDelta
ToolCallCompleted
ResponseCompleted
ResponseCancelled
UsageUpdated
Error
```

Provider event를 그대로 core schema로 노출하지 않고 adapter가 canonical event로 변환한다.

## A.12 Voice storage default

```text
raw input audio: false
raw output audio: false
partial transcript: false
final transcript: redacted/configurable
final assistant message: configurable
playback metrics: true
```

Recording은 consent, encryption, retention을 명시해야 한다.

## A.13 Voice TCK

```text
session close/cancel race
control lane priority
VAD authority uniqueness
false interruption recovery
barge-in to audio stop latency
playback cursor/truncation
provider disconnect/reconnect
raw audio capture default
```

## A.14 OpenAI realtime adapter profile

OpenAI realtime adapter는 provider model/version과 session capabilities를 runtime bind 시점에 조회 또는 선언한다. `gpt-realtime-2` 같은 bidirectional speech-to-speech model을 지원할 수 있지만 GraphBlocks core가 특정 모델명에 의존하지 않는다.

Adapter는 다음을 mapping한다.

```text
session configuration
input audio buffer
server/semantic VAD
conversation items
response audio/text
function/tool calls
output buffer clear
conversation truncation
usage and errors
```

# Extension B. Durable Unbounded Dataflow

## B.1 위치

대부분의 문서 ingestion은 bounded job과 checkpoint만으로 충분하다. Kafka topic, CDC, continuous sync, unbounded window가 필요한 경우에만 `graphblocks-durable` extension을 사용한다.

## B.2 패키지

```text
graphblocks-durable
graphblocks-kafka
graphblocks-nats
graphblocks-sqs
graphblocks-pubsub
graphblocks-etcd, future
```

## B.3 Source contract

```rust
#[async_trait]
pub trait DurableSource: Send + Sync {
    async fn poll(&self, cursor: Option<SourceCursor>, demand: usize) -> Result<SourceBatch>;
    async fn commit(&self, cursor: SourceCursor) -> Result<()>;
    async fn pause(&self) -> Result<()>;
    async fn resume(&self) -> Result<()>;
}
```

## B.4 Delivery guarantee

```text
best_effort
at_most_once
at_least_once
```

GraphBlocks는 일반적인 distributed sink에 대해 exactly-once를 무조건 주장하지 않는다. Idempotent sink와 transactional source/sink 조합으로 effectively-once 결과를 제공할 수 있다.

## B.5 Checkpoint barrier

```text
source cursors
operator state
pending effect journal
sink commit metadata
plan hash
schema versions
```

Checkpoint commit 순서와 source offset commit 순서를 connector profile별로 명시한다.

## B.6 Event time

```text
event time
processing time
watermark
allowed lateness
trigger
accumulation mode
```

`window(size_ms)`만으로 unbounded aggregation 완료를 결정하지 않는다.

## B.7 Operators

```text
stream.map
stream.filter
stream.flat_map
stream.key_by
stream.window
stream.aggregate
stream.join
stream.batch
stream.sink
```

Core의 `control.reduce`와 extension의 unbounded aggregate를 구분한다.

## B.8 Recovery

```text
restore checkpoint
→ recreate operators
→ restore state
→ seek source cursor
→ reconcile effect journal
→ resume demand
```

Block upgrade 시 state migration schema가 필요하다.

## B.9 Backpressure

Bounded channel, demand, pause capability를 사용한다. Source가 pause를 지원하지 않으면 broker prefetch/partition assignment와 local spill 정책을 선언한다.

## B.10 Durable TCK

```text
source cursor replay
checkpoint atomicity
worker crash recovery
idempotent sink replay
late event/window semantics
state migration
partition ordering
rebalance
poison item/dead-letter
```

# Appendix A. Package Catalog

## A.1 Core release train

| Distribution | Import | Type | Default install | Primary responsibility |
|---|---|---|---|---|
| `graphblocks-core` | `graphblocks` | pure Python | yes, via meta | schemas, GraphSpec, SDK |
| `graphblocks-runtime` | `graphblocks_runtime` | native wheel | yes, via meta | Rust execution engine |
| `graphblocks-stdlib` | `graphblocks_stdlib` | Python | yes, via meta | provider-neutral blocks |
| `graphblocks` | none/meta | metapackage | primary install | common provider-neutral install |
| `graphblocks-documents` | `graphblocks_documents` | Python | yes, via meta | document profile |
| `graphblocks-rag` | `graphblocks_rag` | Python | yes, via meta | retrieval/RAG |
| `graphblocks-conversation` | `graphblocks_conversation` | Python | yes, via meta | chat/session state |
| `graphblocks-policy` | `graphblocks_policy` | Python | yes, via meta | policy composition, PEP, default evaluator |
| `graphblocks-budget` | `graphblocks_budget` | Python | yes, via meta | budget/quota SPI and local ledger |
| `graphblocks-usage` | `graphblocks_usage` | Python | yes, via meta | usage facts and local ledger |
| `graphblocks-agents` | `graphblocks_agents` | Python | optional | tools/agent loop |
| `graphblocks-evaluation` | `graphblocks_evaluation` | Python | optional | check/metric/gate/trial |
| `graphblocks-orchestration` | `graphblocks_orchestration` | Python | optional | TaskPlan and budget delegation |
| `graphblocks-review` | `graphblocks_review` | Python | optional | immutable-subject review workflow |
| `graphblocks-workspace` | `graphblocks_workspace` | Python | optional | snapshot/ChangeSet/CAS workspace |
| `graphblocks-cli` | `graphblocks_cli` | Python/native helper | yes, via meta | CLI |
| `graphblocks-server` | `graphblocks_server` | Python | optional | HTTP/SSE/WebSocket |
| `graphblocks-worker` | `graphblocks_worker` | Python | optional | isolated Python execution |
| `graphblocks-devtools` | `graphblocks_devtools` | Python | dev | visualization/migration/codegen |
| `graphblocks-testing` | `graphblocks_testing` | Python | dev/test | deterministic runtime/TCK |

## A.2 Initial official integrations

| Category | Priority packages |
|---|---|
| Model | `graphblocks-openai`, `graphblocks-anthropic`, `graphblocks-google-genai` |
| Converter | `graphblocks-pypdf`, `graphblocks-docling`, `graphblocks-hwp` |
| Blob | `graphblocks-s3`, `graphblocks-gcs` |
| Knowledge | `graphblocks-qdrant`, `graphblocks-pgvector`, `graphblocks-opensearch` |
| State/record | `graphblocks-postgres`, `graphblocks-firestore`, `graphblocks-redis` |
| Observability | `graphblocks-langfuse`, `graphblocks-otel`, `graphblocks-prometheus` |
| Policy | `graphblocks-policy-opa`, `graphblocks-policy-cedar` |
| Durable ledger | `graphblocks-budget-postgres`, `graphblocks-usage-postgres` |
| Framework | `graphblocks-haystack`, `graphblocks-langgraph`, `graphblocks-langchain` |

## A.3 Optional extensions

| Extension | Packages |
|---|---|
| Voice | `graphblocks-voice`, `graphblocks-webrtc`, `graphblocks-websocket-media`, `graphblocks-openai-realtime`, `graphblocks-silero-vad` |
| Durable stream | `graphblocks-durable`, `graphblocks-kafka`, `graphblocks-nats`, `graphblocks-sqs`, `graphblocks-pubsub` |

# Appendix B. Acceptance Application Pseudocode

## B.1 Federated enterprise RAG

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: enterprise-rag-turn
spec:
  interface:
    inputs:
      turn: graphblocks.ai/ConversationTurnInput@1
      auth: graphblocks.ai/AuthContext@1
    outputs:
      result: graphblocks.ai/TurnCandidate@1
    events:
      - graphblocks.ai/AssistantDraftDelta@1

  nodes:
    begin:
      block: conversation.begin_turn@1

    classify:
      block: query.classify@1

    rewrite:
      block: query.rewrite@1

    plan:
      block: query.plan_retrieval@1

    retrieve:
      block: retrieve.execute_plan@1
      bindings:
        retrievers:
          dense: company_dense
          keyword: company_keyword
          tickets: support_tickets
        embedding: query_embedding
      config:
        minimumSuccessfulSources: 1
        sourceTimeout: 2s

    fuse:
      block: retrieve.fuse@1
      config:
        algorithm: reciprocal_rank_fusion

    rerank:
      block: rank.documents@1
      bindings:
        reranker: answer_reranker

    context:
      block: context.build@1
      config:
        maxTokens: 48000
        reserveOutputTokens: 8000

    generate:
      block: model.generate@1
      bindings:
        model: answer_model
      projection:
        text: AssistantDraftDelta

    validate:
      block: answer.validate_grounding@1
      config:
        requireCitation: true
        onInsufficientEvidence: abstain

    commit:
      block: conversation.commit_turn@1
```

### B.1.1 Production BindingSpec

GraphSpec에는 logical resource name만 기록하고, provider·endpoint·credential은 별도 BindingSpec에서 해석한다.

```yaml
apiVersion: graphblocks.ai/v1alpha1
kind: Binding
metadata:
  name: enterprise-rag-production
spec:
  resources:
    company_dense:
      kind: Retriever
      implementation: qdrant.dense
      config:
        collection: company_docs_v17
        endpoint: https://qdrant.internal
      credentials: {secretRef: secret://qdrant/production}

    company_keyword:
      kind: Retriever
      implementation: opensearch.keyword
      config:
        index: company_docs_v17
        endpoint: https://opensearch.internal
      credentials: {secretRef: secret://opensearch/production}

    support_tickets:
      kind: Retriever
      implementation: company.ticket_search
      config: {endpoint: https://tickets.internal/search}
      credentials: {secretRef: secret://tickets/production}

    query_embedding:
      kind: EmbeddingModel
      implementation: openai.embeddings
      config: {model: embedding-model-production}
      credentials: {secretRef: secret://openai/production}

    answer_reranker:
      kind: Reranker
      implementation: cross_encoder.remote
      config: {endpoint: https://reranker.internal}

    answer_model:
      kind: ChatModel
      implementation: openai.responses
      config: {model: chat-model-production}
      credentials: {secretRef: secret://openai/production}
```

## B.2 TUI workspace assistant

```yaml
apiVersion: graphblocks.ai/v1alpha1
kind: Application
metadata:
  name: workspace-assistant
spec:
  surfaces:
    default:
      kind: tui
      implementation: textual
      protocol: graphblocks.app.v1
  graphs:
    default: graphs/workspace-agent.yaml
  capabilities:
    - assistant_drafts
    - approval
    - artifact_preview
    - breakpoint_resume
```

Workspace graph는 `workspace.snapshot/context`, `agent.run`, `workspace.propose_patch`, `test.run`을 사용하고, patch 적용과 process 실행은 approval/sandbox policy를 요구한다.

## B.3 Durable document preprocessing

```yaml
nodes:
  snapshot:
    block: asset.snapshot_source@1

  diff:
    block: asset.diff_snapshot@1

  process:
    block: control.map@2
    config:
      graph: graphs/process-single-asset.yaml
      itemKey: $.revision_id
      concurrency: 16
      stateIsolation: item
      checkpoint: per_item
      onError: collect

  delete:
    block: control.map@2
    config:
      graph: graphs/delete-single-asset.yaml
      itemKey: $.revision_id
      checkpoint: per_item
```

Single asset graph는 begin revision, cache lookup, deterministic converter selection, quality/OCR fallback, normalize/redact/enrich, structured extraction, artifact/manifest/index staging, commit을 포함한다.

## B.4 Usage policy — finish-current-turn profile

이미 시작된 turn을 bounded overdraft 안에서 마치고 새 turn을 차단하는 profile이다.

```yaml
apiVersion: graphblocks.ai/v1alpha1
kind: PolicyProfile
metadata:
  name: interactive-graceful
spec:
  quotaAccounts:
    userInteractive:
      scope: principal
      window:
        kind: rolling
        duration: 5h
      limits:
        - kind: model_input_tokens
          hard: 200000
          unit: token
        - kind: model_output_tokens
          hard: 40000
          unit: token

  budgets:
    turn:
      inheritFrom: userInteractive
      reservation:
        required: true
        safetyMargin: 0.15
      completionReserve:
        - kind: model_output_tokens
          quantity: 2000
          unit: token

  thresholds:
    - at: 0.80
      actions: [notify]
    - at: 0.90
      actions: [prefer_economy_model, reduce_parallelism]

  exhaustion:
    preset: finish_current_turn
    denyNewWork: true
    inFlight: finish_current_unit
    unit: turn
    continuation:
      allowedWork: [already_admitted_child_work, declared_finalization, checkpoint, cleanup]
      forbiddenWork: [new_turn, plan_expansion, optional_task, state_changing_effect]
      maxAdditionalUsage:
        - {kind: model_output_tokens, quantity: 4000, unit: token}
        - {kind: wall_time_ms, quantity: 600000, unit: ms}
      maxAdditionalSteps: 2
      deadline: 10m
    maxOverdraft:
      - {kind: model_output_tokens, quantity: 4000, unit: token}
      - {kind: wall_time_ms, quantity: 600000, unit: ms}
    output:
      clientDelivery: continue_to_boundary
      durableResult: commit_with_exhaustion_notice
    effects: preserve_atomicity
    afterUnit: reject
```

## B.5 Usage policy — hard-stop profile

현재 provider call에 cancellation을 요청하고 미완성 draft를 retract하는 profile이다.

```yaml
apiVersion: graphblocks.ai/v1alpha1
kind: PolicyProfile
metadata:
  name: interactive-hard-stop
spec:
  quotaAccounts:
    userInteractive:
      scope: principal
      window: {kind: rolling, duration: 5h}
      limits:
        - {kind: model_input_tokens, hard: 200000, unit: token}
        - {kind: model_output_tokens, hard: 40000, unit: token}

  exhaustion:
    preset: hard_stop
    denyNewWork: true
    inFlight: cancel_immediately
    unit: provider_call
    continuation:
      allowedWork: [cleanup]
      forbiddenWork: [new_turn, plan_expansion, unreserved_provider_call, state_changing_effect]
    maxOverdraft: []
    output:
      clientDelivery: stop_immediately
      durableResult: retract
    effects: preserve_atomicity
    afterUnit: reject
```

`cancel_immediately`는 best-effort remote cancellation이다. 이미 effect commit critical section에 들어간 작업은 effect policy에 따라 마무리하거나 indeterminate/compensation 상태를 기록한다.

## B.6 Adaptive research orchestration budget

Research domain type을 core에 추가하지 않고 generic TaskPlan, EvidenceRef, Check/Gate, ResultBundle을 사용한다.

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: bounded-research-orchestrator
spec:
  interface:
    inputs:
      objective: company.research/Objective@1
      sources: list[graphblocks.core/SourceRef@1]
    outputs:
      result: graphblocks.core/ResultBundle@1

  nodes:
    snapshot:
      block: resource.snapshot@1

    plan:
      block: orchestration.plan@1
      config:
        outputSchema: graphblocks.orchestration/TaskPlan@1
        limits:
          maxTasks: 48
          maxDepth: 4
        phaseBudgets:
          planning: 0.10
          execution: 0.55
          verification: 0.20
          finalization: 0.15

    validatePlan:
      block: orchestration.validate_plan@1

    execute:
      block: orchestration.execute_task_plan@1
      config:
        checkpoint: each_task
        reservation: per_task
        onBudgetPressure:
          cancelPriorities: [optional, normal]
          preserve: [required, verification, finalization]

    verify:
      block: check.run_suite@1

    gate:
      block: gate.evaluate@1

    bundle:
      block: result.bundle@1
```

## B.7 RTL candidate trial with budget and scarce-resource lease

반도체/Verilog 타입은 application-local schema로 유지한다. GraphBlocks는 snapshot, ChangeSet, Trial, Check/Gate, Review, LeasePool 계약만 제공한다.

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: rtl-candidate-trial
spec:
  interface:
    inputs:
      candidate: company.hdl/PatchCandidate@1
      base: graphblocks.core/ResourceSnapshotRef@1
    outputs:
      trial: graphblocks.evaluation/TrialResult@1

  nodes:
    reserveTrialBudget:
      block: budget.reserve@1
      config:
        limits:
          - {kind: model_total_tokens, quantity: 30000, unit: token}
          - {kind: cpu_seconds, quantity: 3600, unit: second}
          - {kind: licensed_resource_seconds, quantity: 900, unit: second}

    fork:
      block: workspace.fork@1
      execution:
        requires: {isolation: sandbox}

    apply:
      block: workspace.apply_changeset@1

    fastChecks:
      block: check.run_suite@1
      config:
        checks: [lint, compile, smoke_simulation]
        stopOnFailure: true

    formal:
      block: check.run_suite@1
      when: fastChecks.passed
      flow:
        leasePool: formal-license
      config:
        checks: [formal_properties]

    synthesis:
      block: check.run_suite@1
      when: formal.hardGatePassed
      flow:
        leasePool: synthesis-license
      config:
        checks: [synthesis, timing, area]

    gate:
      block: gate.evaluate@1
      config:
        hardConstraints:
          - lint_passed
          - compile_passed
          - regression_passed
          - formal_not_failed
        objectives:
          - {metric: area, direction: minimize}
          - {metric: worst_slack, direction: maximize}

    seal:
      block: trial.seal_result@1
      policies:
        integrity: trusted-oracle-unchanged
        budget:
          onExhaustion:
            inFlight: checkpoint_then_pause
            unit: trial
```

# Appendix C. Architecture Decision Log

## C.1 Product core

문서, 자연어, RAG, conversation을 core로 유지하고 voice와 범용 stream은 extension으로 둔다.

## C.2 Runtime ownership

Rust runtime이 scheduler, cancellation, bounded flow, leases, terminal state를 소유한다. Python은 authoring/provider/custom block 계층이다.

## C.3 Layered specs

GraphSpec, ApplicationSpec, BindingSpec, GraphRelease, GraphDeployment를 분리한다.

## C.4 Control semantics

Automatic DAG concurrency를 기본으로 하고 generic parallel/join을 구체적 primitive로 해체한다.

## C.5 Outcome semantics

Absent, skipped, failed, cancelled, null을 명시적으로 구분한다.

## C.6 Packaging

Standard metapackage에는 provider-neutral documents/RAG/conversation을 포함하되 provider/parser/cloud/server/voice는 분리한다.

## C.7 Release and operations

Production run은 immutable release와 deployment revision에 pin하고 workload-aware rollout/drain을 적용한다.

## C.8 Observability

ExecutionJournal, AuditLog, UsageLedger, BudgetLedger, ApplicationEventStream, Telemetry를 분리한다.

## C.9 Policy enforcement

Policy는 prompt/observer가 아니라 compile, admission, node, provider, effect, commit, publish enforcement point를 가진다.

## C.10 Usage exhaustion

Finish-current-unit과 hard-stop을 모두 지원하되 unit, overdraft, draft 처리, effect atomicity를 반드시 명시한다.

## C.11 Cross-domain work contracts

법률, 연구, Verilog 같은 domain package를 core에 추가하지 않고 Snapshot, ChangeSet, Evidence, Check/Gate/Trial, Review, ResultBundle, TaskPlan으로 일반화한다.

# Appendix D. Legacy Architecture Decision Log

## D.1 Native backend 유지

Draft v0.3의 핵심 결정인 NativeBackend 우선 원칙을 유지하되 이름을 `NativeRustRuntime`으로 명확히 했다.

## D.2 Framework backend 축소

`LangGraphBackend`를 전체 GraphBlocks 의미론을 구현하는 동급 backend로 보지 않는다. 대신 turn-level subgraph bridge로 정의한다. Haystack도 component/pipeline bridge로 통합한다.

## D.3 EjectedBackend 재분류

Ejection은 실행 backend가 아니라 배포/code-generation target이다.

## D.4 FlowBlock 정리

Semaphore, rate limit, retry, timeout은 기본적으로 node wrapper/scheduler policy다. Wait 결과가 graph data일 때만 explicit flow node를 사용한다.

## D.5 Streaming 재분류

Draft v0.4의 event streaming과 data streaming 분리는 유지한다. 다만 LLM token delta는 finite invocation의 incremental projection으로 이동하고, raw media와 unbounded dataflow는 extension으로 이동한다.

## D.6 Voice 재배치

VAD, duplex session, interruption, playback ledger 설계는 유지하되 core conversation model 위의 Extension A로 이동한다.

## D.7 DocumentStore 명칭 변경

일반 구조화 저장소는 `RecordStore`, 검색용 저장소는 `KnowledgeIndex`, 검색 공개 계약은 `Retriever`로 분리한다.

## D.8 Event-sourcing 범위 축소

모든 request를 event-sourced로 강제하지 않는다. Durable job과 audit effect에 필요한 경우만 EventStore/checkpoint를 요구한다.

## D.9 Package architecture 강화

Core/runtime/domain/integration/extension을 별도 distribution으로 배포한다. 공식 mega package는 제공하지 않는다.

## D.10 Policy와 quota 분리

Draft v0.3의 flow rate limit과 Draft v0.7의 UsageLedger만으로는 entitlement와 in-flight exhaustion을 정의할 수 없다. v0.8은 PolicyBundle, BudgetLedger, reservation, exhaustion boundary를 추가한다.

# Appendix E. Design References

아래 자료는 설계 참고이며 GraphBlocks가 해당 API를 그대로 복제한다는 뜻은 아니다.

1. PyO3 User Guide - https://pyo3.rs/
2. Maturin project layout - https://www.maturin.rs/project_layout.html
3. Cargo workspaces - https://doc.rust-lang.org/cargo/reference/workspaces.html
4. Python Packaging User Guide, optional dependencies - https://packaging.python.org/en/latest/guides/writing-pyproject-toml/
5. Python namespace packages - https://packaging.python.org/guides/packaging-namespace-packages/
6. Python package metadata and plugin discovery - https://packaging.python.org/en/latest/guides/creating-and-discovering-plugins/
7. Entry points specification - https://packaging.python.org/en/latest/specifications/entry-points/
8. Dependency Groups specification - https://packaging.python.org/en/latest/specifications/dependency-groups/
9. pylock.toml specification - https://packaging.python.org/en/latest/specifications/pylock-toml/
10. Haystack Pipeline and components - https://docs.haystack.deepset.ai/
11. Haystack Core Integrations - https://github.com/deepset-ai/haystack-core-integrations
12. LangChain component architecture - https://docs.langchain.com/oss/python/langchain/component-architecture
13. OpenTelemetry semantic conventions - https://opentelemetry.io/docs/concepts/semantic-conventions/
14. OpenTelemetry Rust - https://opentelemetry.io/docs/languages/rust/
15. Langfuse data model - https://langfuse.com/docs/observability/data-model
16. Langfuse SDK/OpenTelemetry - https://langfuse.com/docs/observability/sdk/overview
17. Langfuse experiments - https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk
18. OpenAI Realtime API - https://developers.openai.com/api/docs/guides/realtime
19. OpenAI Realtime VAD - https://developers.openai.com/api/docs/guides/realtime-vad
20. OpenAI Realtime conversations - https://developers.openai.com/api/docs/guides/realtime-conversations
21. OpenAI GPT-Realtime-2 model - https://developers.openai.com/api/docs/models/gpt-realtime-2
22. OpenTelemetry GenAI semantic conventions repository - https://github.com/open-telemetry/semantic-conventions-genai
23. Kubernetes Gateway API - https://kubernetes.io/docs/concepts/services-networking/gateway/
24. Kubernetes custom resources - https://kubernetes.io/docs/concepts/extend-kubernetes/api-extension/custom-resources/
25. Kubernetes NetworkPolicy - https://kubernetes.io/docs/concepts/services-networking/network-policies/
26. Kubernetes PodDisruptionBudget - https://kubernetes.io/docs/tasks/run-application/configure-pdb/
27. Terraform documentation - https://developer.hashicorp.com/terraform/docs
28. Terraform modules - https://developer.hashicorp.com/terraform/language/modules
29. Terraform state - https://developer.hashicorp.com/terraform/language/state
30. Langfuse native OpenTelemetry integration - https://langfuse.com/integrations/native/opentelemetry
31. Haystack SuperComponents - https://docs.haystack.deepset.ai/docs/supercomponents
32. Haystack Retrievers - https://docs.haystack.deepset.ai/docs/retrievers
33. OpenAI Codex usage limits and active-turn continuation behavior - https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
34. Open Policy Agent - https://www.openpolicyagent.org/
35. Cedar policy language - https://docs.cedarpolicy.com/
36. Kubernetes ResourceQuota - https://kubernetes.io/docs/concepts/policy/resource-quotas/
37. OpenTelemetry core semantic conventions and versioning - https://opentelemetry.io/docs/concepts/semantic-conventions/

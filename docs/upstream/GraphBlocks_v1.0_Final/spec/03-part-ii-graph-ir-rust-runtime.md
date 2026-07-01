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
- `PortRef`, `Outcome`, `InputDependency`, `ResolvedInput`, `Readiness` record는 construction 시
  non-empty node/port/input identity, valid literal status/mode/kind, terminal reason code,
  retryability boolean, metadata keys, resolved outcome payload type, readiness state field
  combination을 검증해야 한다. Tracker는 publish/readiness 입력에서 typed record만 수락해야 한다.

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

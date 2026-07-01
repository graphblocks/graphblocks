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

Ingestion manifest를 ready 상태로 publish할 때 parsed document, chunk set, index record 중 하나라도 생성되면 `acl_revision`이 비어 있어서는 안 된다. ACL이 없는 dry-run/metadata-only manifest는 publish output을 만들기 전까지 ready commit으로 승격하지 않는다.

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

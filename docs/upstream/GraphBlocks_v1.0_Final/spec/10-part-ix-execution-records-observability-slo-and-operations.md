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

Remote worker edge payload는 worker protocol boundary에서 mapping-shaped payload와 non-negative
inline byte limit을 검증해야 한다. Inline payload는 canonical JSON byte size가 limit 이하일 때만 허용하고,
large or durable data는 `artifact_ref` payload로 전달해야 한다.

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

Audit outbox record가 published terminal state에 도달하면 publish timestamp와 terminal metadata는 immutable하다. 동일 timestamp의 중복 publish 확인은 idempotent replay로 허용할 수 있지만, 다른 terminal timestamp로 덮어쓰면 안 된다.

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

`RunRecord`와 `RunDeploymentProvenance`는 construction 시 non-empty run/graph/provenance identity,
valid run status, non-negative state revision, object-shaped inputs/state, and typed
model-visible tool references를 검증해야 한다. In-memory와 durable run store는 create/patch/status
mutation boundary에서 동일 contract를 적용하고 caller-owned input/state/tool collections를 defensive
snapshot으로 보존해야 한다.

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

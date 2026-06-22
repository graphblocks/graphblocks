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


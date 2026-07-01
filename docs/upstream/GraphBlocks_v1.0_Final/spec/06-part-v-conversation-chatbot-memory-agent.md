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
`include_attachments`가 true인 branch는 branch 범위 안의 message-scoped attachment와
conversation-scoped attachment만 복사해야 한다. Branch 지점 이후 message에 묶인
attachment는 새 branch에 포함하지 않으며, `include_attachments`가 false이면 attachment를
복사하지 않는다. 공유 conversation TCK는 이 scoping 규칙을 검증해야 한다.

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
`attachment.resolve`는 ready 상태의 attachment만 context로 반환해야 한다. Message-scoped
attachment는 요청된 message id에 묶인 경우에만 포함하고, conversation-scoped attachment는
호출자가 conversation scope 포함을 요청한 경우에만 포함한다. User/project/tenant-scoped
attachment는 별도 capability resolution 없이 conversation context로 승격하지 않는다.
공유 conversation TCK는 readiness와 scope filtering을 검증해야 한다.

Compaction record는 source message id, output summary message id, method, model, token
before/after를 보존해야 하며 conversation revision을 증가시킨다. 공유 conversation TCK는
이 provenance와 token delta contract를 검증해야 한다.

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

승인 후 arguments 또는 subject digest 변경을 허용하지 않는다. 변경되면 새 approval을 요청해야 한다. 내용 검토는 `ReviewRecord`를 사용한다. Approval과 review timestamp field는 parse 가능한 ISO datetime이어야 한다. `expires_at`이 있는 approval은 만료 시각 이후 action을 authorize하지 않는다. `approved`/`denied` terminal record의 `decided_at`은 request `expires_at` 이후가 아니어야 하며, 만료 후 재승인이 필요하면 새 approval request를 만들어야 한다. Reviewer credential의 `expires_at`은 `issued_at` 이후여야 하며, review 생성 시각이 credential expiry보다 앞선 경우에만 사용할 수 있다. Review request, reviewer credential, canonical `ReviewRecord`의 identity, scope, decision, metadata key, credential reference는 비어 있지 않은 typed 값으로 검증해야 한다.

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

Core는 domain-specific candidate를 정의하지 않는다. Trial executor와 typed result contract만 제공한다. `EvidenceRef`, `TypedValueRef`, `CheckResult`, `MetricObservation`, `GateConstraint`, `GateResult`는 identity, schema/version/digest, literal status/decision/operator, typed subject/evidence/artifact/metric reference, mutable mapping/collection copy를 검증해야 한다.

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
Archive는 conversation identity와 existing state를 보존하지만 archived flag와 revision을 갱신하고
이후 append/turn mutation을 거부해야 한다. 공유 conversation TCK는 archive 후 append rejection을
검증해야 한다.
`tombstone` delete는 conversation identity를 보존하되 message, attachment, compaction state를
비우고 archived/deleted metadata를 남겨야 한다. `hard` delete는 conversation 조회가 실패하도록
record를 제거해야 한다. 공유 conversation TCK는 두 retention mode를 모두 검증해야 한다.

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
